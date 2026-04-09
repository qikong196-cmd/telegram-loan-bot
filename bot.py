import os
import re
import sqlite3
import logging
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
DB_PATH = os.getenv("DB_PATH", "loan_bot.db")

# 汇率：1 USDT = 多少 THB
THB_PER_USDT = float(os.getenv("THB_PER_USDT", "36.50"))

ADMIN_IDS = set()
for item in ADMIN_IDS_RAW.split(","):
    item = item.strip()
    if item.isdigit():
        ADMIN_IDS.add(int(item))


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

    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_main_menu():
    keyboard = [
        ["📥 借款", "📤 还款", "🔎 查询"],
        ["📊 报表", "👤 我的ID", "ℹ️ 帮助"],
        ["🧾 借款示例", "🧾 还款示例", "🏠 主菜单"],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def format_usdt(amount: float) -> str:
    return f"{amount:.2f} USDT"


def format_thb_from_usdt(amount_usdt: float) -> str:
    thb = amount_usdt * THB_PER_USDT
    return f"฿{thb:,.2f}"


def format_dual(amount_usdt: float) -> str:
    return f"{amount_usdt:.2f} USDT（≈ {format_thb_from_usdt(amount_usdt)}）"


def parse_amount(text: str):
    """
    支持：
    1000
    1000U
    1000USDT
    36500THB
    36500泰铢
    """
    raw = text.strip().upper().replace(" ", "")

    # 中文单位兼容
    raw = raw.replace("泰铢", "THB")
    raw = raw.replace("銖", "THB")
    raw = raw.replace("铢", "THB")

    # 兼容 U
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
        # 没写单位，默认按 USDT
        number_part = raw

    try:
        value = float(number_part)
        if value <= 0:
            return None
    except Exception:
        return None

    if unit == "THB":
        usdt_value = value / THB_PER_USDT
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
    msg = (
        "🤖 借资记账机器人已上线\n\n"
        "支持中文输入：\n"
        "借款 姓名 金额单位\n"
        "还款 姓名 金额单位\n"
        "查询 姓名\n"
        "报表\n"
        "我的ID\n"
        "帮助\n\n"
        "金额支持：\n"
        "1000U / 1000USDT / 36500泰铢 / 36500THB\n\n"
        "系统统一按 USDT 入账并显示\n"
        f"当前参考汇率：1 USDT ≈ ฿{THB_PER_USDT:.2f}\n\n"
        "示例：\n"
        "借款 张三 1000U\n"
        "借款 张三 36500泰铢\n"
        "还款 张三 10000THB"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "ℹ️ 使用说明\n\n"
        "中文命令：\n"
        "借款 姓名 金额单位\n"
        "还款 姓名 金额单位\n"
        "查询 姓名\n"
        "报表\n"
        "我的ID\n\n"
        "金额支持：\n"
        "1000U / 1000USDT / 36500泰铢 / 36500THB\n\n"
        "英文命令也支持：\n"
        "/borrow 姓名 金额单位\n"
        "/repay 姓名 金额单位\n"
        "/status 姓名\n"
        "/report\n"
        "/myid"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu())


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
    parsed = parse_amount(context.args[1])

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
        f"💰 本次借资：{format_dual(parsed['usdt_value'])}\n"
        f"📊 累计借资：{format_dual(borrowed)}\n"
        f"📉 累计还款：{format_dual(repaid)}\n"
        f"🧾 当前未还：{format_dual(balance)}",
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
    parsed = parse_amount(context.args[1])

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
        f"💸 本次还款：{format_dual(parsed['usdt_value'])}\n"
        f"📊 累计借资：{format_dual(borrowed)}\n"
        f"📉 累计还款：{format_dual(repaid)}\n"
        f"🧾 当前未还：{format_dual(balance)}",
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

    if borrowed == 0 and repaid == 0:
        await update.message.reply_text(
            f"未找到员工 {employee_name} 的记录。",
            reply_markup=get_main_menu(),
        )
        return

    await update.message.reply_text(
        f"🔎 员工：{employee_name}\n\n"
        f"📊 累计借资：{format_dual(borrowed)}\n"
        f"📉 累计还款：{format_dual(repaid)}\n"
        f"🧾 当前未还：{format_dual(balance)}",
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

    total_borrowed = sum(row[1] for row in rows)
    total_repaid = sum(row[2] for row in rows)
    total_balance = sum(row[3] for row in rows)

    lines = ["📊 借资汇总：", ""]

    for name, borrowed, repaid, balance in rows:
        lines.append(
            f"{name}：借资 {format_dual(borrowed)} / 还款 {format_dual(repaid)} / 未还 {format_dual(balance)}"
        )

    lines.extend(
        [
            "",
            f"总借资：{format_dual(total_borrowed)}",
            f"总还款：{format_dual(total_repaid)}",
            f"总未还：{format_dual(total_balance)}",
        ]
    )

    await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu())


async def chinese_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
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

    if text in ["📊 报表", "报表", "汇总", "总账"]:
        await report(update, context)
        return

    if text == "🧾 借款示例":
        await update.message.reply_text(
            "借款示例：\n借款 张三 1000U\n借款 张三 36500泰铢",
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
        await update.message.reply_text(
            "请输入：借款 姓名 金额单位\n例如：借款 张三 1000U\n或：借款 张三 36500泰铢",
            reply_markup=get_main_menu(),
        )
        return

    if text == "📤 还款":
        await update.message.reply_text(
            "请输入：还款 姓名 金额单位\n例如：还款 张三 1000THB",
            reply_markup=get_main_menu(),
        )
        return

    if text == "🔎 查询":
        await update.message.reply_text(
            "请输入：查询 姓名\n例如：查询 张三",
            reply_markup=get_main_menu(),
        )
        return

    # 支持：
    # 借款 张三 1000U
    # 借款 张三=1000U
    # 借款 张三，1000THB
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chinese_text_handler))

    logging.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
