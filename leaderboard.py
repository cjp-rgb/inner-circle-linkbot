"""Leaderboard rendering, the /leaderboard command, and the pinned auto-update."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from bot.config import Config
from bot.db import Database, LeaderboardRow

log = logging.getLogger(__name__)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
_META_MSG_KEY = "leaderboard_message_id"


def _format_leaderboard(rows: list[LeaderboardRow], event: str) -> str:
    if not rows:
        return (
            f"🏆 <b>{event} Referral Leaderboard</b>\n\n"
            "No referrals yet — be the first! Use /getmylink to grab your link."
        )

    lines = [f"🏆 <b>{event} Referral Leaderboard</b>\n"]
    for i, row in enumerate(rows, start=1):
        rank = _MEDALS.get(i, f"{i}.")
        plural = "s" if row.referrals != 1 else ""
        lines.append(f"{rank} {row.display_name} — <b>{row.referrals}</b> referral{plural}")
    lines.append("\n💰 Top referrer wins the cash prize. Use /getmylink to share yours!")
    return "\n".join(lines)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    db: Database = context.bot_data["db"]
    event = context.bot_data.get("event_name", "Webinar")

    rows = await db.get_leaderboard(limit=10)
    text = _format_leaderboard(rows, event)

    # Append the caller's own standing if they're not already in the top 10.
    user = update.effective_user
    if user is not None and all(r.user_id != user.id for r in rows):
        rank = await db.get_rank(user.id)
        if rank is not None:
            count = await db.get_referral_count(user.id)
            plural = "s" if count != 1 else ""
            text += f"\n\nYou're <b>#{rank}</b> with {count} referral{plural}."

    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def post_scheduled_leaderboard(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: post a FRESH leaderboard message (with notification) and pin it.

    Unlike refresh_leaderboard_message (which silently edits the existing message),
    this surfaces a new post so members see the standings twice a day.
    """
    config: Config = context.bot_data["config"]
    if config.leaderboard_chat_id is None:
        return

    db: Database = context.bot_data["db"]
    event = context.bot_data.get("event_name", "Webinar")
    rows = await db.get_leaderboard(limit=10)
    text = _format_leaderboard(rows, event)

    try:
        sent = await context.bot.send_message(
            chat_id=config.leaderboard_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        log.exception("Failed to post scheduled leaderboard")
        return

    # Make the newest post the one that real-time referral updates edit.
    await db.set_meta(_META_MSG_KEY, str(sent.message_id))
    try:
        await context.bot.pin_chat_message(
            chat_id=config.leaderboard_chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except TelegramError:
        log.debug("Could not pin scheduled leaderboard message")
    log.info("Posted scheduled leaderboard (message %s)", sent.message_id)


async def refresh_leaderboard_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post or edit the persistent leaderboard message in LEADERBOARD_CHAT_ID."""
    config: Config = context.bot_data["config"]
    if config.leaderboard_chat_id is None:
        return

    db: Database = context.bot_data["db"]
    event = context.bot_data.get("event_name", "Webinar")
    rows = await db.get_leaderboard(limit=10)
    text = _format_leaderboard(rows, event)

    msg_id_raw = await db.get_meta(_META_MSG_KEY)
    msg_id = int(msg_id_raw) if msg_id_raw else None

    if msg_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=config.leaderboard_chat_id,
                message_id=msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            # "message is not modified" is fine; anything else => repost.
            if "not modified" in str(exc).lower():
                return
            log.info("Leaderboard message %s unusable (%s); reposting.", msg_id, exc)
        except TelegramError:
            log.exception("Failed editing leaderboard message; reposting.")

    try:
        sent = await context.bot.send_message(
            chat_id=config.leaderboard_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        log.exception("Failed to post leaderboard message")
        return

    await db.set_meta(_META_MSG_KEY, str(sent.message_id))
    try:
        await context.bot.pin_chat_message(
            chat_id=config.leaderboard_chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except TelegramError:
        log.debug("Could not pin leaderboard message (missing permission?)")
