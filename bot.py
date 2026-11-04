"""
Inner Circle migration bot
--------------------------
1. Posts a daily "we've moved" message (rotating variants) in the OLD Trade Hub group,
   with a button that opens this bot.
2. Gates the NEW group: both admins' invite links are set to "Request to join".
   When someone requests, the bot checks them:
     - VERIFY_URL set (John's lookup endpoint)  -> auto-approve verified TradeQuo members
     - otherwise / not found                    -> bot DMs them, asks for their TradeQuo UID,
                                                   and posts Approve / Reject buttons to the admin chat
3. No TradeQuo account? The bot asks who referred them (affiliates come from the Google Sheet)
   and sends that affiliate's TradeQuo link. After approval it explains app setup and tiers.
4. Affiliate invite links: an affiliate DMs /affiliate, you approve in the admin chat, and the bot creates
   their own "request to join" link to the new group. Everyone who requests through it is tracked to that
   affiliate (upline set automatically, their TradeQuo link sent if needed). You still approve each person
   with one tap after checking their UID in TradeQuo. /tradequo <link>, /mystats for affiliates;
   /affiliates for admins.
5. FTDs: when a client who came through an affiliate's link is approved, the affiliate gets a "Send FTD"
   button. The bot checks it (approved, has a UID, came through their link, not already counted), posts it
   in the Partner Hub FTD topic and updates the pinned leaderboard in the Leaderboard topic.
   Admin: /setleaderboard, /addftd @user, /removeftd @user. Anyone: /leaderboard.
6. /status (admins only) -> counts; /export -> CSV of everyone who has moved.

Run:  pip install "python-telegram-bot[job-queue]==21.6" httpx
      cp .env.example .env  (fill in)  ->  python bot.py
"""
import csv, io, logging, os, random, re, sqlite3
from datetime import time, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, ChatJoinRequestHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

# ---------- config ----------
def env(k, default=None, cast=str):
    v = os.getenv(k, default)
    return None if v is None or (v == "" and cast is not str) else cast(v)

