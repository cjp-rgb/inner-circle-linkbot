"""Async SQLite persistence layer for the referral bot."""
from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    invite_link    TEXT UNIQUE,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS referrals (
    invited_id     INTEGER PRIMARY KEY,   -- one credit per invited person
    referrer_id    INTEGER NOT NULL,
    joined_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (referrer_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_invite_link ON users(invite_link);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class LeaderboardRow:
    user_id: int
    username: str | None
    first_name: str | None
    referrals: int

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or f"User {self.user_id}"


class Database:
    """Thin wrapper around an aiosqlite connection."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    # --- users ---------------------------------------------------------------

    async def upsert_user(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> None:
        """Insert a user or refresh their cached name/username."""
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
        )
        await self.conn.commit()

    async def set_invite_link(self, user_id: int, invite_link: str) -> None:
        await self.conn.execute(
            "UPDATE users SET invite_link = ? WHERE user_id = ?",
            (invite_link, user_id),
        )
        await self.conn.commit()

    async def get_invite_link(self, user_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT invite_link FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["invite_link"] if row else None

    async def get_referrer_by_invite_link(self, invite_link: str) -> int | None:
        async with self.conn.execute(
            "SELECT user_id FROM users WHERE invite_link = ?", (invite_link,)
        ) as cur:
            row = await cur.fetchone()
        return row["user_id"] if row else None

    # --- meta key/value ------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.conn.commit()

    # --- referrals -----------------------------------------------------------

    async def add_referral(self, invited_id: int, referrer_id: int) -> bool:
        """Credit a referral. Returns True if newly recorded, False if a dupe.

        Self-referrals are ignored.
        """
        if invited_id == referrer_id:
            return False
        cur = await self.conn.execute(
            """
            INSERT INTO referrals (invited_id, referrer_id)
            VALUES (?, ?)
            ON CONFLICT(invited_id) DO NOTHING
            """,
            (invited_id, referrer_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_referral_count(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM referrals WHERE referrer_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0

    async def get_rank(self, user_id: int) -> int | None:
        """1-based rank of a user by referral count, or None if they have zero."""
        count = await self.get_referral_count(user_id)
        if count == 0:
            return None
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS ahead FROM (
                SELECT referrer_id, COUNT(*) AS n
                FROM referrals
                GROUP BY referrer_id
                HAVING n > ?
            )
            """,
            (count,),
        ) as cur:
            row = await cur.fetchone()
        return (row["ahead"] if row else 0) + 1

    async def get_leaderboard(self, limit: int = 10) -> list[LeaderboardRow]:
        async with self.conn.execute(
            """
            SELECT u.user_id, u.username, u.first_name, COUNT(r.invited_id) AS referrals
            FROM users u
            JOIN referrals r ON r.referrer_id = u.user_id
            GROUP BY u.user_id
            ORDER BY referrals DESC, u.created_at ASC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            LeaderboardRow(
                user_id=row["user_id"],
                username=row["username"],
                first_name=row["first_name"],
                referrals=row["referrals"],
            )
            for row in rows
        ]
