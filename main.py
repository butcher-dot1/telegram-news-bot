"""
Telegram Subscription Bot — Main Entry Point
=============================================
Admin uploads daily PDFs, approves users.
Users purchase plans, receive papers on schedule.
JobQueue handles daily broadcast and cleanup.
"""

import os
import logging
from pathlib import Path
from datetime import time, timezone, timedelta, datetime
import threading

from dotenv import load_dotenv
from telegram import Update, Document
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from fastapi import FastAPI
import uvicorn

import db

# ─── Configuration ──────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
ASSETS_DIR: Path = Path(__file__).parent / "assets"

# IST = UTC+5:30
IST_BROADCAST_TIME = time(hour=7, minute=10)   # 12:40 PM IST = 07:10 UTC
IST_CLEANUP_TIME = time(hour=20, minute=30)     # 2:00 AM IST  = 20:30 UTC (prev day)

# Plan configs
PLANS = {
    "hindu": {"name": "The Hindu", "price": "₹69/month"},
    "toi": {"name": "Times of India", "price": "₹65/month"},
}

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ConversationHandler states
AWAITING_PLAN_NAME = 0


# ─── Admin Handlers ─────────────────────────────────────────────────

def admin_only(func):
    """Decorator: restrict handler to ADMIN_ID only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return  # silently ignore non-admin
        return await func(update, context)
    return wrapper


async def handle_pdf_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sends a PDF — store file_id and ask which plan it belongs to."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    document: Document = update.message.document
    if document.mime_type != "application/pdf":
        await update.message.reply_text("⚠️ Please send a PDF file.")
        return ConversationHandler.END

    context.user_data["pending_file_id"] = document.file_id
    plan_list = "\n".join(f"  • `{k}` — {v['name']}" for k, v in PLANS.items())
    await update.message.reply_text(
        f"📄 PDF received!\n\nWhich plan is this for?\n{plan_list}\n\n"
        f"Reply with the plan code (e.g. `hindu` or `toi`):",
        parse_mode="Markdown",
    )
    return AWAITING_PLAN_NAME


async def handle_plan_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin replies with the plan name after sending a PDF."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    plan_name = update.message.text.strip().lower()
    file_id = context.user_data.get("pending_file_id")

    if plan_name not in PLANS:
        await update.message.reply_text(
            f"❌ Unknown plan `{plan_name}`. Valid plans: {', '.join(PLANS.keys())}"
        )
        return AWAITING_PLAN_NAME

    if not file_id:
        await update.message.reply_text("❌ No pending PDF. Please send the PDF first.")
        return ConversationHandler.END

    await db.add_paper(plan_name, file_id)
    context.user_data.pop("pending_file_id", None)
    
    # Check if we missed the 12:40 PM broadcast
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).time()
    
    if now_ist >= IST_BROADCAST_TIME:
        await update.message.reply_text("⏳ It's past 12:40 PM! Broadcasting immediately to all subscribers...")
        count = await broadcast_paper(context.bot, plan_name, file_id)
        await update.message.reply_text(
            f"✅ Paper saved and sent to {count} users for *{PLANS[plan_name]['name']}*!",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"✅ Paper saved for *{PLANS[plan_name]['name']}* plan!\nIt will be broadcasted automatically at 12:40 PM.",
            parse_mode="Markdown",
        )
        
    return ConversationHandler.END


async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the PDF upload conversation."""
    context.user_data.pop("pending_file_id", None)
    await update.message.reply_text("❌ Upload cancelled.")
    return ConversationHandler.END