def load_dotenv(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
load_dotenv()

TOKEN          = env("BOT_TOKEN")
OLD_GROUP_ID   = env("OLD_GROUP_ID", cast=int)          # old Trade Hub
OLD_TOPIC_IDS  = [int(x) for x in env("OLD_TOPIC_IDS", "").split(",") if x.strip()]  # topics to post the move message in (blank = General)
ADMIN_TOPIC_ID = env("ADMIN_TOPIC_ID", cast=int)       # optional: if the admin chat is a topic in a group
NEW_GROUP_ID   = env("NEW_GROUP_ID", cast=int)          # new joint group
ADMIN_CHAT_ID  = env("ADMIN_CHAT_ID", cast=int)         # private admin group with you + John
ADMIN_IDS      = {int(x) for x in env("ADMIN_IDS", "").split(",") if x.strip()}
CONTACT        = env("CONTACT_HANDLE", "@carsonpickardd")
SIGNUP_URL     = env("SIGNUP_URL", "https://breakoutalgo.com/innercircle")
POST_TIME      = env("POST_TIME", "09:00")              # Europe/London
VERIFY_URL     = env("VERIFY_URL")                      # optional: John's lookup endpoint
VERIFY_KEY     = env("VERIFY_KEY", "")
DB_PATH        = env("DB_PATH", "migration.db")
AFFILIATES_CSV_URL = env("AFFILIATES_CSV_URL")             # published "Bot Feed" tab of the Google Sheet
AFFILIATES_FALLBACK = env("AFFILIATES", "")               # "Name|https://tradequo-link;Name2|link"
DEFAULT_UPLINE = env("DEFAULT_UPLINE", "CJ (Inner Circle)")
HUB_CHAT_ID    = env("HUB_CHAT_ID", cast=int)            # Partner Hub (topics group)
FTD_TOPIC_ID   = env("FTD_TOPIC_ID", cast=int)           # FTD Chat topic id
LB_TOPIC_ID    = env("LEADERBOARD_TOPIC_ID", cast=int)   # Leaderboard topic id
TIERS = [(20, 300), (15, 250), (10, 200), (5, 175), (1, 150)]
EMOJIS = "🦊🐺🦁🐉🦅🐯🐻🦈🐼🦉🐲🦄🐆🦬🐊🦂🐝🦋🐙🦜🐍🦏🐘🦓🐗🦌🐧🦩🐬🐳"
TZ             = ZoneInfo("Europe/London")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("migration-bot")

UID_RE = re.compile(r"^\s*([A-Za-z0-9\-]{4,20})\s*$")

# ---------- daily post variants ----------
VARIANTS = [
    "🚚 <b>We've moved.</b>\n\nThe Inner Circle now lives in a brand-new group, alongside our new broker TradeQuo and the new app.\n\nThis group is no longer active. Tap below to get moved across 👇",
    "📍 <b>Heads up: this chat is closed.</b>\n\nAll signals, updates and the new app are in the new Inner Circle group now.\n\nTap below and we'll get you in.",
    "🔐 <b>The new Inner Circle is live.</b>\n\nNew group, new broker (TradeQuo), new app with duels, leaderboards and the Hall of Fame.\n\nNothing new gets posted here anymore. Tap below to move over.",
    "👋 <b>Still in here?</b>\n\nYou're missing everything. We've moved to a new group and this one is read-only.\n\nTap below to get swapped over. It takes about 5 minutes.",
    f"⏳ <b>Last call for anyone still here.</b>\n\nThe Inner Circle has moved. If you're stuck, message {CONTACT} and we'll sort it personally.\n\nOr tap below 👇",
]

# ---------- storage ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS members(
        user_id INTEGER PRIMARY KEY, username TEXT, name TEXT,
        status TEXT,            -- requested | awaiting_uid | pending_admin | approved | rejected
        uid TEXT, via_link TEXT, source TEXT,
        requested_at TEXT, decided_at TEXT, decided_by TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS affiliates(
        tg_id INTEGER PRIMARY KEY, username TEXT, name TEXT, status TEXT,   -- pending | active | declined
        invite_link TEXT, tradequo_link TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ftds(
        id INTEGER PRIMARY KEY AUTOINCREMENT, aff_tg_id INTEGER, member_id INTEGER UNIQUE,
        month TEXT, created_at TEXT, source TEXT)""")
    acols = [r[1] for r in c.execute("PRAGMA table_info(affiliates)")]
    if "emoji" not in acols:
        c.execute("ALTER TABLE affiliates ADD COLUMN emoji TEXT")
    cols = [r[1] for r in c.execute("PRAGMA table_info(members)")]
    if "upline" not in cols:
        c.execute("ALTER TABLE members ADD COLUMN upline TEXT")
    return c

def upsert(user, **fields):
    c = db()
    c.execute("INSERT OR IGNORE INTO members(user_id, username, name) VALUES(?,?,?)",
              (user.id, user.username, user.full_name))
    c.execute("UPDATE members SET username=?, name=? WHERE user_id=?",
              (user.username, user.full_name, user.id))
    for k, v in fields.items():
        c.execute(f"UPDATE members SET {k}=? WHERE user_id=?", (v, user.id))
    c.commit(); c.close()

def get(user_id):
    c = db(); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM members WHERE user_id=?", (user_id,)).fetchone()
    c.close(); return r

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------- verification ----------
async def lookup(user_id: int, uid: str | None = None) -> bool:
    """Ask John's backend whether this Telegram user (or UID) is a linked TradeQuo member.
    Expected response: {"verified": true/false}. Returns False if not configured or on error."""
    if not VERIFY_URL:
        return False
    try:
        params = {"telegram_id": user_id}
        if uid: params["uid"] = uid
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get(VERIFY_URL, params=params, headers={"Authorization": f"Bearer {VERIFY_KEY}"})
            return bool(r.json().get("verified"))
    except Exception as e:
        log.warning("lookup failed: %s", e)
        return False

# ---------- affiliates (from the Google Sheet) ----------
_aff_cache = {"at": 0, "rows": []}

async def affiliates():
    """[(name, tradequo_link)] from the sheet's published Bot Feed CSV, cached 5 min."""
    import time as _t
    if _t.time() - _aff_cache["at"] < 300 and _aff_cache["rows"]:
        return _aff_cache["rows"]
    rows = []
    if AFFILIATES_CSV_URL:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
                r = await h.get(AFFILIATES_CSV_URL)
            for i, line in enumerate(csv.reader(io.StringIO(r.text))):
                if i == 0 or len(line) < 2: continue
                name, link = line[0].strip(), line[1].strip()
                if name and link.startswith("http"): rows.append((name, link))
        except Exception as e:
            log.warning("affiliate sheet fetch failed: %s", e)
    c = db()
    for name, link in c.execute("SELECT name, tradequo_link FROM affiliates WHERE status='active' AND tradequo_link LIKE 'http%'"):
        if name not in [r[0] for r in rows]: rows.append((name, link))
    c.close()
    if not rows and AFFILIATES_FALLBACK:
        for part in AFFILIATES_FALLBACK.split(";"):
            if "|" in part:
                n, l = part.split("|", 1); rows.append((n.strip(), l.strip()))
    _aff_cache.update(at=_t.time(), rows=rows)
    return rows

TIERS_TEXT = (
    "📲 <b>Setting up the app</b>\n"
    f"1. Go to {SIGNUP_URL}\n"
    "2. Enter your email and a password\n"
    "3. Enter your <b>TradeQuo UID</b>\n"
    "4. Link your Telegram\n"
    "5. Choose your membership and you're in\n\n"
    "💎 <b>What each tier unlocks</b>\n\n"
    "🆓 <b>Free</b>\n"
    "Signal Vault app, free London & NY session signals, academy, news & calendar\n\n"
    "⚡ <b>Premium · £49/month</b> (connect your TradeQuo account)\n"
    "Breakout Algo autotrader, 1v1 duels, leaderboards, Hall of Fame, journal & analytics, Funded Royale\n\n"
    "👑 <b>Pro · £49/month</b> ($5,000+ account)\n"
    "Everything in Premium, plus XAU Pro 24/7 (weekends included) and the XAU Pro updates channel\n\n"
    "🎁 Refer 3 friends who actively trade and your membership is free.\n\n"
    "<i>Trading carries a high risk of losing money. Past performance doesn't guarantee future results.</i>"
)

def has_account_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I have a TradeQuo account", callback_data="acct:yes")],
        [InlineKeyboardButton("🆕 I need a TradeQuo account", callback_data="acct:no")],
    ])

