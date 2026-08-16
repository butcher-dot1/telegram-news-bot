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
            logger.warning(
                "Unauthorized access attempt from user_id=%s",
                user.id if user else "None",
            )
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
        logger.warning("Unauthorized PDF upload attempt")
        return ConversationHandler.END

    if not update.message or not update.message.document:
        return ConversationHandler.END

    document: Document = update.message.document

    if document.mime_type != "application/pdf":
        try:
            await update.message.reply_text(
                "⚠️ Please send a PDF file."
            )
        except Exception as e:
            logger.exception("Failed to send error message: %s", e)
        return ConversationHandler.END

    context.user_data["pending_file_id"] = document.file_id

    newspaper_list = "\n".join(
        f"  • `{code}` — {name}"
        for code, name in NEWSPAPERS.items()
    )

    try:
        await update.message.reply_text(
            "📄 *NEWSPAPER RECEIVED*\n\n"
            "Which newspaper is this PDF for?\n\n"
            f"{newspaper_list}\n\n"
            "Reply with the code, for example `hindu`.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Failed to send newspaper selection prompt: %s", e)
        return ConversationHandler.END

    return AWAITING_NEWSPAPER_NAME


async def handle_newspaper_name_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Admin identifies the newspaper for the uploaded PDF."""

    user = update.effective_user

    if not user or user.id != ADMIN_ID:
        logger.warning("Unauthorized newspaper name reply")
        return ConversationHandler.END

    if not update.message or not update.message.text:
        return AWAITING_NEWSPAPER_NAME

    newspaper_code = update.message.text.strip().lower()
    file_id = context.user_data.get("pending_file_id")

    if newspaper_code not in NEWSPAPERS:
        try:
            await update.message.reply_text(
                "❌ Unknown newspaper code.\n\n"
                "Use one of:\n"
                "• `hindu`\n"
                "• `toi`\n"
                "• `ie`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Failed to send invalid code message: %s", e)
        return AWAITING_NEWSPAPER_NAME

    if not file_id:
        try:
            await update.message.reply_text(
                "❌ No pending PDF found. Please send the PDF again."
            )
        except Exception as e:
            logger.exception("Failed to send no PDF message: %s", e)
        return ConversationHandler.END

    # 🔴 FIXED: Added try-except for database operation
    try:
        await db.add_paper(newspaper_code, file_id)
    except Exception as e:
        logger.exception(
            "Failed to save newspaper PDF for %s: %s",
            newspaper_code,
            e,
        )
        try:
            await update.message.reply_text(
                "❌ Failed to save newspaper to database. "
                "Please try again."
            )
        except Exception as send_err:
            logger.exception("Failed to send error message: %s", send_err)
        return ConversationHandler.END

    context.user_data.pop("pending_file_id", None)

    # IMPORTANT:
    # If the newspaper is uploaded after 7:30 AM IST,
    # distribute it immediately.
    now_ist = datetime.now(IST)

    scheduled_today = now_ist.replace(
        hour=IST_BROADCAST_TIME.hour,
        minute=IST_BROADCAST_TIME.minute,
        second=0,
        microsecond=0,
    )

    if now_ist >= scheduled_today:
        try:
            await update.message.reply_text(
                "⏰ The scheduled broadcast time has passed.\n\n"
                "📡 Sending this newspaper to all active subscribers now..."
            )
        except Exception as e:
            logger.exception(
                "Failed to send broadcast notice: %s",
                e,
            )

        try:
            count = await broadcast_paper(
                context.bot,
                newspaper_code,
                file_id,
            )

            try:
                await update.message.reply_text(
                    f"✅ *{NEWSPAPERS[newspaper_code]}* saved.\n\n"
                    f"📤 Sent to *{count}* active subscriber(s).",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.exception(
                    "Failed to send success message: %s",
                    e,
                )

        except Exception as e:
            logger.exception(
                "Failed to broadcast newspaper: %s",
                e,
            )

            try:
                await update.message.reply_text(
                    "⚠️ Saved but failed to broadcast. "
                    "Check logs for details."
                )
            except Exception as send_err:
                logger.exception(
                    "Failed to send error notification: %s",
                    send_err,
                )

    else:
        try:
            await update.message.reply_text(
                f"✅ *{NEWSPAPERS[newspaper_code]}* saved successfully.\n\n"
                "📡 It will be delivered automatically at the "
                "scheduled broadcast time to all active subscribers.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception(
                "Failed to send save confirmation: %s",
                e,
            )

    return ConversationHandler.END


async def cancel_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Cancel the current newspaper PDF upload conversation."""

    context.user_data.pop("pending_file_id", None)

    if update.message:
        try:
            await update.message.reply_text(
                "❌ Newspaper upload cancelled."
            )
        except Exception as e:
            logger.exception(
                "Failed to send cancellation message: %s",
                e,
            )

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────
# Single subscription activation
# ─────────────────────────────────────────────────────────────────────