@admin_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve <user_id> <plan> — Activate a subscription for 30 days."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/approve <user_id> <plan>`\n"
            f"Plans: {', '.join(PLANS.keys())}",
            parse_mode="Markdown",
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    plan = context.args[1].lower()
    if plan not in PLANS:
        await update.message.reply_text(
            f"❌ Unknown plan `{plan}`. Valid: {', '.join(PLANS.keys())}"
        )
        return

    await db.add_user(target_user_id, plan)
    await update.message.reply_text(
        f"✅ User `{target_user_id}` approved for *{PLANS[plan]['name']}* (30 days).",
        parse_mode="Markdown",
    )

    # Notify the user about their activation
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 Your *{PLANS[plan]['name']}* subscription is now active for 30 days!",
            parse_mode="Markdown",
        )
    except Exception:
        logger.warning(f"Could not notify user {target_user_id} about approval.")


@admin_only
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/debug — Admin tool to print the cloud server's file structure."""
    import os
    
    base_dir = ASSETS_DIR.parent
    root_files = "\n".join(os.listdir(base_dir))
    
    if ASSETS_DIR.exists():
        assets_files = "\n".join(os.listdir(ASSETS_DIR))
    else:
        assets_files = "⚠️ FOLDER DOES NOT EXIST"
        
    msg = (
        f"📁 *Root Directory:*\n`{root_files}`\n\n"
        f"🖼️ *Assets Directory:*\n`{assets_files}`"
    )
    
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── User Handlers ──────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Bare minimum diagnostic test."""
    await update.message.reply_text("welcome")


async def buy_hindu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/buy_hindu — Show The Hindu plan details and QR code."""
    qr_path = ASSETS_DIR / "qr.png"
    text = (
        "📰 The Hindu — ₹69/month\n\n"
        "Scan the QR code below to pay via UPI.\n"
        "After payment, click /paid_hindu to notify us!"
    )
    if qr_path.exists():
        try:
            await update.message.reply_photo(photo=qr_path, caption=text)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Image error: {e}")
    else:
        await update.message.reply_text("⚠️ QR code not found in folder.")


async def buy_toi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/buy_toi — Show TOI plan details and QR code."""
    qr_path = ASSETS_DIR / "qr.png"
    text = (
        "📰 Times of India — ₹65/month\n\n"
        "Scan the QR code below to pay via UPI.\n"
        "After payment, click /paid_toi to notify us!"
    )
    if qr_path.exists():
        try:
            await update.message.reply_photo(photo=qr_path, caption=text)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Image error: {e}")
    else:
        await update.message.reply_text("⚠️ QR code not found in folder.")


async def notify_admin_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: str) -> None:
    """Helper function to send payment notifications to the admin."""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    # Notify admin
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"💰 *Payment Claim*\n\n"
            f"User: @{username} (`{user_id}`)\n"
            f"Plan: *{PLANS[plan]['name']}*\n\n"
            f"Verify your bank and run:\n"
            f"`/approve {user_id} {plan}`"
        ),
        parse_mode="Markdown",
    )

    await update.message.reply_text(
        "✅ Payment notification sent to admin!\n"
        "You'll be activated once the admin verifies the payment."
    )


async def paid_hindu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/paid_hindu — User confirms Hindu payment."""
    await notify_admin_payment(update, context, "hindu")


async def paid_toi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/paid_toi — User confirms TOI payment."""
    await notify_admin_payment(update, context, "toi")


async def myplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myplan — Check subscription status."""
    user_id = update.effective_user.id
    user_data = await db.get_user(user_id)

    if not user_data:
        await update.message.reply_text(
            "❌ You don't have an active subscription.\n"
            "Use /start to see available plans!"
        )
        return

    from datetime import date
    expiry = user_data["expiry_date"]
    days_left = (expiry - date.today()).days

    if days_left < 0:
        await update.message.reply_text(
            "⏰ Your subscription has expired.\n"
            "Use /start to renew!"
        )
        return

    plan_info = PLANS.get(user_data["plan"], {})
    plan_display = plan_info.get("name", user_data["plan"])

    await update.message.reply_text(
        f"📋 *Your Subscription*\n\n"
        f"Plan: *{plan_display}*\n"
        f"Started: {user_data['start_date']}\n"
        f"Expires: {expiry}\n"
        f"Days left: *{days_left}*",
        parse_mode="Markdown",
    )


