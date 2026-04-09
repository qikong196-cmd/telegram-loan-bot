import os
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


def get_employee_display_name_by_id(user_id: int) -> str:
    row = get_employee_by_telegram_id(user_id)
    return row[2] if row else str(user_id)


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
        (rate_date, thb_per_usdt, source, set_by, now_local().strftime("%Y-%m-%d %H:%M:%S")),
    )
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


def get_group_entry_menu():
    return ReplyKeyboardMarkup(
        [["📝 员工登记", "🛠 管理中心"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


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
    return ReplyKeyboardMarkup(
        [["📊 今日记录", "📜 最近流水"], ["📚 历史记录", "💰 我的总账"], ["🏠 返回首页"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_open_bot_private_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🤖 打开机器人", url=get_private_link())]]
    )


def get_open_admin_private_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛠 打开管理后台", url=get_private_link())]]
    )


def get_admin_center_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 员工审批", callback_data="admin|approvals"),
             InlineKeyboardButton("➕ 新增借款", callback_data="admin|borrow")],
            [InlineKeyboardButton("💸 记录还款", callback_data="admin|repay"),
             InlineKeyboardButton("📊 全部报表", callback_data="admin|report")],
            [InlineKeyboardButton("📜 员工流水", callback_data="admin|employee_flow"),
             InlineKeyboardButton("🗂 全部流水", callback_data="admin|all_flows")],
            [InlineKeyboardButton("📆 今日全部流水", callback_data="admin|today_all_flows"),
             InlineKeyboardButton("❌ 删除员工", callback_data="admin|delete_employee")],
            [InlineKeyboardButton("💱 修改今日汇率", callback_data="admin|set_rate"),
             InlineKeyboardButton("💱 查看今日汇率", callback_data="admin|show_rate")],
            [InlineKeyboardButton("🏠 返回首页", callback_data="menu|home")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = get_role(update.effective_user.id)
    rate, _ = get_daily_rate(today_key())

    if is_group_chat(update):
        await update.message.reply_text("📌 群入口：请点击按钮后前往私聊操作", reply_markup=get_group_entry_menu())
        return

    msg = (
        "🤖 欢迎使用借资系统\n\n"
        "员工：查看自己的账本和资料\n"
        "管理员：进入管理中心操作\n\n"
        f"今日汇率：1 USDT ≈ ฿{rate:.2f}"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu_for_role(role))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = get_role(update.effective_user.id)

    if is_group_chat(update):
        await update.message.reply_text("请私聊机器人继续操作。", reply_markup=get_open_bot_private_menu())
        return

    msg = (
        "ℹ️ 使用说明\n\n"
        "员工首页：\n"
        "📒 我的账本\n"
        "👤 我的资料\n"
        "📨 审核状态\n\n"
        "管理员首页：\n"
        "🛠 管理中心"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu_for_role(role))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = get_role(update.effective_user.id)

    if is_group_chat(update):
        await update.message.reply_text("请私聊机器人查看个人信息。", reply_markup=get_open_bot_private_menu())
        return

    await update.message.reply_text(f"👤 你的 Telegram 用户ID 是：{update.effective_user.id}", reply_markup=get_main_menu_for_role(role))


async def chinese_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    role = get_role(user_id)
    text = update.message.text.strip()

    if is_group_chat(update):
        if text == "📝 员工登记":
            await update.message.reply_text("请私聊我完成员工登记。", reply_markup=get_open_bot_private_menu())
            return

        if text == "🛠 管理中心":
            if not is_manager(user_id):
                return
            await update.message.reply_text("请到私聊中打开管理中心。", reply_markup=get_open_admin_private_menu())
            return

        return

    if text in ["🏠 返回首页", "菜单", "首页"]:
        await start(update, context)
        return

    if text in ["ℹ️ 帮助", "帮助"]:
        await help_cmd(update, context)
        return

    await update.message.reply_text("当前版本请使用按钮操作。", reply_markup=get_main_menu_for_role(role))


async def post_init(application: Application):
    rate = get_live_thb_per_usdt()
    upsert_daily_rate(today_key(), rate, "auto", None)

    application.job_queue.run_daily(
        lambda c: daily_fx_job(c),
        time=dtime(hour=9, minute=0, tzinfo=APP_TZ),
        name="daily_fx_job",
    )
    application.job_queue.run_daily(
        lambda c: daily_report_job(c),
        time=dtime(hour=21, minute=0, tzinfo=APP_TZ),
        name="daily_report_job",
    )
    logging.info("定时任务已启动。")


async def daily_fx_job(context: ContextTypes.DEFAULT_TYPE):
    rate = get_live_thb_per_usdt()
    upsert_daily_rate(today_key(), rate, "auto", None)
    logging.info("每日自动汇率更新完成：%s", rate)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text="📊 每日汇总已生成。请在私聊后台查看。") 
        except Exception as e:
            logging.warning("发送日报给管理员 %s 失败：%s", admin_id, e)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少 BOT_TOKEN 环境变量")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chinese_text_handler))

    logging.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
