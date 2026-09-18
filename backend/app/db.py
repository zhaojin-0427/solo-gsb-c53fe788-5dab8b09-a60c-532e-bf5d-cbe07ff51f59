"""SQLite persistence. The event log is the source of truth; rows are append-only.

Concurrency model
-----------------
* One writer at a time, guarded by an asyncio.Lock inside Engine (same process).
* Every state-changing operation runs in a single BEGIN IMMEDIATE transaction,
  which gives cross-process atomicity as well.
* Readers (snapshots/replays/exports) never block thanks to WAL mode.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS drills (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id        TEXT PRIMARY KEY,
    drill_id  TEXT NOT NULL REFERENCES drills(id),
    status    TEXT NOT NULL DEFAULT 'created',   -- created|running|ended
    reason    TEXT,                              -- terminal|complete|deadline|host
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    role_id      TEXT NOT NULL,
    display_name TEXT NOT NULL,
    token        TEXT NOT NULL,
    joined_at    TEXT NOT NULL,
    PRIMARY KEY (session_id, role_id),
    UNIQUE (session_id, token)
);

CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version    INTEGER NOT NULL,
    seq        INTEGER NOT NULL,               -- ordering inside one version commit
    type       TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',       -- role id, 'host' or 'system'
    at         TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, version, seq)
);

-- Duplicate submit guard (choice events are also the audit record).
CREATE TABLE IF NOT EXISTS submissions (
    session_id TEXT NOT NULL,
    version    INTEGER NOT NULL,
    role_id    TEXT NOT NULL,
    stage_id   TEXT NOT NULL,
    choice_id  TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (session_id, stage_id, role_id)
);
"""


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()

    # ---------- low level helpers (connection in autocommit mode) ----------
    async def begin(self) -> None:
        await self.db.execute("BEGIN IMMEDIATE")

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()

    async def all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()
