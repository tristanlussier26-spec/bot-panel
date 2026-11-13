"""Base SQLite du bot (data/bot.db) : réglages de chaque serveur, et une base à part pour chaque plugin."""
from __future__ import annotations

from pathlib import Path

import aiosqlite

KV_SCHEMA = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);"


class Database:
    """Une base SQLite avec quelques raccourcis. Les plugins en reçoivent une à eux (plugin_host.open_db)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self, schema: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(KV_SCHEMA + "\n" + schema)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def one(self, query: str, args: tuple = ()) -> dict | None:
        async with self.conn.execute(query, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, query: str, args: tuple = ()) -> list[dict]:
        async with self.conn.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def run(self, query: str, args: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(query, args)
        await self.conn.commit()
        return cur

    # ---- Petites valeurs -----------------------------------------------------------------------
    async def get_value(self, key: str) -> str | None:
        row = await self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_value(self, key: str, value: str) -> None:
        await self.run("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    async def delete_value(self, key: str) -> None:
        await self.run("DELETE FROM kv WHERE key = ?", (key,))

    async def values_with_prefix(self, prefix: str) -> dict[str, str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = await self.all("SELECT key, value FROM kv WHERE key LIKE ? ESCAPE '\\'", (escaped + "%",))
        return {r["key"]: r["value"] for r in rows}
