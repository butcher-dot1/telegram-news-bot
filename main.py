"""
Telegram Subscription Bot — Main Entry Point
=============================================

Admin uploads daily PDFs, approves users.
Users purchase plans, receive papers on schedule.
JobQueue handles daily broadcast and cleanup.
"""

import os
import logging
import threading
from pathlib import Path
from datetime import time, timezone, timedelta, datetime, date

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
from telegram.request import HTTPXRequest
from fastapi import FastAPI
import uvicorn

import db


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

# Optional Telegram proxy. Leave empty for direct connection.
# Set PROXY_URL as a Hugging Face Secret if a proxy is required.
PROXY_URL: str = os.getenv("PROXY_URL", "").strip()

ASSETS_DIR: Path = Path(__file__).parent / "assets"

# Indian Standard Time — UTC +5:30
IST = timezone(timedelta(hours=5, minutes=30))

# Daily schedules
IST_BROADCAST_TIME = time(
    hour=12,
    minute=40,
    tzinfo=IST,
)

IST_CLEANUP_TIME = time(
    hour=2,
    minute=0,
    tzinfo=IST,
)

# Plans
PLANS = {
    "hindu": {
        "name": "The Hindu",
        "price": "₹29/month",
    },
    "toi": {
        "name": "Times of India",
        "price": "₹29/month",
    },
    "ie": {
        "name": "Indian Express",
        "price": "₹29/month",
    },
}


# ─────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# ConversationHandler states
# ─────────────────────────────────────────────────────────────────────

AWAITING_PLAN_NAME = 0


# ─────────────────────────────────────────────────────────────────────
# Admin decorator
# ─────────────────────────────────────────────────────────────────────

def admin_only(func):
    """Restrict a command to the admin only."""

    async def wrapper(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not update.effective_user:
            return

        if update.effective_user.id != ADMIN_ID:
            return

        return await func(update, context)

    return wrapper


# ─────────────────────────────────────────────────────────────────────
# Admin PDF upload conversation
# ─────────────────────────────────────────────────────────────────────

async def handle_pdf_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """
    Admin sends a PDF.
    Store its Telegram file_id and ask which plan it belongs to.
    """

    if not update.effective_user:
        return ConversationHandler.END

    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    if not update.message or not update.message.document:
        return ConversationHandler.END

    document: Document = update.message.document

    if document.mime_type != "application/pdf":
        await update.message.reply_text(
            "⚠️ Please send a PDF file."
        )
        return ConversationHandler.END

    context.user_data["pending_file_id"] = document.file_id

    plan_list = "\n".join(
        f"  • `{key}` — {value['name']}"
        for key, value in PLANS.items()
    )

    await update.message.reply_text(
        "📄 PDF received!\n\n"
        "Which plan is this for?\n"
        f"{plan_list}\n\n"
        "Reply with the plan code "
        "(for example: `hindu` or `toi`).",
        parse_mode="Markdown",
    )

    return AWAITING_PLAN_NAME


async def handle_plan_name_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """
    Admin replies with the plan name after sending a PDF.
    """

    if not update.effective_user:
        return ConversationHandler.END

    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    if not update.message or not update.message.text:
        return AWAITING_PLAN_NAME

    plan_name = update.message.text.strip().lower()

    file_id = context.user_data.get("pending_file_id")

    if plan_name not in PLANS:
        await update.message.reply_text(
            f"❌ Unknown plan `{plan_name}`.\n\n"
            f"Valid plans: {', '.join(PLANS.keys())}",
            parse_mode="Markdown",
        )
        return AWAITING_PLAN_NAME

    if not file_id:
        await update.message.reply_text(
            "❌ No pending PDF found.\n"
            "Please send the PDF first."
        )
        return ConversationHandler.END

    # Save the paper in database
    await db.add_paper(plan_name, file_id)

    context.user_data.pop("pending_file_id", None)

    # Check current IST time
    now_ist = datetime.now(IST).time()

    if now_ist >= IST_BROADCAST_TIME:
        await update.message.reply_text(
            "⏳ It's already past 12:40 PM!\n"
            "Broadcasting immediately to all subscribers..."
        )

        count = await broadcast_paper(
            context.bot,
            plan_name,
            file_id,
        )

        await update.message.reply_text(
            f"✅ Paper saved and sent to {count} users "
            f"for *{PLANS[plan_name]['name']}*!",
            parse_mode="Markdown",
        )

    else:
        await update.message.reply_text(
            f"✅ Paper saved for "
            f"*{PLANS[plan_name]['name']}* plan!\n\n"
            "It will be broadcast automatically at "
            "12:40 PM.",
            parse_mode="Markdown",
        )

    return ConversationHandler.END


async def cancel_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Cancel the PDF upload conversation."""

    context.user_data.pop("pending_file_id", None)

    if update.message:
        await update.message.reply_text(
            "❌ Upload cancelled."
        )

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────
# Admin commands
# ─────────────────────────────────────────────────────────────────────

@admin_only
async def approve_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    /approve <user_id> <plan>

    Activate a subscription for 30 days.
    """

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "`/approve <user_id> <plan>`\n\n"
            f"Plans: {', '.join(PLANS.keys())}",
            parse_mode="Markdown",
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ user_id must be a number."
        )
        return

    plan = context.args[1].lower()

    if plan not in PLANS:
        await update.message.reply_text(
            f"❌ Unknown plan `{plan}`.\n"
            f"Valid plans: {', '.join(PLANS.keys())}",
            parse_mode="Markdown",
        )
        return

    await db.add_user(
        target_user_id,
        plan,
    )

    await update.message.reply_text(
        f"✅ User `{target_user_id}` approved for "
        f"*{PLANS[plan]['name']}* (30 days).",
        parse_mode="Markdown",
    )

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                f"🎉 Your *{PLANS[plan]['name']}* "
                "subscription is now active for 30 days!"
            ),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.warning(
            f"Could not notify user "
            f"{target_user_id}: {e}"
        )


