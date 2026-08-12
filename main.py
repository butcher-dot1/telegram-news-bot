"""
Telegram Newspaper Subscription Bot
===================================

Customer-facing model:
    ONE subscription — All Newspapers — ₹29/month

Included:
    • The Hindu
    • Times of India
    • Indian Express

Customer commands:
    /start
    /buy
    /paid
    /myplan

Admin commands:
    /approve <user_id>
    /debug

The existing database is intentionally preserved.
Internally, approval activates all three existing newspaper keys
for the same 30-day subscription, so db.py does not need to change.
"""

import os
import logging
import threading
from pathlib import Path
from datetime import time, timezone, timedelta, datetime, date
from functools import wraps

from dotenv import load_dotenv

from telegram import (
    Update,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

# Existing Vercel proxy setup.
PROXY_URL: str = os.getenv("PROXY_URL", "").strip()
PROXY_SECRET: str = os.getenv("PROXY_SECRET", "").strip()

ASSETS_DIR: Path = Path(__file__).parent / "assets"

IST = timezone(timedelta(hours=5, minutes=30))

# Keep your current schedule.
IST_BROADCAST_TIME = time(
    hour=7,
    minute=30,
    tzinfo=IST,
)

IST_CLEANUP_TIME = time(
    hour=4,
    minute=0,
    tzinfo=IST,
)

# One customer-facing subscription.
SUBSCRIPTION_NAME = "All Newspapers"
SUBSCRIPTION_PRICE = "₹29/month"
SUBSCRIPTION_DAYS = 30

# Existing DB newspaper identifiers.
# These are NOT separate customer plans anymore.
NEWSPAPERS = {
    "hindu": "The Hindu",
    "toi": "Times of India",
    "ie": "Indian Express",
}

# Compatibility structure for existing DB/paper records.
PLANS = {
    code: {
        "name": name,
        "price": SUBSCRIPTION_PRICE,
    }
    for code, name in NEWSPAPERS.items()
}


# ─────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

AWAITING_NEWSPAPER_NAME = 0


# ─────────────────────────────────────────────────────────────────────
# Admin decorator
# ─────────────────────────────────────────────────────────────────────

def admin_only(func):
    """Allow only the configured admin to use the handler."""

    @wraps(func)
    async def wrapper(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        user = update.effective_user

        if not user or user.id != ADMIN_ID:
            return

        return await func(update, context)

    return wrapper


# ─────────────────────────────────────────────────────────────────────
# Admin PDF upload
# ─────────────────────────────────────────────────────────────────────

async def handle_pdf_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Admin sends a newspaper PDF."""

    user = update.effective_user

    if not user or user.id != ADMIN_ID:
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

    newspaper_list = "\n".join(
        f"  • `{code}` — {name}"
        for code, name in NEWSPAPERS.items()
    )

    await update.message.reply_text(
        "📄 *NEWSPAPER RECEIVED*\n\n"
        "Which newspaper is this PDF for?\n\n"
        f"{newspaper_list}\n\n"
        "Reply with the code, for example `hindu`.",
        parse_mode="Markdown",
    )

    return AWAITING_NEWSPAPER_NAME


async def handle_newspaper_name_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Admin identifies the newspaper for the uploaded PDF."""

    user = update.effective_user

    if not user or user.id != ADMIN_ID:
        return ConversationHandler.END

    if not update.message or not update.message.text:
        return AWAITING_NEWSPAPER_NAME

    newspaper_code = update.message.text.strip().lower()
    file_id = context.user_data.get("pending_file_id")

    if newspaper_code not in NEWSPAPERS:
        await update.message.reply_text(
            "❌ Unknown newspaper code.\n\n"
            "Use one of:\n"
            "• `hindu`\n"
            "• `toi`\n"
            "• `ie`",
            parse_mode="Markdown",
        )
        return AWAITING_NEWSPAPER_NAME

    if not file_id:
        await update.message.reply_text(
            "❌ No pending PDF found. Please send the PDF again."
        )
        return ConversationHandler.END

    await db.add_paper(newspaper_code, file_id)

    context.user_data.pop("pending_file_id", None)

    now_ist = datetime.now(IST).time()

    # IMPORTANT:
    # The scheduled job is only a fallback. If the admin uploads the
    # newspaper after the scheduled time, distribute it immediately.
    if now_ist >= IST_BROADCAST_TIME:
        await update.message.reply_text(
            "⏰ The scheduled broadcast time has passed.\n\n"
            "📡 Sending this newspaper to all active subscribers now..."
        )

        count = await broadcast_paper(
            context.bot,
            newspaper_code,
            file_id,
        )

        await update.message.reply_text(
            f"✅ *{NEWSPAPERS[newspaper_code]}* saved.\n\n"
            f"📤 Sent to *{count}* active subscriber(s).",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"✅ *{NEWSPAPERS[newspaper_code]}* saved successfully.\n\n"
            "📡 It will be delivered automatically at the "
            "scheduled broadcast time to all active subscribers.",
            parse_mode="Markdown",
        )

    return ConversationHandler.END


async def cancel_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Cancel PDF upload."""

    context.user_data.pop("pending_file_id", None)

    if update.message:
        await update.message.reply_text(
            "❌ Newspaper upload cancelled."
        )

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────
# Single subscription activation
# ─────────────────────────────────────────────────────────────────────

async def activate_all_newspapers(user_id: int) -> None:
    """
    Activate the ONE All Newspapers subscription.

    Existing db.py stores subscriptions using the three newspaper keys,
    so we activate all three for the same user.
    """
    for newspaper_code in NEWSPAPERS:
        await db.add_user(user_id, newspaper_code)


async def notify_approved_user(
    bot,
    user_id: int,
) -> None:
    """Tell the subscriber that their subscription is active."""

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 *SUBSCRIPTION ACTIVATED!*\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "📚 *ALL NEWSPAPERS*\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "📰 The Hindu\n"
                "📰 Times of India\n"
                "📰 Indian Express\n\n"
                f"⏳ Valid for *{SUBSCRIPTION_DAYS} days*\n\n"
                "Your newspapers will be delivered automatically. "
                "Enjoy your reading! ☕🗞️"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(
            "Could not notify approved user %s: %s",
            user_id,
            e,
        )


@admin_only
async def approve_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    /approve <user_id>

    Approve the ONE All Newspapers subscription.
    """

    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ *Missing user ID*\n\n"
            "Use:\n"
            "`/approve 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ User ID must be a number."
        )
        return

    try:
        await activate_all_newspapers(target_user_id)
    except Exception as e:
        logger.exception(
            "Failed to approve user %s: %s",
            target_user_id,
            e,
        )

        await update.message.reply_text(
            "❌ Approval failed. Please check the logs/database."
        )
        return

    await update.message.reply_text(
        "✅ *PAYMENT VERIFIED*\n\n"
        f"👤 User ID: `{target_user_id}`\n"
        "📚 Plan: *All Newspapers*\n"
        f"💰 Price: *{SUBSCRIPTION_PRICE}*\n"
        f"⏳ Duration: *{SUBSCRIPTION_DAYS} days*",
        parse_mode="Markdown",
    )

    await notify_approved_user(
        context.bot,
        target_user_id,
    )


async def approve_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Admin presses the inline APPROVE button."""

    query = update.callback_query

    if not query:
        return

    if not query.from_user or query.from_user.id != ADMIN_ID:
        await query.answer(
            "⛔ You are not authorized.",
            show_alert=True,
        )
        return

    data = query.data or ""

    if not data.startswith("approve:"):
        return

    try:
        target_user_id = int(
            data.split(":", 1)[1]
        )
    except ValueError:
        await query.answer(
            "Invalid user ID.",
            show_alert=True,
        )
        return

    await query.answer("Approving user...")

    try:
        await activate_all_newspapers(target_user_id)
    except Exception as e:
        logger.exception(
            "Callback approval failed for user %s: %s",
            target_user_id,
            e,
        )

        await query.answer(
            "Approval failed. Check logs.",
            show_alert=True,
        )
        return

    try:
        await query.edit_message_text(
            text=(
                "✅ *PAYMENT VERIFIED*\n\n"
                f"👤 User ID: `{target_user_id}`\n"
                "📚 *All Newspapers*\n"
                f"⏳ {SUBSCRIPTION_DAYS} days activated."
            ),
            parse_mode="Markdown",
        )
    except Exception:
        logger.warning(
            "Could not edit payment claim message for %s.",
            target_user_id,
        )

    await notify_approved_user(
        context.bot,
        target_user_id,
    )


# ─────────────────────────────────────────────────────────────────────
# Admin debug
# ─────────────────────────────────────────────────────────────────────

@admin_only
async def debug_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show basic Hugging Face filesystem information."""

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

    await update.message.reply_text(
        "📁 *Root Directory*\n"
        f"`{root_files}`\n\n"
        "🖼️ *Assets Directory*\n"
        f"`{assets_files}`",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────
# Customer interface
# ─────────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main customer menu."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📰 Subscribe — ₹29/month",
                    callback_data="buy",
                )
            ],
            [
                InlineKeyboardButton(
                    "💳 I've Paid",
                    callback_data="paid",
                ),
                InlineKeyboardButton(
                    "📋 My Subscription",
                    callback_data="myplan",
                ),
            ],
        ]
    )


