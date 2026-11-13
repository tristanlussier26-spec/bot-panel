"""Pont avec Bot Panel : les réglages du bot s'affichent dans l'onglet « Options du bot » du panel.

Tout passe par des fichiers dans le dossier du bot (voir botpanel/options.py dans Bot Panel) :
  .botpanel/schema.json  les pages de réglages            (écrit ici, au démarrage)
  .botpanel/state.json   serveurs, salons, rôles, valeurs (écrit ici, souvent)
  .botpanel/inbox/       les modifications demandées par le panel
  .botpanel/outbox/      nos réponses (ok, ou l'erreur à montrer)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from botng.config import PAGES, ValidationError, validate_page

if TYPE_CHECKING:
    from bot import Bot

log = logging.getLogger("botng.panel")

ROLE_TYPES = ("role", "roles", "role_map", "level_roles")


def _jsonable(value):
    """Les IDs Discord dépassent la précision des nombres JavaScript : on les envoie en texte."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and value > 2**53:
        return str(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def schema() -> dict:
    pages = []
    for page in PAGES:
        fields = []
        for f in page.fields:
            field = {"key": f.key, "label": f.label, "help": f.help, "type": f.type}
            if f.type == "role_map":
                field["keys"] = [list(k) for k in f.keys]
            elif f.type == "level_roles":
                field.update(type="number_roles", min=1, max=1000)
            if f.min is not None:
                field["min"] = f.min
            if f.max is not None:
                field["max"] = f.max
            if f.choices:
                field["choices"] = [list(c) for c in f.choices]
            fields.append(field)
        pages.append({"id": page.id, "title": page.title, "icon": page.icon, "description": page.description, "fields": fields})
    return {"title": "Bot NG", "scope": "guild", "pages": pages}


def check_ids(guild: discord.Guild, clean: dict) -> None:
    """Les salons et rôles choisis doivent exister sur CE serveur."""
    page_fields = {f.key: f for page in PAGES for f in page.fields}
    for key, value in clean.items():
        f = page_fields.get(key)
        if f is None:
            continue
        ids: list[int] = []
        if f.type in ("channel", "category", "role"):
            ids = [value] if value else []
        elif f.type in ("channels", "roles"):
            ids = value
        elif f.type in ("role_map", "level_roles"):
            ids = [v for v in value.values() if v]
        for item in ids:
            exists = guild.get_role(item) if f.type in ROLE_TYPES else guild.get_channel(item)
            if exists is None:
                raise ValidationError(f"« {f.label} » : salon ou rôle introuvable sur ce serveur")


class PanelBridge:
    def __init__(self, bot: "Bot", base: Path) -> None:
        self.bot = bot
        self.dir = base / ".botpanel"
        self.inbox = self.dir / "inbox"
        self.outbox = self.dir / "outbox"
        self.task: asyncio.Task | None = None
        self._dirty = True

    def start(self) -> None:
        for d in (self.dir, self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)
        self.write_static()
        self.task = asyncio.create_task(self._loop(), name="pont Bot Panel")
        log.info("Réglages disponibles dans l'onglet « Options du bot » de Bot Panel")

    def write_static(self) -> None:
        """Les pages de réglages et les plugins chargés (onglets « Options du bot » et « Plugins » du panel)."""
        self._write("schema.json", schema())
        self._write("host.json", self.bot.plugin_host.report())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()

    def touch(self) -> None:
        """À appeler quand les salons/rôles changent : l'état sera réécrit bientôt."""
        self._dirty = True

    def _write(self, name: str, data: dict, folder: Path | None = None) -> None:
        folder = folder or self.dir
        tmp = folder / f".{name}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, folder / name)  # le panel lit jamais un fichier à moitié écrit

    def _state(self) -> dict:
        scopes = []
        for guild in sorted(self.bot.guilds, key=lambda g: g.name.lower()):
            channels, categories = [], []
            for channel in sorted(guild.channels, key=lambda c: (c.position, c.id)):
                if isinstance(channel, discord.CategoryChannel):
                    categories.append({"id": str(channel.id), "name": channel.name})
                elif isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel)):
                    channels.append({"id": str(channel.id), "name": channel.name,
                                     "category": channel.category.name if channel.category else "",
                                     "voice": isinstance(channel, discord.VoiceChannel)})
            me = guild.me
            roles = [{"id": str(r.id), "name": r.name, "color": str(r.color) if r.color.value else None,
                      "manageable": bool(me) and r < me.top_role and not r.managed}
                     for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed]
            cfg = self.bot.config_for(guild.id)
            values = {p.id: {f.key: _jsonable(cfg.get(p.section, {}).get(f.key)) for f in p.fields} for p in PAGES}
            # Qui peut régler ce serveur dans Bot Panel (bots partagés) : ceux qui ont « Gérer le serveur »
            managers = {str(guild.owner_id)} if guild.owner_id else set()
            for m in getattr(guild, "members", []):
                perms = m.guild_permissions
                if not getattr(m, "bot", False) and (perms.manage_guild or perms.administrator):
                    managers.add(str(m.id))
            info = [["Membres", str(guild.member_count or "?")]]
            for cog in list(self.bot.cogs.values()):  # les plugins ajoutent leurs lignes (ex. état NationsGlory)
                if hook := getattr(cog, "panel_info", None):
                    try:
                        info.extend([str(k), str(v)] for k, v in hook(guild.id) or [])
                    except Exception:  # noqa: BLE001
                        log.exception("panel_info de %s", type(cog).__name__)
            scopes.append({
                "id": str(guild.id), "name": guild.name, "icon": guild.icon.url if guild.icon else None,
                "channels": channels, "categories": categories, "roles": roles, "values": values,
                "info": info, "managers": sorted(managers),
            })
        app_id = self.bot.application_id or (self.bot.user.id if self.bot.user else None)
        return {"updated": time.time(), "app_id": str(app_id) if app_id else None, "scopes": scopes}

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        last = 0.0
        while True:
            try:
                for request in sorted(self.inbox.glob("*.json")):
                    await self._handle(request)
                if self._dirty or time.time() - last > 30:
                    self._write("state.json", self._state())
                    self._dirty, last = False, time.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Pont Bot Panel")
            await asyncio.sleep(1)

    async def _handle(self, path: Path) -> None:
        rid = path.stem
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            guild = self.bot.get_guild(int(req.get("scope", 0)))
            if guild is None:
                raise ValidationError("Le bot est plus sur ce serveur")
            values = req.get("values")
            if not isinstance(values, dict):
                raise ValidationError("Données invalides")
            section, clean = validate_page(str(req.get("page")), values)
            check_ids(guild, clean)
            for cog in list(self.bot.cogs.values()):  # un plugin peut nettoyer ou refuser ses réglages
                if hook := getattr(cog, "before_panel_save", None):
                    await hook(guild, section, clean)
            errors = await self.bot.save_section(guild.id, section, clean)
            if errors:
                raise ValidationError("Pas enregistré : " + " · ".join(errors))
            user = req.get("user") or {}
            log.info("%s a modifié « %s » sur %s (Bot Panel)", user.get("name", "?"), req.get("page"), guild.name)
            answer = {"ok": True}
            self._dirty = True
        except (ValidationError, ValueError, TypeError) as e:
            answer = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("Réglage venant de Bot Panel")
            answer = {"ok": False, "error": f"Erreur dans le bot : {e}"}
        path.unlink(missing_ok=True)
        if rid.isalnum():
            self._write(f"{rid}.json", answer, self.outbox)
