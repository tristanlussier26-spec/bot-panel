"""Modèles de bots (comme les « eggs » de Pterodactyl) : un bot prêt à rouler, à créer en un clic.

Chaque modèle est un dossier de templates/ avec un template.json :
  {"format": 1, "id": "bot-ng", "name": "Bot NG", "version": "1.0.0", "description": "…",
   "language": "python", "main_file": "bot.py", "plugins": ["nationsglory"]}
Ses fichiers sont copiés dans le dossier du bot ; les plugins listés sont installés depuis le catalogue.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from botpanel.files import BotFiles

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "templates"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SKIP_DIRS = {"__pycache__", "data", "plugins", ".botpanel", ".git"}
SKIP_FILES = {"template.json", ".env"}
MAX_FILES = 500


class KitError(Exception):
    """Erreur affichable à l'utilisateur."""


def _read(folder: Path) -> dict | None:
    try:
        raw = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("format") != 1 or raw.get("id") != folder.name or not ID_RE.match(folder.name):
        return None
    if raw.get("language") not in ("python", "node") or not isinstance(raw.get("main_file"), str):
        return None
    plugins = [p for p in raw.get("plugins") or [] if isinstance(p, str)]
    return {"id": folder.name, "name": str(raw.get("name") or folder.name)[:60], "version": str(raw.get("version") or "")[:20],
            "description": str(raw.get("description") or "")[:500], "language": raw["language"],
            "main_file": raw["main_file"][:200], "plugins": plugins[:20]}


def list_kits(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    return [kit for folder in sorted(directory.iterdir()) if folder.is_dir() and (kit := _read(folder))]


def get_kit(directory: Path, kit_id: str) -> dict:
    kit = _read(directory / kit_id) if ID_RE.match(kit_id or "") else None
    if kit is None:
        raise KitError("Modèle de bot introuvable")
    return kit


def kit_files(directory: Path, kit_id: str) -> dict[str, bytes]:
    """Les fichiers du modèle (sans template.json, ni données, ni plugins)."""
    root = directory / kit_id
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_symlink() or path.is_dir() or SKIP_DIRS & set(rel.parts) or rel.as_posix() in SKIP_FILES or path.suffix == ".pyc":
            continue
        files[rel.as_posix()] = path.read_bytes()
        if len(files) > MAX_FILES:
            raise KitError("Modèle trop gros")
    return files


def install(files: BotFiles, directory: Path, kit_id: str) -> int:
    """Copie (ou recopie) les fichiers du modèle dans le dossier du bot. Les plugins, les données et .env restent."""
    files.ensure()
    contents = kit_files(directory, kit_id)
    for rel, data in contents.items():
        files.write_bytes(rel, data)
    return len(contents)
