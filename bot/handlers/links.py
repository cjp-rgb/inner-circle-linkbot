"""Commands that hand each user their personal referral link (via private DM)."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from bot.config import Config
from bot.db import Database

log = logging.getLogger(__name__)

_LINK_MESSAGE = (
    "🚀 <b>{event} — Referral Contest</b>\n\n"
    "Here's your <b>personal invite link</b>. Anyone who joins the group through "
    "it counts as one of your referrals:\n\n"
    "🔗 {link}\n\n"
    "💰 <b>$250 CASH</b> to whoever brings in the <b>MOST</b> people before the "
    "contest ends.\n\n"
    "👉 Your mission: <b>refer as many people as possible</b> into the group. "
    "Share your link everywhere — DMs, your story, social posts, communities. "
    "The more you bring in, the higher you climb the leaderboard.\n\n"
    "📊 Check your rank any time with /leaderboard — updated live, and posted in "
    "the group daily at <b>1pm &amp; 8pm</b>.\n\n"
    "The race is on. Get sharing. 🔥"
)

# Shown in the group when the bot can't DM the user yet (they must press Start).
_NEEDS_START = (
    "👋 Hi {who}, tap the button below to open a private chat with me — your "
    "personal referral link will be sent to you instantly."
)

_DM_SENT = "👋 Hi {who}, I've sent your personal referral link to your DMs! 📬"


def _handle(user) -> str:
    """Plain @username (no clickable hyperlink mention), or first name."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or "there"


async def _ensure_invite_link(
    user_id: int, username: str | None, first_name: str | None, context: ContextTypes.DEFAULT_TYPE
) -> str | None:
    """Return the user's invite link, creating + persisting one if needed."""
    db: Database = context.bot_data["db"]
    config: Config = context.bot_data["config"]

    await db.upsert_user(user_id, username, first_name)

    link = await db.get_invite_link(user_id)
    if link:
        return link

    # Label the invite link with the client's @username (visible to group admins),
    # falling back to their name or id if they have no username. Max 32 chars.
    if username:
        label = f"@{username}"
    else:
        label = first_name or f"user {user_id}"
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=config.group_id,
            name=label[:32],
        )
    except TelegramError:
        log.exception("Failed to create invite link for user %s", user_id)
        return None

    await db.set_invite_link(user_id, invite.invite_link)
    return invite.invite_link


async def _send_link_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, link: str) -> bool:
    """DM the onboarding message + link to the user. Returns False if blocked."""
    event = context.bot_data.get("event_name", "Webinar")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=_LINK_MESSAGE.format(event=event, link=link),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except Forbidden:
        # User has never started a private chat with the bot.
        return False
    except TelegramError:
        log.exception("Failed to DM link to user %s", user_id)
        return False


async def getmylink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver the caller's personal link privately, wherever they invoke it."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or update.message is None:
        return

    link = await _ensure_invite_link(user.id, user.username, user.first_name, context)
    if link is None:
        await update.message.reply_text(
            "⚠️ I couldn't create your invite link right now. Please try again in a "
            "moment."
        )
        return

    sent = await _send_link_dm(context, user.id, link)
    is_private = chat is not None and chat.type == "private"

    if sent:
        if not is_private:
            await update.message.reply_text(_DM_SENT.format(who=_handle(user)))
        # In a private chat the DM itself is the reply — nothing more to add.
        return

    # Couldn't DM the user — they need to press Start first.
    bot_username = context.bot.username
    if is_private:
        # Shouldn't normally happen in a private chat, but fall back gracefully.
        await update.message.reply_text(
            _LINK_MESSAGE.format(
                event=context.bot_data.get("event_name", "Webinar"), link=link
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "🔗 Get my referral link",
                url=f"https://t.me/{bot_username}?start=getlink",
            )]]
        )
        await update.message.reply_text(
            _NEEDS_START.format(who=_handle(user)),
            reply_markup=keyboard,
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private /start: greet and immediately deliver the personal link."""
    if update.message is None or update.effective_user is None:
        return
    if update.effective_chat is not None and update.effective_chat.type != "private":
        await update.message.reply_text(
            "👋 Message me privately and press Start, then send /getmylink to get "
            "your personal referral link."
        )
        return

    user = update.effective_user
    link = await _ensure_invite_link(user.id, user.username, user.first_name, context)
    if link is None:
        await update.message.reply_text(
            "⚠️ I couldn't create your invite link right now. Please try /getmylink "
            "again in a moment."
        )
        return

    await _send_link_dm(context, user.id, link)