@admin_only
async def debug_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show files available on the Hugging Face Space."""

    base_dir = ASSETS_DIR.parent

    try:
        root_files = "\n".join(
            os.listdir(base_dir)
        )
    except Exception as e:
        root_files = f"Error: {e}"

    if ASSETS_DIR.exists():

        try:
            assets_files = "\n".join(
                os.listdir(ASSETS_DIR)
            )
        except Exception as e:
            assets_files = f"Error: {e}"

    else:
        assets_files = "⚠️ FOLDER DOES NOT EXIST"

    msg = (
        "📁 *Root Directory:*\n"
        f"`{root_files}`\n\n"
        "🖼️ *Assets Directory:*\n"
        f"`{assets_files}`"
    )

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────
# User commands
# ─────────────────────────────────────────────────────────────────────

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Welcome message."""

    user = update.effective_user

    if not user:
        return

    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        "I deliver daily newspaper PDFs "
        "straight to your Telegram.\n\n"

        "📋 *Available Plans:*\n"
        "  📰 *The Hindu* — ₹29/month → /buyhindu\n"
        "  📰 *Times of India* — ₹29/month → /buytoi\n"
        "  📰 *Indian Express* — ₹29/month → /buyie\n\n"

        "After purchasing, use:\n"
        "/paidhindu\n"
        "/paidtoi\n"
        "/paidie\n\n"

        "Check your subscription with /myplan.",
        parse_mode="Markdown",
    )


async def buy_hindu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show The Hindu plan and QR code."""

    qr_path = ASSETS_DIR / "qr.png"

    text = (
        "📰 The Hindu — ₹29/month\n\n"
        "Scan the QR code below to pay via UPI.\n"
        "After payment, click /paidhindu "
        "to notify us!"
    )

    if qr_path.exists():

        try:
            await update.message.reply_photo(
                photo=qr_path,
                caption=text,
            )

        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Image error: {e}"
            )

    else:
        await update.message.reply_text(
            "⚠️ QR code not found in assets folder."
        )


async def buy_toi_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show Times of India plan and QR code."""

    qr_path = ASSETS_DIR / "qr.png"

    text = (
        "📰 Times of India — ₹29/month\n\n"
        "Scan the QR code below to pay via UPI.\n"
        "After payment, click /paidtoi "
        "to notify us!"
    )

    if qr_path.exists():

        try:
            await update.message.reply_photo(
                photo=qr_path,
                caption=text,
            )

        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Image error: {e}"
            )

    else:
        await update.message.reply_text(
            "⚠️ QR code not found in assets folder."
        )