# ─── Scheduled Jobs ─────────────────────────────────────────────────

async def broadcast_paper(bot, plan_name: str, file_id: str) -> int:
    """Helper: Send a specific paper to all active users of that plan."""
    active_users = await db.get_active_users(plan=plan_name)
    plan_display = PLANS.get(plan_name, {}).get("name", plan_name)
    
    success_count = 0
    for user in active_users:
        try:
            await bot.send_document(
                chat_id=user["user_id"],
                document=file_id,
                caption=f"📰 Today's *{plan_display}*",
                parse_mode="Markdown",
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to send to {user['user_id']}: {e}")
            
    return success_count


async def send_pdfs_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 12:40 PM IST — Send today's papers to all active subscribers."""
    logger.info("Running daily PDF broadcast job...")

    papers = await db.get_todays_papers()
    if not papers:
        logger.info("No papers uploaded for today. Skipping broadcast.")
        return

    for paper in papers:
        plan_name = paper["plan_name"]
        file_id = paper["file_id"]
        
        count = await broadcast_paper(context.bot, plan_name, file_id)
        logger.info(f"Broadcast '{plan_name}' to {count} user(s).")

    logger.info("Daily PDF broadcast complete.")


async def cleanup_users_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 2:00 AM IST — Remove expired subscriptions."""
    logger.info("Running expired users cleanup job...")
    deleted = await db.delete_expired_users()
    logger.info(f"Cleanup complete. Removed {deleted} expired user(s).")


# ─── Application Lifecycle ──────────────────────────────────────────

async def post_init(application) -> None:
    """Called after the application is initialized — connect to DB."""
    await db.init_db()
    logger.info("Bot started and database connected.")


async def post_shutdown(application) -> None:
    """Called when the application shuts down — close DB pool."""
    await db.close_db()
    logger.info("Bot stopped and database disconnected.")


# ─── Dummy Web Server for Hugging Face ──────────────────────────────

app_api = FastAPI()

@app_api.get("/")
def health_check():
    return {"status": "ok", "message": "Telegram Bot is running"}

def run_dummy_server():
    """Run FastAPI on the port provided by the cloud environment."""
    # Render uses the PORT environment variable, defaulting to 10000
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app_api, host="0.0.0.0", port=port, log_level="warning")


# ─── Main ────────────────────────────────────────────────────────────

def main() -> None:
    """Build and run the Telegram bot application."""
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in .env")
    if not ADMIN_ID:
        raise ValueError("ADMIN_ID is not set in .env")
        
    # Start the dummy FastAPI server in a background thread
    threading.Thread(target=run_dummy_server, daemon=True).start()
    logger.info("Started dummy web server on port 7860 for Hugging Face.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # ── Admin: PDF upload conversation handler ──
    pdf_upload_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Document.PDF & filters.User(ADMIN_ID),
                handle_pdf_received,
            )
        ],
        states={
            AWAITING_PLAN_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID),
                    handle_plan_name_reply,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_upload)],
    )
    app.add_handler(pdf_upload_handler)

    # ── Admin commands ──
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("debug", debug_command))

    # ── User commands ──
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("buy_hindu", buy_hindu_command))
    app.add_handler(CommandHandler("buy_toi", buy_toi_command))
    app.add_handler(CommandHandler("paid_hindu", paid_hindu_command))
    app.add_handler(CommandHandler("paid_toi", paid_toi_command))
    app.add_handler(CommandHandler("myplan", myplan_command))

    # ── Scheduled jobs ──
    job_queue = app.job_queue
    job_queue.run_daily(send_pdfs_job, time=IST_BROADCAST_TIME, name="daily_broadcast")
    job_queue.run_daily(cleanup_users_job, time=IST_CLEANUP_TIME, name="daily_cleanup")

    logger.info("Bot is starting... Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
