"""Partner Hub bot.

- Adds partners to the leaderboard when they join the hub (or first post in it).
- Counts every "NEW FTD" post in the FTD topic for the partner who posted it.
- Keeps one pinned, live leaderboard in the Leaderboard topic, reset monthly.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, time

from telegram import Update, User
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from partnerbot.config import Config, tier_rate
from partnerbot.db import Database, Row

logging.basicConfig(format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("partnerbot")

FTD_RE = re.compile(r"new\s*ftd", re.IGNORECASE)
DEPOSIT_RE = re.compile(r"\$\s?([\d][\d,.]*)")
STRATEGY_RE = re.compile(r"strategy\s*:\s*(.+)", re.IGNORECASE)
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
MAX_LINES = 60
IN_GROUP = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


# --- helpers ------------------------------------------------------------------

def _cfg(ctx: ContextTypes.DEFAULT_TYPE) -> Config:
    return ctx.bot_data["config"]


def _db(ctx: ContextTypes.DEFAULT_TYPE) -> Database:
    return ctx.bot_data["db"]


def _month(cfg: Config, now: datetime | None = None) -> str:
    return (now or datetime.now(cfg.tz)).strftime("%Y-%m")


def _month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def _tier_text(count: int) -> str:
    rate = tier_rate(count)
    return f"${rate} tier" if rate else "no tier yet"


def _next_tier_hint(count: int) -> str:
    for minimum in (1, 5, 10, 15, 20):
        if count < minimum:
            gap = minimum - count
            return f"{gap} more for the ${tier_rate(minimum)} tier"
    return "top tier reached 🔥"


DIVIDER = "━━━━━━━━━━━━━━━━━━"


def _plural(n: int, word: str = "FTD") -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def render(rows: list[Row], month: str, final: bool = False) -> str:
    """Leaderboard text, trimmed so it always fits in one Telegram message (4096 chars)."""
    limit = MAX_LINES
    while True:
        text = _render(rows, month, final, limit)
        if len(text) <= 4000 or limit <= 5:
            return text
        limit -= 5


def _render(rows: list[Row], month: str, final: bool, limit: int) -> str:
    ranked = [r for r in rows if r.count > 0]
    waiting = [r for r in rows if r.count == 0]
    total = sum(r.count for r in ranked)

    if final:
        lines = ["🏁 <b>FINAL STANDINGS</b> 🏁", f"📅 <i>{_month_label(month)}</i>", "", DIVIDER, ""]
    else:
        lines = ["🏆 <b>PARTNER LEADERBOARD</b> 🏆", f"📅 <i>{_month_label(month)}</i>", "", DIVIDER, ""]

    if not ranked:
        lines += ["🚀 No FTDs yet this month.", "First one on the board takes 🥇!", ""]
    for i, r in enumerate(ranked[:limit], start=1):
        place = MEDALS.get(i, f"<b>{i}.</b>")
        lines.append(f"{place} {r.emoji or '⭐'} <b>{html.escape(r.name)}</b>")
        lines.append(f"      🔥 {_plural(r.count)}  ·  💰 {_tier_text(r.count)}")
        lines.append("")
    if len(ranked) > limit:
        lines += [f"…and {len(ranked) - limit} more on the board", ""]

    if waiting and not final:
        lines += [DIVIDER, "", "⏳ <b>Still to get started</b>", ""]
        shown = waiting[:limit]
        lines += [f"{r.emoji or '⭐'} {html.escape(r.name)}" for r in shown]
        if len(waiting) > limit:
            lines.append(f"…and {len(waiting) - limit} more")
        lines.append("")

    lines += [DIVIDER, "",
              f"📊 <b>{_plural(total)}</b> {'in total' if final else 'this month'}",
              f"👥 <b>{len(rows)}</b> partner{'s' if len(rows) != 1 else ''}", ""]
    if final:
        lines += ["🔄 The board has reset for the new month.", "Good luck everyone! 🚀"]
    else:
        lines += ["💎 <b>TIERS</b>",
                  "1–4 FTDs → $150",
                  "5–9 FTDs → $175",
                  "10–14 FTDs → $200",
                  "15–19 FTDs → $250",
                  "20+ FTDs → $300", "",
                  "⚡ Updates live with every FTD",
                  "🔄 Resets on the 1st of every month"]
    return "\n".join(lines)


async def _is_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None or chat.id != _cfg(ctx).group_id:
        return False
    if msg.sender_chat is not None and msg.sender_chat.id == chat.id:
        return True  # anonymous group admin
    user = update.effective_user
    if user is None:
        return False
    try:
        member = await ctx.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def _is_person(user: User | None) -> bool:
    return user is not None and not user.is_bot


async def _is_hub_admin(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Whether this user is an admin of the Partner Hub (works from a private chat too)."""
    try:
        member = await ctx.bot.get_chat_member(_cfg(ctx).group_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _thread(ctx: ContextTypes.DEFAULT_TYPE, key: str) -> int | None:
    raw = await _db(ctx).get_meta(key)
    return int(raw) if raw else None


# --- leaderboard message --------------------------------------------------------

async def refresh_board(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit the live leaderboard in place (posting and pinning it if needed)."""
    cfg, db = _cfg(ctx), _db(ctx)
    thread = await _thread(ctx, "leaderboard_thread")
    if thread is None:
        return
    month = _month(cfg)
    text = render(await db.standings(month), month)
    msg_raw = await db.get_meta("board_message_id")
    if msg_raw and await db.get_meta("board_month") == month:
        try:
            await ctx.bot.edit_message_text(text, chat_id=cfg.group_id, message_id=int(msg_raw),
                                            parse_mode=ParseMode.HTML)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.info("Leaderboard message unusable (%s); reposting", exc)
        except TelegramError:
            log.exception("Editing leaderboard failed; reposting")
    await _post_board(ctx, text, month)


async def _post_board(ctx: ContextTypes.DEFAULT_TYPE, text: str, month: str) -> None:
    cfg, db = _cfg(ctx), _db(ctx)
    thread = await _thread(ctx, "leaderboard_thread")
    try:
        sent = await ctx.bot.send_message(cfg.group_id, text, message_thread_id=thread,
                                          parse_mode=ParseMode.HTML)
    except TelegramError:
        log.exception("Posting leaderboard failed")
        return
    await db.set_meta("board_message_id", str(sent.message_id))
    await db.set_meta("board_month", month)
    try:
        await ctx.bot.pin_chat_message(cfg.group_id, sent.message_id, disable_notification=True)
    except TelegramError:
        log.info("Could not pin leaderboard (bot needs Pin Messages)")


async def check_month_rollover(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """On a new month: post last month's final standings, then a fresh board."""
    cfg, db = _cfg(ctx), _db(ctx)
    if await _thread(ctx, "leaderboard_thread") is None:
        return
    current = _month(cfg)
    previous = await db.get_meta("board_month")
    if previous is None or previous == current:
        return
    await send_digest(ctx, previous)
    thread = await _thread(ctx, "leaderboard_thread")
    try:
        await ctx.bot.send_message(cfg.group_id, render(await db.standings(previous), previous, final=True),
                                   message_thread_id=thread, parse_mode=ParseMode.HTML)
    except TelegramError:
        log.exception("Posting final standings failed")
    await _post_board(ctx, render(await db.standings(current), current), current)
    log.info("Month rolled over %s -> %s", previous, current)


# --- payout digest (admins only, private) -------------------------------------

def _payout_date(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return datetime(y, m, 15).strftime("%-d %B %Y")


def render_payouts(rows: list[Row], month: str, final: bool) -> str:
    ranked = [r for r in rows if r.count > 0]
    title = "💸 <b>PAYOUT DIGEST</b>" if final else "💸 <b>PAYOUTS SO FAR</b>"
    lines = [title, f"📅 <i>{_month_label(month)}</i>", "", DIVIDER, ""]
    total_ftds = total_due = 0
    if not ranked:
        lines += ["No FTDs logged this month.", ""]
    for r in ranked:
        rate = tier_rate(r.count) or 0
        due = r.count * rate
        total_ftds += r.count
        total_due += due
        lines.append(f"{r.emoji or '⭐'} <b>{html.escape(r.name)}</b>")
        lines.append(f"      {_plural(r.count)} × ${rate} = <b>${due:,}</b>")
        lines.append("")
    lines += [DIVIDER, "",
              f"📊 <b>{_plural(total_ftds)}</b> across {len(ranked)} partner{'s' if len(ranked) != 1 else ''}",
              f"💰 <b>Estimated total: ${total_due:,}</b>",
              f"📆 Due on the 15th: <b>{_payout_date(month)}</b>", "",
              "⚠️ <i>Estimate from FTDs posted in the hub. Check each one against Kudo's qualified CPAs "
              "before paying, take off any client-referral splits, and add PU Prime rebates separately.</i>"]
    return "\n".join(lines)


async def _digest_recipients(ctx: ContextTypes.DEFAULT_TYPE) -> set[int]:
    raw = await _db(ctx).get_meta("digest_recipients")
    return {int(x) for x in raw.split(",") if x} if raw else set()


async def send_digest(ctx: ContextTypes.DEFAULT_TYPE, month: str) -> None:
    """DM the final payout digest for a month to every admin who has used /payouts."""
    text = render_payouts(await _db(ctx).standings(month), month, final=True)
    for uid in await _digest_recipients(ctx):
        if not await _is_hub_admin(ctx, uid):
            continue
        try:
            await ctx.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            log.info("Could not DM payout digest to %s", uid)


async def _rollover_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await check_month_rollover(ctx)


async def add_admins(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Add every (human) admin of the hub to the leaderboard. Returns how many."""
    cfg, db = _cfg(ctx), _db(ctx)
    try:
        admins = await ctx.bot.get_chat_administrators(cfg.group_id)
    except TelegramError:
        log.info("Could not list admins")
        return 0
    n = 0
    for a in admins:
        if _is_person(a.user) and not getattr(a, "is_anonymous", False):
            await db.upsert_partner(a.user.id, a.user.username, a.user.first_name, active=True)
            n += 1
    return n


# --- membership -----------------------------------------------------------------

async def on_member_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.chat_member
    if change is None or change.chat.id != _cfg(ctx).group_id:
        return
    user = change.new_chat_member.user
    if not _is_person(user):
        return
    was_in = change.old_chat_member.status in IN_GROUP or bool(getattr(change.old_chat_member, "is_member", False))
    is_in = change.new_chat_member.status in IN_GROUP or bool(getattr(change.new_chat_member, "is_member", False))
    db = _db(ctx)
    if is_in and not was_in:
        await db.upsert_partner(user.id, user.username, user.first_name, active=True)
        log.info("Partner joined: %s", user.id)
        await refresh_board(ctx)
    elif was_in and not is_in:
        await db.set_active(user.id, False)
        log.info("Partner left: %s", user.id)
        await refresh_board(ctx)


# --- messages -------------------------------------------------------------------

async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    cfg, db = _cfg(ctx), _db(ctx)
    if msg is None or update.effective_chat is None or update.effective_chat.id != cfg.group_id:
        return
    user = update.effective_user
    if _is_person(user) and msg.sender_chat is None:
        if await db.upsert_partner(user.id, user.username, user.first_name, active=True):
            await refresh_board(ctx)

    ftd_thread = await _thread(ctx, "ftd_thread")
    if ftd_thread is None or msg.message_thread_id != ftd_thread or not msg.is_topic_message:
        return
    text = msg.text or msg.caption or ""

    if not FTD_RE.search(text) or not _is_person(user) or msg.sender_chat is not None:
        return

    month = _month(cfg)
    before = await db.count(user.id, month)
    deposit = DEPOSIT_RE.search(text)
    strategy = STRATEGY_RE.search(text)
    added = await db.add_ftd(user.id, month, msg.message_id,
                             f"${deposit.group(1)}" if deposit else None,
                             strategy.group(1).strip()[:60] if strategy else None)
    if not added:
        return
    after = before + 1
    log.info("FTD logged for %s (now %s in %s)", user.id, after, month)
    try:
        await ctx.bot.set_message_reaction(cfg.group_id, msg.message_id, reaction="🔥")
    except TelegramError:
        pass
    if tier_rate(after) != tier_rate(before):
        try:
            await msg.reply_text(f"🎉 {after} FTD{'s' if after != 1 else ''} this month. You've reached the "
                                 f"<b>${tier_rate(after)} tier</b>!", parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
    await refresh_board(ctx)


async def on_reaction(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Anyone who reacts to any message in the hub is added to the leaderboard (used for roll call)."""
    r = update.message_reaction
    if r is None or r.chat.id != _cfg(ctx).group_id or not _is_person(r.user):
        return
    if await _db(ctx).upsert_partner(r.user.id, r.user.username, r.user.first_name, active=True):
        log.info("Partner added via reaction: %s", r.user.id)
        await refresh_board(ctx)


# --- commands -------------------------------------------------------------------

async def cmd_rollcall(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, ctx):
        return
    msg = update.effective_message
    await add_admins(ctx)
    await ctx.bot.send_message(
        _cfg(ctx).group_id,
        "👋 <b>PARTNER ROLL CALL</b>\n\n"
        "React to this message with any emoji to be added to the 🏆 Leaderboard.\n\n"
        "Takes one tap. Do it now so you're on the board before your first FTD! 🚀",
        message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
        parse_mode=ParseMode.HTML)
    try:
        await msg.delete()
    except TelegramError:
        pass
    await refresh_board(ctx)


async def cmd_removepartner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, ctx):
        return
    msg = update.effective_message
    db = _db(ctx)
    uid = None
    if msg.reply_to_message and _is_person(msg.reply_to_message.from_user):
        uid = msg.reply_to_message.from_user.id
    elif ctx.args:
        uid = await db.find_by_username(ctx.args[0])
    if uid is None:
        await msg.reply_text("Reply to the partner's message, or use /removepartner @username.")
        return
    await db.set_active(uid, False)
    await msg.reply_text("✅ Removed from the leaderboard. Their FTDs this month still count if they have any.")
    await refresh_board(ctx)


async def cmd_setftd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not await _is_admin(update, ctx):
        return
    if not msg.is_topic_message or msg.message_thread_id is None:
        await msg.reply_text("Send /setftd inside the FTD Chat topic.")
        return
    await _db(ctx).set_meta("ftd_thread", str(msg.message_thread_id))
    await msg.reply_text("✅ This topic is now the FTD Chat. Every ✅ NEW FTD post here will be counted.")


async def cmd_setleaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not await _is_admin(update, ctx):
        return
    if not msg.is_topic_message or msg.message_thread_id is None:
        await msg.reply_text("Send /setleaderboard inside the Leaderboard topic.")
        return
    db = _db(ctx)
    await add_admins(ctx)
    await db.set_meta("leaderboard_thread", str(msg.message_thread_id))
    await db.set_meta("board_message_id", "")
    month = _month(_cfg(ctx))
    await _post_board(ctx, render(await db.standings(month), month), month)
    try:
        await msg.delete()
    except TelegramError:
        pass


async def _target_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    """The partner a command refers to: the replied-to message's author, or an @username argument."""
    msg = update.effective_message
    if msg.reply_to_message and _is_person(msg.reply_to_message.from_user):
        u = msg.reply_to_message.from_user
        await _db(ctx).upsert_partner(u.id, u.username, u.first_name, active=True)
        return u.id
    if ctx.args:
        return await _db(ctx).find_by_username(ctx.args[0])
    return None


async def cmd_addftd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, ctx):
        return
    uid = await _target_user(update, ctx)
    if uid is None:
        await update.effective_message.reply_text("Reply to the partner's message, or use /addftd @username.")
        return
    await _db(ctx).add_ftd(uid, _month(_cfg(ctx)), None, None, "added by admin")
    await update.effective_message.reply_text("✅ FTD added.")
    await refresh_board(ctx)


async def cmd_removeftd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, ctx):
        return
    msg = update.effective_message
    db = _db(ctx)
    if msg.reply_to_message:
        removed = await db.remove_ftd_by_message(msg.reply_to_message.message_id)
        if removed is None and _is_person(msg.reply_to_message.from_user):
            removed = msg.reply_to_message.from_user.id if await db.remove_latest_ftd(
                msg.reply_to_message.from_user.id, _month(_cfg(ctx))) else None
    elif ctx.args:
        uid = await db.find_by_username(ctx.args[0])
        removed = uid if uid and await db.remove_latest_ftd(uid, _month(_cfg(ctx))) else None
    else:
        await msg.reply_text("Reply to the FTD post, or use /removeftd @username to remove their latest one.")
        return
    await msg.reply_text("🗑 FTD removed." if removed else "Nothing to remove for that partner this month.")
    if removed:
        await refresh_board(ctx)


async def cmd_addpartner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, ctx):
        return
    msg = update.effective_message
    if not msg.reply_to_message or not _is_person(msg.reply_to_message.from_user):
        await msg.reply_text("Reply to one of the partner's messages with /addpartner.")
        return
    u = msg.reply_to_message.from_user
    await _db(ctx).upsert_partner(u.id, u.username, u.first_name, active=True)
    await msg.reply_text("✅ Added to the leaderboard.")
    await refresh_board(ctx)


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = _month(_cfg(ctx))
    await update.effective_message.reply_text(render(await _db(ctx).standings(month), month),
                                              parse_mode=ParseMode.HTML)


