"""SQLite storage for partners and their FTDs."""
from __future__ import annotations

import random
from dataclasses import dataclass

import aiosqlite

# Each partner is given one of these when they are added, and keeps it.
EMOJI_POOL = (
    "🦁", "🐺", "🦊", "🐯", "🐻", "🐼", "🐨", "🦅", "🦈", "🐉", "🦄", "🐆", "🦍", "🐘", "🦬", "🐎",
    "🦉", "🐬", "🐙", "🦂", "🐝", "🦋", "🐲", "🦖", "🐊", "🦩", "🦚", "🐧", "🐳", "🦜", "🐸", "🦦",
    "⚡", "💎", "🚀", "🔱", "⚔️", "🛡️", "👑", "🎯", "🧨", "🌪️", "☄️", "🌋", "🌊", "❄️", "🌙", "☀️",
    "🍀", "🌵", "🔮", "🎲", "🏹", "🪐", "🧿", "💠", "🗡️", "🪙", "🏎️", "🛸", "🎩", "🧲",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    emoji       TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    joined_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ftds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    month       TEXT NOT NULL,            -- 'YYYY-MM' in the hub's timezone
    message_id  INTEGER,
    deposit     TEXT,
    strategy    TEXT,
    removed     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (message_id)
);
CREATE INDEX IF NOT EXISTS idx_ftds_month ON ftds(month, removed);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass(frozen=True)
class Row:
    user_id: int
    username: str | None
    first_name: str | None
    count: int
    emoji: str | None = None

    @property
    def name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or f"Partner {self.user_id}"


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        async with self._conn.execute("PRAGMA table_info(partners)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        if "emoji" not in cols:  # databases created before emojis existed
            await self._conn.execute("ALTER TABLE partners ADD COLUMN emoji TEXT")
        await self._conn.commit()
        async with self._conn.execute("SELECT user_id FROM partners WHERE emoji IS NULL") as cur:
            missing = [r["user_id"] for r in await cur.fetchall()]
        for uid in missing:
            await self._assign_emoji(uid)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    # partners ---------------------------------------------------------------
    async def upsert_partner(self, user_id: int, username: str | None, first_name: str | None,
                             active: bool = True) -> bool:
        """Insert or refresh a partner. Returns True if they were new."""
        async with self.c.execute("SELECT active FROM partners WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        await self.c.execute(
            """INSERT INTO partners (user_id, username, first_name, active) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,
               first_name = excluded.first_name, active = excluded.active""",
            (user_id, username, first_name, 1 if active else 0),
        )
        await self.c.commit()
        await self._assign_emoji(user_id)
        return row is None or (active and row["active"] == 0)

    async def _assign_emoji(self, user_id: int) -> None:
        """Give a partner a random emoji (unused by others where possible), once."""
        async with self.c.execute("SELECT emoji FROM partners WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None or row["emoji"]:
            return
        async with self.c.execute("SELECT emoji FROM partners WHERE emoji IS NOT NULL") as cur:
            used = {r["emoji"] for r in await cur.fetchall()}
        choices = [e for e in EMOJI_POOL if e not in used] or list(EMOJI_POOL)
        await self.c.execute("UPDATE partners SET emoji = ? WHERE user_id = ?", (random.choice(choices), user_id))
        await self.c.commit()

    async def set_active(self, user_id: int, active: bool) -> None:
        await self.c.execute("UPDATE partners SET active = ? WHERE user_id = ?", (1 if active else 0, user_id))
        await self.c.commit()

    async def find_by_username(self, username: str) -> int | None:
        async with self.c.execute(
            "SELECT user_id FROM partners WHERE lower(username) = lower(?)", (username.lstrip("@"),)
        ) as cur:
            row = await cur.fetchone()
        return row["user_id"] if row else None

    # ftds -------------------------------------------------------------------
    async def add_ftd(self, user_id: int, month: str, message_id: int | None,
                      deposit: str | None, strategy: str | None) -> bool:
        cur = await self.c.execute(
            """INSERT INTO ftds (user_id, month, message_id, deposit, strategy) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(message_id) DO NOTHING""",
            (user_id, month, message_id, deposit, strategy),
        )
        await self.c.commit()
        return cur.rowcount > 0

    async def remove_ftd_by_message(self, message_id: int) -> int | None:
        """Mark the FTD logged from this message as removed. Returns its partner's id."""
        async with self.c.execute(
            "SELECT user_id FROM ftds WHERE message_id = ? AND removed = 0", (message_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self.c.execute("UPDATE ftds SET removed = 1 WHERE message_id = ?", (message_id,))
        await self.c.commit()
        return row["user_id"]

    async def remove_latest_ftd(self, user_id: int, month: str) -> bool:
        async with self.c.execute(
            "SELECT id FROM ftds WHERE user_id = ? AND month = ? AND removed = 0 ORDER BY id DESC LIMIT 1",
            (user_id, month),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        await self.c.execute("UPDATE ftds SET removed = 1 WHERE id = ?", (row["id"],))
        await self.c.commit()
        return True

    async def count(self, user_id: int, month: str) -> int:
        async with self.c.execute(
            "SELECT COUNT(*) AS n FROM ftds WHERE user_id = ? AND month = ? AND removed = 0", (user_id, month)
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0

    async def standings(self, month: str) -> list[Row]:
        """Every active partner (plus anyone with FTDs this month), best first."""
        async with self.c.execute(
            """SELECT p.user_id, p.username, p.first_name, p.emoji,
                      COALESCE(SUM(CASE WHEN f.removed = 0 THEN 1 ELSE 0 END), 0) AS n
               FROM partners p
               LEFT JOIN ftds f ON f.user_id = p.user_id AND f.month = ?
               GROUP BY p.user_id
               HAVING p.active = 1 OR n > 0
               ORDER BY n DESC, lower(COALESCE(p.username, p.first_name, '')) ASC""",
            (month,),
        ) as cur:
            rows = await cur.fetchall()
        return [Row(r["user_id"], r["username"], r["first_name"], r["n"], r["emoji"]) for r in rows]

    # meta -------------------------------------------------------------------
    async def get_meta(self, key: str) -> str | None:
        async with self.c.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.c.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.c.commit()
