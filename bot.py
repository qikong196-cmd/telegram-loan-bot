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

ADMIN_IDS = set()
for item in ADMIN_IDS_RAW.split(","):
    item = item.strip()
    if item.isdigit():
        ADMIN_IDS.add(int(item))

USER_SESSIONS = {}

_fx_cache = {
    "thb_per_usdt": THB_PER_USDT_FALLBACK,
    "updated_at": 0.0,
}


# ----------------------------
# 时间 / 基础工具
# ----------------------------
def now_local() -> datetime:
    return datetime.now(APP_TZ)


def today_key() -> str:
    return now_local().strftime("%Y-%m-%d")


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            tg_username TEXT,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            approved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by INTEGER
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
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
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_rates (
            rate_date TEXT PRIMARY KEY,
            thb_per_usdt REAL NOT NULL,
            source TEXT NOT NULL,
            set_by INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


# ----------------------------
# 角色 / 员工
# ----------------------------
def get_employee_by_telegram_id(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id, tg_username, name, role, approved, created_at, approved_at, approved_by
        FROM employees
        WHERE telegram_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_employee_by_name(name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id, tg_username, name, role, approved, created_at, approved_at, approved_by
        FROM employees
        WHERE name = ?
        """,
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_employee_display_name_by_id(user_id: int) -> str:
    row = get_employee_by_telegram_id(user_id)
    if row:
        return row[2]
    return str(user_id)


def get_all_approved_employees():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id, name, role
        FROM employees
        WHERE approved = 1
        ORDER BY name ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_pending_employees():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id, tg_username, name, created_at
        FROM employees
        WHERE approved = 0
        ORDER BY created_at ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def register_employee_request(user_id: int, username: str, name: str) -> str:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT approved, name FROM employees WHERE telegram_id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    if row:
        approved, old_name = row
        if approved == 1:
            conn.close()
            return f"你已经是已通过员工：{old_name}"
        cur.execute(
            """
            UPDATE employees
            SET tg_username = ?, name = ?, created_at = ?
            WHERE telegram_id = ?
            """,
            (username, name, now_local().strftime("%Y-%m-%d %H:%M:%S"), user_id),
        )
        conn.commit()
        conn.close()
        return f"已更新登记资料：{name}，等待管理员审批。"

    cur.execute(
        """
        INSERT INTO employees (telegram_id, tg_username, name, role, approved, created_at)
        VALUES (?, ?, ?, 'staff', 0, ?)
        """,
        (user_id, username, name, now_local().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return f"登记成功：{name}，等待管理员审批。"


def approve_employee(user_id: int, approved_by: int, role: str = "staff") -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE employees
        SET approved = 1,
            role = ?,
            approved_at = ?,
            approved_by = ?
        WHERE telegram_id = ?
        """,
        (role, now_local().strftime("%Y-%m-%d %H:%M:%S"), approved_by, user_id),
    )
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


# ----------------------------
# 汇率
# ----------------------------
def get_live_thb_per_usdt() -> float:
    global _fx_cache

    now_ts = time.time()
    if now_ts - _fx_cache["updated_at"] < FX_CACHE_SECONDS:
        return _fx_cache["thb_per_usdt"]

    headers = {}
    if COINGECKO_DEMO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_DEMO_API_KEY

    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": "tether",
        "vs_currencies": "thb",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["tether"]["thb"])
        if rate <= 0:
            raise ValueError("invalid rate")

        _fx_cache = {
            "thb_per_usdt": rate,
            "updated_at": now_ts,
        }
        return rate
    except Exception as e:
        logging.warning("获取实时汇率失败，使用回退汇率。错误：%s", e)
        return THB_PER_USDT_FALLBACK


def upsert_daily_rate(rate_date: str, thb_per_usdt: float, source: str, set_by: int | None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO daily_rates (rate_date, thb_per_usdt, source, set_by, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rate_date) DO UPDATE SET
            thb_per_usdt=excluded.thb_per_usdt,
            source=excluded.source,
            set_by=excluded.set_by,
            updated_at=excluded.updated_at
        """,
        (
            rate_date,
            thb_per_usdt,
            source,
            set_by,
            now_local().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def get_daily_rate(rate_date: str | None = None) -> tuple[float, str]:
    if not rate_date:
        rate_date = today_key()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT thb_per_usdt, source FROM daily_rates WHERE rate_date = ?",
        (rate_date,),
    )
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
    cur.execute(
        """
        SELECT rate_date, thb_per_usdt, source, updated_at
        FROM daily_rates
        ORDER BY rate_date DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ----------------------------
# 菜单
# ----------------------------
def get_main_menu_for_role(role: str):
    if role in {"guest", "pending"}:
        keyboard = [
            ["📝 员工登记", "📨 审核状态"],
            ["👤 我的ID", "ℹ️ 帮助"],
        ]
    elif role == "staff":
        keyboard = [
            ["📊 今日记录", "📜 我的流水"],
            ["📚 历史记录", "💰 我的总账"],
            ["👤 我的资料", "ℹ️ 帮助"],
        ]
    else:
        keyboard = [
            ["📊 今日记录", "📜 我的流水"],
            ["📚 历史记录", "💰 我的总账"],
            ["🛠 管理中心", "👤 我的ID"],
            ["👤 我的资料", "ℹ️ 帮助"],
        ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_admin_center_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 员工审批", callback_data="admin|approvals"),
                InlineKeyboardButton("➕ 新增借款", callback_data="admin|borrow"),
            ],
            [
                InlineKeyboardButton("💸 记录还款", callback_data="admin|repay"),
                InlineKeyboardButton("📊 全部报表", callback_data="admin|report"),
            ],
            [
                InlineKeyboardButton("📜 员工流水", callback_data="admin|employee_flow"),
                InlineKeyboardButton("❌ 删除员工", callback_data="admin|delete_employee"),
            ],
            [
                InlineKeyboardButton("💱 修改今日汇率", callback_data="admin|set_rate"),
                InlineKeyboardButton("💱 查看今日汇率", callback_data="admin|show_rate"),
            ],
            [InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")],
        ]
    )


def get_employee_picker(action: str):
    employees = get_all_approved_employees()
    if not employees:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")]]
        )

    rows = []
    row = []
    for telegram_id, name, role in employees:
        row.append(
            InlineKeyboardButton(name, callback_data=f"{action}|employee|{telegram_id}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def get_currency_picker(action: str, employee_id: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💵 USDT", callback_data=f"{action}|currency|{employee_id}|USDT"),
                InlineKeyboardButton("💴 泰铢", callback_data=f"{action}|currency|{employee_id}|THB"),
            ],
            [InlineKeyboardButton("⬅️ 返回员工列表", callback_data=f"{action}|back_employees")],
            [InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")],
        ]
    )


