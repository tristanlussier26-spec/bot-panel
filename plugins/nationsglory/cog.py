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
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .api import NationsGlory, RateLimiter, normalize_target
from .common import BLUE, GREEN, GREY, RED, NationsGloryError, ServiceError, make_embed, reply, truncate
from .logic import RANK_LABELS, RANKS, ConfigError, SyncPlan, parse_settings, plan_sync, plan_unlink
from .store import Store

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
                 ("bank", "💰 Trésorerie"), ("mmr", "🏅 MMR"), ("level", "Niveau"))


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
            stats.append(f"{label} : {text}{' $' if key == 'bank' else ''}")
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
        self.stats_channel_id = chan("salon_stats_id")
        self.status_interval = max(1, int(cfg.get("statut_minutes", 5)))
        self.announce_channel_id = chan("salon_annonces_id")
        self.announce = {
            k: bool(cfg.get(f"annoncer_{k}", d)) for k, d in (("arrivees", True), ("promotions", True), ("departs", False))
        }
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


class NGSync(commands.Cog):
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

    async def cog_load(self) -> None:
        await self.store.connect()
        if not self.api_key():
            log.warning("NG_API_KEY est vide : mets la clé de l'API NationsGlory dans l'onglet Plugins de Bot Panel")
        self.bot.add_dynamic_items(LinkButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.bot.remove_dynamic_items(LinkButton)
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
            parse_settings(ng)
        except ConfigError as e:
            errors.append(f"NationsGlory : {e}")
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
        if st.announce_channel_id:
            await self.announce_country_changes(guild, st, members)
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
            try:
                if now - st.last_sync >= st.interval * 60:
                    st.last_sync = now
                    await self.run_sync(guild, st)
                if st.stats_channel_id and now - st.last_status >= st.status_interval * 60:
                    st.last_status = now
                    await self.run_status(guild, st)
            except ServiceError as e:
                self.set_status(guild.id, self.status_for_error(e))
                if getattr(e, "status", None) in (401, 403) or not self.api_key():
                    st.paused_until = now + KEY_PAUSE  # rien à faire tant que la clé est pas réglée
                if st.should_warn(str(e)):
                    log.warning("%s : synchro NG impossible (%s)", guild.name, e)
            except Exception:
                log.exception("Erreur inattendue dans la synchro NG de %s", guild.name)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()
        try:  # comptes liés avec l'ancienne synchro NG intégrée au Bot Amour : repris une seule fois
            count = await self.store.import_legacy(getattr(self.bot, "db", None), [g.id for g in self.bot.guilds])
            if count:
                log.info("%d compte(s) lié(s) repris de l'ancienne synchro NG", count)
        except Exception:  # noqa: BLE001
            log.exception("Reprise des comptes liés")

    async def _link(self, member: discord.Member, name: str, st: GuildNG, *, by: discord.abc.User | None = None) -> str:
        """Enregistre la liaison, synchronise et retourne un résumé."""
        guild_id = member.guild.id
        await self.store.add_link(guild_id, member.id, name)
        await self._log_link(guild_id, member, name, linked=True, by=by)
        log.info("%s : %s lié à %s%s", member.guild.name, member, name, f" (validé par {by})" if by else "")
        if (welcome := self.bot.get_cog("Welcome")) is not None:
            await welcome.on_ng_linked(member)  # retire le rôle d'arrivée restreint, si configuré
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
        """Un membre lié revient sur le Discord : on lui redonne ses rôles."""
        st = self.state(member.guild.id)
        link = await self.store.get_link(member.guild.id, member.id) if st else None
        if link is None:
            return
        try:
            await self.sync_member(member, link, st)
        except ServiceError as e:
            log.warning("Synchro de %s à son arrivée : %s", member, e)

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
        card = await st.api.get_country(cid) or {}
        embed = build_stats_embed(st.api.server, st.country_name, card, members)
        await self._edit_or_send(channel, f"stats_message:{channel.id}", embed=embed)

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
        st = await self._st(interaction)
        if st is None:
            return
        guild_id = interaction.guild.id
        pseudo = pseudo.strip()
        if not NAME_RE.match(pseudo):
            return await reply(interaction, "Pseudo invalide (lettres, chiffres et _ seulement).")
        await interaction.response.defer(ephemeral=True)

        if link := await self.store.get_link(guild_id, interaction.user.id):
            return await reply(interaction, f"Ton compte est déjà lié à **{link['ng_name']}**. Fais `/delier` d'abord.")
        if await self.store.get_link_by_name(guild_id, pseudo):
            return await reply(interaction, f"**{pseudo}** est déjà lié à un autre membre. Contacte un officier si c'est une erreur.")

        entry = (await st.members()).get(pseudo.lower())
        if entry is None and await st.api.get_player(pseudo) is None:
            return await reply(interaction, f"Je trouve pas **{pseudo}** sur le serveur NationsGlory {st.api.server}.")
        name = entry["username"] if entry else pseudo

        if not st.validation_channel_id:
            summary = await self._link(interaction.user, name, st)
            return await reply(interaction, f"✅ Ton compte est lié à **{name}**!\n{summary}")

        channel = interaction.guild.get_channel(st.validation_channel_id)
        if channel is None:
            log.error("%s : salon de validation %s introuvable", interaction.guild.name, st.validation_channel_id)
            return await reply(interaction, "⚠️ Le salon de validation est mal configuré. Préviens un admin.")
        embed = make_embed(
            "🔗 Demande de liaison", BLUE, user=interaction.user,
            description=f"{interaction.user.mention} dit être **{name}** sur NationsGlory.",
        )
        embed.add_field(name=f"Grade dans {st.country_name}", value=rank_label(entry["role"]) if entry else "Pas dans le pays")
        embed.add_field(name="Compte Discord créé", value=discord.utils.format_dt(interaction.user.created_at, "R"))
        view = discord.ui.View(timeout=None)
        view.add_item(LinkButton("ok", interaction.user.id, name))
        view.add_item(LinkButton("no", interaction.user.id, name))
        await channel.send(embed=embed, view=view)
        await reply(interaction, "📨 Demande envoyée aux officiers. Tu vas recevoir un MP quand elle sera acceptée.")

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

    @app_commands.command(name="pays", description="Le pays NationsGlory et ses membres par grade")
    @app_commands.guild_only()
    async def pays(self, interaction: discord.Interaction) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        country_id = await st.country()
        members = await st.members()
        country = await st.api.get_country(country_id) or {}

        by_rank: dict[str, list[str]] = {r: [] for r in RANKS}
        others: list[str] = []
        for m in members.values():
            (by_rank[m["role"]] if m["role"] in by_rank else others).append(m["username"])

        embed = discord.Embed(
            title=f"🏳️ {st.country_name}",
            description=f"Serveur **{st.api.server}** · **{len(members)}** membres",
            color=BLUE,
        )
        fields = (
            ("power", "Power"), ("power_max", "Power max"), ("claims", "Claims"),
            ("bank", "Trésorerie"), ("mmr", "MMR"), ("level", "Niveau"),
        )
        for key, label in fields:
            value = country.get(key)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                text = fmt_int(value) if isinstance(value, int) else str(value)
                embed.add_field(name=label, value=f"{text} $" if key == "bank" else text)
        if isinstance(flag := country.get("flag"), str) and flag.startswith("/"):
            embed.set_thumbnail(url=f"https://nationsglory.fr{flag}")
        for rank in RANKS:
            names = sorted(by_rank[rank], key=str.lower)
            if names:
                embed.add_field(name=f"{rank_label(rank)} ({len(names)})", value=truncate(", ".join(names)), inline=False)
        if others:
            embed.add_field(name=f"Autres ({len(others)})", value=truncate(", ".join(sorted(others, key=str.lower))), inline=False)
        embed.set_footer(text="Données NationsGlory, mises à jour aux 5 minutes")
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="joueur", description="Pays et grade d'un joueur NationsGlory")
    @app_commands.describe(pseudo="Le pseudo NationsGlory")
    @app_commands.guild_only()
    async def joueur(self, interaction: discord.Interaction, pseudo: Pseudo) -> None:
        st = await self._st(interaction)
        if st is None:
            return
        pseudo = pseudo.strip()
        if not NAME_RE.match(pseudo):
            return await reply(interaction, "Pseudo invalide.")
        await interaction.response.defer(ephemeral=True)
        data = await st.api.get_player(pseudo)
        if data is None:
            return await reply(interaction, f"Je trouve pas **{pseudo}** sur le serveur {st.api.server}.")
        name = data.get("username") or pseudo
        cid = data.get("country_id")
        country = data.get("country_name") or data.get("country")
        if not country and cid:
            country = st.country_name if cid == st.country_id else cid
        embed = discord.Embed(title=f"🎮 {name}", color=BLUE)
        embed.set_thumbnail(url=head_url(name))
        embed.add_field(name="Pays", value=str(country) if country else "Aucun")
        if country:
            embed.add_field(name="Grade", value=rank_label(data.get("role")))
        if data.get("online") is not None:
            embed.add_field(name="Statut", value="🟢 En ligne" if data["online"] else "⚫ Hors ligne")
        if link := await self.store.get_link_by_name(interaction.guild.id, name):
            embed.add_field(name="Discord", value=f"<@{link['discord_id']}>")
        embed.set_footer(text=f"Serveur {st.api.server}")
        await reply(interaction, embed=embed)

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
