import os
import re
import time
import sqlite3
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

APP_TZ = ZoneInfo("Asia/Bangkok")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
DB_PATH = os.getenv("DB_PATH", "loan_bot.db")

THB_PER_USDT_FALLBACK = float(os.getenv("THB_PER_USDT_FALLBACK", "36.50"))
COINGECKO_DEMO_API_KEY = os.getenv("COINGECKO_DEMO_API_KEY", "")
FX_CACHE_SECONDS = int(os.getenv("FX_CACHE_SECONDS", "60"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "Air_4bot")

ADMIN_IDS = set()
for item in ADMIN_IDS_RAW.split(","):
    item = item.strip()
    if item.isdigit():
        ADMIN_IDS.add(int(item))

USER_SESSIONS = {}
_fx_cache = {"thb_per_usdt": THB_PER_USDT_FALLBACK, "updated_at": 0.0}

def now_local() -> datetime:
    return datetime.now(APP_TZ)

def today_key() -> str:
    return now_local().strftime("%Y-%m-%d")

def is_group_chat(update: Update) -> bool:
    return update.effective_chat.type in ["group", "supergroup"]

def get_private_link() -> str:
    return f"https://t.me/{BOT_USERNAME}"

def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL UNIQUE,
        tg_username TEXT,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'staff',
        approved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        approved_at TEXT,
        approved_by INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_telegram_id INTEGER NOT NULL,
        employee_name_snapshot TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('borrow', 'repay')),
        original_amount REAL NOT NULL,
        original_currency TEXT NOT NULL,
        fx_rate REAL NOT NULL,
        amount_usdt REAL NOT NULL,
        operator_id INTEGER NOT NULL,
        operator_name TEXT,
        created_at TEXT NOT NULL,
        date_key TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_rates (
        rate_date TEXT PRIMARY KEY,
        thb_per_usdt REAL NOT NULL,
        source TEXT NOT NULL,
        set_by INTEGER,
        updated_at TEXT NOT NULL
    )""")
    conn.commit()
    conn.close()

def get_employee_by_telegram_id(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT telegram_id, tg_username, name, role, approved, created_at, approved_at, approved_by
        FROM employees WHERE telegram_id = ?""", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_employee_by_name(name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT telegram_id, tg_username, name, role, approved, created_at, approved_at, approved_by
        FROM employees WHERE name = ?""", (name,))
    row = cur.fetchone()
    conn.close()
    return row

def get_employee_display_name_by_id(user_id: int) -> str:
    row = get_employee_by_telegram_id(user_id)
    return row[2] if row else str(user_id)

def get_all_approved_employees():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, name, role FROM employees WHERE approved = 1 ORDER BY name ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_pending_employees():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, tg_username, name, created_at FROM employees WHERE approved = 0 ORDER BY created_at ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

def register_employee_request(user_id: int, username: str, name: str) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT approved, name FROM employees WHERE telegram_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        approved, old_name = row
        if approved == 1:
            conn.close()
            return f"你已经是已通过员工：{old_name}"
        cur.execute("UPDATE employees SET tg_username = ?, name = ?, created_at = ? WHERE telegram_id = ?",
                    (username, name, now_local().strftime('%Y-%m-%d %H:%M:%S'), user_id))
        conn.commit()
        conn.close()
        return f"已更新登记资料：{name}，等待管理员审批。"
    cur.execute("INSERT INTO employees (telegram_id, tg_username, name, role, approved, created_at) VALUES (?, ?, ?, 'staff', 0, ?)",
                (user_id, username, name, now_local().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()
    return f"登记成功：{name}，等待管理员审批。"

def approve_employee(user_id: int, approved_by: int, role: str = "staff") -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE employees SET approved = 1, role = ?, approved_at = ?, approved_by = ? WHERE telegram_id = ?",
                (role, now_local().strftime('%Y-%m-%d %H:%M:%S'), approved_by, user_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def reject_employee(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM employees WHERE telegram_id = ? AND approved = 0", (user_id,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def delete_employee(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM employees WHERE telegram_id = ?", (user_id,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def get_role(user_id: int) -> str:
    if user_id in ADMIN_IDS:
        return "superadmin"
    row = get_employee_by_telegram_id(user_id)
    if not row:
        return "guest"
    role = row[3]
    approved = row[4]
    if approved != 1:
        return "pending"
    if role == "admin":
        return "admin"
    return "staff"

def is_manager(user_id: int) -> bool:
    return get_role(user_id) in {"superadmin", "admin"}

def get_live_thb_per_usdt() -> float:
    global _fx_cache
    now_ts = time.time()
    if now_ts - _fx_cache["updated_at"] < FX_CACHE_SECONDS:
        return _fx_cache["thb_per_usdt"]
    headers = {}
    if COINGECKO_DEMO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_DEMO_API_KEY
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "tether", "vs_currencies": "thb"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["tether"]["thb"])
        if rate <= 0:
            raise ValueError("invalid rate")
        _fx_cache = {"thb_per_usdt": rate, "updated_at": now_ts}
        return rate
    except Exception as e:
        logging.warning("获取实时汇率失败，使用回退汇率。错误：%s", e)
        return THB_PER_USDT_FALLBACK

def upsert_daily_rate(rate_date: str, thb_per_usdt: float, source: str, set_by: int | None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO daily_rates (rate_date, thb_per_usdt, source, set_by, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rate_date) DO UPDATE SET
            thb_per_usdt=excluded.thb_per_usdt,
            source=excluded.source,
            set_by=excluded.set_by,
            updated_at=excluded.updated_at""",
        (rate_date, thb_per_usdt, source, set_by, now_local().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def get_daily_rate(rate_date: str | None = None) -> tuple[float, str]:
    if not rate_date:
        rate_date = today_key()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT thb_per_usdt, source FROM daily_rates WHERE rate_date = ?", (rate_date,))
    row = cur.fetchone()
    conn.close()
    if row:
        return float(row[0]), row[1]
    rate = get_live_thb_per_usdt()
    upsert_daily_rate(rate_date, rate, "auto", None)
    return rate, "auto"

def get_recent_rates(limit: int = 7):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT rate_date, thb_per_usdt, source, updated_at FROM daily_rates ORDER BY rate_date DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_group_entry_menu():
    return ReplyKeyboardMarkup([["📝 员工登记", "🛠 管理中心"]], resize_keyboard=True, one_time_keyboard=False)

def get_main_menu_for_role(role: str):
    if role == "guest":
        keyboard = [["📝 员工登记"], ["ℹ️ 帮助"]]
    elif role == "pending":
        keyboard = [["📨 审核状态"], ["ℹ️ 帮助"]]
    elif role == "staff":
        keyboard = [["📒 我的账本", "👤 我的资料"], ["📨 审核状态", "ℹ️ 帮助"]]
    else:
        keyboard = [["📒 我的账本", "👤 我的资料"], ["🛠 管理中心", "ℹ️ 帮助"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_wallet_menu():
    return ReplyKeyboardMarkup([["📊 今日记录", "📜 最近流水"], ["📚 历史记录", "💰 我的总账"], ["🏠 返回首页"]], resize_keyboard=True, one_time_keyboard=False)

def get_admin_center_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 员工审批", callback_data="admin|approvals"), InlineKeyboardButton("➕ 新增借款", callback_data="admin|borrow")],
        [InlineKeyboardButton("💸 记录还款", callback_data="admin|repay"), InlineKeyboardButton("📊 全部报表", callback_data="admin|report")],
        [InlineKeyboardButton("📜 员工流水", callback_data="admin|employee_flow"), InlineKeyboardButton("🗂 全部流水", callback_data="admin|all_flows")],
        [InlineKeyboardButton("📆 今日全部流水", callback_data="admin|today_all_flows"), InlineKeyboardButton("❌ 删除员工", callback_data="admin|delete_employee")],
        [InlineKeyboardButton("💱 修改今日汇率", callback_data="admin|set_rate"), InlineKeyboardButton("💱 查看今日汇率", callback_data="admin|show_rate")],
        [InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")],
    ])

def get_employee_picker(action: str):
    employees = get_all_approved_employees()
    if not employees:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")]])
    rows, row = [], []
    for telegram_id, name, role in employees:
        row.append(InlineKeyboardButton(name, callback_data=f"{action}|employee|{telegram_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)

def get_currency_picker(action: str, employee_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 USDT", callback_data=f"{action}|currency|{employee_id}|USDT"),
         InlineKeyboardButton("💴 泰铢", callback_data=f"{action}|currency|{employee_id}|THB")],
        [InlineKeyboardButton("⬅️ 返回员工列表", callback_data=f"{action}|back_employees")],
        [InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")],
    ])

def get_pending_approval_menu():
    rows = []
    pending = get_pending_employees()
    for user_id, tg_username, name, created_at in pending:
        rows.append([
            InlineKeyboardButton(f"✅ 通过 {name}", callback_data=f"approve|{user_id}"),
            InlineKeyboardButton(f"❌ 拒绝 {name}", callback_data=f"reject|{user_id}"),
        ])
    if not rows:
        rows.append([InlineKeyboardButton("无待审批人员", callback_data="noop")])
    rows.append([InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)

def get_delete_employee_menu():
    rows = []
    employees = get_all_approved_employees()
    for telegram_id, name, role in employees:
        rows.append([InlineKeyboardButton(f"❌ 删除 {name}", callback_data=f"delete|{telegram_id}")])
    if not rows:
        rows.append([InlineKeyboardButton("无员工可删除", callback_data="noop")])
    rows.append([InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)

def get_history_page_menu(target_user_id: int, page: int):
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"history|{target_user_id}|{page-1}"))
    nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"history|{target_user_id}|{page+1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")]])

def get_all_flows_page_menu(page: int):
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"allflows|{page-1}"))
    nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"allflows|{page+1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")]])

# 其余函数与上一版一致：流水、报表、日报、按钮处理、文本处理、启动
# 为避免消息超长，建议直接使用我已生成的文件链接下载。
# 如果你需要，我下一条可继续发“精简不省略版后半段”。
