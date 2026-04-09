import os
import re
import time
import sqlite3
import logging
from datetime import datetime

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
DB_PATH = os.getenv("DB_PATH", "loan_bot.db")

# 汇率相关
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


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('borrow', 'repay')),
            amount REAL NOT NULL,
            operator_id INTEGER NOT NULL,
            operator_name TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_main_menu():
    keyboard = [
        ["📥 借款", "📤 还款", "🔎 查询"],
        ["➕ 新增员工", "❌ 删除员工", "📊 报表"],
        ["👤 我的ID", "ℹ️ 帮助", "💱 当前汇率"],
        ["🧾 借款示例", "🧾 还款示例", "🏠 主菜单"],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_conn_cursor():
    conn = get_conn()
    cur = conn.cursor()
    return conn, cur


def get_employees():
    conn, cur = get_conn_cursor()
    cur.execute("SELECT name FROM employees ORDER BY name ASC")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


def add_employee(name: str) -> bool:
    name = name.strip()
    if not name:
        return False

    conn, cur = get_conn_cursor()
    try:
        cur.execute(
            "INSERT INTO employees (name, created_at) VALUES (?, ?)",
            (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_employee(name: str) -> bool:
    conn, cur = get_conn_cursor()
    cur.execute("DELETE FROM employees WHERE name = ?", (name,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def employee_exists(name: str) -> bool:
    conn, cur = get_conn_cursor()
    cur.execute("SELECT 1 FROM employees WHERE name = ? LIMIT 1", (name,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_employee_inline_menu(action: str):
    employees = get_employees()
    if not employees:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")]]
        )

    rows = []
    row = []

    for name in employees:
        row.append(
            InlineKeyboardButton(name, callback_data=f"{action}|employee|{name}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def get_currency_inline_menu(action: str, employee_name: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💵 USDT",
                    callback_data=f"{action}|currency|{employee_name}|USDT",
                ),
                InlineKeyboardButton(
                    "💴 泰铢",
                    callback_data=f"{action}|currency|{employee_name}|THB",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回员工列表",
                    callback_data=f"{action}|back_employees",
                )
            ],
            [InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")],
        ]
    )


def get_query_employee_inline_menu():
    employees = get_employees()
    if not employees:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")]]
        )

    rows = []
    row = []

    for name in employees:
        row.append(
            InlineKeyboardButton(name, callback_data=f"query|employee|{name}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def get_delete_employee_inline_menu():
    employees = get_employees()
    if not employees:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")]]
        )

    rows = []
    row = []

    for name in employees:
        row.append(
            InlineKeyboardButton(f"❌ {name}", callback_data=f"delete_employee|{name}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def format_thb(amount_usdt: float, thb_per_usdt: float) -> str:
    thb = amount_usdt * thb_per_usdt
    return f"฿{thb:,.2f}"


def format_dual(amount_usdt: float, thb_per_usdt: float) -> str:
    return f"{amount_usdt:.2f} USDT（≈ {format_thb(amount_usdt, thb_per_usdt)}）"


def get_live_thb_per_usdt() -> float:
    global _fx_cache

    now = time.time()
    if now - _fx_cache["updated_at"] < FX_CACHE_SECONDS:
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

        thb_per_usdt = float(data["tether"]["thb"])
        if thb_per_usdt <= 0:
            raise ValueError("Invalid exchange rate")

        _fx_cache = {
            "thb_per_usdt": thb_per_usdt,
            "updated_at": now,
        }
        return thb_per_usdt
    except Exception as e:
        logging.warning("获取实时汇率失败，使用回退汇率。错误：%s", e)
        return THB_PER_USDT_FALLBACK


def parse_amount(text: str, thb_per_usdt: float):
    raw = text.strip().upper().replace(" ", "")

    raw = raw.replace("泰铢", "THB")
    raw = raw.replace("銖", "THB")
    raw = raw.replace("铢", "THB")

    if raw.endswith("U"):
        raw = raw[:-1] + "USDT"

    unit = "USDT"

    if raw.endswith("USDT"):
        unit = "USDT"
        number_part = raw[:-4]
    elif raw.endswith("THB"):
        unit = "THB"
        number_part = raw[:-3]
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


def get_balance(employee_name: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type='repay' THEN amount ELSE 0 END), 0)
        FROM transactions
        WHERE employee_name = ?
        """,
        (employee_name,),
    )
    row = cur.fetchone()
    conn.close()

    borrowed = row[0] or 0
    repaid = row[1] or 0
    balance = borrowed - repaid
    return borrowed, repaid, balance


def get_report():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            employee_name,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount ELSE 0 END), 0) AS borrowed,
            COALESCE(SUM(CASE WHEN type='repay' THEN amount ELSE 0 END), 0) AS repaid,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='repay' THEN amount ELSE 0 END), 0) AS balance
        FROM transactions
        GROUP BY employee_name
        ORDER BY balance DESC, employee_name ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


async def require_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text(
                "你没有权限使用这个命令。",
                reply_markup=get_main_menu(),
            )
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thb_per_usdt = get_live_thb_per_usdt()
    employee_count = len(get_employees())
    msg = (
        "🤖 借资记账机器人已上线\n\n"
        "支持按钮流程：\n"
        "借款 / 还款 / 查询 → 选择员工 → 选择币种 → 输入金额\n\n"
        "支持员工管理：\n"
        "➕ 新增员工\n"
        "❌ 删除员工\n\n"
        "当前员工人数："
        f"{employee_count}\n"
        f"当前实时参考汇率：1 USDT ≈ ฿{thb_per_usdt:.2f}"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "ℹ️ 使用说明\n\n"
        "按钮模式：\n"
        "点击 借款 / 还款 / 查询\n"
        "按步骤选择即可\n\n"
        "员工管理：\n"
        "➕ 新增员工\n"
        "❌ 删除员工\n\n"
        "文本模式：\n"
        "借款 姓名 金额单位\n"
        "还款 姓名 金额单位\n"
        "查询 姓名\n"
        "报表\n"
        "新增员工 姓名\n"
        "删除员工 姓名\n\n"
        "金额支持：\n"
        "1000U / 1000USDT / 36500泰铢 / 36500THB"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu())


async def fx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thb_per_usdt = get_live_thb_per_usdt()
    await update.message.reply_text(
        f"💱 当前实时参考汇率：1 USDT ≈ ฿{thb_per_usdt:.2f}",
        reply_markup=get_main_menu(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👤 你的 Telegram 用户ID 是：{user.id}",
        reply_markup=get_main_menu(),
    )


async def borrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "用法：/borrow 姓名 金额单位\n示例：/borrow 张三 1000U",
            reply_markup=get_main_menu(),
        )
        return

    employee_name = context.args[0].strip()
    if not employee_exists(employee_name):
        await update.message.reply_text(
            f"员工 {employee_name} 不存在，请先新增员工。",
            reply_markup=get_main_menu(),
        )
        return

    thb_per_usdt = get_live_thb_per_usdt()
    parsed = parse_amount(context.args[1], thb_per_usdt)

    if not employee_name:
        await update.message.reply_text("姓名不能为空。", reply_markup=get_main_menu())
        return

    if parsed is None:
        await update.message.reply_text(
            "金额格式不正确。\n示例：1000U、1000USDT、36500泰铢、36500THB",
            reply_markup=get_main_menu(),
        )
        return

    user = update.effective_user
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO transactions (employee_name, type, amount, operator_id, operator_name, created_at)
        VALUES (?, 'borrow', ?, ?, ?, ?)
        """,
        (
            employee_name,
            parsed["usdt_value"],
            user.id,
            user.full_name,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()

    borrowed, repaid, balance = get_balance(employee_name)
    await update.message.reply_text(
        f"✅ 已记录借资\n\n"
        f"👤 员工：{employee_name}\n"
        f"📝 录入金额：{parsed['original_value']:.2f} {parsed['unit']}\n"
        f"💰 本次借资：{format_dual(parsed['usdt_value'], thb_per_usdt)}\n"
        f"📊 累计借资：{format_dual(borrowed, thb_per_usdt)}\n"
        f"📉 累计还款：{format_dual(repaid, thb_per_usdt)}\n"
        f"🧾 当前未还：{format_dual(balance, thb_per_usdt)}",
        reply_markup=get_main_menu(),
    )


async def repay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "用法：/repay 姓名 金额单位\n示例：/repay 张三 1000THB",
            reply_markup=get_main_menu(),
        )
        return

    employee_name = context.args[0].strip()
    if not employee_exists(employee_name):
        await update.message.reply_text(
            f"员工 {employee_name} 不存在，请先新增员工。",
            reply_markup=get_main_menu(),
        )
        return

    thb_per_usdt = get_live_thb_per_usdt()
    parsed = parse_amount(context.args[1], thb_per_usdt)

    if not employee_name:
        await update.message.reply_text("姓名不能为空。", reply_markup=get_main_menu())
        return

    if parsed is None:
        await update.message.reply_text(
            "金额格式不正确。\n示例：1000U、1000USDT、36500泰铢、36500THB",
            reply_markup=get_main_menu(),
        )
        return

    borrowed, repaid, balance = get_balance(employee_name)
    if borrowed == 0 and repaid == 0:
        await update.message.reply_text(
            f"未找到员工 {employee_name} 的借资记录。",
            reply_markup=get_main_menu(),
        )
        return

    user = update.effective_user
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO transactions (employee_name, type, amount, operator_id, operator_name, created_at)
        VALUES (?, 'repay', ?, ?, ?, ?)
        """,
        (
            employee_name,
            parsed["usdt_value"],
            user.id,
            user.full_name,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()

    borrowed, repaid, balance = get_balance(employee_name)
    await update.message.reply_text(
        f"✅ 已记录还款\n\n"
        f"👤 员工：{employee_name}\n"
        f"📝 录入金额：{parsed['original_value']:.2f} {parsed['unit']}\n"
        f"💸 本次还款：{format_dual(parsed['usdt_value'], thb_per_usdt)}\n"
        f"📊 累计借资：{format_dual(borrowed, thb_per_usdt)}\n"
        f"📉 累计还款：{format_dual(repaid, thb_per_usdt)}\n"
        f"🧾 当前未还：{format_dual(balance, thb_per_usdt)}",
        reply_markup=get_main_menu(),
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text(
            "用法：/status 姓名",
            reply_markup=get_main_menu(),
        )
        return

    employee_name = context.args[0].strip()
    borrowed, repaid, balance = get_balance(employee_name)
    thb_per_usdt = get_live_thb_per_usdt()

    if borrowed == 0 and repaid == 0:
        await update.message.reply_text(
            f"未找到员工 {employee_name} 的记录。",
            reply_markup=get_main_menu(),
        )
        return

    await update.message.reply_text(
        f"🔎 员工：{employee_name}\n\n"
        f"📊 累计借资：{format_dual(borrowed, thb_per_usdt)}\n"
        f"📉 累计还款：{format_dual(repaid, thb_per_usdt)}\n"
        f"🧾 当前未还：{format_dual(balance, thb_per_usdt)}",
        reply_markup=get_main_menu(),
    )


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    rows = get_report()
    if not rows:
        await update.message.reply_text(
            "当前没有任何账目记录。",
            reply_markup=get_main_menu(),
        )
        return

    thb_per_usdt = get_live_thb_per_usdt()
    total_borrowed = sum(row[1] for row in rows)
    total_repaid = sum(row[2] for row in rows)
    total_balance = sum(row[3] for row in rows)

    lines = ["📊 借资汇总：", ""]

    for name, borrowed, repaid, balance in rows:
        lines.append(
            f"{name}：借资 {format_dual(borrowed, thb_per_usdt)} / 还款 {format_dual(repaid, thb_per_usdt)} / 未还 {format_dual(balance, thb_per_usdt)}"
        )

    lines.extend(
        [
            "",
            f"总借资：{format_dual(total_borrowed, thb_per_usdt)}",
            f"总还款：{format_dual(total_repaid, thb_per_usdt)}",
            f"总未还：{format_dual(total_balance, thb_per_usdt)}",
            "",
            f"当前参考汇率：1 USDT ≈ ฿{thb_per_usdt:.2f}",
        ]
    )

    await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu())


async def add_employee_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    if len(context.args) < 1:
        await update.message.reply_text(
            "用法：新增员工 姓名",
            reply_markup=get_main_menu(),
        )
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("员工姓名不能为空。", reply_markup=get_main_menu())
        return

    if add_employee(name):
        await update.message.reply_text(
            f"✅ 已新增员工：{name}",
            reply_markup=get_main_menu(),
        )
    else:
        await update.message.reply_text(
            f"员工 {name} 已存在，或新增失败。",
            reply_markup=get_main_menu(),
        )


async def delete_employee_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    if len(context.args) < 1:
        await update.message.reply_text(
            "用法：删除员工 姓名",
            reply_markup=get_main_menu(),
        )
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("员工姓名不能为空。", reply_markup=get_main_menu())
        return

    if delete_employee(name):
        await update.message.reply_text(
            f"✅ 已删除员工：{name}",
            reply_markup=get_main_menu(),
        )
    else:
        await update.message.reply_text(
            f"员工 {name} 不存在，或删除失败。",
            reply_markup=get_main_menu(),
        )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "menu|home":
        USER_SESSIONS.pop(user_id, None)
        await query.message.reply_text("🏠 已返回主菜单", reply_markup=get_main_menu())
        return

    if data == "borrow|start":
        USER_SESSIONS[user_id] = {"action": "borrow"}
        employees = get_employees()
        if not employees:
            await query.message.reply_text(
                "当前还没有员工，请先新增员工。",
                reply_markup=get_main_menu(),
            )
            return
        await query.message.reply_text(
            "请选择借款员工：",
            reply_markup=get_employee_inline_menu("borrow"),
        )
        return

    if data == "repay|start":
        USER_SESSIONS[user_id] = {"action": "repay"}
        employees = get_employees()
        if not employees:
            await query.message.reply_text(
                "当前还没有员工，请先新增员工。",
                reply_markup=get_main_menu(),
            )
            return
        await query.message.reply_text(
            "请选择还款员工：",
            reply_markup=get_employee_inline_menu("repay"),
        )
        return

    if data == "query|start":
        employees = get_employees()
        if not employees:
            await query.message.reply_text(
                "当前还没有员工，请先新增员工。",
                reply_markup=get_main_menu(),
            )
            return
        await query.message.reply_text(
            "请选择查询员工：",
            reply_markup=get_query_employee_inline_menu(),
        )
        return

    if data == "delete_employee|start":
        employees = get_employees()
        if not employees:
            await query.message.reply_text(
                "当前没有员工可删除。",
                reply_markup=get_main_menu(),
            )
            return
        await query.message.reply_text(
            "请选择要删除的员工：",
            reply_markup=get_delete_employee_inline_menu(),
        )
        return

    if data == "borrow|back_employees":
        await query.message.reply_text(
            "请选择借款员工：",
            reply_markup=get_employee_inline_menu("borrow"),
        )
        return

    if data == "repay|back_employees":
        await query.message.reply_text(
            "请选择还款员工：",
            reply_markup=get_employee_inline_menu("repay"),
        )
        return

    if data.startswith("delete_employee|"):
        parts = data.split("|", 1)
        if len(parts) == 2:
            name = parts[1]
            if delete_employee(name):
                await query.message.reply_text(
                    f"✅ 已删除员工：{name}",
                    reply_markup=get_main_menu(),
                )
            else:
                await query.message.reply_text(
                    f"删除失败，员工 {name} 可能不存在。",
                    reply_markup=get_main_menu(),
                )
        return

    parts = data.split("|")

    if len(parts) == 3 and parts[0] == "query" and parts[1] == "employee":
        employee_name = parts[2]
        context.args = [employee_name]
        update.message = query.message
        await status_cmd(update, context)
        return

    if len(parts) == 3 and parts[1] == "employee":
        action = parts[0]
        employee_name = parts[2]

        USER_SESSIONS[user_id] = {
            "action": action,
            "employee_name": employee_name,
        }

        await query.message.reply_text(
            f"已选择员工：{employee_name}\n请选择币种：",
            reply_markup=get_currency_inline_menu(action, employee_name),
        )
        return

    if len(parts) == 4 and parts[1] == "currency":
        action = parts[0]
        employee_name = parts[2]
        currency = parts[3]

        USER_SESSIONS[user_id] = {
            "action": action,
            "employee_name": employee_name,
            "currency": currency,
            "waiting_amount": True,
        }

        unit_name = "USDT" if currency == "USDT" else "泰铢"
        await query.message.reply_text(
            f"已选择：{employee_name} / {unit_name}\n\n请输入金额数字即可，例如：1000",
            reply_markup=get_main_menu(),
        )
        return


async def chinese_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    user_id = update.effective_user.id

    if user_id in USER_SESSIONS:
        session = USER_SESSIONS[user_id]

        if session.get("waiting_amount"):
            text_amount = text.strip()
            currency = session["currency"]

            if currency == "USDT":
                amount_text = f"{text_amount}USDT"
            else:
                amount_text = f"{text_amount}THB"

            context.args = [session["employee_name"], amount_text]

            if session["action"] == "borrow":
                await borrow(update, context)
            elif session["action"] == "repay":
                await repay(update, context)

            USER_SESSIONS.pop(user_id, None)
            return

        if session.get("waiting_new_employee_name"):
            name = text.strip()
            if add_employee(name):
                await update.message.reply_text(
                    f"✅ 已新增员工：{name}",
                    reply_markup=get_main_menu(),
                )
            else:
                await update.message.reply_text(
                    f"员工 {name} 已存在，或新增失败。",
                    reply_markup=get_main_menu(),
                )
            USER_SESSIONS.pop(user_id, None)
            return

    if text in ["🏠 主菜单", "菜单", "首页"]:
        await start(update, context)
        return

    if text in ["ℹ️ 帮助", "帮助"]:
        await help_cmd(update, context)
        return

    if text in ["👤 我的ID", "我的ID", "ID", "id"]:
        await myid(update, context)
        return

    if text in ["📊 报表", "报表", "汇总", "总账"]:
        await report(update, context)
        return

    if text in ["💱 当前汇率", "汇率"]:
        await fx_cmd(update, context)
        return

    if text == "🧾 借款示例":
        await update.message.reply_text(
            "借款示例：\n借款 张三 1000U\n借款 张三 36500泰铢\n借款 张三=36500THB",
            reply_markup=get_main_menu(),
        )
        return

    if text == "🧾 还款示例":
        await update.message.reply_text(
            "还款示例：\n还款 张三 1000U\n还款 张三 36500THB",
            reply_markup=get_main_menu(),
        )
        return

    if text == "📥 借款":
        employees = get_employees()
        if not employees:
            await update.message.reply_text(
                "当前还没有员工，请先新增员工。",
                reply_markup=get_main_menu(),
            )
            return
        await update.message.reply_text(
            "请选择借款员工：",
            reply_markup=get_employee_inline_menu("borrow"),
        )
        return

    if text == "📤 还款":
        employees = get_employees()
        if not employees:
            await update.message.reply_text(
                "当前还没有员工，请先新增员工。",
                reply_markup=get_main_menu(),
            )
            return
        await update.message.reply_text(
            "请选择还款员工：",
            reply_markup=get_employee_inline_menu("repay"),
        )
        return

    if text == "🔎 查询":
        employees = get_employees()
        if not employees:
            await update.message.reply_text(
                "当前还没有员工，请先新增员工。",
                reply_markup=get_main_menu(),
            )
            return
        await update.message.reply_text(
            "请选择查询员工：",
            reply_markup=get_query_employee_inline_menu(),
        )
        return

    if text == "➕ 新增员工":
        USER_SESSIONS[user_id] = {"waiting_new_employee_name": True}
        await update.message.reply_text(
            "请输入要新增的员工姓名：",
            reply_markup=get_main_menu(),
        )
        return

    if text == "❌ 删除员工":
        employees = get_employees()
        if not employees:
            await update.message.reply_text(
                "当前没有员工可删除。",
                reply_markup=get_main_menu(),
            )
            return
        await update.message.reply_text(
            "请选择要删除的员工：",
            reply_markup=get_delete_employee_inline_menu(),
        )
        return

    normalized = re.sub(r"[=，,]", " ", text)
    parts = normalized.split()

    if not parts:
        return

    action = parts[0]

    if action == "借款":
        if len(parts) < 3:
            await update.message.reply_text(
                "用法：借款 姓名 金额单位\n示例：借款 张三 1000U",
                reply_markup=get_main_menu(),
            )
            return
        context.args = [parts[1], parts[2]]
        await borrow(update, context)

    elif action == "还款":
        if len(parts) < 3:
            await update.message.reply_text(
                "用法：还款 姓名 金额单位\n示例：还款 张三 36500THB",
                reply_markup=get_main_menu(),
            )
            return
        context.args = [parts[1], parts[2]]
        await repay(update, context)

    elif action == "查询":
        if len(parts) < 2:
            await update.message.reply_text(
                "用法：查询 姓名",
                reply_markup=get_main_menu(),
            )
            return
        context.args = [parts[1]]
        await status_cmd(update, context)

    elif action == "新增员工":
        if len(parts) < 2:
            await update.message.reply_text(
                "用法：新增员工 姓名",
                reply_markup=get_main_menu(),
            )
            return
        context.args = parts[1:]
        await add_employee_cmd(update, context)

    elif action == "删除员工":
        if len(parts) < 2:
            await update.message.reply_text(
                "用法：删除员工 姓名",
                reply_markup=get_main_menu(),
            )
            return
        context.args = parts[1:]
        await delete_employee_cmd(update, context)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少 BOT_TOKEN 环境变量")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("borrow", borrow))
    app.add_handler(CommandHandler("repay", repay))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("report", report))

    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chinese_text_handler))

    logging.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
