import io
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
import qrcode
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

app = FastAPI(title="Hệ thống Quản lý Nề nếp THPT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "ne_nep.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# 1. KHỞI TẠO DATABASE & DỮ LIỆU MẪU
def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Bảng tài khoản
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        full_name TEXT,
        role TEXT,
        target_ref TEXT DEFAULT ''
    )""")

    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "target_ref" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN target_ref TEXT DEFAULT ''")

    # Bảng học sinh
    cur.execute("""
    CREATE TABLE IF NOT EXISTS students (
        student_code TEXT PRIMARY KEY,
        full_name TEXT,
        class_name TEXT,
        grade TEXT,
        dob TEXT,
        face_descriptor TEXT DEFAULT ''
    )""")

    scols = [r[1] for r in cur.execute("PRAGMA table_info(students)").fetchall()]
    if "face_descriptor" not in scols:
        cur.execute("ALTER TABLE students ADD COLUMN face_descriptor TEXT DEFAULT ''")

    # Bảng danh mục lỗi
    cur.execute("""
    CREATE TABLE IF NOT EXISTS violation_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        points INTEGER
    )""")

    # Bảng ghi nhận vi phạm
    cur.execute("""
    CREATE TABLE IF NOT EXISTS violation_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_code TEXT,
        violation_names TEXT,
        total_points INTEGER,
        recorded_by TEXT,
        recorded_at TEXT,
        note TEXT,
        status TEXT DEFAULT 'pending'
    )""")

    # Bảng điểm danh khuôn mặt
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_code TEXT,
        attendance_date TEXT,
        check_in_time TEXT,
        status TEXT DEFAULT 'present', -- 'present', 'late'
        recorded_by TEXT,
        UNIQUE(student_code, attendance_date)
    )""")

    # Tạo tài khoản mặc định nếu chưa có
    users_seed = [
        ("admin", "123456", "Quản trị viên Hệ thống", "admin", ""),
        ("giamhieu", "123456", "Thầy Hiệu Phó Nề Nếp", "giamhieu", ""),
        ("totruong", "123456", "Nguyễn Văn A (Tổ trưởng)", "totruong", ""),
        ("tovien1", "123456", "Trần Thị B (Cờ đỏ 1)", "tovien", ""),
        ("gvcn10a1", "123456", "Cô Nguyễn Lan (GVCN 10A1)", "gvcn", "10A1"),
        ("ph_hs1001", "123456", "Phụ huynh em Lê Minh Khôi", "phuhuynh", "HS1001"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO users (username, password, full_name, role, target_ref) VALUES (?, ?, ?, ?, ?)",
        users_seed,
    )

    # Tạo danh mục lỗi mẫu
    cur.execute("SELECT COUNT(*) FROM violation_types")
    if cur.fetchone()[0] == 0:
        violations_seed = [
            ("Đi học muộn", 2),
            ("Không đeo thẻ học sinh", 2),
            ("Sai đồng phục / Không sơ vin", 2),
            ("Nhuộm tóc / Sơn móng tay", 5),
            ("Sử dụng điện thoại trong giờ", 5),
            ("Nói tục, chửi bậy", 5),
            ("Bỏ tiết / Trốn học", 10),
        ]
        cur.executemany(
            "INSERT INTO violation_types (name, points) VALUES (?, ?)",
            violations_seed,
        )

    # Tạo 3 học sinh mẫu để test ngay
    students_seed = [
        ("HS1001", "Lê Minh Khôi", "10A1", "10", "12/05/2010", ""),
        ("HS1002", "Phạm Thu Hà", "10A1", "10", "20/08/2010", ""),
        ("HS1101", "Hoàng Gia Bảo", "11A2", "11", "03/11/2009", ""),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO students (student_code, full_name, class_name, grade, dob, face_descriptor) VALUES (?, ?, ?, ?, ?, ?)",
        students_seed,
    )

    conn.commit()
    conn.close()


init_db()


# --- API MODELS ---
class LoginRequest(BaseModel):
    username: str
    password: str


class RecordViolationRequest(BaseModel):
    student_code: str
    violation_ids: List[int]
    recorded_by: str
    note: Optional[str] = ""


class UserCreateUpdate(BaseModel):
    username: str
    password: Optional[str] = None
    full_name: str
    role: str
    target_ref: Optional[str] = ""


class ViolationTypeCreate(BaseModel):
    name: str
    points: int


class EnrollFaceRequest(BaseModel):
    student_code: str
    descriptor: List[float]


class MatchFaceRequest(BaseModel):
    descriptor: List[float]
    class_name: Optional[str] = None
    threshold: Optional[float] = 0.58


class AttendanceCheckInRequest(BaseModel):
    student_code: str
    attendance_date: Optional[str] = None
    late_after: Optional[str] = "07:15"
    recorded_by: str


def euclidean_distance(v1: List[float], v2: List[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 999.0
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(v1, v2)))


# --- API ENDPOINTS ---
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "templates", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


@app.post("/api/login")
def login(req: LoginRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT username, full_name, role, COALESCE(target_ref, '') AS target_ref FROM users WHERE username=? AND password=?",
        (req.username.strip(), req.password.strip()),
    ).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Sai tên đăng nhập hoặc mật khẩu")
    return dict(user)


@app.get("/api/classes")
def get_classes():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT class_name FROM students WHERE class_name IS NOT NULL AND TRIM(class_name) != '' ORDER BY class_name ASC"
    ).fetchall()
    conn.close()
    return [r["class_name"] for r in rows]


