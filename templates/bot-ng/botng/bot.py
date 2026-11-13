"""Bot NG : un bot Discord de base pour les pays NationsGlory, qu'on complète avec des plugins Bot Panel.

Le bot de base fait seulement : les réglages par serveur (onglet « Options du bot »), le lien avec Bot Panel,
le chargement des plugins (dossier plugins/) et quelques commandes générales. Tout le reste vient des plugins.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from botng.config import build_config
from botng.db import Database
from botng.helpers import reply
from botng.plugins import Plugin, PluginHost, discover, register_pages

BASE_DIR = Path(__file__).resolve().parents[1]
log = logging.getLogger("botng")

PANEL_EVENTS = ("on_guild_join", "on_guild_remove", "on_guild_update", "on_guild_channel_create",
                "on_guild_channel_delete", "on_guild_channel_update", "on_guild_role_create",
                "on_guild_role_delete", "on_guild_role_update", "on_member_update")


def build_intents(plugins: list[Plugin]) -> discord.Intents:
    """Les intents privilégiés demandés par les plugins installés, et rien de plus."""
    intents = discord.Intents.default()
    wanted = {name for p in plugins if not p.error for name in p.intents}
    intents.members = "members" in wanted
    intents.message_content = "message_content" in wanted
    intents.presences = "presences" in wanted
    return intents


class Bot(commands.Bot):
    def __init__(self, base: Path = BASE_DIR, plugins: list[Plugin] | None = None) -> None:
        plugins = discover(base) if plugins is None else plugins
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(plugins),
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )
        self.base = base
        self.db = Database(base / "data" / "bot.db")
        self.panel_bridge = None
        self.plugin_host = PluginHost(self, base)
        self.plugin_host.plugins = plugins
        self.optional_modules: dict[str, tuple[str, str]] = {}  # module -> (section, clé) qui active ses commandes
        self._saved: dict[int, dict[str, dict]] = {}
        self._config_cache: dict[int, dict] = {}
        self.tree.on_error = self.on_app_command_error

    # ---- Réglages par serveur ------------------------------------------------------------
    def config_for(self, guild_id: int | None) -> dict:
        """Réglages d'un serveur : valeurs par défaut des pages < valeurs enregistrées."""
        if guild_id is None:
            return build_config({})
        if guild_id not in self._config_cache:
            self._config_cache[guild_id] = build_config(self._saved.get(guild_id, {}))
        return self._config_cache[guild_id]

    def cfg(self, guild_id: int | None, section: str) -> dict:
        return self.config_for(guild_id).get(section, {})

    async def _load_saved(self) -> None:
        for key, raw in (await self.db.values_with_prefix("config:")).items():
            _, guild_id, section = key.split(":", 2)
            self._saved.setdefault(int(guild_id), {})[section] = json.loads(raw)
        self._config_cache.clear()

    def validate_config(self, config: dict) -> list[str]:
        """Erreurs qui empêchent d'enregistrer : chaque plugin vérifie ses propres réglages."""
        errors = []
        for cog in list(self.cogs.values()):
            if hook := getattr(cog, "validate_guild_config", None):
                try:
                    errors.extend(hook(config) or [])
                except Exception as e:  # noqa: BLE001
                    log.exception("validate_guild_config de %s", type(cog).__name__)
                    errors.append(f"{type(cog).__name__} : {e}")
        return errors

    async def save_section(self, guild_id: int, section: str, values: dict) -> list[str]:
        """Enregistre des réglages. Retourne les erreurs (rien n'est enregistré s'il y en a)."""
        saved = copy.deepcopy(self._saved.get(guild_id, {}))
        saved[section] = {**saved.get(section, {}), **values}
        if errors := self.validate_config(build_config(saved)):
            return errors
        await self.db.set_value(f"config:{guild_id}:{section}", json.dumps(saved[section]))
        self._saved[guild_id] = saved
        self._config_cache.pop(guild_id, None)
        for cog in list(self.cogs.values()):  # les plugins qui gardent un état par serveur le recréent
            if hook := getattr(cog, "on_guild_config_update", None):
                await hook(guild_id)
        await self.sync_guild_commands(guild_id)
        return []

    # ---- Commandes slash par serveur ---------------------------------------------------------
    def optional_module(self, module: str) -> tuple[str, str] | None:
        for name, option in self.optional_modules.items():
            if module == name or module.startswith(name + "."):
                return option
        return None

    async def sync_guild_commands(self, guild_id: int, *, force: bool = False) -> None:
        """Enregistre sur un serveur les commandes des plugins qui y sont activés (seulement si ça a changé)."""
        cfg = self.config_for(guild_id)
        guild = discord.Object(id=guild_id)
        self.tree.clear_commands(guild=guild)
        for command in self.tree.get_commands():
            option = self.optional_module(getattr(command, "module", "") or "")
            if option and not cfg.get(option[0], {}).get(option[1]):
                continue
            self.tree.add_command(command, guild=guild, override=True)
        payload = sorted((c.to_dict(self.tree) for c in self.tree.get_commands(guild=guild)), key=lambda d: d["name"])
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        key = f"cmdsig:{guild_id}"
        if not force and await self.db.get_value(key) == signature:
            return
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as e:
            log.warning("Serveur %s : commandes pas enregistrées (%s)", guild_id, e)
            return
        await self.db.set_value(key, signature)
        log.info("Serveur %s : %d commande(s) enregistrée(s)", guild_id, len(synced))

    # ---- Démarrage -----------------------------------------------------------------------------
    async def setup_hook(self) -> None:
        await self.db.connect()
        # Les pages de réglages des plugins doivent exister avant de lire les réglages enregistrés
        register_pages(self.plugin_host.plugins)
        await self._load_saved()
        await self.load_extension("botng.general")
        await self.load_plugins()
        if os.getenv("BOT_ID"):  # hébergé par Bot Panel : les réglages vont dans l'onglet « Options du bot »
            from botng.panelbridge import PanelBridge

            self.panel_bridge = PanelBridge(self, self.base)
            self.panel_bridge.start()
            for event in PANEL_EVENTS:
                self.add_listener(self._panel_touch, event)

    async def load_plugins(self) -> None:
        for plugin in self.plugin_host.plugins:
            if plugin.error:
                continue
            if plugin.enabled_by:
                self.optional_modules[plugin.module] = plugin.enabled_by
            try:
                await self.load_extension(plugin.module)
            except commands.ExtensionError as e:
                plugin.error = str(getattr(e, "original", e))
                log.error("Plugin %s pas chargé : %s", plugin.id, plugin.error, exc_info=getattr(e, "original", None))
                continue
            plugin.loaded = True
            log.info("Plugin chargé : %s %s", plugin.name, plugin.version)
        if not any(p.loaded for p in self.plugin_host.plugins):
            log.info("Aucun plugin : ajoute-en dans l'onglet « Plugins » de Bot Panel")

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (ID %s) sur %d serveur(s)", self.user, self.user.id, len(self.guilds))
        for guild in self.guilds:
            await self.sync_guild_commands(guild.id)
        statut = os.getenv("BOT_STATUT", "").strip()
        await self.change_presence(activity=discord.CustomActivity(name=statut[:128]) if statut else None)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Ajouté sur le serveur %s (%s)", guild.name, guild.id)
        await self.sync_guild_commands(guild.id, force=True)

    async def _panel_touch(self, *args) -> None:
        if self.panel_bridge is not None:
            self.panel_bridge.touch()

    async def close(self) -> None:
        if self.panel_bridge is not None:
            await self.panel_bridge.stop()
        await super().close()
        await self.plugin_host.close_dbs()
        await self.db.close()

    async def send_log(self, guild_id: int, embed: discord.Embed, category: str = "divers") -> None:
        await self.plugin_host.send_log(guild_id, embed, category)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Attends encore {error.retry_after:.0f} s avant de refaire cette commande."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "⛔ T'as pas la permission d'utiliser cette commande."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "⚠️ Il me manque des permissions : " + ", ".join(error.missing_permissions)
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "Cette commande marche juste sur un serveur."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "⛔ Tu peux pas utiliser cette commande."
        elif isinstance(getattr(original, "user_message", None), str):
            log.warning("%s", original)  # erreur prévue d'un plugin (ex. un service externe) : message clair au membre
            msg = original.user_message
        elif isinstance(original, discord.Forbidden):
            msg = "⚠️ Discord m'a refusé l'action : vérifie mes permissions et que mon rôle est assez haut."
        else:
            name = interaction.command.qualified_name if interaction.command else "?"
            log.error("Erreur dans /%s", name, exc_info=original)
            msg = "❌ Oups, une erreur est survenue. Les détails sont dans la console du bot."
        try:
            await reply(interaction, msg)
        except discord.HTTPException:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:  # en dehors de Bot Panel, on peut mettre les variables dans un fichier .env
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        sys.exit("❌ DISCORD_TOKEN manquant : mets le jeton du bot dans ses variables (onglet Variables de Bot Panel).")
    sys.path.insert(0, str(BASE_DIR))  # « plugins.<id> » s'importe depuis le dossier du bot
    try:
        Bot().run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        sys.exit("❌ Des plugins ont besoin d'intents privilégiés : Developer Portal › ton application › Bot › "
                 "active « Server Members Intent » et « Message Content Intent ».")
