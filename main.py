import io
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
    # role: 'tovien', 'totruong', 'giamhieu', 'gvcn', 'phuhuynh', 'admin'
    # target_ref: lưu tên lớp phụ trách (đối với 'gvcn', VD: '10A1') hoặc Mã HS (đối với 'phuhuynh', VD: 'HS1001')
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        full_name TEXT,
        role TEXT,
        target_ref TEXT DEFAULT ''
    )""")

    # Bổ sung cột target_ref nếu DB cũ chưa có
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
        dob TEXT
    )""")

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
        status TEXT DEFAULT 'pending' -- 'pending', 'approved', 'rejected'
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
        ("HS1001", "Lê Minh Khôi", "10A1", "10", "12/05/2010"),
        ("HS1002", "Phạm Thu Hà", "10A1", "10", "20/08/2010"),
        ("HS1101", "Hoàng Gia Bảo", "11A2", "11", "03/11/2009"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO students VALUES (?, ?, ?, ?, ?)", students_seed
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
    role: str  # 'tovien', 'totruong', 'giamhieu', 'gvcn', 'phuhuynh', 'admin'
    target_ref: Optional[str] = ""


class ViolationTypeCreate(BaseModel):
    name: str
    points: int


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
    sql = "SELECT * FROM students WHERE 1=1"
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
        "SELECT * FROM students WHERE student_code=?", (code.strip(),)
    ).fetchone()
    conn.close()
    if not st:
        raise HTTPException(status_code=404, detail="Không tìm thấy mã học sinh này!")
    return dict(st)


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
        conn.execute(
            """
            INSERT OR REPLACE INTO students (student_code, full_name, class_name, grade, dob)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                str(row["MaHS"]).strip(),
                str(row["HoTen"]).strip(),
                str(row["Lop"]).strip(),
                str(row["Khoi"]).strip(),
                str(row.get("NgaySinh", "")),
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