async def buy_ie_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show Indian Express plan and QR code."""

    qr_path = ASSETS_DIR / "qr.png"

    text = (
        "📰 Indian Express — ₹29/month\n\n"
        "Scan the QR code below to pay via UPI.\n"
        "After payment, click /paidie "
        "to notify us!"
    )

    if qr_path.exists():

        try:
            await update.message.reply_photo(
                photo=qr_path,
                caption=text,
            )

        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Image error: {e}"
            )

    else:
        await update.message.reply_text(
            "⚠️ QR code not found in assets folder."
        )


# ─────────────────────────────────────────────────────────────────────
# Payment notifications
# ─────────────────────────────────────────────────────────────────────

async def notify_admin_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    plan: str,
) -> None:
    """Notify admin that a user claims to have paid."""

    user = update.effective_user

    if not user:
        return

    user_id = user.id
    username = user.username or user.first_name

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "💰 *Payment Claim*\n\n"
            f"User: @{username} (`{user_id}`)\n"
            f"Plan: *{PLANS[plan]['name']}*\n\n"
            "Verify your bank and run:\n"
            f"`/approve {user_id} {plan}`"
        ),
        parse_mode="Markdown",
    )

    await update.message.reply_text(
        "✅ Payment notification sent to admin!\n\n"
        "You'll be activated once the admin "
        "verifies the payment."
    )


async def paid_hindu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/paidhindu — Hindu payment notification."""

    await notify_admin_payment(
        update,
        context,
        "hindu",
    )


async def paid_toi_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/paidtoi — TOI payment notification."""

    await notify_admin_payment(
        update,
        context,
        "toi",
    )


async def paid_ie_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/paidie — Indian Express payment notification."""

    await notify_admin_payment(
        update,
        context,
        "ie",
    )


# ─────────────────────────────────────────────────────────────────────
# Subscription status
# ─────────────────────────────────────────────────────────────────────

async def myplan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Check subscription status."""

    user_id = update.effective_user.id

    user_plans = await db.get_user_plans(user_id)

    if not user_plans:
        await update.message.reply_text(
            "❌ You don't have any active subscriptions.\n\n"
            "Use /start to see available plans!"
        )
        return

    response_lines = [
        "📋 *Your Subscriptions*\n"
    ]

    for plan_data in user_plans:

        expiry = plan_data["expiry_date"]

        days_left = (
            expiry - date.today()
        ).days

        plan_info = PLANS.get(
            plan_data["plan"],
            {},
        )

        plan_display = plan_info.get(
            "name",
            plan_data["plan"],
        )

        if days_left < 0:

            response_lines.append(
                f"❌ *{plan_display}*: "
                f"Expired on {expiry}"
            )

        else:

            response_lines.append(
                f"✅ *{plan_display}*: "
                f"*{days_left}* days left "
                f"(Expires: {expiry})"
            )

    await update.message.reply_text(
        "\n".join(response_lines),
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────
# Paper broadcasting
# ─────────────────────────────────────────────────────────────────────

async def broadcast_paper(
    bot,
    plan_name: str,
    file_id: str,
) -> int:
    """Send a paper to all active users of a plan."""

    active_users = await db.get_active_users(
        plan=plan_name
    )

    plan_display = PLANS.get(
        plan_name,
        {},
    ).get(
        "name",
        plan_name,
    )

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

            logger.error(
                f"Failed to send to "
                f"{user['user_id']}: {e}"
            )

    return success_count


# ─────────────────────────────────────────────────────────────────────
# Scheduled jobs
# ─────────────────────────────────────────────────────────────────────

async def send_pdfs_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Daily 12:40 PM IST.
    Send today's papers to subscribers.
    """

    logger.info(
        "Running daily PDF broadcast job..."
    )

    papers = await db.get_todays_papers()

    if not papers:

        logger.info(
            "No papers uploaded for today. "
            "Skipping broadcast."
        )

        return

    for paper in papers:

        plan_name = paper["plan_name"]
        file_id = paper["file_id"]

        count = await broadcast_paper(
            context.bot,
            plan_name,
            file_id,
        )

        logger.info(
            f"Broadcast '{plan_name}' "
            f"to {count} user(s)."
        )

    logger.info(
        "Daily PDF broadcast complete."
    )