def get_pending_approval_menu():
    rows = []
    pending = get_pending_employees()
    for user_id, tg_username, name, created_at in pending:
        rows.append(
            [
                InlineKeyboardButton(f"✅ 通过 {name}", callback_data=f"approve|{user_id}"),
                InlineKeyboardButton(f"❌ 拒绝 {name}", callback_data=f"reject|{user_id}"),
            ]
        )

    if not rows:
        rows.append([InlineKeyboardButton("无待审批人员", callback_data="noop")])

    rows.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def get_delete_employee_menu():
    rows = []
    employees = get_all_approved_employees()
    for telegram_id, name, role in employees:
        rows.append(
            [InlineKeyboardButton(f"❌ 删除 {name}", callback_data=f"delete|{telegram_id}")]
        )

    if not rows:
        rows.append([InlineKeyboardButton("无员工可删除", callback_data="noop")])

    rows.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def get_history_page_menu(target_user_id: int, page: int):
    buttons = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"history|{target_user_id}|{page-1}"))
    nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"history|{target_user_id}|{page+1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(buttons)


# ----------------------------
# 格式化 / 交易
# ----------------------------
def format_dual(amount_usdt: float, thb_per_usdt: float) -> str:
    thb = amount_usdt * thb_per_usdt
    return f"{amount_usdt:.2f} USDT（≈ ฿{thb:,.2f}）"


def parse_amount(text: str, thb_per_usdt: float):
    raw = text.strip().upper().replace(" ", "")
    raw = raw.replace("泰铢", "THB").replace("銖", "THB").replace("铢", "THB")

    if raw.endswith("U"):
        raw = raw[:-1] + "USDT"

    unit = "USDT"
    if raw.endswith("USDT"):
        number_part = raw[:-4]
        unit = "USDT"
    elif raw.endswith("THB"):
        number_part = raw[:-3]
        unit = "THB"
    else:
        number_part = raw

    try:
        value = float(number_part)
        if value <= 0:
            return None
    except Exception:
        return None

    if unit == "THB":
        usdt_value = value / thb_per_usdt
    else:
        usdt_value = value

    return {
        "original_value": value,
        "unit": unit,
        "usdt_value": usdt_value,
    }


def record_transaction(
    employee_telegram_id: int,
    tx_type: str,
    amount_text: str,
    operator_id: int,
    operator_name: str,
):
    employee = get_employee_by_telegram_id(employee_telegram_id)
    if not employee:
        return False, "员工不存在。"

    employee_name = employee[2]
    rate, _source = get_daily_rate(today_key())
    parsed = parse_amount(amount_text, rate)
    if parsed is None:
        return False, "金额格式不正确。示例：1000U、1000USDT、36500泰铢、36500THB"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO transactions (
            employee_telegram_id,
            employee_name_snapshot,
            type,
            original_amount,
            original_currency,
            fx_rate,
            amount_usdt,
            operator_id,
            operator_name,
            created_at,
            date_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            employee_telegram_id,
            employee_name,
            tx_type,
            parsed["original_value"],
            parsed["unit"],
            rate,
            parsed["usdt_value"],
            operator_id,
            operator_name,
            now_local().strftime("%Y-%m-%d %H:%M:%S"),
            today_key(),
        ),
    )
    conn.commit()
    conn.close()

    return True, {
        "employee_name": employee_name,
        "parsed": parsed,
        "rate": rate,
    }