def payment_keyboard() -> InlineKeyboardMarkup:
    """Payment screen buttons."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ I've Paid",
                    callback_data="paid",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 My Subscription",
                    callback_data="myplan",
                )
            ],
        ]
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Beautiful customer welcome screen."""

    user = update.effective_user

    if not user or not update.message:
        return

    await update.message.reply_text(
        f"👋 *Welcome, {user.first_name}!*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🗞️ *DAILY NEWSPAPER CLUB*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Get your daily newspapers delivered "
        "straight to Telegram.\n\n"
        "📚 *ONE SUBSCRIPTION • EVERYTHING INCLUDED*\n\n"
        "📰 The Hindu\n"
        "📰 Times of India\n"
        "📰 Indian Express\n\n"
        f"💰 *Only {SUBSCRIPTION_PRICE}*\n"
        f"⏳ *{SUBSCRIPTION_DAYS} days*\n\n"
        "No separate newspaper plans.\n"
        "Subscribe once and receive all available papers. "
        "☕📖",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def buy_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show payment QR for the single subscription."""

    qr_path = ASSETS_DIR / "qr.png"

    caption = (
        "💳 *ALL NEWSPAPERS SUBSCRIPTION*\n\n"
        "📰 The Hindu\n"
        "📰 Times of India\n"
        "📰 Indian Express\n\n"
        f"💰 *{SUBSCRIPTION_PRICE}*\n"
        f"⏳ *{SUBSCRIPTION_DAYS} days*\n\n"
        "Scan the QR code below and complete the UPI payment.\n\n"
        "After payment, press *I've Paid* below.\n"
        "The admin will verify your payment and activate "
        "your subscription."
    )

    if not update.message:
        return

    if qr_path.exists():
        try:
            await update.message.reply_photo(
                photo=qr_path,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=payment_keyboard(),
            )
        except Exception as e:
            logger.exception(
                "Could not send QR image: %s",
                e,
            )

            await update.message.reply_text(
                caption,
                parse_mode="Markdown",
                reply_markup=payment_keyboard(),
            )
    else:
        await update.message.reply_text(
            "⚠️ *Payment QR is currently unavailable.*\n\n"
            "Please contact the admin.",
            parse_mode="Markdown",
        )


async def create_payment_claim(
    bot,
    user,
) -> bool:
    """Send a payment claim to the admin."""

    user_id = user.id
    display_name = user.full_name or "Unknown"
    username = (
        f"@{user.username}"
        if user.username
        else "No username"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ APPROVE USER",
                    callback_data=f"approve:{user_id}",
                )
            ]
        ]
    )

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "💰 *NEW PAYMENT CLAIM*\n\n"
            f"👤 *Name:* {display_name}\n"
            f"🔗 *Username:* {username}\n"
            f"🆔 *User ID:* `{user_id}`\n\n"
            "📚 *Plan:* All Newspapers\n"
            f"💵 *Amount:* {SUBSCRIPTION_PRICE}\n\n"
            "Verify the payment in your UPI/bank app, "
            "then press *APPROVE USER*."
        ),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    return True


async def paid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """User says they completed payment."""

    user = update.effective_user

    if not user or not update.message:
        return

    try:
        await create_payment_claim(
            context.bot,
            user,
        )

        await update.message.reply_text(
            "✅ *Payment claim sent!*\n\n"
            "Your payment will be verified by the admin.\n"
            "Once approved, your *All Newspapers* subscription "
            "will be active for 30 days.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception(
            "Could not send payment claim from %s: %s",
            user.id,
            e,
        )

        await update.message.reply_text(
            "⚠️ We couldn't send your payment claim right now.\n"
            "Please try again in a moment."
        )


async def myplan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show one combined subscription instead of three plans."""

    user = update.effective_user

    if not user:
        return

    user_plans = await db.get_user_plans(user.id)

    valid_expiries = []

    for plan_data in user_plans or []:
        expiry = plan_data.get("expiry_date")

        if isinstance(expiry, datetime):
            expiry = expiry.date()

        if isinstance(expiry, date):
            valid_expiries.append(expiry)

    if not valid_expiries:
        text = (
            "📋 *YOUR SUBSCRIPTION*\n\n"
            "🔴 *No active subscription*\n\n"
            "Get all available newspapers for "
            f"*{SUBSCRIPTION_PRICE}*.\n\n"
            "📰 The Hindu\n"
            "📰 Times of India\n"
            "📰 Indian Express"
        )
    else:
        expiry = max(valid_expiries)
        days_left = (
            expiry - date.today()
        ).days

        if days_left < 0:
            text = (
                "📋 *YOUR SUBSCRIPTION*\n\n"
                "🔴 *ALL NEWSPAPERS — EXPIRED*\n\n"
                f"Expired: `{expiry}`\n\n"
                f"Renew for *{SUBSCRIPTION_PRICE}*."
            )
        else:
            text = (
                "📋 *YOUR SUBSCRIPTION*\n\n"
                "🟢 *ALL NEWSPAPERS — ACTIVE*\n\n"
                "📰 The Hindu\n"
                "📰 Times of India\n"
                "📰 Indian Express\n\n"
                f"⏳ *{days_left} days remaining*\n"
                f"📅 Expires: `{expiry}`\n\n"
                "Your papers will arrive automatically. "
                "☕🗞️"
            )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📰 Subscribe / Renew",
                    callback_data="buy",
                )
            ]
        ]
    )

    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