@app.get("/api/students")
def get_students(class_name: Optional[str] = None, q: Optional[str] = None):
    conn = get_db()
    sql = """
    SELECT student_code, full_name, class_name, grade, dob,
           CASE WHEN face_descriptor IS NOT NULL AND TRIM(face_descriptor) != '' THEN 1 ELSE 0 END AS has_face
    FROM students WHERE 1=1
    """
    params = []

    if class_name and class_name.strip():
        sql += " AND class_name = ?"
        params.append(class_name.strip())

    if q and q.strip():
        kw = f"%{q.strip()}%"
        sql += " AND (full_name LIKE ? OR student_code LIKE ? OR class_name LIKE ?)"
        params.extend([kw, kw, kw])

    sql += " ORDER BY class_name ASC, full_name ASC, student_code ASC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/students/{code}")
def get_student(code: str):
    conn = get_db()
    st = conn.execute(
        """
        SELECT student_code, full_name, class_name, grade, dob,
               CASE WHEN face_descriptor IS NOT NULL AND TRIM(face_descriptor) != '' THEN 1 ELSE 0 END AS has_face
        FROM students WHERE student_code=?
        """,
        (code.strip(),),
    ).fetchone()
    conn.close()
    if not st:
        raise HTTPException(status_code=404, detail="Không tìm thấy mã học sinh này!")
    return dict(st)


# --- KHUÔN MẶT: ĐĂNG KÝ MẪU & NHẬN DIỆN ---
@app.post("/api/face/enroll")
def enroll_face(req: EnrollFaceRequest):
    code = req.student_code.strip()
    if not code or not req.descriptor or len(req.descriptor) < 32:
        raise HTTPException(status_code=400, detail="Dữ liệu đặc trưng khuôn mặt không hợp lệ")

    conn = get_db()
    st = conn.execute("SELECT student_code, full_name, class_name FROM students WHERE student_code=?", (code,)).fetchone()
    if not st:
        conn.close()
        raise HTTPException(status_code=404, detail="Không tìm thấy học sinh")

    conn.execute(
        "UPDATE students SET face_descriptor=? WHERE student_code=?",
        (json.dumps([float(x) for x in req.descriptor]), code),
    )
    conn.commit()
    conn.close()
    return {"message": f"Đã lưu mẫu khuôn mặt cho {st['full_name']} ({st['class_name']})!"}


@app.post("/api/face/match")
def match_face(req: MatchFaceRequest):
    if not req.descriptor or len(req.descriptor) < 32:
        raise HTTPException(status_code=400, detail="Vector khuôn mặt không hợp lệ")

    conn = get_db()
    sql = """
    SELECT student_code, full_name, class_name, grade, dob, face_descriptor
    FROM students
    WHERE face_descriptor IS NOT NULL AND TRIM(face_descriptor) != ''
    """
    params = []
    if req.class_name and req.class_name.strip():
        sql += " AND class_name = ?"
        params.append(req.class_name.strip())

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        return {"matched": False, "message": "Chưa có học sinh nào đăng ký mẫu khuôn mặt"}

    best_match = None
    best_dist = 999.0

    for r in rows:
        try:
            stored_vec = json.loads(r["face_descriptor"])
        except Exception:
            continue
        dist = euclidean_distance(req.descriptor, stored_vec)
        if dist < best_dist:
            best_dist = dist
            best_match = r

    thresh = float(req.threshold or 0.58)
    confidence = max(0, min(100, round((1.0 - min(best_dist, 1.0)) * 100, 1)))

    if best_match and best_dist <= thresh:
        return {
            "matched": True,
            "distance": round(best_dist, 4),
            "confidence": confidence,
            "student": {
                "student_code": best_match["student_code"],
                "full_name": best_match["full_name"],
                "class_name": best_match["class_name"],
                "grade": best_match["grade"],
                "dob": best_match["dob"],
                "has_face": 1,
            },
        }

    return {
        "matched": False,
        "distance": round(best_dist, 4) if best_match else None,
        "confidence": confidence,
        "message": "Không nhận diện được khuôn mặt khớp với dữ liệu đã đăng ký",
    }


