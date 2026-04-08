import os
import sqlite3
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
DB_PATH = os.getenv("DB_PATH", "loan_bot.db")

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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('borrow', 'repay')),
        amount REAL NOT NULL,
        operator_id INTEGER NOT NULL,
        operator_name TEXT,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def parse_amount(text: str):
    try:
        value = float(text)
        if value <= 0:
            return None
        return value
    except Exception:
        return None


def get_balance(employee_name: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type='repay' THEN amount ELSE 0 END), 0)
        FROM transactions
        WHERE employee_name = ?
    """, (employee_name,))
    row = cur.fetchone()
    conn.close()

    borrowed = row[0] or 0
    repaid = row[1] or 0
    balance = borrowed - repaid
    return borrowed, repaid, balance


def get_report():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            employee_name,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount ELSE 0 END), 0) AS borrowed,
            COALESCE(SUM(CASE WHEN type='repay' THEN amount ELSE 0 END), 0) AS repaid,
            COALESCE(SUM(CASE WHEN type='borrow' THEN amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='repay' THEN amount ELSE 0 END), 0) AS balance
        FROM transactions
        GROUP BY employee_name
        ORDER BY balance DESC, employee_name ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


async def require_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text("你没有权限使用这个命令。")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "借资记账机器人已上线。\n\n"
        "可用命令：\n"
        "/borrow 姓名 金额 - 记录借资\n"
        "/repay 姓名 金额 - 记录还款\n"
        "/status 姓名 - 查询个人账目\n"
        "/report - 查看全部汇总\n"
        "/myid - 查看你的 Telegram 用户ID"
    )
    await update.message.reply_text(msg)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"你的 Telegram 用户ID 是：{user.id}")


async def borrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text("用法：/borrow 姓名 金额")
        return

    employee_name = context.args[0].strip()
    amount = parse_amount(context.args[1])

    if not employee_name:
        await update.message.reply_text("姓名不能为空。")
        return

    if amount is None:
        await update.message.reply_text("金额格式不正确，请输入大于 0 的数字。")
        return

    user = update.effective_user
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO transactions (employee_name, type, amount, operator_id, operator_name, created_at)
        VALUES (?, 'borrow', ?, ?, ?, ?)
    """, (
        employee_name,
        amount,
        user.id,
        user.full_name,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()

    borrowed, repaid, balance = get_balance(employee_name)
    await update.message.reply_text(
        f"已记录借资。\n"
        f"员工：{employee_name}\n"
        f"本次借资：{amount:.2f}\n"
        f"累计借资：{borrowed:.2f}\n"
        f"累计还款：{repaid:.2f}\n"
        f"当前未还：{balance:.2f}"
    )


async def repay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text("用法：/repay 姓名 金额")
        return

    employee_name = context.args[0].strip()
    amount = parse_amount(context.args[1])

    if not employee_name:
        await update.message.reply_text("姓名不能为空。")
        return

    if amount is None:
        await update.message.reply_text("金额格式不正确，请输入大于 0 的数字。")
        return

    borrowed, repaid, balance = get_balance(employee_name)
    if borrowed == 0 and repaid == 0:
        await update.message.reply_text(f"未找到员工 {employee_name} 的借资记录。")
        return

    user = update.effective_user
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO transactions (employee_name, type, amount, operator_id, operator_name, created_at)
        VALUES (?, 'repay', ?, ?, ?, ?)
    """, (
        employee_name,
        amount,
        user.id,
        user.full_name,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()

    borrowed, repaid, balance = get_balance(employee_name)
    await update.message.reply_text(
        f"已记录还款。\n"
        f"员工：{employee_name}\n"
        f"本次还款：{amount:.2f}\n"
        f"累计借资：{borrowed:.2f}\n"
        f"累计还款：{repaid:.2f}\n"
        f"当前未还：{balance:.2f}"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("用法：/status 姓名")
        return

    employee_name = context.args[0].strip()
    borrowed, repaid, balance = get_balance(employee_name)

    if borrowed == 0 and repaid == 0:
        await update.message.reply_text(f"未找到员工 {employee_name} 的记录。")
        return

    await update.message.reply_text(
        f"员工：{employee_name}\n"
        f"累计借资：{borrowed:.2f}\n"
        f"累计还款：{repaid:.2f}\n"
        f"当前未还：{balance:.2f}"
    )


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    rows = get_report()
    if not rows:
        await update.message.reply_text("当前没有任何账目记录。")
        return

    total_borrowed = sum(row[1] for row in rows)
    total_repaid = sum(row[2] for row in rows)
    total_balance = sum(row[3] for row in rows)

    lines = ["借资汇总：", ""]

    for name, borrowed, repaid, balance in rows:
        lines.append(
            f"{name}：借资 {borrowed:.2f} / 还款 {repaid:.2f} / 未还 {balance:.2f}"
        )

    lines.extend([
        "",
        f"总借资：{total_borrowed:.2f}",
        f"总还款：{total_repaid:.2f}",
        f"总未还：{total_balance:.2f}"
    ])

    await update.message.reply_text("\n".join(lines))


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少 BOT_TOKEN 环境变量")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("borrow", borrow))
    app.add_handler(CommandHandler("repay", repay))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("report", report))

    logging.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()