async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_person(user):
        return
    month = _month(_cfg(ctx))
    n = await _db(ctx).count(user.id, month)
    await update.effective_message.reply_text(
        f"📊 <b>{_month_label(month)}</b>\nFTDs: <b>{n}</b> · {_tier_text(n)}\n{_next_tier_hint(n)}",
        parse_mode=ParseMode.HTML)


async def cmd_payouts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admins only, in a private chat: estimated payouts for this month (or /payouts YYYY-MM)."""
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if user is None or msg is None:
        return
    if chat is None or chat.type != "private":
        if await _is_hub_admin(ctx, user.id):
            await msg.reply_text("🔒 Payouts are private. Message me directly and send /payouts.")
        return
    if not await _is_hub_admin(ctx, user.id):
        await msg.reply_text("This command is for Partner Hub admins.")
        return
    recipients = await _digest_recipients(ctx)
    new_recipient = user.id not in recipients
    if new_recipient:
        recipients.add(user.id)
        await _db(ctx).set_meta("digest_recipients", ",".join(str(x) for x in sorted(recipients)))
    month = _month(_cfg(ctx))
    if ctx.args and re.fullmatch(r"\d{4}-\d{2}", ctx.args[0]):
        month = ctx.args[0]
    final = month != _month(_cfg(ctx))
    await msg.reply_text(render_payouts(await _db(ctx).standings(month), month, final=final),
                         parse_mode=ParseMode.HTML)
    if new_recipient:
        await msg.reply_text("✅ You'll also get the final digest by DM on the 1st of every month.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = ("<b>Partner Hub bot</b>\n"
            "/leaderboard: this month's standings\n"
            "/mystats: your FTDs and tier\n\n"
            "<b>Admins</b>\n"
            "/setftd: run inside the FTD topic\n"
            "/setleaderboard: run inside the Leaderboard topic\n"
            "/removeftd: reply to an FTD post (or /removeftd @user)\n"
            "/addftd: reply to a partner's message (or /addftd @user)\n"
            "/addpartner: reply to a partner's message to list them\n"
            "/removepartner: reply to a message (or /removepartner @user) to hide someone\n"
            "/rollcall: post a roll call. Everyone who reacts is added\n"
            "/payouts: in a DM with me, estimated payouts (or /payouts 2026-10)")
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# --- app ------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    cfg: Config = app.bot_data["config"]
    db = Database(cfg.db_path)
    await db.connect()
    app.bot_data["db"] = db
    await app.bot.set_my_commands([
        ("leaderboard", "This month's standings"),
        ("mystats", "Your FTDs and tier this month"),
        ("help", "How the bot works"),
    ])
    if app.job_queue is not None:
        app.job_queue.run_daily(_rollover_job, time=time(0, 1, tzinfo=cfg.tz), name="month-rollover")
        app.job_queue.run_once(_rollover_job, when=5, name="startup-rollover-check")
    log.info("Partner bot initialised. Group=%s", cfg.group_id)


async def _post_shutdown(app: Application) -> None:
    db: Database | None = app.bot_data.get("db")
    if db is not None:
        await db.close()


def build_application(cfg: Config) -> Application:
    app = (ApplicationBuilder().token(cfg.bot_token)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    app.bot_data["config"] = cfg
    for name, fn in (("setftd", cmd_setftd), ("setleaderboard", cmd_setleaderboard),
                     ("addftd", cmd_addftd), ("removeftd", cmd_removeftd),
                     ("addpartner", cmd_addpartner), ("leaderboard", cmd_leaderboard),
                     ("mystats", cmd_mystats), ("help", cmd_help), ("start", cmd_help),
                     ("rollcall", cmd_rollcall), ("removepartner", cmd_removepartner),
                     ("payouts", cmd_payouts)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(ChatMemberHandler(on_member_change, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message))
    return app


def main() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    cfg = Config.from_env()
    build_application(cfg).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
