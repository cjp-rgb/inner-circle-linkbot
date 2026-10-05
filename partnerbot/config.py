"""Configuration for the Partner Hub bot, read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Monthly tiers: (minimum FTDs, rate in USD). Highest matching tier wins.
TIERS: tuple[tuple[int, int], ...] = ((20, 300), (15, 250), (10, 200), (5, 175), (1, 150))


def tier_rate(count: int) -> int | None:
    for minimum, rate in TIERS:
        if count >= minimum:
            return rate
    return None


@dataclass(frozen=True)
class Config:
    bot_token: str
    group_id: int
    db_path: str
    tz: ZoneInfo
    hub_name: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is not set.")
        group_raw = os.getenv("GROUP_ID", "").strip()
        if not group_raw:
            raise RuntimeError("GROUP_ID is not set (the Partner Hub chat ID, starting -100).")
        try:
            group_id = int(group_raw)
        except ValueError as exc:
            raise RuntimeError(f"GROUP_ID must be a number, got {group_raw!r}") from exc
        return cls(
            bot_token=token,
            group_id=group_id,
            db_path=os.getenv("DB_PATH", "partners.db").strip() or "partners.db",
            tz=ZoneInfo(os.getenv("TZ_NAME", "Europe/London").strip() or "Europe/London"),
            hub_name=os.getenv("HUB_NAME", "Partner Hub").strip() or "Partner Hub",
        )
