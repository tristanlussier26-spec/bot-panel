"""Réglages du panel, lus dans les variables d'environnement (fichier .env)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.isdigit()}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")).resolve())
    host_data_dir: Path | None = field(default_factory=lambda: Path(p) if (p := os.getenv("HOST_DATA_DIR")) else None)
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/"))
    discord_client_id: str = field(default_factory=lambda: os.getenv("DISCORD_CLIENT_ID", "").strip())
    discord_client_secret: str = field(default_factory=lambda: os.getenv("DISCORD_CLIENT_SECRET", "").strip())
    admin_ids: set[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    bot_network: str = field(default_factory=lambda: os.getenv("BOT_NETWORK", "botpanel-bots"))
    python_image: str = field(default_factory=lambda: os.getenv("PYTHON_IMAGE", "python:3.13-slim"))
    node_image: str = field(default_factory=lambda: os.getenv("NODE_IMAGE", "node:22-slim"))
    discord_api: str = field(default_factory=lambda: os.getenv("DISCORD_API", "https://discord.com/api/v10").rstrip("/"))
    # Images permises aux utilisateurs (l'admin peut mettre n'importe laquelle)
    allowed_images: list[str] = field(default_factory=lambda: [x.strip() for x in os.getenv(
        "ALLOWED_IMAGES", "python:3.13-slim,python:3.12-slim,python:3.11-slim,node:22-slim,node:20-slim").split(",") if x.strip()])
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 100))
    # Ports que le panel peut ouvrir pour les bots qui ont un site web ou une API (comme les « allocations » de Pterodactyl)
    port_range: str = field(default_factory=lambda: os.getenv("PORT_RANGE", "25500-25599"))

    @property
    def ports(self) -> range:
        try:
            lo, hi = (int(x) for x in self.port_range.split("-", 1))
            if 1024 <= lo <= hi <= 65535:
                return range(lo, hi + 1)
        except ValueError:
            pass
        return range(25500, 25600)

    @property
    def public_host(self) -> str:
        from urllib.parse import urlsplit
        return urlsplit(self.public_url).hostname or "localhost"
    # Limites par défaut d'un nouveau compte (l'admin peut les changer compte par compte)
    default_max_bots: int = field(default_factory=lambda: _int("DEFAULT_MAX_BOTS", 3))
    default_max_ram: int = field(default_factory=lambda: _int("DEFAULT_MAX_RAM_MB", 1024))
    default_max_disk: int = field(default_factory=lambda: _int("DEFAULT_MAX_DISK_MB", 2048))

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def bots_dir(self) -> Path:
        return self.data_dir / "bots"

    def load_secret(self) -> str:
        """Clé de signature des sessions : SECRET_KEY, sinon générée une fois et gardée dans data/."""
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / "secret.key"
        if not path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32))
            path.chmod(0o600)
        self.secret_key = path.read_text().strip()
        return self.secret_key