async def ask_account(chat_id, ctx, intro):
    await ctx.bot.send_message(chat_id, intro + "\n\nDo you already have a TradeQuo account?",
                               parse_mode=ParseMode.HTML, reply_markup=has_account_kb())

async def on_account_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; u = q.from_user; await q.answer()
    choice = q.data.split(":")[1]
    if choice == "yes":
        upsert(u, status=(get(u.id)["status"] if get(u.id) and get(u.id)["requested_at"] else "awaiting_uid"))
        c = db(); c.execute("UPDATE members SET status=CASE WHEN status IN ('approved','rejected') THEN status ELSE 'awaiting_uid' END WHERE user_id=?", (u.id,)); c.commit(); c.close()
        await q.edit_message_text("Great 👍 Reply with your <b>TradeQuo UID</b> (you'll find it in your TradeQuo account).", parse_mode=ParseMode.HTML)
        return
    r = get(u.id)
    known = aff_by_link(r["via_link"]) if r and r["via_link"] else None
    if known and known["tradequo_link"]:
        await q.edit_message_text(
            f"You're with <b>{known['name']}</b> ✅\n\n1️⃣ Open your TradeQuo account here:\n{known['tradequo_link']}\n\n"
            "2️⃣ Verify your account\n\n3️⃣ Come back and reply here with your <b>TradeQuo UID</b>\n\n"
            f"Stuck at any point? Message {CONTACT}.", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    affs = await affiliates()
    if not affs:
        await q.edit_message_text(f"Message {CONTACT} and we'll send you the right TradeQuo sign-up link."); return
    rows = [[InlineKeyboardButton(n, callback_data=f"up:{i}")] for i, (n, _) in enumerate(affs[:40])]
    rows.append([InlineKeyboardButton("Nobody / not sure", callback_data="up:none")])
    await q.edit_message_text("Who introduced you to The Inner Circle? Pick them below so you're set up under the right person 👇",
                              reply_markup=InlineKeyboardMarkup(rows))

async def on_upline_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; u = q.from_user; await q.answer()
    affs = await affiliates()
    key = q.data.split(":")[1]
    if key == "none":
        match = [a for a in affs if a[0] == DEFAULT_UPLINE] or affs[:1]
    else:
        i = int(key); match = [affs[i]] if i < len(affs) else []
    if not match:
        await q.edit_message_text(f"Something went wrong. Message {CONTACT}."); return
    name, link = match[0]
    upsert(u, upline=name)
    c = db(); c.execute("UPDATE members SET status=CASE WHEN status IN ('approved','rejected') THEN status ELSE 'awaiting_uid' END WHERE user_id=?", (u.id,)); c.commit(); c.close()
    await q.edit_message_text(
        f"You're with <b>{name}</b> ✅\n\n"
        f"1️⃣ Open your TradeQuo account here:\n{link}\n\n"
        "2️⃣ Verify your account\n\n"
        "3️⃣ Come back and reply here with your <b>TradeQuo UID</b>\n\n"
        f"Stuck at any point? Message {CONTACT}.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- affiliate invite links ----------
def aff_by_link(link):
    c = db(); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM affiliates WHERE invite_link=?", (link,)).fetchone(); c.close(); return r

def aff_by_id(tg_id):
    c = db(); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM affiliates WHERE tg_id=?", (tg_id,)).fetchone(); c.close(); return r

async def cmd_affiliate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiliate asks for their own invite link to the new group."""
    if update.effective_chat.type != "private": return
    u = update.effective_user; a = aff_by_id(u.id)
    if a and a["status"] == "active":
        await update.message.reply_text(f"Your invite link 👇\n{a['invite_link']}\n\n/mystats shows who's joined through it."); return
    if a and a["status"] == "pending":
        await update.message.reply_text("Your request is with CJ. You'll get your link as soon as it's approved."); return
    name = " ".join(ctx.args).strip() if ctx.args else u.full_name
    c = db(); c.execute("INSERT OR REPLACE INTO affiliates(tg_id, username, name, status, created_at) VALUES(?,?,?,?,?)",
                        (u.id, u.username, name[:32], "pending", now())); c.commit(); c.close()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve affiliate", callback_data=f"affok:{u.id}"),
                                InlineKeyboardButton("❌ Decline", callback_data=f"affno:{u.id}")]])
    handle = f"@{u.username}" if u.username else "(no username)"
    await ctx.bot.send_message(ADMIN_CHAT_ID, message_thread_id=ADMIN_TOPIC_ID, text= f"🤝 <b>Affiliate link request</b>\n{name} {handle}", parse_mode=ParseMode.HTML, reply_markup=kb)
    await update.message.reply_text("Request sent ✅ You'll get your personal invite link here once it's approved.\n\n"
                                    "Meanwhile, send your TradeQuo referral link like this:\n/tradequo https://your-tradequo-link")

async def on_affiliate_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await admin_ok(ctx, q.from_user.id):
        await q.answer("Admins only", show_alert=True); return
    action, tid = q.data.split(":"); tid = int(tid); a = aff_by_id(tid)
    if not a: await q.answer("Not found"); return
    if action == "affok":
        link = await ctx.bot.create_chat_invite_link(NEW_GROUP_ID, name=a["name"][:32], creates_join_request=True)
        c = db(); c.execute("UPDATE affiliates SET status='active', invite_link=? WHERE tg_id=?", (link.invite_link, tid)); c.commit(); c.close()
        ensure_emoji(tid)
        try: await refresh_board(ctx)
        except Exception as e: log.warning("board refresh failed: %s", e)
        await ctx.bot.send_message(tid, "🎉 You're approved as an Inner Circle affiliate.\n\n"
            f"Your personal invite link to the new group:\n{link.invite_link}\n\n"
            "Everyone who joins through it is tracked to you. Each request is checked against TradeQuo before they're let in.\n"
            "/mystats to see your numbers · /tradequo <link> to set your TradeQuo referral link")
        await q.edit_message_text(q.message.text_html + f"\n\n✅ Approved by {q.from_user.first_name}\n{link.invite_link}", parse_mode=ParseMode.HTML)
    else:
        c = db(); c.execute("UPDATE affiliates SET status='declined' WHERE tg_id=?", (tid,)); c.commit(); c.close()
        await ctx.bot.send_message(tid, f"Your affiliate request wasn't approved. Message {CONTACT} if you think that's a mistake.")
        await q.edit_message_text(q.message.text_html + f"\n\n❌ Declined by {q.from_user.first_name}", parse_mode=ParseMode.HTML)
    await q.answer()

async def cmd_tradequo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    a = aff_by_id(update.effective_user.id)
    if not a: await update.message.reply_text("Request your affiliate link first with /affiliate"); return
    link = (ctx.args[0] if ctx.args else "").strip()
    if not link.startswith("http"):
        await update.message.reply_text("Send it like this:\n/tradequo https://your-tradequo-link"); return
    c = db(); c.execute("UPDATE affiliates SET tradequo_link=? WHERE tg_id=?", (link, a["tg_id"])); c.commit(); c.close()
    _aff_cache["at"] = 0
    await update.message.reply_text("Saved ✅ Anyone joining through your invite link without a TradeQuo account gets this link.")

async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    a = aff_by_id(update.effective_user.id)
    if not a or a["status"] != "active": await update.message.reply_text("You don't have an active affiliate link yet. Use /affiliate"); return
    c = db()
    rows = dict(c.execute("SELECT status, COUNT(*) FROM members WHERE via_link=? GROUP BY status", (a["invite_link"],)).fetchall()); c.close()
    total = sum(rows.values())
    c = db(); n_ftd = c.execute("SELECT COUNT(*) FROM ftds WHERE aff_tg_id=? AND month=?", (a["tg_id"], month_key())).fetchone()[0]; c.close()
    await update.message.reply_text(f"📊 <b>Your link</b>\nRequests: {total}\nApproved: {rows.get('approved',0)}\n"
        f"Waiting on UID / approval: {rows.get('awaiting_uid',0)+rows.get('pending_admin',0)+rows.get('requested',0)}\n"
        f"Rejected: {rows.get('rejected',0)}\n\n🔥 FTDs this month: {n_ftd} · 💰 ${tier_for(n_ftd)} tier", parse_mode=ParseMode.HTML)

async def cmd_affiliates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_u(update, ctx): return
    c = db()
    rows = c.execute("""SELECT a.name, a.status, COALESCE(a.tradequo_link,'') != '',
                        (SELECT COUNT(*) FROM members m WHERE m.via_link=a.invite_link),
                        (SELECT COUNT(*) FROM members m WHERE m.via_link=a.invite_link AND m.status='approved')
                        FROM affiliates a ORDER BY 5 DESC""").fetchall(); c.close()
    txt = "🤝 <b>Affiliates</b> (requests / approved)\n" + "\n".join(
        f"{'🟢' if s=='active' else '🟡' if s=='pending' else '🔴'} {n}: {r} / {ok}{'' if tq else '  ⚠️ no TradeQuo link'}" for n, s, tq, r, ok in rows)
    await update.message.reply_text(txt if rows else "No affiliates yet.", parse_mode=ParseMode.HTML)

# ---------- FTDs + partner leaderboard ----------
def month_key(dt=None): return (dt or datetime.now(TZ)).strftime("%Y-%m")
def month_label(m): return datetime.strptime(m, "%Y-%m").strftime("%B %Y")
def tier_for(n):
    for need, pay in TIERS:
        if n >= need: return pay
    return 0

def ensure_emoji(tg_id):
    c = db(); r = c.execute("SELECT emoji FROM affiliates WHERE tg_id=?", (tg_id,)).fetchone()
    if r and r[0]: c.close(); return r[0]
    used = {x[0] for x in c.execute("SELECT emoji FROM affiliates WHERE emoji IS NOT NULL")}
    free = [e for e in EMOJIS if e not in used] or list(EMOJIS)
    e = random.choice(free); c.execute("UPDATE affiliates SET emoji=? WHERE tg_id=?", (e, tg_id)); c.commit(); c.close(); return e

def standings(month):
    c = db()
    rows = c.execute("""SELECT a.tg_id, a.name, a.username, COUNT(f.id) n FROM affiliates a
                        LEFT JOIN ftds f ON f.aff_tg_id=a.tg_id AND f.month=?
                        WHERE a.status='active' GROUP BY a.tg_id ORDER BY n DESC, a.name""", (month,)).fetchall()
    c.close(); return rows

def render_board(month, final=False):
    rows = standings(month); medals = ["🥇", "🥈", "🥉"]
    on = [r for r in rows if r[3] > 0]; off = [r for r in rows if r[3] == 0]
    L = [("🏁 FINAL STANDINGS 🏁" if final else "🏆 PARTNER LEADERBOARD 🏆"), f"📅 {month_label(month)}", "", "━━━━━━━━━━━━━━━━━━", ""]
    for i, (tid, name, user, n) in enumerate(on):
        who = f"@{user}" if user else name
        L += [f"{medals[i] if i < 3 else f'{i+1}.'} {ensure_emoji(tid)} {who}", f"      🔥 {n} FTD{'s' if n != 1 else ''}  ·  💰 ${tier_for(n)} tier", ""]
    if not on: L += ["No FTDs yet this month. First one takes the top spot 👀", ""]
    if off:
        L += ["━━━━━━━━━━━━━━━━━━", "", "⏳ Still to get started", ""]
        shown = off[:25]
        L += [f"{ensure_emoji(t)} {('@'+u) if u else n_}" for t, n_, u, _ in shown]
        if len(off) > 25: L.append(f"…and {len(off)-25} more")
        L.append("")
    total = sum(r[3] for r in rows)
    L += ["━━━━━━━━━━━━━━━━━━", "", f"📊 {total} FTDs this month", f"👥 {len(rows)} partners", "",
          "💎 TIERS", "1–4 FTDs → $150", "5–9 FTDs → $175", "10–14 FTDs → $200", "15–19 FTDs → $250", "20+ FTDs → $300", "",
          "⚡ Updates live with every verified FTD", "🔄 Resets on the 1st of every month"]
    return "\n".join(L)[:4000]

def hub_ids():
    """Hub chat + topic ids: values saved by /setleaderboard and /setftd win over .env."""
    def g(k, d):
        v = kv_get(k)
        return int(v) if v not in (None, "", "None") else d
    return g("hub_chat", HUB_CHAT_ID), g("lb_topic", LB_TOPIC_ID), g("ftd_topic", FTD_TOPIC_ID)

def kv_get(k):
    c = db(); r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone(); c.close(); return r[0] if r else None
def kv_set(k, v):
    c = db(); c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v))); c.commit(); c.close()

async def refresh_board(ctx, repost=False):
    HUB, LBT, _ = hub_ids()
    if not (HUB and LBT): return
    text = render_board(month_key()); mid = kv_get("lb_msg")
    if mid and not repost:
        try:
            await ctx.bot.edit_message_text(text, HUB, int(mid)); return
        except Exception as e:
            if "not modified" in str(e).lower(): return
    m = await ctx.bot.send_message(HUB, text, message_thread_id=LBT)
    kv_set("lb_msg", m.message_id)
    try: await ctx.bot.pin_chat_message(HUB, m.message_id, disable_notification=True)
    except Exception: pass

async def record_ftd(ctx, aff_tg_id, member_id, source):
    """Returns (ok, message). Checks: member approved, came via this affiliate, has a UID, not counted before."""
    r = get(member_id); a = aff_by_id(aff_tg_id)
    if not r or not a: return False, "Couldn't find that client or affiliate."
    if r["status"] != "approved": return False, "That client hasn't been approved into the group yet."
    if not r["uid"]: return False, "That client has no TradeQuo UID on file."
    if source == "affiliate" and r["via_link"] != a["invite_link"]: return False, "That client didn't join through your link."
    month = month_key(); before = sum(1 for _ in ())
    c = db()
    if c.execute("SELECT 1 FROM ftds WHERE member_id=?", (member_id,)).fetchone():
        c.close(); return False, "That FTD has already been counted."
    before = c.execute("SELECT COUNT(*) FROM ftds WHERE aff_tg_id=? AND month=?", (aff_tg_id, month)).fetchone()[0]
    c.execute("INSERT INTO ftds(aff_tg_id, member_id, month, created_at, source) VALUES(?,?,?,?,?)", (aff_tg_id, member_id, month, now(), source))
    c.commit(); c.close()
    n = before + 1; emoji = ensure_emoji(aff_tg_id); who = f"@{a['username']}" if a["username"] else a["name"]
    client = (r["name"] or "Client").split()[0]; uid = str(r["uid"])
    HUB, _, FTDT = hub_ids()
    if HUB and FTDT:
        txt = f"✅ NEW FTD\n\n{emoji} {who}\n👤 {client} · UID ••{uid[-4:]}\n🔥 {n} this month · 💰 ${tier_for(n)} tier\n\n✔️ Verified by the bot"
        try: await ctx.bot.send_message(HUB, txt, message_thread_id=FTDT)
        except Exception as e: log.warning("ftd post failed: %s", e)
        if tier_for(n) != tier_for(before):
            try: await ctx.bot.send_message(HUB, f"🚀 {who} just hit the ${tier_for(n)} tier! Keep going 🔥", message_thread_id=FTDT)
            except Exception: pass
    await refresh_board(ctx)
    return True, f"FTD counted ✅ You're on {n} this month (${tier_for(n)} tier)."

def ftd_button(member_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📣 Send FTD", callback_data=f"ftd:{member_id}")]])

async def on_ftd_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; member_id = int(q.data.split(":")[1])
    ok, msg = await record_ftd(ctx, q.from_user.id, member_id, "affiliate")
    await q.answer(msg[:180], show_alert=not ok)
    if ok:
        try: await q.edit_message_text(q.message.text + "\n\n" + msg)
        except Exception: pass

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(render_board(month_key()))

async def cmd_setboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Run inside the Leaderboard topic: saves this group + topic, then posts and pins the board."""
    msg = update.effective_message; chat = update.effective_chat
    if not await is_admin_u(update, ctx):
        await msg.reply_text("Only admins can do that. (If you're posting anonymously as the group, turn 'Remain anonymous' off.)"); return
    if chat.type in ("group", "supergroup"):
        kv_set("hub_chat", chat.id)
        if msg.is_topic_message and msg.message_thread_id: kv_set("lb_topic", msg.message_thread_id)
        kv_set("lb_msg", "")
    try:
        await refresh_board(ctx, repost=True)
        await msg.reply_text("Leaderboard posted and pinned ✅")
    except Exception as e:
        log.warning("setleaderboard failed: %s", e)
        await msg.reply_text(f"Couldn't post the leaderboard: {e}\nMake sure I'm an admin here with Pin Messages on.")

async def cmd_setftd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Run inside the FTD topic: saves it as the FTD topic."""
    msg = update.effective_message; chat = update.effective_chat
    if not await is_admin_u(update, ctx):
        await msg.reply_text("Only admins can do that."); return
    if chat.type in ("group", "supergroup"):
        kv_set("hub_chat", chat.id)
        if msg.is_topic_message and msg.message_thread_id: kv_set("ftd_topic", msg.message_thread_id)
    await msg.reply_text("This is now the FTD topic ✅ Verified FTDs will be posted here.")

async def _find_aff(update, ctx):
    m = update.effective_message
    if m.reply_to_message and m.reply_to_message.from_user: return aff_by_id(m.reply_to_message.from_user.id)
    if ctx.args:
        c = db(); c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM affiliates WHERE lower(username)=? OR lower(name)=?", (ctx.args[0].lstrip("@").lower(), " ".join(ctx.args).lower())).fetchone(); c.close(); return r
    return None

async def cmd_addftd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: manual FTD for an affiliate (e.g. client who joined another way). Reply to them or /addftd @user."""
    if not await is_admin_u(update, ctx): return
    a = await _find_aff(update, ctx)
    if not a: await update.effective_message.reply_text("Reply to the affiliate's message or use /addftd @username"); return
    c = db(); c.execute("INSERT INTO ftds(aff_tg_id, member_id, month, created_at, source) VALUES(?,?,?,?,?)",
                        (a["tg_id"], -int(datetime.now().timestamp()*1000), month_key(), now(), "manual")); c.commit(); c.close()
    await refresh_board(ctx); await update.effective_message.reply_text(f"Added 1 FTD to {a['name']} ✅")

async def cmd_removeftd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_u(update, ctx): return
    a = await _find_aff(update, ctx)
    if not a: await update.effective_message.reply_text("Reply to the affiliate's message or use /removeftd @username"); return
    c = db(); r = c.execute("SELECT id FROM ftds WHERE aff_tg_id=? AND month=? ORDER BY id DESC LIMIT 1", (a["tg_id"], month_key())).fetchone()
    if r: c.execute("DELETE FROM ftds WHERE id=?", (r[0],)); c.commit()
    c.close(); await refresh_board(ctx)
    await update.effective_message.reply_text(f"Removed 1 FTD from {a['name']} ✅" if r else "They have no FTDs this month.")

async def month_rollover(ctx: ContextTypes.DEFAULT_TYPE):
    cur = month_key(); last = kv_get("lb_month")
    HUB, LBT, _ = hub_ids()
    if last and last != cur and HUB and LBT:
        try: await ctx.bot.send_message(HUB, render_board(last, final=True), message_thread_id=LBT)
        except Exception: pass
        kv_set("lb_month", cur); await refresh_board(ctx, repost=True)
    elif not last:
        kv_set("lb_month", cur)

# ---------- helpers ----------
_admin_cache = {}
async def admin_ok(ctx, uid: int) -> bool:
    """Anyone in ADMIN_IDS, or any admin/owner of the admin group, counts as an admin."""
    if uid in ADMIN_IDS: return True
    import time as _t
    hit = _admin_cache.get(uid)
    if hit and _t.time() - hit[1] < 300: return hit[0]
    ok = False
    try:
        m = await ctx.bot.get_chat_member(ADMIN_CHAT_ID, uid)
        ok = m.status in ("administrator", "creator")
    except Exception:
        ok = False
    _admin_cache[uid] = (ok, _t.time()); return ok

async def is_admin_u(update: Update, ctx) -> bool:
    u = update.effective_user; chat = update.effective_chat; msg = update.effective_message
    if msg is not None and msg.sender_chat is not None and chat is not None and msg.sender_chat.id == chat.id:
        return True   # anonymous group admin
    if not u: return False
    if await admin_ok(ctx, u.id): return True
    if chat is not None and chat.type in ("group", "supergroup"):
        try:
            m = await ctx.bot.get_chat_member(chat.id, u.id)
            return m.status in ("administrator", "creator")
        except Exception:
            return False
    return False

async def approve(ctx, user_id: int, by: str):
    await ctx.bot.approve_chat_join_request(NEW_GROUP_ID, user_id)
    c = db(); c.execute("UPDATE members SET status='approved', decided_at=?, decided_by=? WHERE user_id=?",
                        (now(), by, user_id)); c.commit(); c.close()
    r = get(user_id); aff = aff_by_link(r["via_link"]) if r and r["via_link"] else None
    if aff:
        try: await ctx.bot.send_message(aff["tg_id"],
                f"✅ {r['name']} just joined the new group through your link.\n\n"
                "Once they've made their first deposit, tap below to send the FTD. The bot checks it and updates the leaderboard 🏆",
                reply_markup=ftd_button(user_id))
        except Exception: pass
    try:
        await ctx.bot.send_message(user_id, "✅ You're in. Welcome to the new Inner Circle 🔥")
        await ctx.bot.send_message(user_id, TIERS_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass

def move_button(bot_username):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Move me to the new group",
                                  url=f"https://t.me/{bot_username}?start=move")]])

