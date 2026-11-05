"""Base de données SQLite du panel : comptes, bots, variables, modèles de serveurs, journal."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,           -- ID Discord
    name        TEXT NOT NULL,
    avatar      TEXT,
    role        TEXT NOT NULL DEFAULT 'pending', -- admin, user, pending, blocked
    max_bots    INTEGER NOT NULL,
    max_ram_mb  INTEGER NOT NULL,
    max_disk_mb INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    last_login  INTEGER
);
CREATE TABLE IF NOT EXISTS bots (
    id           TEXT PRIMARY KEY,
    owner_id     INTEGER NOT NULL REFERENCES users(id),
    name         TEXT NOT NULL,
    language     TEXT NOT NULL,              -- python, node, custom
    image        TEXT NOT NULL,
    main_file    TEXT NOT NULL,
    startup_cmd  TEXT NOT NULL DEFAULT '',   -- vide = commande par défaut du langage
    ram_mb       INTEGER NOT NULL,
    cpu_percent  INTEGER NOT NULL,
    auto_restart INTEGER NOT NULL DEFAULT 1,
    wanted_running INTEGER NOT NULL DEFAULT 0,  -- le panel le redémarre s'il tombe (et au démarrage du panel)
    git_url      TEXT NOT NULL DEFAULT '',
    git_branch   TEXT NOT NULL DEFAULT '',
    git_token    TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_env (
    bot_id TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    secret INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, key)
);
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    bot_id  TEXT,
    action  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_bot ON activity (bot_id, id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,               -- SHA-256 du jeton du cookie (le jeton lui-même est jamais gardé)
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
"""