# ─────────────────────────────────────────────────────────────────────
# Callback router for customer buttons
# ─────────────────────────────────────────────────────────────────────

async def customer_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle customer inline buttons."""

    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    if data == "buy":
        await query.answer()

        # Callback messages cannot use update.message.
        qr_path = ASSETS_DIR / "qr.png"

        caption = (
            "💳 *ALL NEWSPAPERS SUBSCRIPTION*\n\n"
            "📰 The Hindu\n"
            "📰 Times of India\n"
            "📰 Indian Express\n\n"
            f"💰 *{SUBSCRIPTION_PRICE}*\n"
            f"⏳ *{SUBSCRIPTION_DAYS} days*\n\n"
            "Scan the QR code and complete your UPI payment.\n"
            "Then press *I've Paid*."
        )

        if query.message:
            if qr_path.exists():
                try:
                    await query.message.reply_photo(
                        photo=qr_path,
                        caption=caption,
                        parse_mode="Markdown",
                        reply_markup=payment_keyboard(),
                    )
                except Exception as e:
                    logger.exception(
                        "Could not send callback QR: %s",
                        e,
                    )
                    await query.message.reply_text(
                        caption,
                        parse_mode="Markdown",
                        reply_markup=payment_keyboard(),
                    )
            else:
                await query.message.reply_text(
                    "⚠️ Payment QR is currently unavailable."
                )

        return

    if data == "paid":
        await query.answer()

        user = query.from_user

        if not user or not query.message:
            return

        try:
            await create_payment_claim(
                context.bot,
                user,
            )

            await query.message.reply_text(
                "✅ *Payment claim sent!*\n\n"
                "The admin will verify your payment and activate "
                "your subscription after approval.",
                parse_mode="Markdown",
            )

        except Exception as e:
            logger.exception(
                "Callback payment claim failed for %s: %s",
                user.id,
                e,
            )

            await query.message.reply_text(
                "⚠️ We couldn't send your payment claim. "
                "Please try again."
            )

        return

    if data == "myplan":
        await query.answer()
        await myplan_command(
            update,
            context,
        )


# ─────────────────────────────────────────────────────────────────────
# Paper broadcasting
# ─────────────────────────────────────────────────────────────────────

async def broadcast_paper(
    bot,
    newspaper_code: str,
    file_id: str,
) -> int:
    """
    Send one newspaper to all subscribers who have the corresponding
    internal newspaper entry active.

    Because approval activates all three internal entries, every active
    All Newspapers subscriber receives every newspaper.
    """

    active_users = await db.get_active_users(
        plan=newspaper_code
    )

    newspaper_name = NEWSPAPERS.get(
        newspaper_code,
        newspaper_code,
    )

    success_count = 0

    for user in active_users:
        try:
            await bot.send_document(
                chat_id=user["user_id"],
                document=file_id,
                caption=(
                    f"📰 *{newspaper_name}*\n"
                    "📚 Daily Newspaper Club"
                ),
                parse_mode="Markdown",
            )

            success_count += 1

        except Exception as e:
            logger.error(
                "Failed to send %s to user %s: %s",
                newspaper_name,
                user["user_id"],
                e,
            )

    return success_count


# ─────────────────────────────────────────────────────────────────────
# Scheduled jobs
# ─────────────────────────────────────────────────────────────────────

async def send_pdfs_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Daily scheduled newspaper broadcast."""

    logger.info(
        "Running daily PDF broadcast job..."
    )

    papers = await db.get_todays_papers()

    if not papers:
        logger.info(
            "No papers uploaded for today. Skipping broadcast."
        )
        return

    for paper in papers:
        newspaper_code = paper["plan_name"]
        file_id = paper["file_id"]

        count = await broadcast_paper(
            context.bot,
            newspaper_code,
            file_id,
        )

        logger.info(
            "Broadcast '%s' to %s subscriber(s).",
            newspaper_code,
            count,
        )

    logger.info(
        "Daily PDF broadcast complete."
    )


