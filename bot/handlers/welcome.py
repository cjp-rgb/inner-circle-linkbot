"""Greet new members in the group with a button to get their own referral link."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Config

log = logging.getLogger(__name__)

_WELCOME = (
    "👋 Welcome to <b>{event}</b>, {who}!\n\n"
    "You're in early. The live webinar details are in the pinned message at the top.\n\n"
    "💰 <b>{prize}</b> goes to whoever invites the most people before we go live. "
    "Tap below to get your personal invite link 👇"
)


async def _delete_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        log.debug("Could not delete welcome message %s", message_id)


async def send_welcome(context: ContextTypes.DEFAULT_TYPE, user: User) -> None:
    config: Config = context.bot_data["config"]
    if user.is_bot:
        return

    who = html.escape(user.first_name or (f"@{user.username}" if user.username else "there"))
    text = _WELCOME.format(
        event=html.escape(context.bot_data.get("event_name", "the webinar")),
        who=who,
        prize=html.escape(context.bot_data.get("prize", "$100")),
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "🔗 Get my invite link",
            url=f"https://t.me/{context.bot.username}?start=getlink",
        )]]
    )
    try:
        sent = await context.bot.send_message(
            chat_id=config.group_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramError:
        log.exception("Failed to send welcome message")
        return

    # Keep the chat tidy: remove the welcome after a while (0 = keep forever).
    if config.welcome_delete_after > 0 and context.job_queue is not None:
        context.job_queue.run_once(
            _delete_later,
            when=config.welcome_delete_after,
            data=(config.group_id, sent.message_id),
            name=f"welcome-delete-{sent.message_id}",
        )