# 🔴 FIXED: Now returns bool instead of None
async def activate_all_newspapers(user_id: int) -> bool:
    """
    Activate the ONE All Newspapers subscription.

    Existing db.py stores subscriptions using the three newspaper keys,
    so we activate all three for the same user.
    
    Returns True if successful, False otherwise.
    """
    try:
        for newspaper_code in NEWSPAPERS:
            await db.add_user(user_id, newspaper_code)
        logger.info("Successfully activated newspapers for user %s", user_id)
        return True
    except Exception as e:
        logger.exception(
            "Failed to activate newspapers for user %s: %s",
            user_id,
            e,
        )
        return False


# 🔴 FIXED: Now returns bool; handles exceptions
async def notify_approved_user(
    bot,
    user_id: int,
) -> bool:
    """
    Tell the subscriber that their subscription is active.
    
    Returns True if notification sent successfully, False otherwise.
    """

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
        return True
    except Exception as e:
        logger.warning(
            "Could not notify approved user %s: %s",
            user_id,
            e,
        )
        return False


@admin_only
async def approve_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    /approve <user_id>

    Approve the ONE All Newspapers subscription.
    """

    # 🔴 FIXED: Added null check for update.message
    if not update.message:
        logger.warning("approve_command called with no message")
        return

    if len(context.args) < 1:
        try:
            await update.message.reply_text(
                "❌ *Missing user ID*\n\n"
                "Use:\n"
                "`/approve 123456789`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Failed to send missing ID message: %s", e)
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        try:
            await update.message.reply_text(
                "❌ User ID must be a number."
            )
        except Exception as e:
            logger.exception("Failed to send invalid ID message: %s", e)
        return

    # 🔴 FIXED: Check return value of activate_all_newspapers
    success = await activate_all_newspapers(target_user_id)

    if not success:
        try:
            await update.message.reply_text(
                "❌ Approval failed. Please check the logs/database."
            )
        except Exception as e:
            logger.exception("Failed to send failure message: %s", e)
        return

    try:
        await update.message.reply_text(
            "✅ *PAYMENT VERIFIED*\n\n"
            f"👤 User ID: `{target_user_id}`\n"
            "📚 Plan: *All Newspapers*\n"
            f"💰 Price: *{SUBSCRIPTION_PRICE}*\n"
            f"⏳ Duration: *{SUBSCRIPTION_DAYS} days*",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Failed to send approval confirmation: %s", e)

    # Notify user
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
        try:
            await query.answer(
                "⛔ You are not authorized.",
                show_alert=True,
            )
        except Exception as e:
            logger.exception("Failed to send unauthorized answer: %s", e)
        return

    data = query.data or ""

    if not data.startswith("approve:"):
        return

    try:
        target_user_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        try:
            await query.answer(
                "Invalid user ID.",
                show_alert=True,
            )
        except Exception as e:
            logger.exception("Failed to send invalid ID answer: %s", e)
        return

    try:
        await query.answer("Approving user...")
    except Exception as e:
        logger.exception("Failed to send processing answer: %s", e)

    # 🔴 FIXED: Check return value
    success = await activate_all_newspapers(target_user_id)

    if not success:
        try:
            await query.answer(
                "Approval failed. Check logs.",
                show_alert=True,
            )
        except Exception as e:
            logger.exception("Failed to send failure answer: %s", e)
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
    except Exception as e:
        logger.warning(
            "Could not edit payment claim message for %s: %s",
            target_user_id,
            e,
        )

    # Notify user
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
    """Show basic filesystem information."""

    # 🔴 FIXED: Added null check for update.message
    if not update.message:
        logger.warning("debug_command called with no message")
        return

    base_dir = ASSETS_DIR.parent

    try:
        root_files = "\n".join(os.listdir(base_dir))
    except Exception as e:
        root_files = f"Error: {e}"

    if ASSETS_DIR.exists():
        try:
            assets_files = "\n".join(os.listdir(ASSETS_DIR))
        except Exception as e:
            assets_files = f"Error: {e}"
    else:
        assets_files = "⚠️ FOLDER DOES NOT EXIST"

    try:
        await update.message.reply_text(
            "📁 *Root Directory*\n"
            f"`{root_files}`\n\n"
            "🖼️ *Assets Directory*\n"
            f"`{assets_files}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Failed to send debug info: %s", e)


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

    try:
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
    except Exception as e:
        logger.exception("Failed to send start message: %s", e)


async def buy_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show payment QR for the single subscription."""

    if not update.message:
        return

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

    if not qr_path.exists():
        try:
            await update.message.reply_text(
                "⚠️ *Payment QR is currently unavailable.*\n\n"
                "Please contact the admin.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Failed to send unavailable QR message: %s", e)
        return

    try:
        await update.message.reply_photo(
            photo=qr_path,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=payment_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to send QR image: %s", e)
        try:
            await update.message.reply_text(
                caption,
                parse_mode="Markdown",
                reply_markup=payment_keyboard(),
            )
        except Exception as fallback_err:
            logger.exception("Failed to send text fallback: %s", fallback_err)


# 🔴 FIXED: Added try-except for database call; returns bool
async def create_payment_claim(
    bot,
    user,
) -> bool:
    """Send a payment claim to the admin. Returns True if successful."""

    if not user:
        return False

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

    try:
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
    except Exception as e:
        logger.exception(
            "Failed to send payment claim to admin for user %s: %s",
            user_id,
            e,
        )
        return False


async def paid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """User says they completed payment."""

    user = update.effective_user

    if not user or not update.message:
        return

    success = await create_payment_claim(
        context.bot,
        user,
    )

    if success:
        try:
            await update.message.reply_text(
                "✅ *Payment claim sent!*\n\n"
                "Your payment will be verified by the admin.\n"
                "Once approved, your *All Newspapers* subscription "
                "will be active for 30 days.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Failed to send payment confirmation: %s", e)
    else:
        try:
            await update.message.reply_text(
                "⚠️ We couldn't send your payment claim right now.\n"
                "Please try again in a moment."
            )
        except Exception as e:
            logger.exception("Failed to send error message: %s", e)


# 🔴 FIXED: Added try-except for database call; improved date handling; use edit_message_text
async def myplan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show one combined subscription instead of three plans."""

    user = update.effective_user

    if not user:
        return

    # 🔴 FIXED: Wrapped database call in try-except
    try:
        user_plans = await db.get_user_plans(user.id)
    except Exception as e:
        logger.exception("Failed to fetch user plans for %s: %s", user.id, e)
        user_plans = None

    valid_expiries = []

    if user_plans:
        # 🔴 FIXED: Better date handling with logging
        for plan_data in user_plans:
            try:
                expiry = plan_data.get("expiry_date")

                if expiry is None:
                    logger.warning(
                        "Plan data missing expiry_date for user %s: %s",
                        user.id,
                        plan_data,
                    )
                    continue

                if isinstance(expiry, datetime):
                    expiry = expiry.date()
                elif isinstance(expiry, str):
                    try:
                        expiry = datetime.fromisoformat(expiry).date()
                    except ValueError:
                        logger.error(
                            "Invalid expiry date format for user %s: %s",
                            user.id,
                            expiry,
                        )
                        continue

                if isinstance(expiry, date):
                    valid_expiries.append(expiry)
            except Exception as e:
                logger.exception(
                    "Error processing plan data for user %s: %s",
                    user.id,
                    e,
                )
                continue

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
        days_left = (expiry - date.today()).days

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

    try:
        # 🔴 FIXED: Use edit_message_text for callbacks instead of reply_text
        if update.callback_query and update.callback_query.message:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        elif update.message:
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.exception("Failed to send myplan message for user %s: %s", user.id, e)


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

    # ─────────────────────────────────────────────────────────────────
    # BUY - Show payment QR
    # ─────────────────────────────────────────────────────────────────
    if data == "buy":
        try:
            await query.answer()
        except Exception as e:
            logger.exception("Failed to answer buy callback: %s", e)

        if not query.message:
            logger.warning("No message in buy callback query")
            return

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

        if not qr_path.exists():
            try:
                await query.message.reply_text(
                    "⚠️ Payment QR is currently unavailable. "
                    "Please try again later."
                )
            except Exception as e:
                logger.exception("Failed to send unavailable QR message: %s", e)
            return

        try:
            await query.message.reply_photo(
                photo=qr_path,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=payment_keyboard(),
            )
        except Exception as e:
            logger.exception("Failed to send QR photo in callback: %s", e)
            try:
                await query.message.reply_text(
                    caption,
                    parse_mode="Markdown",
                    reply_markup=payment_keyboard(),
                )
            except Exception as fallback_err:
                logger.exception("Failed to send text fallback: %s", fallback_err)

        return

    # ─────────────────────────────────────────────────────────────────
    # PAID - Create payment claim
    # ─────────────────────────────────────────────────────────────────
    if data == "paid":
        try:
            await query.answer()
        except Exception as e:
            logger.exception("Failed to answer paid callback: %s", e)

        user = query.from_user

        if not user or not query.message:
            logger.warning("Missing user or message in paid callback")
            return

        success = await create_payment_claim(
            context.bot,
            user,
        )

        if success:
            try:
                await query.message.reply_text(
                    "✅ *Payment claim sent!*\n\n"
                    "The admin will verify your payment and activate "
                    "your subscription after approval.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.exception("Failed to send payment claim confirmation: %s", e)
        else:
            try:
                await query.message.reply_text(
                    "⚠️ We couldn't send your payment claim. "
                    "Please try again."
                )
            except Exception as e:
                logger.exception("Failed to send error message: %s", e)

        return

    # ─────────────────────────────────────────────────────────────────
    # MYPLAN - Show subscription status
    # ─────────────────────────────────────────────────────────────────
    if data == "myplan":
        try:
            await query.answer()
        except Exception as e:
            logger.exception("Failed to answer myplan callback: %s", e)

        await myplan_command(update, context)
        return


# ─────────────────────────────────────────────────────────────────────
# Paper broadcasting
# ─────────────────────────────────────────────────────────────────────

# 🔴 FIXED: Added database exception handling and null check
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
    
    Returns the number of successful deliveries.
    """

    try:
        active_users = await db.get_active_users(plan=newspaper_code)
    except Exception as e:
        logger.exception(
            "Failed to fetch active users for %s: %s",
            newspaper_code,
            e,
        )
        return 0

    # 🔴 FIXED: Check if active_users is None or empty
    if not active_users:
        logger.info(
            "No active users found for newspaper: %s",
            newspaper_code,
        )
        return 0

    newspaper_name = NEWSPAPERS.get(
        newspaper_code,
        newspaper_code,
    )

    success_count = 0

    for user in active_users:
        try:
            user_id = user.get("user_id")

            if not user_id:
                logger.warning("User record missing user_id: %s", user)
                continue

            await bot.send_document(
                chat_id=user_id,
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
                user.get("user_id", "UNKNOWN"),
                e,
            )

    return success_count


# ─────────────────────────────────────────────────────────────────────
# Scheduled jobs
# ─────────────────────────────────────────────────────────────────────

# 🔴 FIXED: Added comprehensive error handling
async def send_pdfs_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Daily scheduled newspaper broadcast."""

    logger.info("Running daily PDF broadcast job...")

    try:
        papers = await db.get_todays_papers()
    except Exception as e:
        logger.exception("Failed to fetch today's papers: %s", e)
        return

    if not papers:
        logger.info("No papers uploaded for today. Skipping broadcast.")
        return

    for paper in papers:
        try:
            newspaper_code = paper.get("plan_name")
            file_id = paper.get("file_id")

            if not newspaper_code or not file_id:
                logger.warning(
                    "Paper record missing required fields: %s",
                    paper,
                )
                continue

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
        except Exception as e:
            logger.exception(
                "Failed to process paper in broadcast job: %s",
                e,
            )

    logger.info("Daily PDF broadcast complete.")


# 🔴 FIXED: Added error handling
async def cleanup_users_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Remove expired subscription rows."""

    logger.info("Running expired subscription cleanup...")

    try:
        deleted = await db.delete_expired_users()
        logger.info(
            "Cleanup complete. Removed %s expired row(s).",
            deleted,
        )
    except Exception as e:
        logger.exception("Failed to cleanup expired users: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Application lifecycle
# ─────────────────────────────────────────────────────────────────────

# 🔴 FIXED: Added error handling for database initialization
async def post_init(application) -> None:
    """Initialize database."""

    try:
        await db.init_db()
        logger.info("Bot started and database connected.")
    except Exception as e:
        logger.critical("Failed to initialize database: %s", e)
        raise


# 🔴 FIXED: Added error handling for database close
async def post_shutdown(application) -> None:
    """Close database."""

    try:
        await db.close_db()
        logger.info("Bot stopped and database disconnected.")
    except Exception as e:
        logger.exception("Failed to close database: %s", e)


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


# 🔴 FIXED: Added error handling for uvicorn
def run_dummy_server():
    """Run FastAPI health server on port 7860."""

    try:
        uvicorn.run(
            app_api,
            host="0.0.0.0",
            port=7860,
            log_level="warning",
        )
    except Exception as e:
        logger.critical("FastAPI server crashed: %s", e)
        raise


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

    logger.info("🌐 Using Vercel Telegram proxy.")

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

    application.add_handler(pdf_upload_handler)

    # Admin commands
    application.add_handler(
        CommandHandler("approve", approve_command)
    )

    application.add_handler(
        CommandHandler("debug", debug_command)
    )

    # Customer commands
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("buy", buy_command)
    )

    application.add_handler(
        CommandHandler("paid", paid_command)
    )

    application.add_handler(
        CommandHandler("myplan", myplan_command)
    )

    # Compatibility aliases for users who still have old commands
    application.add_handler(
        CommandHandler("buyhindu", buy_command)
    )

    application.add_handler(
        CommandHandler("buytoi", buy_command)
    )

    application.add_handler(
        CommandHandler("buyie", buy_command)
    )

    application.add_handler(
        CommandHandler("paidhindu", paid_command)
    )

    application.add_handler(
        CommandHandler("paidtoi", paid_command)
    )

    application.add_handler(
        CommandHandler("paidie", paid_command)
    )

    # Admin approval button MUST be registered before general customer callbacks
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

    # Register scheduled jobs
    job_queue = application.job_queue

    if job_queue is None:
        logger.critical(
            "JobQueue not available. Install with: "
            "pip install python-telegram-bot[job-queue]"
        )
        raise RuntimeError(
            "JobQueue is required but not installed. "
            "Install with: pip install python-telegram-bot[job-queue]"
        )

    try:
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
            "All handlers and scheduled jobs registered successfully."
        )
    except Exception as e:
        logger.critical("Failed to register scheduled jobs: %s", e)
        raise


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

    # Start FastAPI health server in background
    try:
        threading.Thread(
            target=run_dummy_server,
            daemon=True,
        ).start()

        logger.info("Started FastAPI health server on port 7860.")
    except Exception as e:
        logger.exception("Failed to start health server: %s", e)

    logger.info("Building Telegram application...")

    # 🔴 FIXED: Added error handling for build and register
    try:
        application = build_application()
    except ValueError as e:
        logger.critical("Configuration error: %s", e)
        raise

    # Register EVERYTHING before polling
    try:
        register_handlers(application)
    except Exception as e:
        logger.critical("Failed to register handlers: %s", e)
        raise

    logger.info("🚀 Telegram bot is starting polling...")

    try:
        # Exactly one polling call
        application.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical("Bot crashed: %s", e)
        raise


if __name__ == "__main__":
    import sys
    import traceback

    try:
        main()
    except Exception:
        logger.critical(
            "FATAL: Bot crashed on startup:\n%s",
            traceback.format_exc(),
        )
        sys.stdout.flush()
        sys.stderr.flush()
        raise
    