def get_balance_by_employee_id(employee_telegram_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount_usdt ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type='repay' THEN amount_usdt ELSE 0 END), 0)
        FROM transactions
        WHERE employee_telegram_id = ?
        """,
        (employee_telegram_id,),
    )
    row = cur.fetchone()
    conn.close()

    borrowed = row[0] or 0
    repaid = row[1] or 0
    balance = borrowed - repaid
    return borrowed, repaid, balance


def get_today_transactions_by_employee(employee_telegram_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT type, original_amount, original_currency, fx_rate, amount_usdt, created_at
        FROM transactions
        WHERE employee_telegram_id = ? AND date_key = ?
        ORDER BY created_at DESC
        """,
        (employee_telegram_id, today_key()),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_recent_transactions_by_employee(employee_telegram_id: int, limit: int = 5):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT type, original_amount, original_currency, fx_rate, amount_usdt, created_at
        FROM transactions
        WHERE employee_telegram_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (employee_telegram_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_history_transactions_by_employee(employee_telegram_id: int, page: int = 1, page_size: int = 10):
    offset = (page - 1) * page_size
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT type, original_amount, original_currency, fx_rate, amount_usdt, created_at
        FROM transactions
        WHERE employee_telegram_id = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (employee_telegram_id, page_size, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_report_rows():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            employee_telegram_id,
            employee_name_snapshot,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount_usdt ELSE 0 END), 0) AS borrowed,
            COALESCE(SUM(CASE WHEN type='repay' THEN amount_usdt ELSE 0 END), 0) AS repaid,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount_usdt ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='repay' THEN amount_usdt ELSE 0 END), 0) AS balance
        FROM transactions
        GROUP BY employee_telegram_id, employee_name_snapshot
        ORDER BY balance DESC, employee_name_snapshot ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_recent_transactions_all(limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT employee_name_snapshot, type, original_amount, original_currency, fx_rate, amount_usdt, created_at
        FROM transactions
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_today_text_for_user(user_id: int) -> str:
    rows = get_today_transactions_by_employee(user_id)
    if not rows:
        return "📊 今天还没有你的记录。"

    borrowed, repaid, balance = get_balance_by_employee_id(user_id)
    lines = ["📊 今日记录：", ""]

    today_borrow_usdt = 0.0
    today_repay_usdt = 0.0

    for tx_type, original_amount, original_currency, fx_rate, amount_usdt, created_at in rows:
        kind = "借款" if tx_type == "borrow" else "还款"
        lines.append(
            f"{created_at[11:16]} {kind}：{original_amount:.2f} {original_currency} → {amount_usdt:.2f} USDT（汇率 {fx_rate:.2f}）"
        )
        if tx_type == "borrow":
            today_borrow_usdt += amount_usdt
        else:
            today_repay_usdt += amount_usdt

    lines.extend(
        [
            "",
            f"今日借款：{today_borrow_usdt:.2f} USDT",
            f"今日还款：{today_repay_usdt:.2f} USDT",
            f"当前未还：{balance:.2f} USDT",
        ]
    )
    return "\n".join(lines)


def build_recent_flow_text_for_user(user_id: int) -> str:
    rows = get_recent_transactions_by_employee(user_id, limit=5)
    if not rows:
        return "📜 你还没有流水记录。"

    lines = ["📜 我的流水（最近 5 笔）：", ""]
    for tx_type, original_amount, original_currency, fx_rate, amount_usdt, created_at in rows:
        kind = "借款" if tx_type == "borrow" else "还款"
        lines.append(
            f"{created_at}｜{kind}｜{original_amount:.2f} {original_currency} → {amount_usdt:.2f} USDT（汇率 {fx_rate:.2f}）"
        )
    return "\n".join(lines)


def build_history_text_for_user(user_id: int, page: int) -> str:
    rows = get_history_transactions_by_employee(user_id, page=page, page_size=10)
    if not rows:
        return f"📚 历史记录（第 {page} 页）暂无数据。"

    lines = [f"📚 历史记录（第 {page} 页）", ""]
    idx = (page - 1) * 10
    for tx_type, original_amount, original_currency, fx_rate, amount_usdt, created_at in rows:
        idx += 1
        kind = "借款" if tx_type == "borrow" else "还款"
        lines.append(
            f"{idx}. {created_at}｜{kind}｜{original_amount:.2f} {original_currency} → {amount_usdt:.2f} USDT（汇率 {fx_rate:.2f}）"
        )
    return "\n".join(lines)


def build_total_text_for_user(user_id: int) -> str:
    employee = get_employee_by_telegram_id(user_id)
    if not employee:
        return "未找到你的员工资料。"

    name = employee[2]
    borrowed, repaid, balance = get_balance_by_employee_id(user_id)
    rate, _ = get_daily_rate(today_key())
    return (
        f"💰 我的总账\n\n"
        f"员工：{name}\n"
        f"累计借资：{format_dual(borrowed, rate)}\n"
        f"累计还款：{format_dual(repaid, rate)}\n"
        f"当前未还：{format_dual(balance, rate)}"
    )


def build_profile_text_for_user(user_id: int) -> str:
    role = get_role(user_id)
    row = get_employee_by_telegram_id(user_id)

    if role in {"guest"} or not row:
        return "👤 你还没有登记员工资料。"

    tg_username = row[1] or "-"
    name = row[2]
    approved = "已通过" if row[4] == 1 else "待审批"
    return (
        f"👤 我的资料\n\n"
        f"姓名：{name}\n"
        f"用户名：@{tg_username}" if tg_username != "-" else f"👤 我的资料\n\n姓名：{name}\n用户名：-"
    ) + (
        f"\n角色：{role}\n状态：{approved}\n登记时间：{row[5]}"
    )


def build_all_report_text() -> str:
    rows = get_all_report_rows()
    if not rows:
        return "📊 当前没有任何账目记录。"

    rate, _ = get_daily_rate(today_key())
    total_borrowed = sum(row[2] for row in rows)
    total_repaid = sum(row[3] for row in rows)
    total_balance = sum(row[4] for row in rows)

    lines = ["📊 全部报表：", ""]
    for _employee_id, name, borrowed, repaid, balance in rows:
        lines.append(
            f"{name}：借资 {format_dual(borrowed, rate)} / 还款 {format_dual(repaid, rate)} / 未还 {format_dual(balance, rate)}"
        )

    lines.extend(
        [
            "",
            f"总借资：{format_dual(total_borrowed, rate)}",
            f"总还款：{format_dual(total_repaid, rate)}",
            f"总未还：{format_dual(total_balance, rate)}",
        ]
    )
    return "\n".join(lines)


def build_employee_flow_text(target_user_id: int) -> str:
    employee = get_employee_by_telegram_id(target_user_id)
    if not employee:
        return "未找到该员工。"

    rows = get_recent_transactions_by_employee(target_user_id, limit=10)
    if not rows:
        return f"📜 员工流水\n\n员工：{employee[2]}\n暂无流水记录。"

    lines = [f"📜 员工流水\n\n员工：{employee[2]}", ""]
    for tx_type, original_amount, original_currency, fx_rate, amount_usdt, created_at in rows:
        kind = "借款" if tx_type == "borrow" else "还款"
        lines.append(
            f"{created_at}｜{kind}｜{original_amount:.2f} {original_currency} → {amount_usdt:.2f} USDT（汇率 {fx_rate:.2f}）"
        )
    return "\n".join(lines)


# ----------------------------
# 日报 / 定时任务
# ----------------------------
def build_daily_report_text() -> str:
    today = today_key()
    rate, source = get_daily_rate(today)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount_usdt ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type='repay' THEN amount_usdt ELSE 0 END), 0),
            COUNT(*)
        FROM transactions
        WHERE date_key = ?
        """,
        (today,),
    )
    row = cur.fetchone()
    today_borrow = row[0] or 0
    today_repay = row[1] or 0
    today_count = row[2] or 0

    cur.execute(
        """
        SELECT COUNT(*)
        FROM employees
        WHERE approved = 1 AND DATE(approved_at) = ?
        """,
        (today,),
    )
    new_approved = cur.fetchone()[0] or 0

    cur.execute(
        """
        SELECT
            employee_name_snapshot,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount_usdt ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='repay' THEN amount_usdt ELSE 0 END), 0) AS balance
        FROM transactions
        GROUP BY employee_telegram_id, employee_name_snapshot
        HAVING balance > 0
        ORDER BY balance DESC
        LIMIT 3
        """
    )
    top_rows = cur.fetchall()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount_usdt ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='repay' THEN amount_usdt ELSE 0 END), 0)
        FROM transactions
        """
    )
    total_balance = cur.fetchone()[0] or 0

    conn.close()

    lines = [
        f"📊 每日汇总（{today}）",
        "",
        f"💰 今日借款：{today_borrow:.2f} USDT",
        f"💸 今日还款：{today_repay:.2f} USDT",
        f"📉 净借出：{today_borrow - today_repay:.2f} USDT",
        "",
        f"📦 当前总未还：{total_balance:.2f} USDT",
        f"👥 今日新通过员工：{new_approved} 人",
        f"📄 今日流水：{today_count} 笔",
        "",
        f"💱 今日汇率：1 USDT ≈ ฿{rate:.2f}（{source}）",
        "",
        "🏆 欠款前 3：",
    ]

    if not top_rows:
        lines.append("暂无")
    else:
        for idx, (name, balance) in enumerate(top_rows, start=1):
            lines.append(f"{idx}. {name} - {balance:.2f} USDT")

    return "\n".join(lines)