# ---------- old group: daily post ----------
async def daily_post(ctx: ContextTypes.DEFAULT_TYPE):
    c = db()
    row = c.execute("SELECT v FROM kv WHERE k='last_variant'").fetchone()
    last = int(row[0]) if row else -1
    choices = [i for i in range(len(VARIANTS)) if i != last]
    i = random.choice(choices)
    c.execute("INSERT OR REPLACE INTO kv VALUES('last_variant', ?)", (str(i),)); c.commit(); c.close()
    me = await ctx.bot.get_me()
    for tid in (OLD_TOPIC_IDS or [None]):
        try:
            await ctx.bot.send_message(OLD_GROUP_ID, VARIANTS[i], parse_mode=ParseMode.HTML,
                                       reply_markup=move_button(me.username), message_thread_id=tid)
        except Exception as e:
            log.warning("old group post failed in topic %s: %s", tid, e)
    log.info("posted variant %s to old group", i)

# ---------- DM: /start ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    r = get(u.id)
    if r and r["status"] == "approved":
        await update.message.reply_text("You're already in the new group ✅"); return
    upsert(u, source=(r["source"] if r and r["source"] else "old_group_button"))
    if await lookup(u.id):
        await update.message.reply_text(
            "You're verified ✅ Tap your join link (from CJ or John) and you'll be let straight in.\n"
            f"No link? Message {CONTACT}.")
        return
    upsert(u, status="awaiting_uid")
    await ask_account(u.id, ctx, "👋 Let's get you moved across to the new Inner Circle.\n"
                      "The new group is for TradeQuo members only.")

