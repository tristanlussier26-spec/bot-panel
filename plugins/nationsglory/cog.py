"""Plugin « Synchro NationsGlory » : grades d'un pays NationsGlory <-> rôles Discord, stats et annonces.

Multi-serveurs : chaque serveur Discord choisit son serveur NG, son pays et ses salons dans « Options du bot ».
La clé de l'API officielle est celle de l'hébergeur du bot (variable NG_API_KEY, réglée dans l'onglet Plugins) :
les serveurs Discord qui utilisent le bot ont rien à faire avec l'API.

Liaison des comptes :
  1. Le membre fait /lier <pseudo NG>.
  2. Une demande apparaît dans le salon de validation : un officier clique « Accepter ».
     (sans salon de validation, la liaison est directe, mais n'importe qui peut prétendre être n'importe qui)
  3. Aux X minutes, le bot lit les membres du pays et ajuste les rôles : Chef / Officier / Membre / Recrue,
     + un rôle « membre du pays » et un rôle « hors pays » optionnels.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import alerts, insights
from .api import NationsGlory, RateLimiter, normalize_target
from .common import BLUE, GREEN, GREY, RED, NationsGloryError, ServiceError, make_embed, reply, truncate
from .liaison import HIGH_RANKS, AskButton, UndoButton, ask_view, candidate_names, match_member
from .logic import RANK_LABELS, RANKS, ConfigError, SyncPlan, parse_settings, plan_sync, plan_unlink
from .store import Store
from .veille_run import ENTERPRISE_CHANNELS_EVERY, ENTERPRISES_EVERY, FORUM_EVERY, MARKET_EVERY, ONLINE_EVERY, VeilleMixin

log = logging.getLogger("plugin.nationsglory")

NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")
Pseudo = app_commands.Range[str, 2, 16]


def rank_label(rank: str | None) -> str:
    return RANK_LABELS.get(rank or "", rank or "Sans grade")


RANK_EMOJI = {"LEADER": "👑", "OFFICER": "⭐", "MEMBER": "🛡️", "RECRUIT": "🌱"}
RANK_COLOR = {"LEADER": 0xF1C40F, "OFFICER": 0x3498DB, "MEMBER": 0x2ECC71, "RECRUIT": 0x95A5A6}
KEY_PAUSE = 10 * 60  # clé refusée (IP changée?) : on attend avant de réessayer


def head_url(username: str) -> str:
    """Tête du skin, la même image que sur nationsglory.fr."""
    return f"https://files.recube.gg/nationsglory/face/{quote(username)}.png"


def fmt_int(value) -> str:
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else "?"


COUNTRY_STATS = (("power", "⚡ Power"), ("power_max", "Power max"), ("claims", "🗺️ Claims"),
                 ("bank_balance", "💰 Trésorerie"), ("score", "🏆 Score"), ("mmr", "🏅 MMR"), ("level", "Niveau"))
ALERTS_EVERY = 5 * 60  # pays aux 5 min ; les guerres sont mises à jour aux 10 min chez NationsGlory
WARZONE_EVERY = 15 * 60
ORANGE = 0xE67E22
ACTIVITY_REFRESH = 12 * 3600  # dernière connexion d'un membre relue aux 12 h (publiée au jour près)
ACTIVITY_EVERY = 30  # une vérification aux 30 s par pays : ~110 membres en une heure, ~2 requêtes/min


def paris_today() -> date:
    """La date à Paris : NationsGlory publie les dernières connexions à l'heure de Paris."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:  # noqa: BLE001 - pas de base de fuseaux horaires : Paris ≈ UTC+1
        return (datetime.now(timezone.utc) + timedelta(hours=1)).date()


def build_stats_embed(server: str, country: str, card: dict, members: dict[str, dict]) -> discord.Embed:
    """Salon de stats : membres du pays par grade et fiche du pays (ce que l'API publique donne)."""
    embed = discord.Embed(title=f"📊 {country} · {server.capitalize()}", color=BLUE, timestamp=discord.utils.utcnow())
    counts = {rank: 0 for rank in RANKS}
    for m in members.values():
        if m.get("role") in counts:
            counts[m["role"]] += 1
    lines = [f"👥 **{len(members)}** membres"]
    lines += [f"{RANK_EMOJI[r]} {rank_label(r)} : {n}" for r, n in counts.items() if n]
    embed.add_field(name="Membres", value="\n".join(lines), inline=True)
    stats = []
    for key, label in COUNTRY_STATS:
        value = card.get(key)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            text = fmt_int(value) if isinstance(value, int) else str(value)
            stats.append(f"{label} : {text}{' $' if key == 'bank_balance' else ''}")
    if stats:
        embed.add_field(name="Pays", value="\n".join(stats), inline=True)
    embed.set_footer(text="Données de l'API NationsGlory, mises à jour aux 5 minutes")
    return embed


class LinkButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ngl:(?P<action>ok|no):(?P<user_id>\d+):(?P<name>[A-Za-z0-9_]{2,16})",
):
    """Boutons Accepter/Refuser des demandes de liaison. Ils marchent encore après un redémarrage du bot."""

    def __init__(self, action: str, user_id: int, name: str) -> None:
        self.action, self.user_id, self.name = action, user_id, name
        super().__init__(
            discord.ui.Button(
                label="Accepter" if action == "ok" else "Refuser",
                emoji="✅" if action == "ok" else "✖️",
                style=discord.ButtonStyle.success if action == "ok" else discord.ButtonStyle.danger,
                custom_id=f"ngl:{action}:{user_id}:{name}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["user_id"]), match["name"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("NGSync")
        if cog is None:
            return await reply(interaction, "La synchro NationsGlory est désactivée.")
        await cog.handle_decision(interaction, self.action, self.user_id, self.name)


class GuildNG:
    """Réglages et état de la synchro NationsGlory pour UN serveur Discord."""

    def __init__(self, guild_id: int, cfg: dict, api) -> None:
        self.guild_id = guild_id
        self.settings, self.warnings = parse_settings(cfg)
        self.api = api
        # On accepte aussi l'adresse de la page du pays (https://nationsglory.fr/servers/jade/countries/Amour)
        self.server, self.country_query = normalize_target(cfg.get("serveur", ""), cfg.get("pays", ""))
        if not self.country_query or not self.server:
            raise ConfigError("remplis le serveur et le pays NationsGlory")
        self.country_id: str | None = None
        self.country_name = self.country_query

        def chan(key: str) -> int:
            return int(cfg.get(key, 0) or 0)

        self.rename = bool(cfg.get("renommer", True))
        self.interval = max(1, int(cfg.get("intervalle_minutes", 5)))
        self.validation_channel_id = chan("salon_validation_id")
        self.validator_role_id = chan("role_validateur_id")
        self.auto_link = bool(cfg.get("liaison_auto", True))
        self.ask_pseudo = bool(cfg.get("demander_pseudo", True))
        self.link_channel_id = chan("salon_liaison_id")
        self.stats_channel_id = chan("salon_stats_id")
        self.status_interval = max(1, int(cfg.get("statut_minutes", 5)))
        self.online_channel_id = chan("salon_en_ligne_id")
        self.last_online_board = -1e9
        self.announce_channel_id = chan("salon_annonces_id")
        self.announce = {
            k: bool(cfg.get(f"annoncer_{k}", d)) for k, d in (("arrivees", True), ("promotions", True), ("departs", False))
        }
        # Guerres et alertes (page « NationsGlory : guerres et alertes »)
        self.war_channel_id = chan("salon_guerres_id")
        self.alert_role_id = chan("role_alerte_id")
        self.announce_assaults = bool(cfg.get("annoncer_assauts", True))
        self.power_alert = bool(cfg.get("alerte_power", True))
        self.power_margin = max(0, int(cfg.get("marge_power", 50) or 0))
        self.enemy_alert = bool(cfg.get("alerte_ennemis", True))
        self.warzone_channel_id = chan("salon_warzone_id")
        self.warzone_weekly = bool(cfg.get("warzone_semaine", True))
        self.track_activity = bool(cfg.get("suivre_activite", True))
        # Veille (page « NationsGlory : rapports et veille »)
        self.report_channel_id = chan("salon_rapport_id")
        self.report_hour = min(23, max(0, int(cfg.get("heure_rapport", 22) or 0)))
        self.tz = str(cfg.get("fuseau") or "America/Toronto")
        self.diplomacy_alert = bool(cfg.get("alerte_diplomatie", True))
        self.watchlist = [str(x).strip() for x in cfg.get("pays_surveilles") or [] if str(x).strip()][:25]
        self.forum_channel_id = chan("salon_forum_id")
        self.forum_category = str(cfg.get("categorie_forum") or "annonces").strip()
        self.market_channel_id = chan("salon_marche_id")
        self.market_pct = max(1, int(cfg.get("seuil_marche", 10) or 10))
        self.ingame_role_id = chan("role_en_jeu_id")
        self.inactive_role_id = chan("role_inactif_id")
        self.inactive_days = max(1, int(cfg.get("jours_inactif", 14) or 14))
        self.announce_enterprises = bool(cfg.get("annoncer_entreprises", True))
        self.enterprise_roles = bool(cfg.get("roles_entreprises", False))
        self.patron_role_id = chan("role_patron_id")
        self.enterprise_channels = bool(cfg.get("salons_entreprises", False))
        self.enterprise_category_id = chan("categorie_entreprises_id")
        self.last_enterprise_channels = -1e9
        self.extra_enterprises = [str(x).strip() for x in cfg.get("entreprises_pays") or [] if str(x).strip()][:50]
        self.last_enterprises = -1e9
        self.last_report_day: str | None = None
        self.last_forum = -1e9
        self.last_market = -1e9
        self.last_alerts = -1e9
        self.last_warzone = -1e9
        self.last_sync = -1e9
        self.last_status = -1e9
        self.paused_until = 0.0
        self.recent_warnings: dict[str, float] = {}
        self.warned: set[int] = set()

    def should_warn(self, message: str, every: float = 900) -> bool:
        """Le même avertissement au plus une fois aux 15 min (évite d'inonder la console)."""
        now = time.monotonic()
        if now - self.recent_warnings.get(message, -1e9) < every:
            return False
        self.recent_warnings[message] = now
        return True

    async def country(self) -> str:
        if self.country_id is None:
            self.country_id, self.country_name = await self.api.resolve_country(self.country_query)
            log.info("Serveur %s : pays %s sur %s", self.guild_id, self.country_name, self.api.server)
        return self.country_id

    async def members(self, max_age: float = 60) -> dict[str, dict]:
        return await self.api.members(await self.country(), max_age=max_age)


class NGSync(VeilleMixin, commands.Cog):
    synchro = app_commands.Group(
        name="synchro",
        description="Gestion des comptes liés et de la synchro des grades NationsGlory",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel) : réglages, dossier de données, logs
        self.store = Store(self.host.data_dir("nationsglory") / "nationsglory.db")
        self.states: dict[int, GuildNG] = {}
        self.clients: dict[str, object] = {}
        self.statuses: dict[int, str] = {}
        self.limiter = RateLimiter(int(os.getenv("NG_API_RPM", "50") or 50))  # NationsGlory : 60/min par clé
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_activity: dict[tuple[str, str], float] = {}  # (serveur NG, pays) -> dernière vérification

    async def cog_load(self) -> None:
        await self.store.connect()
        if not self.api_key():
            log.warning("NG_API_KEY est vide : mets la clé de l'API NationsGlory dans l'onglet Plugins de Bot Panel")
        self.bot.add_dynamic_items(LinkButton, AskButton, UndoButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.bot.remove_dynamic_items(LinkButton, AskButton, UndoButton)
        for client in self.clients.values():
            await client.close()
        await self.store.close()

    # ---- État par serveur -------------------------------------------------------------
    @staticmethod
    def api_key() -> str:
        return os.getenv("NG_API_KEY", "").strip()

    def client(self, server: str) -> NationsGlory:
        """Un client par serveur NG, partagé entre les serveurs Discord (cache commun, moins de requêtes).
        Ils partagent aussi la limite de requêtes de la clé."""
        key = server.strip().lower()
        if key not in self.clients:
            proxy = os.getenv("NG_PROXY", "").strip() or None
            self.clients[key] = NationsGlory(self.api_key(), key, proxy=proxy, limiter=self.limiter)
        return self.clients[key]

    def set_status(self, guild_id: int, text: str | None) -> None:
        """État affiché dans Bot Panel (Options du bot, à côté du nom du serveur)."""
        if self.statuses.get(guild_id) == text:
            return
        if text is None:
            self.statuses.pop(guild_id, None)
        else:
            self.statuses[guild_id] = text
        self.host.refresh_panel()

    def status_for_error(self, e: ServiceError) -> str:
        status = getattr(e, "status", None)
        if not self.api_key():
            return "🔑 En attente de la clé API NationsGlory (l'hébergeur du bot la met dans l'onglet Plugins)"
        if status in (401, 403):
            return "🔑 L'API NationsGlory refuse la clé (l'IP de l'hébergeur a peut-être changé)"
        if status == 404:
            return "⚙️ Pays introuvable : vérifie le serveur et le pays"
        if status == 429:
            return "⏳ Trop de requêtes à l'API NationsGlory, ça reprend tout seul"
        return "⚠️ NationsGlory répond pas pour l'instant"

    async def check_target(self, server: str, country: str) -> str:
        """Vérifie qu'un pays existe (avant d'enregistrer les options). Retourne son nom exact."""
        _, name = await self.client(server).resolve_country(country)
        return name

    def state(self, guild_id: int | None) -> GuildNG | None:
        """État NG d'un serveur Discord, ou None si la synchro y est désactivée/mal réglée."""
        if guild_id is None:
            return None
        cfg = self.host.config(guild_id, "ng")
        if not cfg.get("active"):
            self.states.pop(guild_id, None)
            self.set_status(guild_id, None)
            return None
        st = self.states.get(guild_id)
        if st is None:
            try:
                server, _ = normalize_target(cfg.get("serveur", ""), cfg.get("pays", ""))
                st = GuildNG(guild_id, cfg, self.client(server))
            except ConfigError as e:
                if guild_id not in self.statuses:
                    log.warning("Serveur %s : synchro NG pas démarrée (%s)", guild_id, e)
                self.set_status(guild_id, f"⚙️ {e}")
                return None
            for warning in st.warnings:
                log.warning("Serveur %s : %s", guild_id, warning)
            self.states[guild_id] = st
        return st

    async def on_guild_config_update(self, guild_id: int) -> None:
        self.states.pop(guild_id, None)  # recréé avec les nouveaux réglages au prochain usage

    # ---- Crochets du bot hôte (Bot Panel) -------------------------------------------------
    def validate_guild_config(self, config: dict) -> list[str]:
        """Erreurs qui empêchent d'enregistrer les réglages d'un serveur."""
        ng = config.get("ng", {})
        if not ng.get("active"):
            return []
        errors = []
        server, country = normalize_target(ng.get("serveur", ""), ng.get("pays", ""))
        if not server or not country:
            errors.append("NationsGlory : remplis le serveur et le pays")
        try:
            settings, _ = parse_settings(ng)
        except ConfigError as e:
            errors.append(f"NationsGlory : {e}")
        else:
            for key, label in (("role_en_jeu_id", "« connecté en jeu »"), ("role_inactif_id", "« inactif »"),
                               ("role_patron_id", "« patron »")):
                role_id = int(ng.get(key, 0) or 0)
                if role_id and role_id in settings.managed_roles:
                    errors.append(f"NationsGlory : le rôle {label} est déjà utilisé pour un grade ou le pays")
            if int(ng.get("role_en_jeu_id", 0) or 0) and ng.get("role_en_jeu_id") == ng.get("role_inactif_id"):
                errors.append("NationsGlory : les rôles « connecté en jeu » et « inactif » doivent être différents")
        return errors

    async def before_panel_save(self, guild: discord.Guild, section: str, values: dict) -> None:
        """Nettoie le serveur et le pays (adresse acceptée) et vérifie que le pays existe."""
        if section != "ng" or "pays" not in values:
            return
        server, country = normalize_target(values.get("serveur", ""), values.get("pays", ""))
        values["serveur"], values["pays"] = server, country
        if not values.get("active") or not server or not country or not self.api_key():
            return
        try:
            values["pays"] = await asyncio.wait_for(self.check_target(server, country), 7)  # le nom exact
        except NationsGloryError as e:
            if e.status == 404:
                raise ValueError(f"Pays « {country} » introuvable sur le serveur {server.capitalize()}") from None
            # API injoignable ou clé refusée : on enregistre quand même, l'état du serveur le dira
        except asyncio.TimeoutError:
            pass

    def panel_info(self, guild_id: int) -> list[tuple[str, str]]:
        status = self.statuses.get(guild_id)
        return [("NationsGlory", status)] if status else []

    async def _st(self, interaction: discord.Interaction) -> GuildNG | None:
        st = self.state(interaction.guild_id)
        if st is None:
            await reply(interaction, "La synchro NationsGlory est pas activée sur ce serveur (voir le panel).")
        return st

    # ---- Rôles ------------------------------------------------------------------------
    @staticmethod
    def _resolve_roles(guild: discord.Guild, role_ids: set[int], warned: set[int]) -> list[discord.Role]:
        roles = []
        me = guild.me
        for role_id in role_ids:
            role = guild.get_role(role_id)
            problem = None
            if role is None:
                problem = f"rôle {role_id} introuvable : vérifie les rôles dans le panel"
            elif role.managed or role >= me.top_role or not me.guild_permissions.manage_roles:
                problem = f"je peux pas gérer le rôle « {role.name} » : monte le rôle du bot au-dessus"
            if problem:
                if role_id not in warned:
                    warned.add(role_id)
                    log.warning("Serveur %s : %s", guild.name, problem)
                continue
            roles.append(role)
        return roles

    async def _apply(self, member: discord.Member, plan: SyncPlan, st: GuildNG, reason: str) -> None:
        to_add = self._resolve_roles(member.guild, plan.add_roles, st.warned)
        to_remove = self._resolve_roles(member.guild, plan.remove_roles, st.warned)
        try:
            if to_add:
                await member.add_roles(*to_add, reason=reason)
            if to_remove:
                await member.remove_roles(*to_remove, reason=reason)
        except discord.HTTPException as e:
            log.warning("Rôles de %s pas modifiés : %s", member, e)

    @staticmethod
    async def _rename(member: discord.Member, name: str) -> None:
        guild = member.guild
        if (
            member.nick == name
            or member.id == guild.owner_id
            or member.top_role >= guild.me.top_role
            or not guild.me.guild_permissions.manage_nicknames
        ):
            return
        try:
            await member.edit(nick=name, reason="Pseudo NationsGlory")
        except discord.HTTPException:
            pass

    # ---- Synchro -------------------------------------------------------------------------
    async def sync_member(
        self, member: discord.Member, link: dict, st: GuildNG, members: dict[str, dict] | None = None
    ) -> tuple[SyncPlan, dict | None]:
        """Synchronise un membre lié. Retourne (changements, fiche NG ou None s'il est pas dans le pays)."""
        async with self._locks[(member.guild.id, member.id)]:
            member = member.guild.get_member(member.id) or member  # version la plus fraîche du cache
            if members is None:
                members = await st.members()
            entry = members.get(link["ng_name"].lower())
            plan = plan_sync({r.id for r in member.roles}, entry is not None, entry and entry["role"], st.settings)
            if not plan.empty:
                await self._apply(member, plan, st, "Synchro des grades NationsGlory")
            if entry and entry["username"] != link["ng_name"]:
                await self.store.rename_link(member.guild.id, member.id, entry["username"])  # bonne casse du pseudo
            if st.rename:
                await self._rename(member, entry["username"] if entry else link["ng_name"])
            return plan, entry

    async def sync_all(self, guild: discord.Guild, st: GuildNG, members: dict[str, dict] | None = None) -> tuple[int, int, int]:
        """Synchronise tous les membres liés d'un serveur. Retourne (vérifiés, modifiés, erreurs)."""
        links = await self.store.all_links(guild.id)
        if not links:
            return 0, 0, 0
        if members is None:
            members = await st.members(max_age=0)
        checked = changed = errors = 0
        for link in links:
            member = guild.get_member(link["discord_id"])
            if member is None:
                continue  # parti du Discord : on garde la liaison pour quand il revient
            checked += 1
            try:
                plan, _ = await self.sync_member(member, link, st, members)
                changed += not plan.empty
            except (ServiceError, discord.HTTPException) as e:
                errors += 1
                log.warning("Synchro de %s : %s", member, e)
        return checked, changed, errors

    async def run_sync(self, guild: discord.Guild, st: GuildNG) -> None:
        members = await st.members(max_age=0)
        self.bot.dispatch("ng_country_update", guild.id, st.country_name, members)  # ex. le plugin Salons compteurs
        online_now: set[str] | None = None
        try:
            online_now = await st.api.online()
            online = insights.online_members(members, online_now)
            self.bot.dispatch("ng_online_update", guild.id, st.country_name, len(online))
            await self.record_online_peak(guild.id, st, len(online))
        except ServiceError as e:
            if st.should_warn(f"enligne:{e}"):
                log.info("%s : joueurs en ligne pas lus (%s)", guild.name, e)
        await self.sync_special_roles(guild, st, members, online_now)
        if st.announce_channel_id:
            await self.announce_country_changes(guild, st, members)
        await self.auto_link_pass(guild, st, members)
        checked, changed, errors = await self.sync_all(guild, st, members)
        if changed or errors:
            log.info("%s : synchro NG, %d vérifié(s), %d mis à jour, %d erreur(s)", guild.name, checked, changed, errors)
        self.set_status(
            guild.id,
            f"✅ {st.country_name} sur {st.server.capitalize()} · {len(members)} membres · {checked} compte(s) lié(s)",
        )

    async def run_status(self, guild: discord.Guild, st: GuildNG) -> None:
        try:
            await self.update_stats_message(guild, st)
        except discord.HTTPException as e:
            if st.should_warn(str(e)):
                log.warning("%s : Discord a refusé (%s)", guild.name, e)

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        """Une seule boucle pour tous les serveurs : chacun selon ses propres délais."""
        now = time.monotonic()
        for guild in list(self.bot.guilds):
            st = self.state(guild.id)
            if st is None or now < st.paused_until:
                continue
            steps = []
            if now - st.last_sync >= st.interval * 60:
                st.last_sync = now
                steps.append(("synchro", self.run_sync))
            if st.stats_channel_id and now - st.last_status >= st.status_interval * 60:
                st.last_status = now
                steps.append(("stats", self.run_status))
            if st.online_channel_id and now - st.last_online_board >= ONLINE_EVERY:
                st.last_online_board = now
                steps.append(("en jeu", self.run_online_board))
            if st.war_channel_id and now - st.last_alerts >= ALERTS_EVERY:
                st.last_alerts = now
                steps.append(("alertes", self.run_alerts))
            if st.warzone_channel_id and now - st.last_warzone >= WARZONE_EVERY:
                st.last_warzone = now
                steps.append(("warzone", self.run_warzone))
            if st.report_channel_id and self.report_due(st):
                steps.append(("rapport", self.run_report))
            if st.forum_channel_id and now - st.last_forum >= FORUM_EVERY:
                st.last_forum = now
                steps.append(("forum", self.run_forum))
            wants_enterprises = (st.announce_channel_id and st.announce_enterprises) or st.enterprise_roles or st.patron_role_id
            if wants_enterprises and now - st.last_enterprises >= ENTERPRISES_EVERY:
                st.last_enterprises = now
                steps.append(("entreprises", self.run_enterprises))
            if st.enterprise_channels and now - st.last_enterprise_channels >= ENTERPRISE_CHANNELS_EVERY:
                st.last_enterprise_channels = now
                steps.append(("salons des entreprises", self.run_enterprise_channels))
            if st.market_channel_id and now - st.last_market >= MARKET_EVERY:
                st.last_market = now
                steps.append(("marché", self.run_market))
            if st.track_activity:
                steps.append(("activité", lambda g, s: self.check_activity(s)))
            for name, step in steps:  # une étape qui plante empêche pas les autres
                if time.monotonic() < st.paused_until:
                    break
                try:
                    await step(guild, st)
                except ServiceError as e:
                    self.set_status(guild.id, self.status_for_error(e))
                    if getattr(e, "status", None) in (401, 403) or not self.api_key():
                        st.paused_until = time.monotonic() + KEY_PAUSE  # rien à faire tant que la clé est pas réglée
                    if st.should_warn(str(e)):
                        log.warning("%s : NationsGlory (%s) impossible (%s)", guild.name, name, e)
                except discord.HTTPException as e:
                    if st.should_warn(f"{name}:{e}"):
                        log.warning("%s : Discord a refusé (%s : %s)", guild.name, name, e)
                except Exception:
                    log.exception("Erreur inattendue (%s) dans NationsGlory pour %s", name, guild.name)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _link(self, member: discord.Member, name: str, st: GuildNG, *, by: discord.abc.User | None = None) -> str:
        """Enregistre la liaison, synchronise et retourne un résumé."""
        guild_id = member.guild.id
        await self.store.add_link(guild_id, member.id, name)
        await self._log_link(guild_id, member, name, linked=True, by=by)
        log.info("%s : %s lié à %s%s", member.guild.name, member, name, f" (validé par {by})" if by else "")
        self.bot.dispatch("ng_linked", member)  # ex. le plugin Bienvenue retire le rôle d'arrivée
        link = await self.store.get_link(guild_id, member.id)
        try:
            plan, entry = await self.sync_member(member, link, st)
        except ServiceError as e:
            log.warning("Synchro après liaison de %s : %s", member, e)
            return "Les rôles vont se synchroniser dans quelques minutes."
        where = (
            f"Grade dans **{st.country_name}** : **{rank_label(entry['role'])}**"
            if entry
            else f"Pas membre de **{st.country_name}** pour l'instant."
        )
        return f"{where}\n{self._describe(plan)}"

    async def _unlink(self, member: discord.Member, link: dict, st: GuildNG, *, by: discord.abc.User | None = None) -> None:
        await self.store.remove_link(member.guild.id, member.id)
        await self._log_link(member.guild.id, member, link["ng_name"], linked=False, by=by)
        async with self._locks[(member.guild.id, member.id)]:
            plan = plan_unlink({r.id for r in member.roles}, st.settings)
            await self._apply(member, plan, st, "Compte NationsGlory délié")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Un membre lié revient : on lui redonne ses rôles. Sinon on essaie de le lier par son nom,
        et si on trouve pas, on lui demande son pseudo NationsGlory."""
        st = self.state(member.guild.id)
        if st is None or member.bot:
            return
        link = await self.store.get_link(member.guild.id, member.id)
        try:
            if link is not None:
                await self.sync_member(member, link, st)
            elif not await self.try_auto_link(member, st) and st.ask_pseudo:
                await self.ask_member(member, st)
        except ServiceError as e:
            log.warning("Synchro de %s à son arrivée : %s", member, e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Un membre pas lié change de surnom (ex. il met son pseudo NG) : on réessaie la liaison automatique."""
        if before.nick == after.nick or after.bot:
            return
        st = self.state(after.guild.id)
        if st is None or not st.auto_link or await self.store.get_link(after.guild.id, after.id):
            return
        try:
            await self.try_auto_link(after, st)
        except ServiceError as e:
            log.warning("Liaison automatique de %s : %s", after, e)

    # ---- Liaison automatique et demande du pseudo ---------------------------------------------
    async def try_auto_link(self, member: discord.Member, st: GuildNG, members: dict[str, dict] | None = None,
                            taken: set[str] | None = None) -> str | None:
        """Lie le membre si un de ses noms Discord correspond à un membre du pays. Retourne le pseudo, ou None."""
        if not st.auto_link or member.bot:
            return None
        members = members if members is not None else await st.members(max_age=300)
        if taken is None:
            taken = {link["ng_name"].lower() for link in await self.store.all_links(member.guild.id)}
        name = match_member(candidate_names(member), members, taken)
        if name is None:
            return None
        entry = members[name.lower()]
        channel = member.guild.get_channel(st.validation_channel_id) if st.validation_channel_id else None
        if entry.get("role") in HIGH_RANKS and channel is not None:
            # Chef ou officier : n'importe qui pourrait prendre ce nom, un officier doit valider
            key = f"ng_auto_demande:{member.guild.id}:{member.id}:{name.lower()}"
            if await self.store.get_value(key):
                return None
            await self.store.set_value(key, "1")
            embed = make_embed("🔗 Liaison à valider (nom identique)", BLUE, user=member,
                               description=f"{member.mention} porte le nom de **{name}** "
                                           f"({rank_label(entry.get('role'))} de {st.country_name}).")
            view = discord.ui.View(timeout=None)
            view.add_item(LinkButton("ok", member.id, name))
            view.add_item(LinkButton("no", member.id, name))
            await channel.send(embed=embed, view=view)
            return None
        summary = await self._link(member, name, st)
        log.info("%s : %s lié automatiquement à %s (même nom)", member.guild.name, member, name)
        if channel is not None:
            embed = make_embed("🔗 Liaison automatique", GREEN, user=member,
                               description=f"{member.mention} ↔ **{name}** (même nom sur Discord et NationsGlory)")
            embed.add_field(name=f"Grade dans {st.country_name}", value=rank_label(entry.get("role")))
            view = discord.ui.View(timeout=None)
            view.add_item(UndoButton(member.id, name))
            await channel.send(embed=embed, view=view)
        await self._dm(member, f"🔗 Ton compte Discord a été lié tout seul à **{name}** sur NationsGlory (même nom).\n"
                               f"{summary}\nSi c'est pas toi, fais `/delier` sur le serveur.")
        return name

    async def ask_member(self, member: discord.Member, st: GuildNG) -> bool:
        """Demande son pseudo NationsGlory au membre : en MP, sinon dans le salon de liaison (une seule fois)."""
        key = f"ng_demande:{member.guild.id}:{member.id}"
        if await self.store.get_value(key):
            return False
        await self.store.set_value(key, "1")
        embed = discord.Embed(
            title=f"🔗 Bienvenue sur le serveur de {st.country_name}!", color=BLUE,
            description=f"Je trouve pas ton pseudo NationsGlory parmi les membres de **{st.country_name}**.\n"
                        "Clique sur le bouton et écris ton pseudo : tu recevras ton grade tout seul.",
        )
        try:
            await member.send(embed=embed, view=ask_view(member.guild.id))
            return True
        except discord.HTTPException:
            pass  # MP fermés
        channel = member.guild.get_channel(st.link_channel_id) if st.link_channel_id else None
        if channel is None:
            return False
        try:
            await channel.send(member.mention, embed=embed, view=ask_view(member.guild.id),
                               allowed_mentions=discord.AllowedMentions(users=[member]))
            return True
        except discord.HTTPException:
            return False

    async def auto_link_pass(self, guild: discord.Guild, st: GuildNG, members: dict[str, dict]) -> int:
        """À chaque synchro : essaie de lier les membres du Discord qui le sont pas encore."""
        if not st.auto_link:
            return 0
        links = await self.store.all_links(guild.id)
        linked = {link["discord_id"] for link in links}
        taken = {link["ng_name"].lower() for link in links}
        everyone = guild.members.values() if isinstance(guild.members, dict) else guild.members
        done = 0
        for member in list(everyone):
            if member.bot or member.id in linked:
                continue
            if name := await self.try_auto_link(member, st, members, taken):
                taken.add(name.lower())
                done += 1
        return done

    async def undo_auto_link(self, interaction: discord.Interaction, user_id: int, name: str) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        validator = interaction.guild.get_member(interaction.user.id)
        if validator is None or not self._can_validate(validator, st):
            return await reply(interaction, "⛔ T'as pas le droit d'annuler une liaison.")
        link = await self.store.get_link(interaction.guild.id, user_id)
        target = interaction.guild.get_member(user_id)
        if link is None or link["ng_name"].lower() != name.lower():
            return await reply(interaction, "Cette liaison existe déjà plus.")
        if target is not None:
            await self._unlink(target, link, st, by=interaction.user)
        else:
            await self.store.remove_link(interaction.guild.id, user_id)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else make_embed("Liaison", GREY)
        embed.color = GREY
        embed.add_field(name="Décision", value=f"↩️ Annulée par {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        if target is not None:
            await self._dm(target, f"↩️ La liaison avec **{name}** a été annulée par un officier. Fais `/lier` avec ton vrai pseudo.")

    # ---- Salon de statut (qui est en ligne + guerres) ------------------------------------------
    async def _edit_or_send(self, channel, key: str, **content) -> None:
        """Modifie le message gardé à jour dans ce salon (ou en publie un nouveau s'il a été supprimé)."""
        message_id = await self.store.get_value(key)
        if message_id:
            try:
                await channel.get_partial_message(int(message_id)).edit(**content)
                return
            except discord.NotFound:
                pass
        message = await channel.send(**content)
        await self.store.set_value(key, str(message.id))

    def _channel(self, guild: discord.Guild, channel_id: int, st: GuildNG, what: str):
        channel = guild.get_channel(channel_id)
        if channel is None and channel_id not in st.warned:
            st.warned.add(channel_id)
            log.warning("%s : salon %s introuvable (%s)", guild.name, channel_id, what)
        return channel

    async def announce_country_changes(self, guild: discord.Guild, st: GuildNG, members: dict[str, dict]) -> list[str]:
        """Annonce les nouvelles recrues, promotions et départs du pays (rien au premier tour)."""
        key = f"ng_rangs:{guild.id}"
        raw = await self.store.get_value(key)
        previous = json.loads(raw) if raw else None
        await self.store.set_value(key, json.dumps({k: [m["username"], m.get("role")] for k, m in members.items()}))
        if previous is None:
            return []
        linked = {link["ng_name"].lower(): link["discord_id"] for link in await self.store.all_links(guild.id)}

        def who(name_key: str, name: str) -> str:
            text = f"**{discord.utils.escape_markdown(name)}**"
            return f"{text} (<@{linked[name_key]}>)" if name_key in linked else text

        lines = []
        for name_key, m in members.items():
            role = m.get("role")
            if name_key not in previous:
                if st.announce["arrivees"]:
                    lines.append(f"👋 {who(name_key, m['username'])} rejoint **{st.country_name}** comme {rank_label(role)}!")
            elif st.announce["promotions"] and role in RANKS and previous[name_key][1] in RANKS:
                if RANKS.index(role) < RANKS.index(previous[name_key][1]):
                    lines.append(f"🎉 {who(name_key, m['username'])} passe **{rank_label(role)}**!")
        if st.announce["departs"]:
            for name_key in previous.keys() - members.keys():
                lines.append(f"🚪 **{discord.utils.escape_markdown(previous[name_key][0])}** a quitté {st.country_name}.")
        if not lines:
            return []
        channel = self._channel(guild, st.announce_channel_id, st, "annonces du pays")
        if channel is None:
            return lines
        if len(lines) > 10:
            lines = lines[:10] + [f"… et {len(lines) - 10} autres changements."]
        users = [discord.Object(uid) for uid in linked.values()]
        await channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions(users=users, roles=False, everyone=False))
        return lines

    async def update_stats_message(self, guild: discord.Guild, st: GuildNG) -> None:
        channel = self._channel(guild, st.stats_channel_id, st, "stats NG")
        if channel is None:
            return
        cid = await st.country()
        members = await st.members(max_age=120)
        card = await st.api.get_country(cid, max_age=120) or {}
        embed = build_stats_embed(st.api.server, st.country_name, card, members)
        extra = []
        try:
            extra.append(f"🟢 En ligne : {len(insights.online_members(members, await st.api.online()))}")
            wars = await st.api.wars(cid)
            active = sum(1 for w in wars if w.get("status") == "in_progress")
            pending = sum(1 for w in wars if w.get("status") == "negotiation")
            extra.append(f"⚔️ Guerres en cours : {active}" + (f" (+{pending} en négociation)" if pending else ""))
        except ServiceError:
            pass
        level = alerts.power_level(card, 0)
        if level == alerts.POWER_UNDER:
            extra.append("🚨 **Sous le power : attaquable!**")
        if extra:
            embed.add_field(name="En ce moment", value="\n".join(extra), inline=False)
        await self._edit_or_send(channel, f"stats_message:{channel.id}", embed=embed)

    # ---- Guerres, power et warzone ------------------------------------------------------------
    async def _load(self, key: str):
        raw = await self.store.get_value(key)
        return json.loads(raw) if raw else None

    async def _save(self, key: str, value) -> None:
        await self.store.set_value(key, json.dumps(value))

    async def collect_alerts(self, guild: discord.Guild, st: GuildNG) -> tuple[list[str], bool]:
        """Compare l'état du pays au tour d'avant. Retourne (lignes à publier, mentionner le rôle?)."""
        cid = await st.country()
        lines: list[str] = []
        important = False

        # Guerres du pays : déclarations, débuts, fins, assauts
        wars = await st.api.wars(cid)
        key = f"ng_guerres:{guild.id}"
        saved = await self._load(key)
        previous = saved.get("statuts") if isinstance(saved, dict) else None
        seen_assaults: dict = (saved or {}).get("assauts", {}) if isinstance(saved, dict) else {}
        for event in alerts.war_events(previous, wars, cid):
            lines.append(alerts.war_line(event, cid))
            important |= event.important
        new_assaults: dict[str, int] = {}
        if st.announce_assaults:
            for w in wars:
                if w.get("status") != "in_progress":
                    continue
                wid = str(w["war_id"])
                detail = await st.api.war(int(wid))
                if detail is None:
                    continue
                known = seen_assaults.get(wid)
                if known is None and previous is not None and previous.get(wid) in (None, "negotiation"):
                    known = 0  # guerre qui vient de commencer : tous ses assauts sont nouveaux
                # sinon (premier tour, option qu'on vient d'activer) : on prend note sans annoncer
                events, new_assaults[wid] = alerts.assault_events(detail, known, cid)
                lines += [alerts.war_line(e, cid) for e in events]
        await self._save(key, {"statuts": alerts.war_snapshot(wars), "assauts": new_assaults})

        # Power du pays et ennemis attaquables
        card = await st.api.get_country(cid, max_age=120) or {}
        if st.power_alert:
            key = f"ng_power:{guild.id}"
            previous_level = await self.store.get_value(key)
            level = alerts.power_level(card, st.power_margin)
            message, mention = alerts.power_alert(previous_level, level, card, st.country_name)
            if message:
                lines.append(message)
                important |= mention
            if level:
                await self.store.set_value(key, level)
        if st.enemy_alert:
            enemies = [e for e in ((card.get("relations") or {}).get("enemies") or []) if isinstance(e, str)]
            key = f"ng_ennemis:{guild.id}"
            previous_ids = await self._load(key)
            now_attackable = alerts.attackable_enemies(enemies, await st.api.countries(max_age=300)) if enemies else {}
            enemy_lines = alerts.enemy_lines(previous_ids, now_attackable)
            lines += enemy_lines
            important |= bool(enemy_lines)
            await self._save(key, sorted(now_attackable))
        if st.diplomacy_alert or st.watchlist:
            more, grave = await self.diplomacy_and_watch(guild, st, card)
            lines += more
            important |= grave
        return lines, important

    async def run_alerts(self, guild: discord.Guild, st: GuildNG) -> list[str]:
        lines, important = await self.collect_alerts(guild, st)
        if not lines:
            return []
        channel = self._channel(guild, st.war_channel_id, st, "guerres et alertes")
        if channel is None:
            return lines
        if len(lines) > 15:
            lines = lines[:15] + [f"… et {len(lines) - 15} autres."]
        text = "\n".join(lines)
        roles = []
        if important and st.alert_role_id:
            text = f"<@&{st.alert_role_id}>\n{text}"
            roles = [discord.Object(st.alert_role_id)]
        await channel.send(truncate(text, 2000), allowed_mentions=discord.AllowedMentions(roles=roles, users=False, everyone=False))
        return lines

    async def run_warzone(self, guild: discord.Guild, st: GuildNG) -> bool:
        """Publie le classement warzone quand une journée (ou une semaine, le dimanche) est close à 20 h, heure de Paris."""
        posted = await self._post_warzone(guild, st, "daily")
        if st.warzone_weekly:
            posted |= await self._post_warzone(guild, st, "weekly")
        return posted

    async def _post_warzone(self, guild: discord.Guild, st: GuildNG, period: str) -> bool:
        cid = await st.country()
        try:
            rows = await st.api.warzone(period)
        except NationsGloryError as e:
            if e.status == 503:
                return False  # pas encore de période close publiée sur ce serveur
            raise
        day, top, ours = alerts.warzone_summary(rows, cid)
        key = f"ng_warzone{'' if period == 'daily' else '_semaine'}:{guild.id}"
        if not day or await self.store.get_value(key) == day:
            return False
        channel = self._channel(guild, st.warzone_channel_id, st, "classement warzone")
        if channel is None:
            return False
        if period == "daily":
            title = f"🏁 Warzone du {day} · {st.api.server.capitalize()}"
            footer = "Journée close à 20 h (heure de Paris) · API NationsGlory"
        else:
            start = rows[0].get("period_start") if rows else None
            title = f"🏆 Warzone de la semaine{f' du {start}' if start else ''} au {day} · {st.api.server.capitalize()}"
            footer = "Semaine close le dimanche à 20 h (heure de Paris) · API NationsGlory"
        embed = discord.Embed(title=title, color=ORANGE if period == "daily" else 0xF1C40F, description="\n".join(top))
        if ours:
            embed.add_field(name=st.country_name, value=ours, inline=False)
        elif not any(r.get("country_id") == cid for r in rows):
            embed.add_field(name=st.country_name, value="Pas classé." , inline=False)
        embed.set_footer(text=footer)
        await channel.send(embed=embed)
        await self.store.set_value(key, day)
        return True

    # ---- Activité des membres (dernière connexion) ---------------------------------------------
    def _activity_key(self, st: GuildNG) -> str:
        return f"ng_connexions:{st.api.server}:{st.country_id}"

    async def _logins(self, st: GuildNG) -> dict[str, list]:
        await st.country()
        return await self._load(self._activity_key(st)) or {}

    async def check_activity(self, st: GuildNG) -> str | None:
        """Vérifie la dernière connexion d'UN membre (le plus vieux à vérifier). Partagé par pays :
        deux serveurs Discord du même pays font pas deux fois le travail. Retourne le pseudo vérifié."""
        cid = await st.country()
        now = time.time()
        tracker = (st.api.server, cid)
        if now - self._last_activity.get(tracker, 0) < ACTIVITY_EVERY:
            return None
        self._last_activity[tracker] = now
        members = await st.members(max_age=300)
        logins = await self._logins(st)
        for gone in set(logins) - set(members):
            logins.pop(gone)  # parti du pays : on oublie
        name = insights.next_to_check(members, logins, now, ACTIVITY_REFRESH)
        if name is None:
            await self._save(self._activity_key(st), logins)
            return None
        # La fiche publique donne d'un coup la dernière connexion ici, le temps de jeu, les succès et le grade
        # boutique. Un joueur sans compte sur le site a pas de fiche (404) : on lit juste sa dernière connexion.
        profile = await st.api.profile(name, max_age=60)
        fallback = None
        here = next((x for x in (profile or {}).get("servers") or []
                     if isinstance(x, dict) and x.get("server") == st.api.server), {})
        if profile is None or (not profile.get("private") and not here.get("last_login_on")):
            fallback = await st.api.last_login(name)
        logins[name.lower()] = insights.profile_entry(profile, st.api.server, fallback, now)
        await self._save(self._activity_key(st), logins)
        return name

    # ---- Validation des liaisons ----------------------------------------------------------
    @staticmethod
    def _can_validate(member: discord.Member, st: GuildNG) -> bool:
        if st.validator_role_id and any(r.id == st.validator_role_id for r in member.roles):
            return True
        return member.guild_permissions.manage_roles

    async def handle_decision(self, interaction: discord.Interaction, action: str, user_id: int, name: str) -> None:
        guild = interaction.guild
        st = await self._st(interaction)
        if st is None:
            return
        validator = guild.get_member(interaction.user.id) if guild else None
        if validator is None or not self._can_validate(validator, st):
            return await reply(interaction, "⛔ T'as pas le droit de valider les liaisons.")
        target = guild.get_member(user_id)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else make_embed("Demande", GREY)

        if action == "no":
            embed.color = RED
            embed.add_field(name="Décision", value=f"✖️ Refusé par {interaction.user.mention}", inline=False)
            await interaction.response.edit_message(embed=embed, view=None)
            if target:
                await self._dm(target, f"✖️ Ta demande de liaison avec **{name}** a été refusée.")
            return

        problem = None
        other = await self.store.get_link_by_name(guild.id, name)
        if target is None:
            problem = "Le membre a quitté le Discord."
        elif other is not None and other["discord_id"] != user_id:
            problem = f"**{name}** est déjà lié à <@{other['discord_id']}>."
        if problem:
            embed.color = GREY
            embed.add_field(name="Décision", value=f"⚠️ {problem}", inline=False)
            return await interaction.response.edit_message(embed=embed, view=None)

        embed.color = GREEN
        embed.add_field(name="Décision", value=f"✅ Accepté par {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        summary = await self._link(target, name, st, by=interaction.user)
        await self._dm(target, f"✅ Ton compte Discord est lié à **{name}** sur NationsGlory!\n{summary}")

    @staticmethod
    async def _dm(member: discord.Member, text: str) -> None:
        try:
            await member.send(text)
        except discord.HTTPException:
            pass

    # ---- Affichage --------------------------------------------------------------------
    @staticmethod
    def _describe(plan: SyncPlan) -> str:
        parts = []
        if plan.add_roles:
            parts.append("➕ " + " ".join(f"<@&{r}>" for r in plan.add_roles))
        if plan.remove_roles:
            parts.append("➖ " + " ".join(f"<@&{r}>" for r in plan.remove_roles))
        return "\n".join(parts) or "Rôles déjà à jour."

    async def _log_link(
        self, guild_id: int, user: discord.abc.User, name: str, *, linked: bool, by: discord.abc.User | None = None
    ) -> None:
        if linked:
            embed = make_embed("🔗 Compte NationsGlory lié", GREEN, user=user, description=f"{user.mention} ↔ **{name}**")
        else:
            embed = make_embed("🔓 Compte NationsGlory délié", GREY, user=user, description=f"{user.mention} ↮ **{name}**")
        if by is not None and by.id != user.id:
            embed.add_field(name="Par", value=by.mention)
        await self.host.send_log(guild_id, embed, "membres")

    # ---- Commandes pour les membres --------------------------------------------------------
    @app_commands.command(name="lier", description="Lie ton compte Discord à ton pseudo NationsGlory")
    @app_commands.describe(pseudo="Ton pseudo NationsGlory")
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id, i.user.id))
    @app_commands.guild_only()
    async def lier(self, interaction: discord.Interaction, pseudo: Pseudo) -> None:
        await self.request_link(interaction, interaction.user, pseudo)

    async def request_link(self, interaction: discord.Interaction, member: discord.Member, pseudo: str) -> None:
        """/lier ou le formulaire du bouton « Lier mon compte » (qui peut venir d'un MP)."""
        guild = member.guild
        st = self.state(guild.id)
        if st is None:
            return await reply(interaction, "La synchro NationsGlory est pas activée sur ce serveur (voir le panel).")
        guild_id = guild.id
        pseudo = pseudo.strip()
        if not NAME_RE.match(pseudo):
            return await reply(interaction, "Pseudo invalide (lettres, chiffres et _ seulement).")
        await interaction.response.defer(ephemeral=True)

        if link := await self.store.get_link(guild_id, member.id):
            return await reply(interaction, f"Ton compte est déjà lié à **{link['ng_name']}**. Fais `/delier` d'abord.")
        if await self.store.get_link_by_name(guild_id, pseudo):
            return await reply(interaction, f"**{pseudo}** est déjà lié à un autre membre. Contacte un officier si c'est une erreur.")

        entry = (await st.members()).get(pseudo.lower())
        if entry is None and await st.api.get_player(pseudo) is None:
            return await reply(interaction, f"Je trouve pas **{pseudo}** sur le serveur NationsGlory {st.api.server}.")
        name = entry["username"] if entry else pseudo

        if not st.validation_channel_id:
            summary = await self._link(member, name, st)
            return await reply(interaction, f"✅ Ton compte est lié à **{name}**!\n{summary}")

        channel = guild.get_channel(st.validation_channel_id)
        if channel is None:
            log.error("%s : salon de validation %s introuvable", guild.name, st.validation_channel_id)
            return await reply(interaction, "⚠️ Le salon de validation est mal configuré. Préviens un admin.")
        embed = make_embed(
            "🔗 Demande de liaison", BLUE, user=member,
            description=f"{member.mention} dit être **{name}** sur NationsGlory.",
        )
        embed.add_field(name=f"Grade dans {st.country_name}", value=rank_label(entry["role"]) if entry else "Pas dans le pays")
        embed.add_field(name="Compte Discord créé", value=discord.utils.format_dt(member.created_at, "R"))
        view = discord.ui.View(timeout=None)
        view.add_item(LinkButton("ok", member.id, name))
        view.add_item(LinkButton("no", member.id, name))
        await channel.send(embed=embed, view=view)
        await reply(interaction, "📨 Demande envoyée aux officiers. Tu vas recevoir un MP quand elle sera acceptée.")

    @app_commands.command(name="liaison-panneau", description="Publie le bouton « Lier mon compte NationsGlory » dans ce salon")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def liaison_panneau(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        embed = discord.Embed(title=f"🔗 Lier ton compte NationsGlory · {st.country_name}", color=BLUE, description=(
            "Clique sur le bouton et écris ton pseudo NationsGlory.\n"
            "Ton grade du pays devient ton rôle Discord, et il se met à jour tout seul."))
        await interaction.channel.send(embed=embed, view=ask_view(interaction.guild.id))
        await reply(interaction, "✅ Panneau publié.")

    @app_commands.command(name="delier", description="Délie ton compte NationsGlory")
    @app_commands.guild_only()
    async def delier(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        link = await self.store.get_link(interaction.guild.id, interaction.user.id)
        if link is None:
            return await reply(interaction, "Ton compte est pas lié.")
        await self._unlink(interaction.user, link, st)
        await reply(interaction, f"🔓 Ton compte est plus lié à **{link['ng_name']}**.")

    @app_commands.command(name="compte", description="Affiche le compte NationsGlory lié à un membre")
    @app_commands.describe(membre="Le membre (toi par défaut)")
    @app_commands.guild_only()
    async def compte(self, interaction: discord.Interaction, membre: discord.Member | None = None) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        target = membre or interaction.user
        link = await self.store.get_link(interaction.guild.id, target.id)
        if link is None:
            if target.id == interaction.user.id:
                return await reply(interaction, "Ton compte est pas lié. Fais `/lier` avec ton pseudo NationsGlory.")
            return await reply(interaction, f"{target.mention} a pas lié son compte.")
        await interaction.response.defer(ephemeral=True)
        embed = make_embed(f"🎮 {link['ng_name']}", BLUE, user=target)
        embed.set_thumbnail(url=head_url(link["ng_name"]))
        linked_at = datetime.fromtimestamp(link["linked_at"], tz=timezone.utc)
        embed.add_field(name="Lié", value=discord.utils.format_dt(linked_at, "R"))
        try:
            entry = (await st.members()).get(link["ng_name"].lower())
            embed.add_field(name=f"Grade dans {st.country_name}", value=rank_label(entry["role"]) if entry else "Pas dans le pays")
        except ServiceError:
            embed.add_field(name="Grade", value="*(NationsGlory injoignable)*")
        await reply(interaction, embed=embed)

    async def _country_embed(self, st: GuildNG, cid: str, members: dict[str, dict]) -> tuple[discord.Embed, list[discord.File]]:
        """Fiche d'un pays : stats, diplomatie, drapeau et membres par grade (noms de grades du pays)."""
        country = await st.api.get_country(cid, max_age=60) or {}
        names = {c.get("country_id"): c.get("name") for c in await st.api.countries()}
        title = country.get("name") or (st.country_name if cid == st.country_id else cid)
        custom = {k: v for k, v in (country.get("role_names") or {}).items() if isinstance(v, str) and v}

        embed = discord.Embed(
            title=f"🏳️ {title}",
            description=f"Serveur **{st.api.server.capitalize()}** · **{len(members)}** membres",
            color=BLUE,
        )
        if desc := country.get("description"):
            if desc.strip().lower() != "no description set":
                embed.description += f"\n*{truncate(desc, 300)}*"
        fields = (
            ("power", "⚡ Power"), ("power_max", "Power max"), ("claims", "🗺️ Claims"),
            ("bank_balance", "💰 Trésorerie"), ("score", "🏆 Score"), ("mmr", "🏅 MMR"), ("level", "Niveau"),
        )
        for key, label in fields:
            value = country.get(key)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                text = fmt_int(value) if isinstance(value, int) else str(value)
                embed.add_field(name=label, value=f"{text} $" if key == "bank_balance" else text)
        if isinstance(country.get("created_on"), str):
            embed.add_field(name="📅 Fondé le", value=country["created_on"])
        if alerts.power_level(country, 0) == alerts.POWER_UNDER:
            embed.add_field(name="⚠️ Attention", value="Sous le power : le pays est attaquable!", inline=False)
        if tags := [t for t in country.get("tags") or [] if isinstance(t, str)]:
            embed.add_field(name="🏷️ Orientations", value=", ".join(tags), inline=False)
        relations = country.get("relations") or {}
        for key, label in (("allies", "🤝 Alliés"), ("enemies", "⚔️ Ennemis")):
            ids = [i for i in relations.get(key) or [] if isinstance(i, str)]
            if ids:
                listed = sorted((names.get(i) or "?" for i in ids), key=str.lower)
                embed.add_field(name=f"{label} ({len(ids)})", value=truncate(", ".join(listed)), inline=False)

        by_rank: dict[str, list[str]] = {r: [] for r in RANKS}
        others: list[str] = []
        for m in members.values():
            (by_rank[m["role"]] if m["role"] in by_rank else others).append(m["username"])
        for rank in RANKS:
            listed = sorted(by_rank[rank], key=str.lower)
            if listed:
                label = custom.get(rank) or rank_label(rank)
                embed.add_field(name=f"{RANK_EMOJI[rank]} {label} ({len(listed)})", value=truncate(", ".join(listed)), inline=False)
        if others:
            embed.add_field(name=f"Autres ({len(others)})", value=truncate(", ".join(sorted(others, key=str.lower))), inline=False)

        files = []
        try:
            if png := await st.api.image(country.get("flag_sha256")):
                files.append(discord.File(io.BytesIO(png), filename="drapeau.png"))
                embed.set_thumbnail(url="attachment://drapeau.png")
        except ServiceError:
            pass  # pas de drapeau, c'est pas grave
        embed.set_footer(text="Données NationsGlory, mises à jour aux 5 minutes")
        return embed, files

    @app_commands.command(name="pays", description="La fiche d'un pays NationsGlory (le tien par défaut)")
    @app_commands.describe(nom="Un autre pays du même serveur (ex. Amour)")
    @app_commands.guild_only()
    async def pays(self, interaction: discord.Interaction, nom: app_commands.Range[str, 1, 64] | None = None) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        if nom and nom.strip().lower() != st.country_name.lower():
            try:
                cid, _ = await st.api.resolve_country(nom)
            except NationsGloryError as e:
                if e.status == 404:
                    return await reply(interaction, f"Je trouve pas le pays **{nom}** sur {st.api.server.capitalize()}.")
                raise
            members = await st.api.country_members(cid)
        else:
            cid = await st.country()
            members = await st.members()
        embed, files = await self._country_embed(st, cid, members)
        await interaction.followup.send(embed=embed, files=files)

    @app_commands.command(name="enligne", description="Les membres du pays connectés sur NationsGlory")
    @app_commands.guild_only()
    async def enligne(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        members = await st.members()
        online = await st.api.online()
        here = insights.online_members(members, online)
        custom = await self._role_names(st)
        embed = discord.Embed(
            title=f"🟢 {st.country_name} : {len(here)} en ligne",
            description=f"{len(online)} joueurs connectés sur {st.api.server.capitalize()}",
            color=GREEN if here else GREY,
        )
        for rank in RANKS:
            listed = [m["username"] for m in here if m.get("role") == rank]
            if listed:
                embed.add_field(name=f"{RANK_EMOJI[rank]} {custom.get(rank) or rank_label(rank)}",
                                value=truncate(", ".join(listed)), inline=False)
        if not here:
            embed.description += "\nPersonne du pays pour l'instant. 😴"
        embed.set_footer(text="Liste des connectés mise à jour aux 5 minutes par NationsGlory")
        await reply(interaction, embed=embed, ephemeral=False)

    async def _role_names(self, st: GuildNG) -> dict[str, str]:
        """Noms de grades choisis par le pays (Empereur, Conseiller…), si la fiche les donne."""
        try:
            card = await st.api.get_country(await st.country(), max_age=300)
        except ServiceError:
            return {}
        return {k: v for k, v in ((card or {}).get("role_names") or {}).items() if isinstance(v, str) and v}

    @app_commands.command(name="classement-pays", description="Les pays du serveur NationsGlory classés")
    @app_commands.describe(critere="Ce qu'on compare")
    @app_commands.choices(critere=[app_commands.Choice(name=label, value=key)
                                   for key, (_, label, _) in insights.RANKINGS.items()])
    @app_commands.guild_only()
    async def classement(self, interaction: discord.Interaction, critere: str = "power") -> None:
        st = await self._st(interaction)
        if st is None:
            return
        if critere not in insights.RANKINGS:
            return await reply(interaction, "Critère inconnu.")
        await interaction.response.defer()
        cid = await st.country()
        rows = insights.ranking(await st.api.countries(max_age=300), critere)
        lines, ours = insights.ranking_lines(rows, critere, cid)
        label = insights.RANKINGS[critere][1]
        embed = discord.Embed(title=f"🏆 Classement {label} · {st.api.server.capitalize()}",
                              description="\n".join(lines) or "Aucun pays.", color=ORANGE)
        if ours:
            embed.add_field(name=st.country_name, value=ours, inline=False)
        embed.set_footer(text=f"{len(rows)} pays avec des membres · données NationsGlory")
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="guerre", description="Le détail d'une guerre : assauts, missiles, accords")
    @app_commands.describe(numero="Le numéro de la guerre (dans /guerres)")
    @app_commands.guild_only()
    async def guerre(self, interaction: discord.Interaction, numero: app_commands.Range[int, 1, 10**9]) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        war = await st.api.war(numero)
        if war is None:
            return await reply(interaction, f"La guerre n°{numero} existe pas (ou est finie depuis plus de 30 jours).")
        embed = discord.Embed(
            title=f"⚔️ Guerre n°{numero} : {war.get('attacker_name')} contre {war.get('defender_name')}",
            description=(f"{alerts.STATUS_LABELS.get(war.get('status'), war.get('status'))} · "
                         f"motif : {alerts.reason_label(war.get('reason'))}"),
            color=RED,
        )
        dates = [f"Déclarée le {war.get('declared_on')}"]
        if war.get("started_on"):
            dates.append(f"commencée le {war['started_on']}")
        if war.get("ended_on"):
            dates.append(f"finie le {war['ended_on']}")
        embed.add_field(name="Dates", value=", ".join(dates), inline=False)
        winner = war.get("winner")
        if winner in ("attacker", "defender"):
            embed.add_field(name="🏆 Vainqueur", value=war.get(f"{winner}_name") or "?", inline=False)
        for name, value in insights.war_detail_lines(war, {}):
            embed.add_field(name=name, value=truncate(value), inline=False)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="alliance", description="L'alliance du pays (ou celle d'un autre pays)")
    @app_commands.describe(pays="Un autre pays du même serveur")
    @app_commands.guild_only()
    async def alliance(self, interaction: discord.Interaction, pays: app_commands.Range[str, 1, 64] | None = None) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        if pays:
            try:
                cid, cname = await st.api.resolve_country(pays)
            except NationsGloryError as e:
                if e.status == 404:
                    return await reply(interaction, f"Je trouve pas le pays **{pays}**.")
                raise
        else:
            cid, cname = await st.country(), st.country_name
        countries = await st.api.countries(max_age=300)
        row = next((c for c in countries if c.get("country_id") == cid), {})
        key = row.get("alliance_key")
        data = await st.api.alliance(key) if key else None
        if data is None:
            return await reply(interaction, f"**{cname}** est dans aucune alliance.", ephemeral=False)
        members = sorted((c.get("name") or "?" for c in countries if c.get("alliance_key") == key), key=str.lower)
        embed = discord.Embed(title=f"🛡️ Alliance {data.get('name')}", color=BLUE)
        if desc := data.get("description"):
            embed.description = truncate(desc, 500)
        for field, label in (("prestige_rank", "Rang de prestige"), ("prestige_points", "Prestige"),
                             ("total_claims", "Claims"), ("bank", "Banque"), ("founded_on", "Fondée le")):
            value = data.get(field)
            if value is not None:
                text = fmt_int(value) if isinstance(value, int) else str(value)
                embed.add_field(name=label, value=f"{text} $" if field == "bank" else text)
        embed.add_field(name="Candidatures", value="Ouvertes" if data.get("opened") else "Fermées")
        if members:
            embed.add_field(name=f"Pays ({len(members)})", value=truncate(", ".join(members)), inline=False)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="inactifs", description="Les membres du pays pas connectés depuis X jours")
    @app_commands.describe(jours="Depuis combien de jours (7 par défaut)")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def inactifs(self, interaction: discord.Interaction, jours: app_commands.Range[int, 1, 90] = 7) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        members = await st.members()
        logins = await self._logins(st)
        report = insights.inactivity(members, logins, paris_today(), jours)
        custom = await self._role_names(st)

        def who(m: dict) -> str:
            return f"{m['username']} ({custom.get(m.get('role')) or rank_label(m.get('role'))})"

        embed = discord.Embed(title=f"😴 {st.country_name} : inactifs depuis {jours} jours ou plus", color=GREY)
        lines = [f"{who(m)} · {n} j" for m, n in report["inactifs"]]
        embed.description = truncate("\n".join(lines), 4000) or "Personne! Tout le monde est passé récemment. 💪"
        if report["absents"]:
            embed.add_field(name=f"Plus de {insights.MISSING_DAYS} jours ({len(report['absents'])})",
                            value=truncate(", ".join(who(m) for m in report["absents"])), inline=False)
        if report["prives"]:
            embed.add_field(name=f"Profil privé ({len(report['prives'])})",
                            value=truncate(", ".join(m["username"] for m in report["prives"])), inline=False)
        missing = len(report["inconnus"])
        footer = f"{report['verifies']}/{len(members)} membres vérifiés"
        if missing:
            footer += f" · les {missing} autres le seront dans les prochaines minutes"
        embed.set_footer(text=footer + " · dernière connexion publiée par NationsGlory (au jour près)")
        await reply(interaction, embed=embed)

    @app_commands.command(name="guerres", description="Les guerres du pays : en cours, en négociation et récentes")
    @app_commands.guild_only()
    async def guerres(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        cid = await st.country()
        wars = await st.api.wars(cid)
        embed = discord.Embed(title=f"⚔️ Guerres de {st.country_name}", color=RED)

        def line(w: dict) -> str:
            enemy = w.get("defender_name") if w.get("attacker_id") == cid else w.get("attacker_name")
            side = "🗡️ attaque" if w.get("attacker_id") == cid else "🛡️ défend"
            text = f"{side} contre {alerts.bold(enemy)} · {alerts.reason_label(w.get('reason'))} · depuis le {w.get('declared_on')}"
            if w.get("status") == "finished":
                winner = w.get("winner")
                if winner in ("attacker", "defender"):
                    won = (winner == "attacker") == (w.get("attacker_id") == cid)
                    text += " · 🏆 gagnée" if won else " · 💀 perdue"
                else:
                    text += " · 🕊️ paix"
            return text

        for status in ("in_progress", "negotiation", "finished"):
            rows = [w for w in wars if w.get("status") == status]
            if rows:
                shown = [line(w) for w in rows[:8]]
                if len(rows) > 8:
                    shown.append(f"… et {len(rows) - 8} autres")
                embed.add_field(name=f"{alerts.STATUS_LABELS[status]} ({len(rows)})", value=truncate("\n".join(shown)), inline=False)
        if not wars:
            embed.description = "Aucune guerre en cours, et aucune finie dans les 30 derniers jours. 🕊️"
        embed.set_footer(text="Données NationsGlory, mises à jour aux 10 minutes")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- Commandes admin : /synchro ... -------------------------------------------------------
    @synchro.command(name="tout", description="Synchronise tous les comptes liés maintenant")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def synchro_tout(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        checked, changed, errors = await self.sync_all(interaction.guild, st)
        msg = f"🔄 {checked} membre(s) vérifié(s), {changed} mis à jour"
        if errors:
            msg += f", {errors} erreur(s) (détails dans la console)"
        await reply(interaction, msg + ".")

    @synchro.command(name="membre", description="Synchronise un membre maintenant")
    @app_commands.describe(membre="Le membre")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def synchro_membre(self, interaction: discord.Interaction, membre: discord.Member) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        link = await self.store.get_link(interaction.guild.id, membre.id)
        if link is None:
            return await reply(interaction, f"{membre.mention} a pas lié son compte.")
        await interaction.response.defer(ephemeral=True)
        plan, entry = await self.sync_member(membre, link, st, await st.members(max_age=0))
        grade = rank_label(entry["role"]) if entry else "pas dans le pays"
        await reply(interaction, f"🔄 {membre.mention} (**{link['ng_name']}**, {grade})\n{self._describe(plan)}")

    @synchro.command(name="forcer", description="Lie un membre à un pseudo NationsGlory sans validation")
    @app_commands.describe(membre="Le membre Discord", pseudo="Son pseudo NationsGlory")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def synchro_forcer(self, interaction: discord.Interaction, membre: discord.Member, pseudo: Pseudo) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        pseudo = pseudo.strip()
        if not NAME_RE.match(pseudo):
            return await reply(interaction, "Pseudo invalide.")
        await interaction.response.defer(ephemeral=True)
        other = await self.store.get_link_by_name(interaction.guild.id, pseudo)
        if other is not None and other["discord_id"] != membre.id:
            return await reply(interaction, f"**{pseudo}** est déjà lié à <@{other['discord_id']}>. Fais `/synchro retirer` avant.")
        entry = (await st.members()).get(pseudo.lower())
        name = entry["username"] if entry else pseudo
        summary = await self._link(membre, name, st, by=interaction.user)
        await reply(interaction, f"🔗 {membre.mention} est lié à **{name}**.\n{summary}")

    @synchro.command(name="retirer", description="Délie le compte NationsGlory d'un membre")
    @app_commands.describe(membre="Le membre")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def synchro_retirer(self, interaction: discord.Interaction, membre: discord.Member) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        link = await self.store.get_link(interaction.guild.id, membre.id)
        if link is None:
            return await reply(interaction, f"{membre.mention} a pas de compte lié.")
        await self._unlink(membre, link, st, by=interaction.user)
        await reply(interaction, f"🔓 {membre.mention} est plus lié à **{link['ng_name']}**.")

    @synchro.command(name="liste", description="Comptes liés et membres du pays pas encore liés")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def synchro_liste(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        links = await self.store.all_links(interaction.guild.id)
        linked_names = {link["ng_name"].lower() for link in links}
        lines = [f"<@{link['discord_id']}> → **{link['ng_name']}**" for link in links]
        embed = discord.Embed(
            title=f"🔗 Comptes liés ({len(links)})",
            description=truncate("\n".join(lines), 4096) or "Aucun pour l'instant.",
            color=BLUE,
        )
        try:
            missing = sorted((m["username"] for key, m in (await st.members()).items() if key not in linked_names), key=str.lower)
            if missing:
                embed.add_field(name=f"Pas encore liés ({len(missing)})", value=truncate(", ".join(missing)), inline=False)
        except ServiceError:
            pass
        await reply(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NGSync(bot))