# --- ĐIỂM DANH KHUÔN MẶT ---
@app.post("/api/attendance/check-in")
def attendance_check_in(req: AttendanceCheckInRequest):
    code = req.student_code.strip()
    att_date = (req.attendance_date or datetime.now().strftime("%Y-%m-%d")).strip()
    now_dt = datetime.now()
    check_time = now_dt.strftime("%H:%M:%S")
    hhmm = now_dt.strftime("%H:%M")

    late_limit = (req.late_after or "07:15").strip()
    status = "late" if hhmm > late_limit else "present"

    conn = get_db()
    st = conn.execute(
        "SELECT student_code, full_name, class_name FROM students WHERE student_code=?",
        (code,),
    ).fetchone()
    if not st:
        conn.close()
        raise HTTPException(status_code=404, detail="Không tìm thấy học sinh")

    existing = conn.execute(
        "SELECT id, check_in_time, status FROM attendance_records WHERE student_code=? AND attendance_date=?",
        (code, att_date),
    ).fetchone()

    if existing:
        conn.close()
        return {
            "already_checked": True,
            "message": f"{st['full_name']} ({st['class_name']}) đã điểm danh lúc {existing['check_in_time']}",
            "student_code": code,
            "full_name": st["full_name"],
            "class_name": st["class_name"],
            "check_in_time": existing["check_in_time"],
            "status": existing["status"],
        }

    conn.execute(
        """
        INSERT INTO attendance_records (student_code, attendance_date, check_in_time, status, recorded_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (code, att_date, check_time, status, req.recorded_by),
    )
    conn.commit()
    conn.close()

    return {
        "already_checked": False,
        "message": f"Đã điểm danh: {st['full_name']} ({st['class_name']}) — {'Đi muộn' if status == 'late' else 'Có mặt'}",
        "student_code": code,
        "full_name": st["full_name"],
        "class_name": st["class_name"],
        "check_in_time": check_time,
        "status": status,
    }


@app.get("/api/attendance")
def list_attendance(
    attendance_date: Optional[str] = None,
    class_name: Optional[str] = None,
    role: Optional[str] = None,
    target_ref: Optional[str] = None,
):
    att_date = (attendance_date or datetime.now().strftime("%Y-%m-%d")).strip()
    effective_class = (class_name or "").strip()
    if role == "gvcn" and target_ref and target_ref.strip():
        effective_class = target_ref.strip()

    conn = get_db()
    sql = """
    SELECT s.student_code, s.full_name, s.class_name, s.grade,
           CASE WHEN s.face_descriptor IS NOT NULL AND TRIM(s.face_descriptor) != '' THEN 1 ELSE 0 END AS has_face,
           a.id AS attendance_id, a.check_in_time, a.recorded_by,
           CASE
               WHEN a.id IS NULL THEN 'absent'
               ELSE a.status
           END AS attendance_status
    FROM students s
    LEFT JOIN attendance_records a
           ON s.student_code = a.student_code AND a.attendance_date = ?
    WHERE 1=1
    """
    params = [att_date]

    if role == "phuhuynh" and target_ref and target_ref.strip():
        sql += " AND UPPER(TRIM(s.student_code)) = UPPER(TRIM(?))"
        params.append(target_ref.strip())
    elif effective_class:
        sql += " AND UPPER(TRIM(s.class_name)) = UPPER(TRIM(?))"
        params.append(effective_class)

    sql += " ORDER BY s.class_name ASC, s.full_name ASC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    present_cnt = sum(1 for x in items if x["attendance_status"] == "present")
    late_cnt = sum(1 for x in items if x["attendance_status"] == "late")
    absent_cnt = sum(1 for x in items if x["attendance_status"] == "absent")

    return {
        "attendance_date": att_date,
        "class_name": effective_class,
        "summary": {
            "total": len(items),
            "present": present_cnt,
            "late": late_cnt,
            "absent": absent_cnt,
        },
        "items": items,
    }


@app.get("/api/attendance/export-excel")
def export_attendance_excel(
    attendance_date: Optional[str] = None,
    class_name: Optional[str] = None,
    role: Optional[str] = None,
    target_ref: Optional[str] = None,
):
    data = list_attendance(
        attendance_date=attendance_date,
        class_name=class_name,
        role=role,
        target_ref=target_ref,
    )
    status_vi = {"present": "Có mặt", "late": "Đi muộn", "absent": "Vắng"}
    df = pd.DataFrame(
        [
            {
                "Ngày": data["attendance_date"],
                "Mã HS": it["student_code"],
                "Họ tên": it["full_name"],
                "Lớp": it["class_name"],
                "Trạng thái": status_vi.get(it["attendance_status"], it["attendance_status"]),
                "Giờ điểm danh": it["check_in_time"] or "",
                "Người ghi nhận": it["recorded_by"] or "",
            }
            for it in data["items"]
        ]
    )
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="DiemDanh")
        ws = writer.sheets["DiemDanh"]
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 36)
    output.seek(0)
    filename = f"DiemDanh_{data['attendance_date']}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/violations")
def get_violations():
    conn = get_db()
    rows = conn.execute("SELECT * FROM violation_types ORDER BY id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/admin/violations")
def create_violation_type(v: ViolationTypeCreate):
    clean_name = v.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Tên lỗi vi phạm không được để trống")
    if v.points <= 0:
        raise HTTPException(status_code=400, detail="Điểm trừ phải lớn hơn 0")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO violation_types (name, points) VALUES (?, ?)",
        (clean_name, int(v.points)),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {
        "id": new_id,
        "name": clean_name,
        "points": int(v.points),
        "message": "Đã thêm mẫu lỗi vi phạm!",
    }


@app.delete("/api/admin/violations/{violation_id}")
def delete_violation_type(violation_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM violation_types WHERE id=?", (violation_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu lỗi vi phạm")
    return {"message": "Đã xóa mẫu lỗi vi phạm!"}


@app.post("/api/records")
def create_record(req: RecordViolationRequest):
    conn = get_db()
    placeholders = ",".join("?" for _ in req.violation_ids)
    v_rows = conn.execute(
        f"SELECT name, points FROM violation_types WHERE id IN ({placeholders})",
        req.violation_ids,
    ).fetchall()
    if not v_rows:
        conn.close()
        raise HTTPException(status_code=400, detail="Chưa chọn lỗi vi phạm")

    v_names = ", ".join([r["name"] for r in v_rows])
    total_pts = sum([r["points"] for r in v_rows])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        INSERT INTO violation_records (student_code, violation_names, total_points, recorded_by, recorded_at, note, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """,
        (req.student_code, v_names, total_pts, req.recorded_by, now_str, req.note),
    )
    conn.commit()
    conn.close()
    return {"message": "Đã ghi nhận vi phạm thành công!"}


