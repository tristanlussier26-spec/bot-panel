"""Données du plugin (SQLite dans son dossier de données) : comptes liés et petites valeurs."""
from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS ng_links (
    guild_id   INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    ng_name    TEXT NOT NULL COLLATE NOCASE,
    linked_at  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, discord_id),
    UNIQUE (guild_id, ng_name)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def _one(self, query: str, args: tuple = ()) -> dict | None:
        async with self.conn.execute(query, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, query: str, args: tuple = ()) -> list[dict]:
        async with self.conn.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _run(self, query: str, args: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(query, args)
        await self.conn.commit()
        return cur

    # ---- Petites valeurs (messages à garder à jour, membres du tour d'avant) ----------------
    async def get_value(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_value(self, key: str, value: str) -> None:
        await self._run("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # ---- Comptes liés -------------------------------------------------------------------------
    async def get_link(self, guild_id: int, discord_id: int) -> dict | None:
        return await self._one("SELECT * FROM ng_links WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))

    async def get_link_by_name(self, guild_id: int, ng_name: str) -> dict | None:
        return await self._one("SELECT * FROM ng_links WHERE guild_id = ? AND ng_name = ?", (guild_id, ng_name))

    async def all_links(self, guild_id: int) -> list[dict]:
        return await self._all("SELECT * FROM ng_links WHERE guild_id = ? ORDER BY linked_at", (guild_id,))

    async def add_link(self, guild_id: int, discord_id: int, ng_name: str, linked_at: int | None = None) -> None:
        await self._run(
            "INSERT OR REPLACE INTO ng_links (guild_id, discord_id, ng_name, linked_at) VALUES (?, ?, ?, ?)",
            (guild_id, discord_id, ng_name, int(linked_at or time.time())),
        )

    async def rename_link(self, guild_id: int, discord_id: int, ng_name: str) -> None:
        await self._run("UPDATE ng_links SET ng_name = ? WHERE guild_id = ? AND discord_id = ?", (ng_name, guild_id, discord_id))

    async def remove_link(self, guild_id: int, discord_id: int) -> bool:
        cur = await self._run("DELETE FROM ng_links WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
        return cur.rowcount > 0