BOT_FIELDS = ("name", "language", "image", "main_file", "startup_cmd", "ram_mb", "cpu_percent",
              "auto_restart", "wanted_running", "git_url", "git_branch", "git_token", "port")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.executescript(SCHEMA)
        # Mises à jour de la base (anciennes installations)
        cols = {row[1] for row in await (await self.conn.execute("PRAGMA table_info(bots)")).fetchall()}
        if "port" not in cols:
            await self.conn.execute("ALTER TABLE bots ADD COLUMN port INTEGER")
        await self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_port ON bots (port)")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def _one(self, q: str, a: tuple = ()) -> dict | None:
        async with self.conn.execute(q, a) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, q: str, a: tuple = ()) -> list[dict]:
        async with self.conn.execute(q, a) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _run(self, q: str, a: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(q, a)
        await self.conn.commit()
        return cur

    # ---- Comptes -------------------------------------------------------------------
    async def get_user(self, user_id: int) -> dict | None:
        return await self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    async def list_users(self) -> list[dict]:
        return await self._all("SELECT * FROM users ORDER BY role = 'pending' DESC, name COLLATE NOCASE")

    async def upsert_login(self, user_id: int, name: str, avatar: str | None, role: str, limits: tuple[int, int, int]) -> dict:
        """Crée le compte à la première connexion (avec le rôle donné), sinon met à jour le nom et l'avatar."""
        now = int(time.time())
        if await self.get_user(user_id) is None:
            await self._run(
                "INSERT INTO users (id, name, avatar, role, max_bots, max_ram_mb, max_disk_mb, created_at, last_login) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, name, avatar, role, *limits, now, now),
            )
        else:
            await self._run("UPDATE users SET name = ?, avatar = ?, last_login = ? WHERE id = ?", (name, avatar, now, user_id))
        return await self.get_user(user_id)

    async def add_user(self, user_id: int, role: str, limits: tuple[int, int, int]) -> dict:
        if await self.get_user(user_id) is None:
            await self._run(
                "INSERT INTO users (id, name, role, max_bots, max_ram_mb, max_disk_mb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, f"Utilisateur {user_id}", role, *limits, int(time.time())),
            )
        return await self.get_user(user_id)

    async def update_user(self, user_id: int, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("role", "max_bots", "max_ram_mb", "max_disk_mb")}
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            await self._run(f"UPDATE users SET {sets} WHERE id = ?", (*allowed.values(), user_id))
        return await self.get_user(user_id)

    # ---- Sessions ------------------------------------------------------------------------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def create_session(self, user_id: int, days: int = 30) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        await self._run("DELETE FROM sessions WHERE expires_at < ?", (now,))
        await self._run("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                        (self._hash(token), user_id, now + days * 86400))
        return token

    async def session_user(self, token: str) -> dict | None:
        return await self._one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
            (self._hash(token), int(time.time())),
        )

    async def delete_session(self, token: str) -> None:
        await self._run("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))

    async def delete_user_sessions(self, user_id: int) -> None:
        await self._run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # ---- Bots ---------------------------------------------------------------------------
    async def create_bot(self, owner_id: int, **fields) -> dict:
        bot_id = secrets.token_hex(4)
        values = {k: fields[k] for k in BOT_FIELDS if k in fields}
        cols = ", ".join(("id", "owner_id", "created_at", *values))
        marks = ", ".join("?" * (3 + len(values)))
        await self._run(f"INSERT INTO bots ({cols}) VALUES ({marks})", (bot_id, owner_id, int(time.time()), *values.values()))
        return await self.get_bot(bot_id)

    async def get_bot(self, bot_id: str) -> dict | None:
        return await self._one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    async def list_bots(self, owner_id: int | None = None) -> list[dict]:
        if owner_id is None:
            return await self._all("SELECT * FROM bots ORDER BY name COLLATE NOCASE")
        return await self._all("SELECT * FROM bots WHERE owner_id = ? ORDER BY name COLLATE NOCASE", (owner_id,))

    async def update_bot(self, bot_id: str, **fields) -> dict | None:
        values = {k: v for k, v in fields.items() if k in BOT_FIELDS}
        if values:
            sets = ", ".join(f"{k} = ?" for k in values)
            await self._run(f"UPDATE bots SET {sets} WHERE id = ?", (*values.values(), bot_id))
        return await self.get_bot(bot_id)

    async def delete_bot(self, bot_id: str) -> None:
        await self._run("DELETE FROM bots WHERE id = ?", (bot_id,))

    # ---- Variables d'environnement -----------------------------------------------------
    async def get_env(self, bot_id: str) -> list[dict]:
        return await self._all("SELECT key, value, secret FROM bot_env WHERE bot_id = ? ORDER BY key", (bot_id,))

    async def set_env(self, bot_id: str, items: list[tuple[str, str, bool]]) -> None:
        """Remplace toutes les variables du bot."""
        await self.conn.execute("DELETE FROM bot_env WHERE bot_id = ?", (bot_id,))
        await self.conn.executemany(
            "INSERT INTO bot_env (bot_id, key, value, secret) VALUES (?, ?, ?, ?)",
            [(bot_id, k, v, int(s)) for k, v, s in items],
        )
        await self.conn.commit()

    # ---- Modèles de serveurs Discord ---------------------------------------------------
    async def list_templates(self, owner_id: int) -> list[dict]:
        return await self._all("SELECT id, name, updated_at FROM templates WHERE owner_id = ? ORDER BY name", (owner_id,))

    async def get_template(self, owner_id: int, template_id: int) -> dict | None:
        return await self._one("SELECT * FROM templates WHERE id = ? AND owner_id = ?", (template_id, owner_id))

    async def save_template(self, owner_id: int, name: str, content: str, template_id: int | None = None) -> int:
        now = int(time.time())
        if template_id and await self.get_template(owner_id, template_id):
            await self._run("UPDATE templates SET name = ?, content = ?, updated_at = ? WHERE id = ?", (name, content, now, template_id))
            return template_id
        cur = await self._run(
            "INSERT INTO templates (owner_id, name, content, updated_at) VALUES (?, ?, ?, ?)", (owner_id, name, content, now)
        )
        return cur.lastrowid

    async def delete_template(self, owner_id: int, template_id: int) -> None:
        await self._run("DELETE FROM templates WHERE id = ? AND owner_id = ?", (template_id, owner_id))

    # ---- Journal d'activité ----------------------------------------------------------------
    async def log(self, user_id: int | None, action: str, *, bot_id: str | None = None, detail: str | dict = "") -> None:
        text = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail
        await self._run(
            "INSERT INTO activity (user_id, bot_id, action, detail, at) VALUES (?, ?, ?, ?, ?)",
            (user_id, bot_id, action, text[:500], int(time.time())),
        )

    async def recent_activity(self, limit: int = 50) -> list[dict]:
        return await self._all(
            "SELECT a.*, u.name AS user_name, b.name AS bot_name FROM activity a LEFT JOIN users u ON u.id = a.user_id "
            "LEFT JOIN bots b ON b.id = a.bot_id ORDER BY a.id DESC LIMIT ?", (limit,))

    async def activity(self, bot_id: str, limit: int = 30) -> list[dict]:
        return await self._all(
            "SELECT a.*, u.name AS user_name FROM activity a LEFT JOIN users u ON u.id = a.user_id "
            "WHERE a.bot_id = ? ORDER BY a.id DESC LIMIT ?",
            (bot_id, limit),
        )