# ---------- new group: join requests ----------
async def on_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if req.chat.id != NEW_GROUP_ID:
        return
    u = req.from_user
    via = req.invite_link.invite_link if req.invite_link else "unknown"
    aff = aff_by_link(via)
    upsert(u, via_link=via, requested_at=now(), status="requested")
    if aff:
        upsert(u, upline=aff["name"])
    if await lookup(u.id):
        await approve(ctx, u.id, "auto"); return
    upsert(u, status="awaiting_uid")
    try:  # bots may DM a user who has an open join request
        intro = ("👋 Thanks for requesting to join the new Inner Circle"
                 + (f" through <b>{aff['name']}</b>" if aff else "") + ".\nThis group is for TradeQuo members only.")
        await ask_account(req.user_chat_id, ctx, intro)
    except Exception as e:
        log.warning("could not DM %s: %s", u.id, e)

# ---------- DM: UID replies ----------
async def on_dm_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    r = get(u.id)
    if not r or r["status"] not in ("awaiting_uid", "requested", "pending_admin"):
        await update.message.reply_text(f"Need a hand? Message {CONTACT}."); return
    m = UID_RE.match(update.message.text or "")
    if not m:
        await update.message.reply_text("That doesn't look like a UID. Please send just the number from your TradeQuo account.")
        return
    uid = m.group(1)
    upsert(u, uid=uid, status="pending_admin")
    if await lookup(u.id, uid):
        await approve(ctx, u.id, "auto"); return
    await update.message.reply_text("Got it ✅ We're checking your account now. You'll be let in shortly.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"ok:{u.id}"),
                                InlineKeyboardButton("❌ Reject", callback_data=f"no:{u.id}")]])
    handle = f"@{u.username}" if u.username else "(no username)"
    r = get(u.id)
    src_aff = aff_by_link(r["via_link"]) if r["via_link"] else None
    came_via = f"{src_aff['name']}'s invite link" if src_aff else (r["via_link"] or "old group button")
    upline = r["upline"] or "not chosen (already had TradeQuo)"
    await ctx.bot.send_message(
        ADMIN_CHAT_ID, message_thread_id=ADMIN_TOPIC_ID, text=
        f"🆕 <b>Join request</b>\n{u.full_name} {handle}\nTradeQuo UID: <code>{uid}</code>\n"
        f"Upline: {upline}\nCame via: {came_via}\n"
        "👉 Check this UID in the TradeQuo IB portal, then approve.",
        parse_mode=ParseMode.HTML, reply_markup=kb)

