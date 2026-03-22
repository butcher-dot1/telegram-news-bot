import os
print("CURRENT WORKING DIRECTORY:", os.getcwd())
import sqlite3
from datetime import datetime, timedelta, time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
# ===== DATABASE SETUP =====
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    plan TEXT,
    start_date TEXT,
    expiry_date TEXT
)
""")
conn.commit()

# ===== CONFIG =====
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("TOKEN")
ADMIN_ID = 1230675110

pending_approvals = {}

PDF_FOLDER = "newspapers"

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! 👋\n\n"
        "Available Plans:\n"
        "1. The Hindu - ₹69/month (/buy_hindu)\n"
        "2. TOI - ₹65/month (/buy_toi)"
    )

# ===== BUY =====
async def buy_hindu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending_approvals[user_id] = "hindu"

    await update.message.reply_text(
        "You selected The Hindu (₹69)\nScan QR and send /paid after payment."
    )

    try:
        with open("qr.png", "rb") as qr:
            await context.bot.send_photo(chat_id=user_id, photo=qr)
    except:
        await update.message.reply_text("QR not found.")

async def buy_toi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending_approvals[user_id] = "toi"

    await update.message.reply_text(
        "You selected TOI (₹65)\nScan QR and send /paid after payment."
    )

    try:
        with open("qr.png", "rb") as qr:
            await context.bot.send_photo(chat_id=user_id, photo=qr)
    except:
        await update.message.reply_text("QR not found.")

# ===== PAID =====
async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in pending_approvals:
        await update.message.reply_text("Please select a plan first using /buy_hindu or /buy_toi")
        return

    plan = pending_approvals[user.id]

    await update.message.reply_text("Payment request sent to admin...")

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"User ID: {user.id}\nPlan: {plan}\n\nApprove:\n/approve {user.id}"
    )

    plan = pending_approvals[user.id]

    # ✅ Step 3: Notify user
    await update.message.reply_text("Payment request sent to admin...")

    # ✅ Step 4: Notify admin
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"User ID: {user.id}\nPlan: {plan}\n\nApprove:\n/approve {user.id}"
    )
# ===== APPROVE =====
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if len(context.args) < 1:
        await update.message.reply_text("Usage: /approve <user_id>")
        return

    user_id = int(context.args[0])

    if user_id not in pending_approvals:
        await update.message.reply_text("No pending request.")
        return

    plan = pending_approvals[user_id]

    start_date = datetime.now()
    expiry_date = start_date + timedelta(days=30)

    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, plan, start_date, expiry_date)
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        plan,
        start_date.strftime("%Y-%m-%d"),
        expiry_date.strftime("%Y-%m-%d")
    ))
    conn.commit()

    del pending_approvals[user_id]

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ {plan.upper()} activated!\nExpires: {expiry_date.strftime('%Y-%m-%d')}"
    )

    await update.message.reply_text("Approved ✅")

# ===== SEND NEWS (manual) =====
async def send_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    cursor.execute("SELECT plan, expiry_date FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Buy a plan first.")
        return

    plan, expiry_date = row

    if datetime.now().date() > datetime.strptime(expiry_date, "%Y-%m-%d").date():
        await update.message.reply_text("Subscription expired.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    prefix = f"{plan}_{today}"

    files_sent = False

    for file in os.listdir(PDF_FOLDER):
        if file.startswith(prefix) and file.endswith(".pdf"):
            with open(os.path.join(PDF_FOLDER, file), "rb") as f:
                await context.bot.send_document(chat_id=user_id, document=f)
                files_sent = True

    if not files_sent:
        await update.message.reply_text("Today's newspaper not uploaded yet.")

# ===== AUTO SEND =====
async def send_pdfs(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    print("📂 CURRENT DIR:", os.getcwd())
    print("📁 FILES IN FOLDER:", os.listdir(PDF_FOLDER))
    cursor.execute("SELECT user_id, plan, expiry_date FROM users")
    users = cursor.fetchall()

    for user_id, plan, expiry_date in users:
        prefix = f"{plan}_{today}"

        print("👤 USER:", user_id)
        print("🧠 PLAN:", plan)
        print("🎯 EXPECTED PREFIX:", prefix)

        for file in os.listdir(PDF_FOLDER):
            print("Checking file:", file)
            print("Expected prefix:", prefix)
            if file.startswith(prefix) and file.endswith(".pdf"):
                print("MATCH FOUND!") 
                try:
                    with open(os.path.join(PDF_FOLDER, file), "rb") as f:
                        await context.bot.send_document(chat_id=user_id, document=f)
                except Exception as e:
                    print(f"Error: {e}")

# ===== CLEANUP =====
async def cleanup_users(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("DELETE FROM users WHERE expiry_date < ?", (today,))
    conn.commit()

# ===== MY PLAN =====
async def myplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    cursor.execute("SELECT plan, expiry_date FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("No active plan.")
        return

    plan, expiry = row
    await update.message.reply_text(f"Plan: {plan}\nExpires: {expiry}")


  # 1. Define your proxy address (replace with your actual proxy)
PROXY_URL = "http://127.0.0.1:8080" 

# 2. Rebuild the application with proxy routing
app = (
    ApplicationBuilder()
    .token(TOKEN)
    .proxy(PROXY_URL)
    .get_updates_proxy(PROXY_URL)
    .build()
)

async def check_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT * FROM users")
    data = cursor.fetchall()

    if not data:
        await update.message.reply_text("Database is EMPTY ❌")
    else:
        await update.message.reply_text(f"Users in DB:\n{data}")
# ===== MAIN =====
def main():
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy_hindu", buy_hindu))
    app.add_handler(CommandHandler("buy_toi", buy_toi))
    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("news", send_news))
    app.add_handler(CommandHandler("myplan", myplan))
    app.add_handler(CommandHandler("checkdb", check_db))

    # ⏰ AUTO SEND DAILY (11 AM)
    app.job_queue.run_daily(send_pdfs, time=time(hour=12, minute=40))

    # 🧹 CLEANUP DAILY (2 AM)
    app.job_queue.run_daily(cleanup_users, time=time(hour=2, minute=0))

    print("Bot running 🚀")
    app.run_polling()
    
if __name__ == "__main__":
    main()