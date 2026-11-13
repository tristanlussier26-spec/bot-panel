"""Plugins Bot Panel : des modules qu'on installe sur le bot depuis l'onglet « Plugins » du panel.

Un plugin, c'est un dossier plugins/<id>/ qui contient :
  plugin.json   le nom, la version, les pages de réglages et les variables (voir le README de Bot Panel)
  __init__.py   une extension discord.py : async def setup(bot)

Le bot hôte (celui-ci) :
  - charge chaque plugin comme l'extension « plugins.<id> » ;
  - ajoute ses pages de réglages à l'onglet « Options du bot » (valeurs gardées par serveur Discord) ;
  - lui donne bot.plugin_host (voir PluginHost) : réglages, base de données, logs ;
  - appelle ces méthodes sur ses Cogs, si elles existent :
      validate_guild_config(config) -> list[str]            erreurs qui bloquent l'enregistrement
      async before_panel_save(guild, section, values)       nettoyer/vérifier (lever ValueError pour refuser)
      panel_info(guild_id) -> list[tuple[str, str]]         lignes affichées à côté du serveur dans le panel
      async on_guild_config_update(guild_id)                les réglages de ce serveur ont changé
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botng.config import PAGES, Field, Page
from botng.db import Database

if TYPE_CHECKING:
    import discord

    from bot import Bot

log = logging.getLogger("botng.plugins")

HOST_FORMAT = 1
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")  # aussi un nom de module Python valide
SECTION_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
INTENTS = {"members", "message_content", "presences"}  # intents privilégiés qu'un plugin peut demander
FIELD_TYPES = {"bool", "int", "text", "textarea", "lines", "select", "channel", "category", "role",
               "channels", "roles", "role_map", "level_roles"}
DEFAULTS = {"bool": False, "int": 0, "text": "", "textarea": "", "lines": [], "channel": 0, "category": 0,
            "role": 0, "channels": [], "roles": [], "level_roles": {}}


class PluginError(ValueError):
    pass


@dataclass
class Plugin:
    id: str
    name: str
    version: str
    path: Path
    pages: list[Page] = field(default_factory=list)
    enabled_by: tuple[str, str] | None = None
    intents: set[str] = field(default_factory=set)
    loaded: bool = False
    error: str | None = None

    @property
    def module(self) -> str:
        return f"plugins.{self.id}"


def _text(value: Any, limit: int, what: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PluginError(f"{what} manquant")
    if len(text) > limit:
        raise PluginError(f"{what} trop long")
    return text


def _field(raw: Any) -> Field:
    if not isinstance(raw, dict):
        raise PluginError("champ invalide")
    key = _text(raw.get("key"), 40, "clé d'un champ")
    if not re.match(r"^[a-z][a-z0-9_]*$", key):
        raise PluginError(f"clé de champ invalide : {key}")
    ftype = raw.get("type")
    if ftype not in FIELD_TYPES:
        raise PluginError(f"type de champ inconnu : {ftype}")
    choices = [(str(c[0]), str(c[1])) for c in raw.get("choices") or [] if isinstance(c, (list, tuple)) and len(c) == 2]
    keys = [(str(k[0]), str(k[1])) for k in raw.get("keys") or [] if isinstance(k, (list, tuple)) and len(k) == 2]
    if ftype == "select" and not choices:
        raise PluginError(f"« {key} » : choix manquants")
    if ftype == "role_map" and not keys:
        raise PluginError(f"« {key} » : clés manquantes")
    default = raw.get("default")
    if default is None:
        if ftype == "select":
            default = choices[0][0]
        elif ftype == "role_map":
            default = {k: 0 for k, _ in keys}
        else:
            default = DEFAULTS[ftype]
    return Field(
        key=key, label=_text(raw.get("label"), 80, "libellé"), type=ftype, default=default,
        help=_text(raw.get("help"), 300, "aide", required=False),
        min=raw.get("min") if isinstance(raw.get("min"), int) else None,
        max=raw.get("max") if isinstance(raw.get("max"), int) else None,
        choices=choices, keys=keys,
    )


def _page(raw: Any) -> Page:
    if not isinstance(raw, dict):
        raise PluginError("page invalide")
    section = _text(raw.get("section"), 32, "section")
    if not SECTION_RE.match(section):
        raise PluginError(f"section invalide : {section}")
    fields = [_field(f) for f in raw.get("fields") or []]
    if not fields:
        raise PluginError("page sans champ")
    if len({f.key for f in fields}) != len(fields):
        raise PluginError("deux champs ont la même clé")
    page_id = _text(raw.get("id"), 32, "id de page")
    return Page(page_id, _text(raw.get("title"), 60, "titre"), _text(raw.get("icon"), 8, "icône", required=False) or "🧩",
                section, _text(raw.get("description"), 300, "description", required=False), fields)


def read_manifest(folder: Path) -> Plugin:
    try:
        raw = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PluginError(f"plugin.json illisible ({e})") from None
    if not isinstance(raw, dict):
        raise PluginError("plugin.json invalide")
    if raw.get("format") != 1:
        raise PluginError("format de plugin pas supporté (format 1 attendu)")
    if raw.get("runtime") != "discord.py":
        raise PluginError(f"ce plugin est pour « {raw.get('runtime')} », pas pour discord.py")
    plugin_id = str(raw.get("id", ""))
    if not ID_RE.match(plugin_id) or plugin_id != folder.name:
        raise PluginError("l'id du plugin doit être le nom de son dossier (lettres minuscules, chiffres, _)")
    if not (folder / "__init__.py").is_file():
        raise PluginError("__init__.py manquant")
    plugin = Plugin(plugin_id, _text(raw.get("name"), 60, "nom"), _text(raw.get("version"), 20, "version"), folder)
    plugin.pages = [_page(p) for p in raw.get("pages") or []]
    intents = raw.get("intents") or []
    if not isinstance(intents, list) or not set(intents) <= INTENTS:
        raise PluginError(f"intents invalides (permis : {', '.join(sorted(INTENTS))})")
    plugin.intents = set(intents)
    if enabled := raw.get("enabled_by"):
        section, _, key = str(enabled).partition(".")
        if not section or not key:
            raise PluginError("enabled_by doit ressembler à « section.cle »")
        plugin.enabled_by = (section, key)
    return plugin


def discover(base: Path) -> list[Plugin]:
    """Plugins installés dans base/plugins. Un plugin brisé est noté avec son erreur, sans empêcher les autres."""
    folder = base / "plugins"
    if not folder.is_dir():
        return []
    found = []
    for sub in sorted(folder.iterdir()):
        if not sub.is_dir() or sub.name.startswith((".", "_")):
            continue
        try:
            found.append(read_manifest(sub))
        except PluginError as e:
            found.append(Plugin(sub.name, sub.name, "?", sub, error=str(e)))
            log.error("Plugin %s ignoré : %s", sub.name, e)
    return found


_REGISTERED: list[Page] = []


def register_pages(plugins: list[Plugin]) -> None:
    """Ajoute les pages des plugins à « Options du bot ». Une page ne peut pas prendre la place d'un module du bot."""
    for page in _REGISTERED:  # appelé une 2e fois (tests, rechargement) : on repart des pages du bot seulement
        if page in PAGES:
            PAGES.remove(page)
    _REGISTERED.clear()
    taken_pages = {p.id for p in PAGES}
    core_sections = {p.section for p in PAGES}
    owners: dict[str, str] = {}
    for plugin in plugins:
        if plugin.error:
            continue
        for page in plugin.pages:
            if page.id in taken_pages or page.section in core_sections or owners.get(page.section, plugin.id) != plugin.id:
                plugin.error = f"la page « {page.id} » (section {page.section}) est déjà prise"
                break
        if plugin.error:
            log.error("Plugin %s ignoré : %s", plugin.id, plugin.error)
            continue
        for page in plugin.pages:
            PAGES.append(page)
            _REGISTERED.append(page)
            taken_pages.add(page.id)
            owners[page.section] = plugin.id


