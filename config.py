"""Configuration loaded from the environment (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _parse_times(raw: str, tz: ZoneInfo) -> tuple[time, ...]:
    """Parse 'HH:MM,HH:MM' into tz-aware time objects."""
    out: list[time] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        hh, _, mm = chunk.partition(":")
        out.append(time(hour=int(hh), minute=int(mm or 0), tzinfo=tz))
    return tuple(out)


@dataclass(frozen=True)
class Config:
    bot_token: str
    group_id: int
    leaderboard_chat_id: int | None
    db_path: str
    event_name: str
    leaderboard_times: tuple[time, ...]

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

        group_raw = os.getenv("GROUP_ID", "").strip()
        if not group_raw:
            raise RuntimeError("GROUP_ID is not set. See .env.example.")
        try:
            group_id = int(group_raw)
        except ValueError as exc:
            raise RuntimeError(f"GROUP_ID must be a number, got {group_raw!r}") from exc

        lb_raw = os.getenv("LEADERBOARD_CHAT_ID", "").strip()
        leaderboard_chat_id = int(lb_raw) if lb_raw else None

        tz = ZoneInfo(os.getenv("LEADERBOARD_TZ", "Europe/London").strip() or "Europe/London")
        times_raw = os.getenv("LEADERBOARD_TIMES", "13:00,20:00").strip() or "13:00,20:00"
        leaderboard_times = _parse_times(times_raw, tz)

        return cls(
            bot_token=token,
            group_id=group_id,
            leaderboard_chat_id=leaderboard_chat_id,
            db_path=os.getenv("DB_PATH", "referrals.db").strip() or "referrals.db",
            event_name=os.getenv("EVENT_NAME", "Webinar").strip() or "Webinar",
            leaderboard_times=leaderboard_times,
        )