async def cleanup_users_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Daily 2:00 AM IST.
    Remove expired subscriptions.
    """

    logger.info(
        "Running expired users cleanup job..."
    )

    deleted = await db.delete_expired_users()

    logger.info(
        f"Cleanup complete. "
        f"Removed {deleted} expired user(s)."
    )


# ─────────────────────────────────────────────────────────────────────
# Application lifecycle
# ─────────────────────────────────────────────────────────────────────

async def post_init(application) -> None:
    """Initialize database after Telegram application starts."""

    await db.init_db()

    logger.info(
        "Bot started and database connected."
    )


async def post_shutdown(application) -> None:
    """Close database when Telegram application shuts down."""

    await db.close_db()

    logger.info(
        "Bot stopped and database disconnected."
    )


# ─────────────────────────────────────────────────────────────────────
# Hugging Face health server
# ─────────────────────────────────────────────────────────────────────

app_api = FastAPI()


@app_api.get("/")
def health_check():
    """Health endpoint for Hugging Face."""

    logger.info(
        "Hugging Face health check received."
    )

    return {
        "status": "ok",
        "message": "Telegram Bot is running",
    }


def run_dummy_server():
    """Run FastAPI on Hugging Face's required port."""

    uvicorn.run(
        app_api,
        host="0.0.0.0",
        port=7860,
        log_level="warning",
    )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def build_application():
    """Build Telegram application with optional proxy support."""

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in environment variables.")

    if not ADMIN_ID:
        raise ValueError("ADMIN_ID is not set in environment variables.")

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )

    if not PROXY_URL:
        logger.info(
            "🌐 PROXY_URL is not set. Using direct Telegram connection."
        )
        return builder.build()

    logger.info("🌐 PROXY_URL detected. Using configured Telegram proxy.")

    request = HTTPXRequest(
        proxy=PROXY_URL,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    get_updates_request = HTTPXRequest(
        proxy=PROXY_URL,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    return (
        builder
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )


def register_handlers(application):
    """Register all Telegram handlers and scheduled jobs."""

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
                    filters.TEXT
                    & ~filters.COMMAND
                    & filters.User(ADMIN_ID),
                    handle_plan_name_reply,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_upload)],
    )

    application.add_handler(pdf_upload_handler)

    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("debug", debug_command))

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("buyhindu", buy_hindu_command))
    application.add_handler(CommandHandler("buytoi", buy_toi_command))
    application.add_handler(CommandHandler("buyie", buy_ie_command))

    application.add_handler(CommandHandler("paidhindu", paid_hindu_command))
    application.add_handler(CommandHandler("paidtoi", paid_toi_command))
    application.add_handler(CommandHandler("paidie", paid_ie_command))

    application.add_handler(CommandHandler("myplan", myplan_command))

    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. "
            "Install python-telegram-bot with the job-queue extra."
        )

    job_queue.run_daily(
        send_pdfs_job,
        time=IST_BROADCAST_TIME,
        name="daily_broadcast",
    )

    job_queue.run_daily(
        cleanup_users_job,
        time=IST_CLEANUP_TIME,
        name="daily_cleanup",
    )

    logger.info("All handlers and scheduled jobs registered.")


def main() -> None:
    """Build and run the Telegram bot."""

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in environment variables.")

    if not ADMIN_ID:
        raise ValueError("ADMIN_ID is not set in environment variables.")

    threading.Thread(
        target=run_dummy_server,
        daemon=True,
    ).start()

    logger.info("Started FastAPI health server on port 7860.")
    logger.info("Building Telegram application...")

    application = build_application()

    # IMPORTANT: register everything before polling.
    register_handlers(application)

    logger.info("🚀 Telegram bot is starting polling...")

    # This is intentionally called exactly once.
    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