# ---------- admin buttons ----------
async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await admin_ok(ctx, q.from_user.id):
        await q.answer("Admins only", show_alert=True); return
    action, uid = q.data.split(":"); uid = int(uid)
    by = q.from_user.first_name
    r = get(uid)
    if action == "ok":
        if r and r["status"] in ("requested", "awaiting_uid", "pending_admin") and r["requested_at"]:
            try:
                await approve(ctx, uid, by)
                await q.edit_message_text(q.message.text_html + f"\n\n✅ Approved by {by}", parse_mode=ParseMode.HTML)
            except Exception as e:
                await q.answer(f"Couldn't approve: {e}", show_alert=True)
        else:
            # verified but hasn't requested yet -> remember, auto-approve when they do
            c = db(); c.execute("UPDATE members SET status='approved', decided_by=? WHERE user_id=?", (by, uid)); c.commit(); c.close()
            await ctx.bot.send_message(uid, "✅ You're verified. Tap CJ's or John's join link and you'll be let in.")
            await q.edit_message_text(q.message.text_html + f"\n\n✅ Verified by {by} (waiting for them to request)", parse_mode=ParseMode.HTML)
    else:
        c = db(); c.execute("UPDATE members SET status='rejected', decided_at=?, decided_by=? WHERE user_id=?", (now(), by, uid)); c.commit(); c.close()
        try: await ctx.bot.decline_chat_join_request(NEW_GROUP_ID, uid)
        except Exception: pass
        try: await ctx.bot.send_message(uid, f"We couldn't find that UID under our TradeQuo links. Message {CONTACT} and we'll sort it.")
        except Exception: pass
        await q.edit_message_text(q.message.text_html + f"\n\n❌ Rejected by {by}", parse_mode=ParseMode.HTML)
    await q.answer()