async def daily_fx_job(context: ContextTypes.DEFAULT_TYPE):
    rate = get_live_thb_per_usdt()
    upsert_daily_rate(today_key(), rate, "auto", None)
    logging.info("每日自动汇率更新完成：%s", rate)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    text = build_daily_report_text()
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            logging.warning("发送日报给管理员 %s 失败：%s", admin_id, e)


# ----------------------------
# 命令
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = get_role(update.effective_user.id)
    rate, _ = get_daily_rate(today_key())

    msg = (
        "🤖 借资记账机器人已上线\n\n"
        "员工可用：今日记录、我的流水、历史记录、我的总账\n"
        "管理员可用：管理中心、员工审批、借款还款、汇率设置、报表\n\n"
        f"今日汇率：1 USDT ≈ ฿{rate:.2f}"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu_for_role(role))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = get_role(update.effective_user.id)
    msg = (
        "ℹ️ 使用说明\n\n"
        "员工：\n"
        "📊 今日记录\n"
        "📜 我的流水\n"
        "📚 历史记录\n"
        "💰 我的总账\n\n"
        "管理员：\n"
        "🛠 管理中心 → 审批、借款、还款、报表、汇率\n\n"
        "文本也支持：\n"
        "借款 张三 1000U\n"
        "还款 张三 36500THB\n"
        "新增员工 张三\n"
        "删除员工 张三"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu_for_role(role))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = get_role(update.effective_user.id)
    await update.message.reply_text(
        f"👤 你的 Telegram 用户ID 是：{update.effective_user.id}",
        reply_markup=get_main_menu_for_role(role),
    )