@app.get("/api/records")
def list_records(role: Optional[str] = None, target_ref: Optional[str] = None):
    conn = get_db()
    sql = """
    SELECT r.*, s.full_name, s.class_name, s.grade 
    FROM violation_records r
    LEFT JOIN students s ON r.student_code = s.student_code
    WHERE 1=1
    """
    params = []

    if role == "gvcn":
        if target_ref and target_ref.strip():
            sql += " AND UPPER(TRIM(s.class_name)) = UPPER(TRIM(?))"
            params.append(target_ref.strip())
        else:
            sql += " AND 1=0"
    elif role == "phuhuynh":
        if target_ref and target_ref.strip():
            sql += " AND UPPER(TRIM(r.student_code)) = UPPER(TRIM(?)) AND r.status != 'rejected'"
            params.append(target_ref.strip())
        else:
            sql += " AND 1=0"

    sql += " ORDER BY r.id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.put("/api/records/{record_id}/status")
def update_status(record_id: int, status: str):
    conn = get_db()
    conn.execute(
        "UPDATE violation_records SET status=? WHERE id=?", (status, record_id)
    )
    conn.commit()
    conn.close()
    return {"message": "Đã cập nhật trạng thái"}


@app.post("/api/admin/import-students")
async def import_students(file: UploadFile = File(...)):
    contents = await file.read()
    df = pd.read_excel(io.BytesIO(contents))
    conn = get_db()
    count = 0
    for _, row in df.iterrows():
        code_val = str(row["MaHS"]).strip()
        existing = conn.execute("SELECT face_descriptor FROM students WHERE student_code=?", (code_val,)).fetchone()
        old_face = existing["face_descriptor"] if existing and existing["face_descriptor"] else ""
        conn.execute(
            """
            INSERT OR REPLACE INTO students (student_code, full_name, class_name, grade, dob, face_descriptor)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                code_val,
                str(row["HoTen"]).strip(),
                str(row["Lop"]).strip(),
                str(row["Khoi"]).strip(),
                str(row.get("NgaySinh", "")),
                old_face,
            ),
        )
        count += 1
    conn.commit()
    conn.close()
    return {"message": f"Đã nhập thành công {count} học sinh!"}


@app.get("/api/export-excel")
def export_excel(role: Optional[str] = None, target_ref: Optional[str] = None):
    conn = get_db()
    sql = """
    SELECT r.id AS 'ID', r.recorded_at AS 'Thời gian', s.student_code AS 'Mã HS', 
           s.full_name AS 'Họ tên', s.class_name AS 'Lớp', s.grade AS 'Khối',
           r.violation_names AS 'Lỗi vi phạm', r.total_points AS 'Điểm trừ',
           r.recorded_by AS 'Người ghi nhận', r.status AS 'Trạng thái'
    FROM violation_records r
    LEFT JOIN students s ON r.student_code = s.student_code
    WHERE r.status != 'rejected'
    """
    params = []
    if role == "gvcn" and target_ref and target_ref.strip():
        sql += " AND UPPER(TRIM(s.class_name)) = UPPER(TRIM(?))"
        params.append(target_ref.strip())

    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="TongHopViPham")
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=BaoCao_NeNep_THPT.xlsx"
        },
    )


# --- QUẢN LÝ TÀI KHOẢN (ADMIN) ---
@app.get("/api/admin/users")
def list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, full_name, role, COALESCE(target_ref, '') AS target_ref FROM users ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/admin/users")
def create_user(u: UserCreateUpdate):
    if not u.password:
        raise HTTPException(
            status_code=400, detail="Cần nhập mật khẩu cho tài khoản mới"
        )
    ref_val = (u.target_ref or "").strip()
    if u.role == "gvcn" and not ref_val:
        raise HTTPException(
            status_code=400, detail="Cần nhập Tên lớp phụ trách cho Giáo viên chủ nhiệm (VD: 10A1)"
        )
    if u.role == "phuhuynh" and not ref_val:
        raise HTTPException(
            status_code=400, detail="Cần nhập Mã học sinh của con cho Phụ huynh (VD: HS1001)"
        )

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password, full_name, role, target_ref) VALUES (?, ?, ?, ?, ?)",
            (u.username.strip(), u.password, u.full_name.strip(), u.role, ref_val),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Tên đăng nhập đã tồn tại!")
    conn.close()
    return {"message": "Đã tạo tài khoản thành công!"}


@app.put("/api/admin/users/{user_id}")
def update_user(user_id: int, u: UserCreateUpdate):
    ref_val = (u.target_ref or "").strip()
    if u.role == "gvcn" and not ref_val:
        raise HTTPException(
            status_code=400, detail="Cần nhập Tên lớp phụ trách cho Giáo viên chủ nhiệm (VD: 10A1)"
        )
    if u.role == "phuhuynh" and not ref_val:
        raise HTTPException(
            status_code=400, detail="Cần nhập Mã học sinh của con cho Phụ huynh (VD: HS1001)"
        )

    conn = get_db()
    if u.password:
        conn.execute(
            "UPDATE users SET username=?, password=?, full_name=?, role=?, target_ref=? WHERE id=?",
            (u.username.strip(), u.password, u.full_name.strip(), u.role, ref_val, user_id),
        )
    else:
        conn.execute(
            "UPDATE users SET username=?, full_name=?, role=?, target_ref=? WHERE id=?",
            (u.username.strip(), u.full_name.strip(), u.role, ref_val, user_id),
        )
    conn.commit()
    conn.close()
    return {"message": "Đã cập nhật tài khoản!"}


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=? AND username != 'admin'", (user_id,))
    conn.commit()
    conn.close()
    return {"message": "Đã xóa tài khoản!"}


@app.get("/api/admin/users-template-excel")
def download_users_template():
    sample_rows = [
        {
            "TenDangNhap": "tovien2",
            "MatKhau": "123456",
            "HoTen": "Lê Văn C (Cờ đỏ 2)",
            "VaiTro": "tovien",
            "PhamVi": "",
        },
        {
            "TenDangNhap": "totruong2",
            "MatKhau": "123456",
            "HoTen": "Phạm Thị D (Tổ trưởng)",
            "VaiTro": "totruong",
            "PhamVi": "",
        },
        {
            "TenDangNhap": "gvcn10a2",
            "MatKhau": "123456",
            "HoTen": "Thầy Trần Minh (GVCN 10A2)",
            "VaiTro": "gvcn",
            "PhamVi": "10A2",
        },
        {
            "TenDangNhap": "ph_hs1002",
            "MatKhau": "123456",
            "HoTen": "Phụ huynh em Phạm Thu Hà",
            "VaiTro": "phuhuynh",
            "PhamVi": "HS1002",
        },
        {
            "TenDangNhap": "hieupho2",
            "MatKhau": "123456",
            "HoTen": "Cô Hoàng Lan (Ban Giám hiệu)",
            "VaiTro": "giamhieu",
            "PhamVi": "",
        },
    ]
    df_sample = pd.DataFrame(sample_rows)
    df_guide = pd.DataFrame(
        [
            {"MaVaiTro_HopLe": "tovien", "MoTa": "Tổ viên (Cờ đỏ quét thẻ/mặt)"},
            {"MaVaiTro_HopLe": "totruong", "MoTa": "Tổ trưởng trực tuần (duyệt/hủy lỗi, điểm danh)"},
            {"MaVaiTro_HopLe": "gvcn", "MoTa": "Giáo viên chủ nhiệm (cột PhamVi điền tên lớp, VD: 10A1)"},
            {"MaVaiTro_HopLe": "phuhuynh", "MoTa": "Phụ huynh học sinh (cột PhamVi điền Mã HS của con, VD: HS1001)"},
            {"MaVaiTro_HopLe": "giamhieu", "MoTa": "Ban Giám hiệu (xem báo cáo, xuất Excel)"},
            {"MaVaiTro_HopLe": "admin", "MoTa": "Quản trị viên hệ thống"},
        ]
    )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_sample.to_excel(writer, index=False, sheet_name="DanhSachTaiKhoan")
        df_guide.to_excel(writer, index=False, sheet_name="HuongDanVaiTro")
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 52)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Mau_Import_TaiKhoan.xlsx"},
    )


@app.post("/api/admin/import-users")
async def import_users(file: UploadFile = File(...)):
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Không đọc được file Excel: {exc}")

    col_map = {}
    for col in df.columns:
        c_clean = str(col).strip().lower()
        if c_clean in ("tendangnhap", "username", "tai_khoan", "taikhoan"):
            col_map["username"] = col
        elif c_clean in ("matkhau", "password", "mat_khau"):
            col_map["password"] = col
        elif c_clean in ("hoten", "full_name", "fullname", "ho_ten"):
            col_map["full_name"] = col
        elif c_clean in ("vaitro", "role", "vai_tro", "quyen"):
            col_map["role"] = col
        elif c_clean in ("phamvi", "target_ref", "lop_mahs", "lop", "mahs", "pham_vi"):
            col_map["target_ref"] = col

    required = {"username", "full_name", "role"}
    if not required.issubset(set(col_map.keys())):
        raise HTTPException(
            status_code=400,
            detail="File Excel cần có đủ các cột: TenDangNhap, MatKhau, HoTen, VaiTro, PhamVi",
        )

    role_aliases = {
        "tovien": "tovien",
        "to vien": "tovien",
        "tổ viên": "tovien",
        "co do": "tovien",
        "cờ đỏ": "tovien",
        "totruong": "totruong",
        "to truong": "totruong",
        "tổ trưởng": "totruong",
        "gvcn": "gvcn",
        "giao vien chu nhiem": "gvcn",
        "giáo viên chủ nhiệm": "gvcn",
        "phuhuynh": "phuhuynh",
        "phu huynh": "phuhuynh",
        "phụ huynh": "phuhuynh",
        "phụ huynh học sinh": "phuhuynh",
        "giamhieu": "giamhieu",
        "giam hieu": "giamhieu",
        "ban giám hiệu": "giamhieu",
        "giám hiệu": "giamhieu",
        "admin": "admin",
        "quản trị": "admin",
    }

    conn = get_db()
    inserted = 0
    updated = 0
    errors = []

    for idx, row in df.iterrows():
        line_no = idx + 2
        raw_user = str(row[col_map["username"]]).strip()
        if not raw_user or raw_user.lower() == "nan":
            continue

        raw_name = str(row[col_map["full_name"]]).strip()
        if not raw_name or raw_name.lower() == "nan":
            errors.append(f"Dòng {line_no} ({raw_user}): Thiếu Họ tên")
            continue

        raw_role = str(row[col_map["role"]]).strip().lower()
        norm_role = role_aliases.get(raw_role)
        if not norm_role:
            errors.append(f"Dòng {line_no} ({raw_user}): Vai trò '{raw_role}' không hợp lệ")
            continue

        raw_pw = ""
        if "password" in col_map:
            val_pw = str(row[col_map["password"]]).strip()
            if val_pw and val_pw.lower() != "nan":
                if val_pw.endswith(".0") and val_pw[:-2].isdigit():
                    val_pw = val_pw[:-2]
                raw_pw = val_pw
        if not raw_pw:
            raw_pw = "123456"

        raw_ref = ""
        if "target_ref" in col_map:
            val_ref = str(row[col_map["target_ref"]]).strip()
            if val_ref and val_ref.lower() != "nan":
                raw_ref = val_ref

        if norm_role == "gvcn" and not raw_ref:
            errors.append(f"Dòng {line_no} ({raw_user}): GVCN thiếu tên lớp ở cột PhamVi")
            continue
        if norm_role == "phuhuynh" and not raw_ref:
            errors.append(f"Dòng {line_no} ({raw_user}): Phụ huynh thiếu Mã HS ở cột PhamVi")
            continue

        existing = conn.execute(
            "SELECT id FROM users WHERE username=?", (raw_user,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET password=?, full_name=?, role=?, target_ref=? WHERE username=?",
                (raw_pw, raw_name, norm_role, raw_ref, raw_user),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO users (username, password, full_name, role, target_ref) VALUES (?, ?, ?, ?, ?)",
                (raw_user, raw_pw, raw_name, norm_role, raw_ref),
            )
            inserted += 1

    conn.commit()
    conn.close()

    msg = f"Import hoàn tất: Thêm mới {inserted}, cập nhật {updated} tài khoản."
    if errors:
        msg += f" ({len(errors)} dòng lỗi: " + "; ".join(errors[:5]) + ")"
    return {"message": msg, "inserted": inserted, "updated": updated, "errors": errors}


# --- SINH FILE PDF IN HÀNG LOẠT MÃ QR TỪ EXCEL ---
@app.post("/api/admin/generate-qr-pdf")
async def generate_qr_pdf(file: UploadFile = File(...)):
    contents = await file.read()
    df = pd.read_excel(io.BytesIO(contents))
    required_cols = {"MaHS", "HoTen", "Lop"}
    if not required_cols.issubset(set(df.columns)):
        raise HTTPException(
            status_code=400, detail="File Excel cần có đủ các cột: MaHS, HoTen, Lop"
        )

    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    width, height = A4

    font_name = "Helvetica"
    for font_path in [
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont("VietFont", font_path))
            font_name = "VietFont"
            break

    cols = 3
    rows = 8
    margin_x = 12 * mm
    margin_y = 12 * mm
    cell_w = (width - 2 * margin_x) / cols
    cell_h = (height - 2 * margin_y) / rows

    idx = 0
    for _, row in df.iterrows():
        code_val = str(row["MaHS"]).strip()
        name_val = str(row["HoTen"]).strip()
        class_val = str(row["Lop"]).strip()
        if not code_val or code_val == "nan":
            continue

        col_idx = idx % cols
        row_idx = (idx // cols) % rows

        if idx > 0 and idx % (cols * rows) == 0:
            c.showPage()

        x = margin_x + col_idx * cell_w
        y = height - margin_y - (row_idx + 1) * cell_h

        c.setStrokeColorRGB(0.75, 0.75, 0.75)
        c.setLineWidth(0.5)
        c.rect(x + 2, y + 2, cell_w - 4, cell_h - 4)

        qr_img = qrcode.make(code_val, box_size=4, border=1)
        qr_io = io.BytesIO()
        qr_img.save(qr_io, format="PNG")
        qr_io.seek(0)
        qr_size = cell_h - 10
        c.drawImage(ImageReader(qr_io), x + 5, y + 5, width=qr_size, height=qr_size)

        text_x = x + qr_size + 9
        c.setFont(font_name, 9)
        c.drawString(text_x, y + cell_h - 18, name_val[:22])
        c.setFont(font_name, 8)
        c.drawString(text_x, y + cell_h - 31, f"Lớp: {class_val}")
        c.drawString(text_x, y + cell_h - 44, f"Mã: {code_val}")

        idx += 1

    c.save()
    pdf_buffer.seek(0)
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=MaQR_TheHocSinh.pdf"},
    )


# --- BẢNG XẾP HẠNG THI ĐUA THEO TUẦN CHO TỪNG LỚP ---
@app.get("/api/rankings")
def get_weekly_rankings(
    start_date: Optional[str] = None, end_date: Optional[str] = None
):
    today = datetime.now().date()
    if not start_date:
        monday = today - timedelta(days=today.weekday())
        start_date = monday.strftime("%Y-%m-%d")
    if not end_date:
        sunday = datetime.strptime(start_date, "%Y-%m-%d").date() + timedelta(days=6)
        end_date = sunday.strftime("%Y-%m-%d")

    conn = get_db()
    classes = [
        r["class_name"]
        for r in conn.execute(
            "SELECT DISTINCT class_name FROM students WHERE class_name != '' ORDER BY class_name"
        ).fetchall()
    ]

    deduct_rows = conn.execute(
        """
        SELECT s.class_name, COALESCE(SUM(r.total_points), 0) AS lost_points, COUNT(r.id) AS violation_count
        FROM violation_records r
        JOIN students s ON r.student_code = s.student_code
        WHERE r.status = 'approved'
          AND substr(r.recorded_at, 1, 10) BETWEEN ? AND ?
        GROUP BY s.class_name
    """,
        (start_date, end_date),
    ).fetchall()
    conn.close()

    deduct_map = {
        r["class_name"]: (r["lost_points"], r["violation_count"]) for r in deduct_rows
    }

    ranking_list = []
    for cls in classes:
        lost_pts, v_count = deduct_map.get(cls, (0, 0))
        final_score = 100 - lost_pts
        ranking_list.append(
            {
                "class_name": cls,
                "base_score": 100,
                "lost_points": lost_pts,
                "final_score": final_score,
                "violation_count": v_count,
            }
        )

    ranking_list.sort(key=lambda x: (-x["final_score"], x["class_name"]))
    for idx, item in enumerate(ranking_list, start=1):
        item["rank"] = idx

    return {
        "start_date": start_date,
        "end_date": end_date,
        "rankings": ranking_list,
    }


# --- SO SÁNH LỊCH SỬ ĐIỂM THI ĐUA GIỮA CÁC TUẦN ---
@app.get("/api/rankings/history")
def get_rankings_history(end_date: Optional[str] = None, weeks: int = 4):
    weeks = max(2, min(weeks, 12))
    today = datetime.now().date()
    if end_date:
        ref_end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        monday = today - timedelta(days=today.weekday())
        ref_end = monday + timedelta(days=6)

    week_ranges = []
    for i in range(weeks - 1, -1, -1):
        w_end = ref_end - timedelta(days=7 * i)
        w_start = w_end - timedelta(days=6)
        label = f"T{w_start.isocalendar()[1]} ({w_start.strftime('%d/%m')} - {w_end.strftime('%d/%m')})"
        week_ranges.append(
            {
                "label": label,
                "start": w_start.strftime("%Y-%m-%d"),
                "end": w_end.strftime("%Y-%m-%d"),
            }
        )

    conn = get_db()
    classes = [
        r["class_name"]
        for r in conn.execute(
            "SELECT DISTINCT class_name FROM students WHERE class_name != '' ORDER BY class_name"
        ).fetchall()
    ]

    scores_by_class = {cls: [100] * len(week_ranges) for cls in classes}

    for w_idx, wr in enumerate(week_ranges):
        rows = conn.execute(
            """
            SELECT s.class_name, COALESCE(SUM(r.total_points), 0) AS lost_points
            FROM violation_records r
            JOIN students s ON r.student_code = s.student_code
            WHERE r.status = 'approved'
              AND substr(r.recorded_at, 1, 10) BETWEEN ? AND ?
            GROUP BY s.class_name
        """,
            (wr["start"], wr["end"]),
        ).fetchall()

        for r in rows:
            cls = r["class_name"]
            if cls in scores_by_class:
                scores_by_class[cls][w_idx] = 100 - r["lost_points"]

    conn.close()

    history_rows = []
    for cls in classes:
        w_scores = scores_by_class[cls]
        latest_score = w_scores[-1]
        prev_score = w_scores[-2]
        delta = latest_score - prev_score
        history_rows.append(
            {
                "class_name": cls,
                "scores": w_scores,
                "latest_score": latest_score,
                "delta": delta,
            }
        )

    history_rows.sort(key=lambda x: (-x["latest_score"], x["class_name"]))

    return {
        "weeks": week_ranges,
        "rows": history_rows,
    }


# --- XUẤT EXCEL BẢNG XẾP HẠNG THI ĐUA TUẦN & SO SÁNH CÁC TUẦN ---
@app.get("/api/rankings/export-excel")
def export_rankings_excel(
    start_date: Optional[str] = None, end_date: Optional[str] = None
):
    ranking_data = get_weekly_rankings(start_date=start_date, end_date=end_date)
    history_data = get_rankings_history(end_date=ranking_data["end_date"], weeks=4)

    def xep_loai(diem: int) -> str:
        if diem >= 95:
            return "Xuất sắc"
        if diem >= 85:
            return "Tốt"
        if diem >= 70:
            return "Khá"
        return "Cần cố gắng"

    df_rank = pd.DataFrame(
        [
            {
                "Hạng": item["rank"],
                "Lớp": item["class_name"],
                "Điểm gốc": item["base_score"],
                "Số lượt lỗi": item["violation_count"],
                "Điểm trừ": -item["lost_points"] if item["lost_points"] > 0 else 0,
                "Tổng điểm": item["final_score"],
                "Xếp loại": xep_loai(item["final_score"]),
                "Từ ngày": ranking_data["start_date"],
                "Đến ngày": ranking_data["end_date"],
            }
            for item in ranking_data["rankings"]
        ]
    )

    week_labels = [w["label"] for w in history_data["weeks"]]
    hist_records = []
    for r in history_data["rows"]:
        rec = {"Lớp": r["class_name"]}
        for idx, lbl in enumerate(week_labels):
            rec[lbl] = r["scores"][idx]
        rec["Chênh lệch (tuần mới vs tuần trước)"] = r["delta"]
        hist_records.append(rec)
    df_hist = pd.DataFrame(hist_records)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_rank.to_excel(writer, index=False, sheet_name="XepHangTuan")
        df_hist.to_excel(writer, index=False, sheet_name="SoSanhCacTuan")

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                col_letter = col[0].column_letter
                ws.column_dimensions[col_letter].width = min(max_len + 4, 36)

    output.seek(0)
    filename = f"XepHang_ThiDua_{ranking_data['start_date']}_den_{ranking_data['end_date']}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