async def cleanup_users_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Remove expired subscription rows."""

    logger.info(
        "Running expired subscription cleanup..."
    )

    deleted = await db.delete_expired_users()

    logger.info(
        "Cleanup complete. Removed %s expired row(s).",
        deleted,
    )


# ─────────────────────────────────────────────────────────────────────
# Application lifecycle
# ─────────────────────────────────────────────────────────────────────

async def post_init(application) -> None:
    """Initialize database."""

    await db.init_db()

    logger.info(
        "Bot started and database connected."
    )


async def post_shutdown(application) -> None:
    """Close database."""

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
    """Hugging Face health endpoint."""

    return {
        "status": "ok",
        "message": "Telegram Bot is running",
    }


def run_dummy_server():
    """Run FastAPI health server on port 7860."""

    uvicorn.run(
        app_api,
        host="0.0.0.0",
        port=7860,
        log_level="warning",
    )


# ─────────────────────────────────────────────────────────────────────
# Telegram application
# ─────────────────────────────────────────────────────────────────────

def build_application():
    """Build Telegram application through the Vercel proxy."""

    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN is not set in environment variables."
        )

    if not ADMIN_ID:
        raise ValueError(
            "ADMIN_ID is not set in environment variables."
        )

    if not PROXY_URL:
        raise ValueError(
            "PROXY_URL is not set. "
            "Please add PROXY_URL to Hugging Face Secrets."
        )

    if not PROXY_SECRET:
        raise ValueError(
            "PROXY_SECRET is not set. "
            "Please add PROXY_SECRET to Hugging Face Secrets."
        )

    logger.info(
        "🌐 Using Vercel Telegram proxy."
    )

    proxy_base = (
        f"{PROXY_URL.rstrip('/')}"
        f"/api/telegram/{PROXY_SECRET}/bot"
    )

    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    get_updates_request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    return (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(proxy_base)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


# ─────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────

def register_handlers(application):
    """Register all handlers before polling."""

    pdf_upload_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Document.PDF & filters.User(ADMIN_ID),
                handle_pdf_received,
            )
        ],
        states={
            AWAITING_NEWSPAPER_NAME: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND
                    & filters.User(ADMIN_ID),
                    handle_newspaper_name_reply,
                )
            ]
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_upload,
            )
        ],
    )

    application.add_handler(
        pdf_upload_handler
    )

    # Admin commands.
    application.add_handler(
        CommandHandler(
            "approve",
            approve_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "debug",
            debug_command,
        )
    )

    # Customer commands.
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "buy",
            buy_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "paid",
            paid_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "myplan",
            myplan_command,
        )
    )

    # Compatibility aliases for users who still have the old commands.
    application.add_handler(
        CommandHandler(
            "buyhindu",
            buy_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "buytoi",
            buy_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "buyie",
            buy_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "paidhindu",
            paid_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "paidtoi",
            paid_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "paidie",
            paid_command,
        )
    )

    # Admin approval button MUST be registered before general customer
    # callbacks.
    application.add_handler(
        CallbackQueryHandler(
            approve_callback,
            pattern=r"^approve:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            customer_callback,
            pattern=r"^(buy|paid|myplan)$",
        )
    )

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

    logger.info(
        "All handlers and scheduled jobs registered."
    )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    """Start the Telegram bot."""

    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN is not set in environment variables."
        )

    if not ADMIN_ID:
        raise ValueError(
            "ADMIN_ID is not set in environment variables."
        )

    threading.Thread(
        target=run_dummy_server,
        daemon=True,
    ).start()

    logger.info(
        "Started FastAPI health server on port 7860."
    )

    logger.info(
        "Building Telegram application..."
    )

    application = build_application()

    # Register EVERYTHING before polling.
    register_handlers(application)

    logger.info(
        "🚀 Telegram bot is starting polling..."
    )

    # Exactly one polling call.
    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