# ----------------------------
# 按钮回调
# ----------------------------
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    role = get_role(user_id)
    data = query.data

    if data == "noop":
        return

    if data == "menu|home":
        USER_SESSIONS.pop(user_id, None)
        await query.message.reply_text("🏠 已返回主菜单", reply_markup=get_main_menu_for_role(role))
        return

    if data == "admin|approvals":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await query.message.reply_text("✅ 员工审批：", reply_markup=get_pending_approval_menu())
        return

    if data == "admin|borrow":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await query.message.reply_text("请选择借款员工：", reply_markup=get_employee_picker("borrow"))
        return

    if data == "admin|repay":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await query.message.reply_text("请选择还款员工：", reply_markup=get_employee_picker("repay"))
        return

    if data == "admin|report":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await query.message.reply_text(build_all_report_text(), reply_markup=get_main_menu_for_role(role))
        return

    if data == "admin|employee_flow":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await query.message.reply_text("请选择员工查看流水：", reply_markup=get_employee_picker("flow"))
        return

    if data == "admin|delete_employee":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await query.message.reply_text("请选择要删除的员工：", reply_markup=get_delete_employee_menu())
        return

    if data == "admin|set_rate":
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        USER_SESSIONS[user_id] = {"waiting_rate_input": True}
        rate, source = get_daily_rate(today_key())
        await query.message.reply_text(
            f"请输入今天要使用的汇率，例如：36.5\n\n当前：1 USDT ≈ ฿{rate:.2f}（{source}）",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if data == "admin|show_rate":
        rate, source = get_daily_rate(today_key())
        recent = get_recent_rates(5)
        lines = [f"💱 今日汇率：1 USDT ≈ ฿{rate:.2f}（{source}）", "", "最近 5 天："]
        for rate_date, thb_per_usdt, src, updated_at in recent:
            lines.append(f"{rate_date}：{thb_per_usdt:.2f}（{src}）")
        await query.message.reply_text("\n".join(lines), reply_markup=get_main_menu_for_role(role))
        return

    if data.startswith("approve|"):
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        target_user_id = int(data.split("|")[1])
        if approve_employee(target_user_id, user_id, "staff"):
            await query.message.reply_text("✅ 已通过员工审批。", reply_markup=get_main_menu_for_role(role))
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text="✅ 你的员工登记已通过审核。",
                    reply_markup=get_main_menu_for_role("staff"),
                )
            except Exception:
                pass
        else:
            await query.message.reply_text("审批失败。", reply_markup=get_main_menu_for_role(role))
        return

    if data.startswith("reject|"):
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        target_user_id = int(data.split("|")[1])
        if reject_employee(target_user_id):
            await query.message.reply_text("❌ 已拒绝并删除登记申请。", reply_markup=get_main_menu_for_role(role))
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text="❌ 你的员工登记未通过审核，请联系管理员。",
                )
            except Exception:
                pass
        else:
            await query.message.reply_text("操作失败。", reply_markup=get_main_menu_for_role(role))
        return

    if data.startswith("delete|"):
        if not is_manager(user_id):
            await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        target_user_id = int(data.split("|")[1])
        target_name = get_employee_display_name_by_id(target_user_id)
        if delete_employee(target_user_id):
            await query.message.reply_text(
                f"✅ 已删除员工：{target_name}",
                reply_markup=get_main_menu_for_role(role),
            )
        else:
            await query.message.reply_text("删除失败。", reply_markup=get_main_menu_for_role(role))
        return

    if data.endswith("|back_employees"):
        action = data.split("|")[0]
        await query.message.reply_text(
            "请选择员工：",
            reply_markup=get_employee_picker(action),
        )
        return

    parts = data.split("|")

    if len(parts) == 3 and parts[1] == "employee":
        action = parts[0]
        target_user_id = int(parts[2])

        if action == "flow":
            if not is_manager(user_id):
                await query.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
                return
            await query.message.reply_text(
                build_employee_flow_text(target_user_id),
                reply_markup=get_main_menu_for_role(role),
            )
            return

        USER_SESSIONS[user_id] = {
            "action": action,
            "target_user_id": target_user_id,
        }

        if action == "query":
            await query.message.reply_text(
                build_employee_flow_text(target_user_id),
                reply_markup=get_main_menu_for_role(role),
            )
            return

        await query.message.reply_text(
            f"已选择员工：{get_employee_display_name_by_id(target_user_id)}\n请选择币种：",
            reply_markup=get_currency_picker(action, target_user_id),
        )
        return

    if len(parts) == 4 and parts[1] == "currency":
        action = parts[0]
        target_user_id = int(parts[2])
        currency = parts[3]

        USER_SESSIONS[user_id] = {
            "action": action,
            "target_user_id": target_user_id,
            "currency": currency,
            "waiting_amount": True,
        }

        unit_name = "USDT" if currency == "USDT" else "泰铢"
        await query.message.reply_text(
            f"已选择：{get_employee_display_name_by_id(target_user_id)} / {unit_name}\n\n请输入金额数字即可，例如：1000",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if parts[0] == "history" and len(parts) == 3:
        target_user_id = int(parts[1])
        page = int(parts[2])

        if target_user_id != user_id and not is_manager(user_id):
            await query.message.reply_text("你没有权限查看。", reply_markup=get_main_menu_for_role(role))
            return

        text = build_history_text_for_user(target_user_id, page)
        await query.message.reply_text(
            text,
            reply_markup=get_history_page_menu(target_user_id, page),
        )
        return


# ----------------------------
# 文本输入处理
# ----------------------------
async def chinese_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    user_id = user.id
    role = get_role(user_id)
    text = update.message.text.strip()

    if not text:
        return

    # 会话：等待输入金额
    if user_id in USER_SESSIONS:
        session = USER_SESSIONS[user_id]

        if session.get("waiting_amount"):
            currency = session["currency"]
            amount_text = f"{text}USDT" if currency == "USDT" else f"{text}THB"

            ok, result = record_transaction(
                employee_telegram_id=session["target_user_id"],
                tx_type="borrow" if session["action"] == "borrow" else "repay",
                amount_text=amount_text,
                operator_id=user_id,
                operator_name=user.full_name,
            )
            USER_SESSIONS.pop(user_id, None)

            if not ok:
                await update.message.reply_text(result, reply_markup=get_main_menu_for_role(role))
                return

            employee_id = session["target_user_id"]
            borrowed, repaid, balance = get_balance_by_employee_id(employee_id)
            employee_name = result["employee_name"]
            parsed = result["parsed"]
            rate = result["rate"]

            if session["action"] == "borrow":
                msg = (
                    f"✅ 已记录借资\n\n"
                    f"👤 员工：{employee_name}\n"
                    f"📝 录入金额：{parsed['original_value']:.2f} {parsed['unit']}\n"
                    f"💰 本次借资：{format_dual(parsed['usdt_value'], rate)}\n"
                    f"📊 累计借资：{format_dual(borrowed, rate)}\n"
                    f"📉 累计还款：{format_dual(repaid, rate)}\n"
                    f"🧾 当前未还：{format_dual(balance, rate)}"
                )
            else:
                msg = (
                    f"✅ 已记录还款\n\n"
                    f"👤 员工：{employee_name}\n"
                    f"📝 录入金额：{parsed['original_value']:.2f} {parsed['unit']}\n"
                    f"💸 本次还款：{format_dual(parsed['usdt_value'], rate)}\n"
                    f"📊 累计借资：{format_dual(borrowed, rate)}\n"
                    f"📉 累计还款：{format_dual(repaid, rate)}\n"
                    f"🧾 当前未还：{format_dual(balance, rate)}"
                )

            await update.message.reply_text(msg, reply_markup=get_main_menu_for_role(role))
            return

        if session.get("waiting_register_name"):
            USER_SESSIONS.pop(user_id, None)
            username = user.username or ""
            msg = register_employee_request(user_id, username, text)
            await update.message.reply_text(msg, reply_markup=get_main_menu_for_role(get_role(user_id)))
            return

        if session.get("waiting_rate_input"):
            try:
                new_rate = float(text)
                if new_rate <= 0:
                    raise ValueError
            except Exception:
                await update.message.reply_text("请输入正确汇率，例如：36.5", reply_markup=get_main_menu_for_role(role))
                return

            upsert_daily_rate(today_key(), new_rate, "manual", user_id)
            USER_SESSIONS.pop(user_id, None)
            await update.message.reply_text(
                f"✅ 已修改今日汇率：1 USDT ≈ ฿{new_rate:.2f}",
                reply_markup=get_main_menu_for_role(role),
            )
            return

    # 按钮菜单
    if text in ["🏠 主菜单", "菜单", "首页"]:
        await start(update, context)
        return

    if text in ["ℹ️ 帮助", "帮助"]:
        await help_cmd(update, context)
        return

    if text in ["👤 我的ID", "我的ID", "ID", "id"]:
        await myid(update, context)
        return

    # 待审批 / guest
    if text == "📝 员工登记":
        USER_SESSIONS[user_id] = {"waiting_register_name": True}
        await update.message.reply_text(
            "请输入你的员工姓名：",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if text == "📨 审核状态":
        if role == "pending":
            await update.message.reply_text("📨 你的登记正在等待管理员审批。", reply_markup=get_main_menu_for_role(role))
        elif role in {"staff", "admin", "superadmin"}:
            await update.message.reply_text("✅ 你已经通过审批。", reply_markup=get_main_menu_for_role(role))
        else:
            await update.message.reply_text("你还没有提交登记。", reply_markup=get_main_menu_for_role(role))
        return

    # 员工菜单
    if text == "📊 今日记录":
        if role not in {"staff", "admin", "superadmin"}:
            await update.message.reply_text("请先完成员工登记并通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        await update.message.reply_text(build_today_text_for_user(user_id), reply_markup=get_main_menu_for_role(role))
        return

    if text == "📜 我的流水":
        if role not in {"staff", "admin", "superadmin"}:
            await update.message.reply_text("请先完成员工登记并通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        await update.message.reply_text(build_recent_flow_text_for_user(user_id), reply_markup=get_main_menu_for_role(role))
        return

    if text == "📚 历史记录":
        if role not in {"staff", "admin", "superadmin"}:
            await update.message.reply_text("请先完成员工登记并通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        await update.message.reply_text(
            build_history_text_for_user(user_id, 1),
            reply_markup=get_history_page_menu(user_id, 1),
        )
        return

    if text == "💰 我的总账":
        if role not in {"staff", "admin", "superadmin"}:
            await update.message.reply_text("请先完成员工登记并通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        await update.message.reply_text(build_total_text_for_user(user_id), reply_markup=get_main_menu_for_role(role))
        return

    if text == "👤 我的资料":
        await update.message.reply_text(build_profile_text_for_user(user_id), reply_markup=get_main_menu_for_role(role))
        return

    # 管理员菜单
    if text == "🛠 管理中心":
        if not is_manager(user_id):
            await update.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        await update.message.reply_text("🛠 管理中心：", reply_markup=get_admin_center_menu())
        return

    if text == "📊 全部报表" and is_manager(user_id):
        await update.message.reply_text(build_all_report_text(), reply_markup=get_main_menu_for_role(role))
        return

    if text == "🧾 借款示例":
        await update.message.reply_text(
            "借款示例：\n借款 张三 1000U\n借款 张三 36500泰铢\n借款 张三=36500THB",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if text == "🧾 还款示例":
        await update.message.reply_text(
            "还款示例：\n还款 张三 1000U\n还款 张三 36500THB",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    # 文本指令兼容
    normalized = re.sub(r"[=，,]", " ", text)
    parts = normalized.split()

    if not parts:
        return

    action = parts[0]

    if action == "借款":
        if not is_manager(user_id):
            await update.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        if len(parts) < 3:
            await update.message.reply_text("用法：借款 姓名 金额单位", reply_markup=get_main_menu_for_role(role))
            return
        emp = get_employee_by_name(parts[1])
        if not emp or emp[4] != 1:
            await update.message.reply_text("员工不存在或未通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        ok, result = record_transaction(emp[0], "borrow", parts[2], user_id, user.full_name)
        if not ok:
            await update.message.reply_text(result, reply_markup=get_main_menu_for_role(role))
            return
        borrowed, repaid, balance = get_balance_by_employee_id(emp[0])
        parsed = result["parsed"]
        rate = result["rate"]
        await update.message.reply_text(
            f"✅ 已记录借资\n\n"
            f"👤 员工：{result['employee_name']}\n"
            f"📝 录入金额：{parsed['original_value']:.2f} {parsed['unit']}\n"
            f"💰 本次借资：{format_dual(parsed['usdt_value'], rate)}\n"
            f"📊 累计借资：{format_dual(borrowed, rate)}\n"
            f"📉 累计还款：{format_dual(repaid, rate)}\n"
            f"🧾 当前未还：{format_dual(balance, rate)}",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if action == "还款":
        if not is_manager(user_id):
            await update.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        if len(parts) < 3:
            await update.message.reply_text("用法：还款 姓名 金额单位", reply_markup=get_main_menu_for_role(role))
            return
        emp = get_employee_by_name(parts[1])
        if not emp or emp[4] != 1:
            await update.message.reply_text("员工不存在或未通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        ok, result = record_transaction(emp[0], "repay", parts[2], user_id, user.full_name)
        if not ok:
            await update.message.reply_text(result, reply_markup=get_main_menu_for_role(role))
            return
        borrowed, repaid, balance = get_balance_by_employee_id(emp[0])
        parsed = result["parsed"]
        rate = result["rate"]
        await update.message.reply_text(
            f"✅ 已记录还款\n\n"
            f"👤 员工：{result['employee_name']}\n"
            f"📝 录入金额：{parsed['original_value']:.2f} {parsed['unit']}\n"
            f"💸 本次还款：{format_dual(parsed['usdt_value'], rate)}\n"
            f"📊 累计借资：{format_dual(borrowed, rate)}\n"
            f"📉 累计还款：{format_dual(repaid, rate)}\n"
            f"🧾 当前未还：{format_dual(balance, rate)}",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if action == "查询":
        if len(parts) < 2:
            await update.message.reply_text("用法：查询 姓名", reply_markup=get_main_menu_for_role(role))
            return
        emp = get_employee_by_name(parts[1])
        if not emp or emp[4] != 1:
            await update.message.reply_text("员工不存在或未通过审批。", reply_markup=get_main_menu_for_role(role))
            return
        if emp[0] != user_id and not is_manager(user_id):
            await update.message.reply_text("你只能查询自己的信息。", reply_markup=get_main_menu_for_role(role))
            return
        target_id = emp[0]
        await update.message.reply_text(build_total_text_for_user(target_id), reply_markup=get_main_menu_for_role(role))
        return

    if action == "新增员工":
        if not is_manager(user_id):
            await update.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        if len(parts) < 2:
            await update.message.reply_text("用法：新增员工 姓名", reply_markup=get_main_menu_for_role(role))
            return
        # 管理员直加仅登记为待审批不合适，这里仍建议员工自助登记
        await update.message.reply_text(
            "建议员工自己点击“员工登记”。当前版本管理员新增员工请让员工先和机器人互动完成登记。",
            reply_markup=get_main_menu_for_role(role),
        )
        return

    if action == "删除员工":
        if not is_manager(user_id):
            await update.message.reply_text("你没有权限。", reply_markup=get_main_menu_for_role(role))
            return
        if len(parts) < 2:
            await update.message.reply_text("用法：删除员工 姓名", reply_markup=get_main_menu_for_role(role))
            return
        emp = get_employee_by_name(parts[1])
        if not emp:
            await update.message.reply_text("员工不存在。", reply_markup=get_main_menu_for_role(role))
            return
        ok = delete_employee(emp[0])
        await update.message.reply_text(
            f"{'✅ 已删除员工：' if ok else '删除失败：'}{parts[1]}",
            reply_markup=get_main_menu_for_role(role),
        )
        return


# ----------------------------
# 启动
# ----------------------------
async def post_init(application: Application):
    rate = get_live_thb_per_usdt()
    upsert_daily_rate(today_key(), rate, "auto", None)

    # 每天 09:00 自动抓汇率
    application.job_queue.run_daily(
        daily_fx_job,
        time=dtime(hour=9, minute=0, tzinfo=APP_TZ),
        name="daily_fx_job",
    )

    # 每天 21:00 自动发日报到超级管理员私聊
    application.job_queue.run_daily(
        daily_report_job,
        time=dtime(hour=21, minute=0, tzinfo=APP_TZ),
        name="daily_report_job",
    )

    logging.info("定时任务已启动。")


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少 BOT_TOKEN 环境变量")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid))

    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chinese_text_handler))

    logging.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
