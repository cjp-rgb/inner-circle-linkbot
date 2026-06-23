"""Helper: print the chat ID of any group the bot can see.

Run this, then add the bot to your group (as admin) and send a message there.
The script will print the real numeric chat ID to paste into .env as GROUP_ID.

    .venv/bin/python -m bot.get_chat_id
"""
from __future__ import annotations

import asyncio

from telegram import Bot

from bot.config import Config


async def main() -> None:
    config = Config.from_env()
    bot = Bot(config.bot_token)
    print("Listening… add the bot to your group as admin and send a message there.")
    print("Press Ctrl+C to stop.\n")
    seen: set[int] = set()
    offset: int | None = None
    async with bot:
        me = await bot.get_me()
        print(f"Bot: @{me.username}\n")
        while True:
            updates = await bot.get_updates(
                offset=offset,
                timeout=30,
                allowed_updates=["message", "my_chat_member", "chat_member"],
            )
            for u in updates:
                offset = u.update_id + 1
                chat = None
                for part in (u.message, u.my_chat_member, u.chat_member):
                    if part is not None:
                        chat = part.chat
                        break
                if chat is not None and chat.id not in seen:
                    seen.add(chat.id)
                    print(f"  chat id = {chat.id}   type={chat.type}   title={chat.title!r}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
