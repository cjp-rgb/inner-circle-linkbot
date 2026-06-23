"""Entry point: wires config, DB, and handlers into a running bot."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
)

from bot.config import Config
from bot.db import Database
from bot.handlers.leaderboard import (
    leaderboard_command,
    post_scheduled_leaderboard,
    refresh_leaderboard_message,
)
from bot.handlers.links import getmylink, start
from bot.handlers.referrals import track_join

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


async def _post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]
    db = Database(config.db_path)
    await db.connect()
    app.bot_data["db"] = db
    app.bot_data["event_name"] = config.event_name

    await app.bot.set_my_commands(
        [
            ("getmylink", "Get your personal referral link (sent privately)"),
            ("start", "Start the bot and receive your referral link"),
            ("leaderboard", "See the current standings"),
        ]
    )
    # Make sure the leaderboard message exists / is up to date on boot.
    await refresh_leaderboard_message(app)

    # Schedule the twice-daily leaderboard posts.
    if config.leaderboard_chat_id is not None and app.job_queue is not None:
        for t in config.leaderboard_times:
            app.job_queue.run_daily(
                post_scheduled_leaderboard,
                time=t,
                name=f"leaderboard-{t.strftime('%H:%M')}",
            )
        posted = ", ".join(t.strftime("%H:%M %Z") for t in config.leaderboard_times)
        log.info("Scheduled leaderboard posts at: %s", posted or "(none)")

    log.info("Bot initialised. Group=%s leaderboard=%s", config.group_id, config.leaderboard_chat_id)


async def _post_shutdown(app: Application) -> None:
    db: Database | None = app.bot_data.get("db")
    if db is not None:
        await db.close()


def build_application(config: Config) -> Application:
    app = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getmylink", getmylink))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(ChatMemberHandler(track_join, ChatMemberHandler.CHAT_MEMBER))
    return app


def main() -> None:
    # Python 3.14 no longer auto-creates an event loop for the main thread, but
    # Application.run_polling() still calls asyncio.get_event_loop(). Provide one.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    config = Config.from_env()
    app = build_application(config)
    # CHAT_MEMBER updates are not delivered unless explicitly requested.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