# ---------- admin commands ----------
async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_u(update, ctx): return
    c = db()
    rows = c.execute("SELECT status, COUNT(*) FROM members GROUP BY status").fetchall()
    links = c.execute("""SELECT COALESCE(a.name, m.via_link, 'old group button'), COUNT(*) FROM members m
                         LEFT JOIN affiliates a ON a.invite_link = m.via_link WHERE m.status='approved' GROUP BY 1""").fetchall()
    c.close()
    txt = "📊 <b>Migration status</b>\n" + "\n".join(f"{s}: {n}" for s, n in rows)
    txt += "\n\n<b>Approved by link</b>\n" + ("\n".join(f"{l}: {n}" for l, n in links) or "none yet")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_u(update, ctx): return
    c = db(); rows = c.execute("SELECT * FROM members").fetchall()
    cols = [d[0] for d in c.execute("SELECT * FROM members LIMIT 0").description]; c.close()
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(cols); w.writerows(rows)
    await update.message.reply_document(io.BytesIO(buf.getvalue().encode()), filename="migration.csv")

async def post_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_u(update, ctx): return
    await daily_post(ctx); await update.message.reply_text("Posted ✅")

# ---------- main ----------
def main():
    missing = [k for k in ("BOT_TOKEN", "OLD_GROUP_ID", "NEW_GROUP_ID", "ADMIN_CHAT_ID") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("affiliate", cmd_affiliate))
    app.add_handler(CommandHandler("tradequo", cmd_tradequo))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CommandHandler("affiliates", cmd_affiliates))
    app.add_handler(CallbackQueryHandler(on_affiliate_button, pattern=r"^aff(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(on_ftd_button, pattern=r"^ftd:\d+$"))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("setleaderboard", cmd_setboard))
    app.add_handler(CommandHandler("setftd", cmd_setftd))
    app.add_handler(CommandHandler("addftd", cmd_addftd))
    app.add_handler(CommandHandler("removeftd", cmd_removeftd))
    app.job_queue.run_repeating(month_rollover, interval=600, first=10, name="month_rollover")
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(on_account_button, pattern=r"^acct:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(on_upline_button, pattern=r"^up:(\d+|none)$"))
    app.add_handler(CommandHandler("tiers", lambda u, c: u.message.reply_text(TIERS_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_dm_text))
    hh, mm = map(int, POST_TIME.split(":"))
    app.job_queue.run_daily(daily_post, time(hh, mm, tzinfo=TZ), name="daily_post")
    log.info("bot running; daily post at %s Europe/London; verify=%s", POST_TIME, "on" if VERIFY_URL else "manual")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_join_request"])

if __name__ == "__main__":
    main()