class PluginHost:
    """Ce que le bot offre aux plugins (bot.plugin_host)."""

    format = HOST_FORMAT

    def __init__(self, bot: "Bot", base: Path) -> None:
        self.bot = bot
        self.base = base
        self.plugins: list[Plugin] = []
        self._dbs: list[Database] = []

    def config(self, guild_id: int | None, section: str) -> dict:
        """Réglages d'une section pour un serveur Discord (ceux de l'onglet « Options du bot »)."""
        return self.bot.cfg(guild_id, section)

    async def save(self, guild_id: int, section: str, values: dict) -> list[str]:
        """Enregistre des réglages (même ceux d'un autre plugin, ex. /adminbuild). Retourne les erreurs."""
        return await self.bot.save_section(guild_id, section, values)

    def sections(self) -> set[str]:
        """Sections qui ont une page de réglages (donc un plugin installé qui s'en sert)."""
        return {p.section for p in PAGES}

    def data_dir(self, plugin_id: str) -> Path:
        """Dossier de données du plugin, gardé quand le plugin est mis à jour ou retiré."""
        path = self.base / "data" / "plugins" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def open_db(self, plugin_id: str, schema: str = "") -> Database:
        """Base SQLite du plugin (dans son dossier de données), avec le schéma donné. Fermée avec le bot."""
        db = Database(self.data_dir(plugin_id) / f"{plugin_id}.db")
        await db.connect(schema)
        self._dbs.append(db)
        return db

    async def close_dbs(self) -> None:
        for db in self._dbs:
            await db.close()
        self._dbs.clear()

    async def send_log(self, guild_id: int, embed: "discord.Embed", category: str = "divers") -> None:
        """Un événement à écrire dans les logs du serveur. C'est le plugin Logs qui l'écrit, s'il est installé."""
        self.bot.dispatch("botpanel_log", guild_id, embed, category)

    def refresh_panel(self) -> None:
        """À appeler quand ce que panel_info() retourne a changé."""
        if (bridge := getattr(self.bot, "panel_bridge", None)) is not None:
            bridge.touch()

    def report(self) -> dict:
        return {
            "format": HOST_FORMAT,
            "runtime": "discord.py",
            "plugins": {p.id: {"name": p.name, "version": p.version, "loaded": p.loaded, "error": p.error}
                        for p in self.plugins},
        }
