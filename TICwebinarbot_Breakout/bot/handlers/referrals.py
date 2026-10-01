"""Credit referrers when someone joins the group through their link."""
from __future__ import annotations

import logging

from telegram import ChatMemberUpdated, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Config
from bot.db import Database
from bot.handlers.leaderboard import refresh_leaderboard_message

log = logging.getLogger(__name__)

_JOINED_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


def _just_joined(change: ChatMemberUpdated) -> bool:
    """True when the member transitioned from not-in-group to in-group."""
    old = change.old_chat_member.status
    new = change.new_chat_member.status
    was_in = old in _JOINED_STATUSES
    is_in = new in _JOINED_STATUSES
    # RESTRICTED members may or may not be present; rely on is_member when set.
    if change.new_chat_member.status == ChatMemberStatus.RESTRICTED:
        is_in = bool(change.new_chat_member.is_member)
    if change.old_chat_member.status == ChatMemberStatus.RESTRICTED:
        was_in = bool(change.old_chat_member.is_member)
    return not was_in and is_in


async def track_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.chat_member
    config: Config = context.bot_data["config"]

    if change is None or change.chat.id != config.group_id:
        return
    if not _just_joined(change):
        return

    invite = change.invite_link
    if invite is None:
        # Joined some other way (admin add, public link, etc.) — no credit.
        return

    db: Database = context.bot_data["db"]
    invited = change.new_chat_member.user
    referrer_id = await db.get_referrer_by_invite_link(invite.invite_link)
    if referrer_id is None:
        return  # Link not one of ours.

    credited = await db.add_referral(invited.id, referrer_id)
    if not credited:
        return  # Duplicate or self-referral.

    count = await db.get_referral_count(referrer_id)
    log.info("Referral credited: %s -> referrer %s (total %s)", invited.id, referrer_id, count)

    await _notify_referrer(context, referrer_id, invited, count)
    await refresh_leaderboard_message(context)


async def _notify_referrer(
    context: ContextTypes.DEFAULT_TYPE, referrer_id: int, invited, count: int
) -> None:
    invited_name = f"@{invited.username}" if invited.username else (invited.first_name or "someone")
    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                f"🎉 {invited_name} just joined using your link!\n"
                f"You now have <b>{count}</b> referral{'s' if count != 1 else ''}. "
                f"Keep sharing to climb the leaderboard 🚀"
            ),
            parse_mode="HTML",
        )
    except TelegramError:
        # Referrer may have never started a private chat with the bot.
        log.debug("Could not DM referrer %s about new referral", referrer_id)
