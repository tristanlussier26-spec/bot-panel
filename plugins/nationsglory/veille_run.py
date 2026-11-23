"""Exécution de la veille NationsGlory (rapport du jour, annonces du forum, marché, rôles « en jeu » et « inactif »).

Mélangé dans le Cog NGSync (voir cog.py) : ces méthodes utilisent son état (self.store, self.state…).
La logique (quoi publier) est dans veille.py, testable sans Discord.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from datetime import datetime, timezone

import discord

from . import alerts, entreprises, insights, tetes, veille
from .common import BLUE, GREEN, GREY, ServiceError, truncate
from .logic import SyncPlan

log = logging.getLogger("plugin.nationsglory")

FORUM_EVERY = 10 * 60
ONLINE_EVERY = 5 * 60  # la liste des connectés est publiée aux 5 min par NationsGlory
FACE_URL = "https://files.recube.gg/nationsglory/face/{}.png"
FACE_MAX_AGE = 6 * 3600
ENTERPRISES_EVERY = 10 * 60
ENTERPRISE_CHANNELS_EVERY = 30 * 60
MARKET_EVERY = 15 * 60
NEWS_COLOR = 0x5865F2


def local_now(tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name or "America/Toronto"))
    except Exception:  # noqa: BLE001 - fuseau inconnu ou pas de base de fuseaux : UTC
        return datetime.now(timezone.utc)


class VeilleMixin:
    # ---- Rôles « en jeu » et « inactif » --------------------------------------------------------
    async def sync_special_roles(self, guild: discord.Guild, st, members: dict[str, dict], online: set[str] | None) -> int:
        """Donne/retire le rôle « connecté en jeu » et le rôle « inactif » aux comptes liés. Retourne les changements."""
        if not (st.ingame_role_id or st.inactive_role_id):
            return 0
        logins = await self._logins(st) if st.inactive_role_id else {}
        today = self._today()
        changed = 0
        for link in await self.store.all_links(guild.id):
            member = guild.get_member(link["discord_id"])
            if member is None:
                continue
            key = link["ng_name"].lower()
            have = {r.id for r in member.roles}
            plan = SyncPlan()
            if st.ingame_role_id and online is not None:
                wanted = key in online and key in members
                if wanted and st.ingame_role_id not in have:
                    plan.add_roles.add(st.ingame_role_id)
                elif not wanted and st.ingame_role_id in have:
                    plan.remove_roles.add(st.ingame_role_id)
            if st.inactive_role_id and key in members:
                entry = logins.get(key)
                inactive = None
                if entry and entry[0] == "none":
                    inactive = True
                elif entry and entry[0] != "private":
                    days = insights.days_since(entry[0], today)
                    inactive = days is not None and days >= st.inactive_days
                if inactive is True and st.inactive_role_id not in have:
                    plan.add_roles.add(st.inactive_role_id)
                elif inactive is False and st.inactive_role_id in have:
                    plan.remove_roles.add(st.inactive_role_id)
            elif st.inactive_role_id and st.inactive_role_id in have:
                plan.remove_roles.add(st.inactive_role_id)  # plus dans le pays
            if not plan.empty:
                await self._apply(member, plan, st, "NationsGlory : en jeu / inactivité")
                changed += 1
        return changed

    async def record_online_peak(self, guild_id: int, st, count: int) -> None:
        day = local_now(st.tz).date().isoformat()
        key = f"ng_pic:{guild_id}"
        peak = await self._load(key) or {}
        if peak.get("day") != day or count > int(peak.get("max") or 0):
            await self._save(key, {"day": day, "max": max(count, int(peak.get("max") or 0) if peak.get("day") == day else 0)})

    # ---- Salon « en jeu » : les têtes des membres connectés ------------------------------------------
    async def face(self, st, name: str) -> bytes | None:
        """La tête du skin (même image que sur nationsglory.fr), gardée 6 h."""
        cache = self.__dict__.setdefault("_faces", {})
        hit = cache.get(name.lower())
        if hit and time.monotonic() - hit[0] < FACE_MAX_AGE:
            return hit[1]
        data = None
        try:
            async with st.api._http().get(FACE_URL.format(name)) as resp:
                if resp.status == 200:
                    data = await resp.read()
        except Exception:  # noqa: BLE001 - pas de tête : on dessine l'initiale
            data = None
        if len(cache) > 400:
            cache.clear()
        cache[name.lower()] = (time.monotonic(), data)
        return data

    async def run_online_board(self, guild: discord.Guild, st) -> bool:
        """Met à jour le message du salon « en jeu ». Retourne True s'il a changé."""
        channel = self._channel(guild, st.online_channel_id, st, "membres en jeu")
        if channel is None:
            return False
        members = await st.members(max_age=300)
        here = insights.online_members(members, await st.api.online())
        custom = await self._role_names(st)
        signature = hashlib.sha1("|".join(f"{m['username']}:{m.get('role')}" for m in here).encode()).hexdigest()
        key = f"ng_enligne:{channel.id}"
        saved = await self._load(key) or {}
        if saved.get("sig") == signature and saved.get("id"):
            return False
        embed = discord.Embed(title=f"🟢 {st.country_name} : {len(here)} en jeu", color=0x2ECC71 if here else GREY,
                              timestamp=discord.utils.utcnow())
        files = []
        if here:
            faces = await asyncio.gather(*(self.face(st, m["username"]) for m in here))
            people = [{"name": m["username"], "role": m.get("role"),
                       "label": custom.get(m.get("role")) or insights_rank(m.get("role")), "face": f}
                      for m, f in zip(here, faces)]
            png = await asyncio.to_thread(tetes.render, people)
            files.append(discord.File(io.BytesIO(png), filename="enligne.png"))
            embed.set_image(url="attachment://enligne.png")
            lines = []
            for rank in ("LEADER", "OFFICER", "MEMBER", "RECRUIT"):
                names = [m["username"] for m in here if m.get("role") == rank]
                if names:
                    lines.append(f"**{custom.get(rank) or insights_rank(rank)}** : {', '.join(names)}")
            embed.description = truncate("\n".join(lines), 4000)
        else:
            embed.description = f"Personne de {st.country_name} est en jeu pour l'instant. 😴"
        embed.set_footer(text=f"Sur {st.api.server.capitalize()} · mis à jour aux 5 minutes")
        message = None
        if saved.get("id"):
            try:
                message = await channel.get_partial_message(int(saved["id"])).edit(embed=embed, attachments=files)
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(embed=embed, files=files)
        await self._save(key, {"id": str(getattr(message, "id", saved.get("id"))), "sig": signature})
        return True

    # ---- Rapport du jour --------------------------------------------------------------------------
    def report_due(self, st) -> str | None:
        """La date locale si c'est l'heure du rapport et qu'il est pas encore publié aujourd'hui."""
        now = local_now(st.tz)
        day = now.date().isoformat()
        if now.hour < st.report_hour or st.last_report_day == day:
            return None
        return day

    async def build_report(self, guild: discord.Guild, st, *, save: bool = True) -> discord.Embed:
        cid = await st.country()
        members = await st.members(max_age=300)
        card = await st.api.get_country(cid, max_age=120) or {}
        countries = await st.api.countries(max_age=300)
        now = veille.report_snapshot(card, members, countries, cid)
        key = f"ng_rapport:{guild.id}"
        previous = await self._load(key)
        embed = discord.Embed(title=f"📰 Rapport du jour · {st.country_name}", color=BLUE,
                              timestamp=discord.utils.utcnow())
        if previous is None:
            embed.description = "Premier rapport : les variations apparaîtront à partir de demain."
        elif isinstance(previous.get("at"), str):
            embed.description = f"Variations depuis le rapport du {previous['at']}."
        for name, value in veille.report_fields(previous, now):
            embed.add_field(name=name, value=truncate(value), inline=False)

        extra = []
        try:
            wars = await st.api.wars(cid)
            active = [w for w in wars if w.get("status") == "in_progress"]
            pending = [w for w in wars if w.get("status") == "negotiation"]
            if active or pending:
                extra.append(f"⚔️ {len(active)} guerre(s) en cours, {len(pending)} en négociation")
            else:
                extra.append("🕊️ Aucune guerre en cours")
        except ServiceError:
            pass
        level = alerts.power_level(card, st.power_margin)
        if level == alerts.POWER_UNDER:
            extra.append("🚨 **Sous le power : attaquable!**")
        elif level == alerts.POWER_LOW:
            extra.append(f"⚠️ Power proche des claims (marge {card.get('power', 0) - card.get('claims', 0)})")
        try:
            rows = await st.api.warzone_daily()
            mine = next((r for r in rows if r.get("country_id") == cid), None)
            if mine:
                extra.append(f"🏁 Warzone du {mine.get('period_end')} : {mine.get('rank')}e ({veille.fmt(mine.get('total'))} pts)")
        except ServiceError:
            pass
        peak = await self._load(f"ng_pic:{guild.id}") or {}
        if peak.get("max"):
            extra.append(f"🟢 Pic de membres en ligne : {peak['max']}")
        try:
            ents = await self.country_enterprises(st, members)
            tot = entreprises.totals(ents)
            before = (previous or {}).get("entreprises_ca")
            delta = f" ({veille.signed(tot['turnover'] - before, ' $')})" if isinstance(before, int) else ""
            if tot["count"]:
                extra.append(f"🏢 {tot['count']} entreprise(s) · chiffre d'affaires {veille.fmt(tot['turnover'])} ${delta}")
            now["entreprises_ca"] = tot["turnover"]
        except ServiceError:
            pass
        logins = await self._logins(st)
        if logins:
            report = insights.inactivity(members, logins, self._today(), 7)
            extra.append(f"😴 Inactifs depuis 7 jours ou plus : {len(report['inactifs']) + len(report['absents'])}"
                         f" (sur {report['verifies']} vérifiés)")
        if extra:
            embed.add_field(name="En bref", value="\n".join(extra), inline=False)
        embed.set_footer(text=f"Données NationsGlory · serveur {st.api.server.capitalize()}")
        if save:  # un aperçu (/rapport) change pas le point de comparaison du prochain rapport
            now["at"] = local_now(st.tz).date().isoformat()
            await self._save(key, now)
        return embed

    async def run_report(self, guild: discord.Guild, st, *, force: bool = False) -> bool:
        day = self.report_due(st)
        if day is None and not force:
            return False
        channel = self._channel(guild, st.report_channel_id, st, "rapport du jour")
        if channel is None:
            return False
        if not force:
            stored = await self.store.get_value(f"ng_rapport_jour:{guild.id}")
            st.last_report_day = stored
            if stored == day:
                return False
        embed = await self.build_report(guild, st)
        await channel.send(embed=embed)
        day = day or local_now(st.tz).date().isoformat()
        st.last_report_day = day
        await self.store.set_value(f"ng_rapport_jour:{guild.id}", day)
        return True

    # ---- Annonces du forum NationsGlory ------------------------------------------------------------
    async def run_forum(self, guild: discord.Guild, st) -> int:
        category = await st.api.forum_category_id(st.forum_category)
        if category is None:
            if st.should_warn(f"forum:{st.forum_category}", 3600):
                log.warning("%s : catégorie du forum « %s » introuvable", guild.name, st.forum_category)
            return 0
        threads = await st.api.forum_threads(category)
        key = f"ng_forum:{guild.id}:{category}"
        previous = await self._load(key)
        fresh = veille.new_threads(previous, threads)
        seen = sorted({t["id"] for t in threads if isinstance(t.get("id"), int)} | set(previous or []))[-200:]
        await self._save(key, seen)
        if not fresh:
            return 0
        channel = self._channel(guild, st.forum_channel_id, st, "annonces du forum")
        if channel is None:
            return 0
        for t in fresh[-5:]:
            embed = discord.Embed(title=truncate(str(t.get("title") or "Nouveau sujet"), 256), url=t.get("url"),
                                  color=NEWS_COLOR)
            if t.get("author"):
                embed.set_author(name=str(t["author"]))
            if t.get("pinned"):
                embed.description = "📌 Épinglé"
            embed.set_footer(text="Forum NationsGlory")
            await channel.send(embed=embed)
        return len(fresh[-5:])

    # ---- Entreprises du pays : fondations, fermetures, changements de propriétaire -------------------
    async def country_enterprises(self, st, members: dict[str, dict] | None = None) -> list[dict]:
        members = members if members is not None else await st.members(max_age=300)
        return entreprises.country_enterprises(await st.api.enterprises(), members, st.extra_enterprises)

    async def run_enterprises(self, guild: discord.Guild, st) -> list[str]:
        rows = await self.country_enterprises(st)
        await self.sync_enterprise_roles(guild, st, rows)
        if not (st.announce_channel_id and st.announce_enterprises):
            return []
        key = f"ng_entreprises:{guild.id}"
        lines = entreprises.enterprise_events(await self._load(key), rows)
        await self._save(key, entreprises.snapshot(rows))
        if not lines:
            return []
        channel = self._channel(guild, st.announce_channel_id, st, "annonces du pays")
        if channel is not None:
            await channel.send("\n".join(lines[:10]), allowed_mentions=discord.AllowedMentions.none())
        return lines

    # ---- Fiche d'une entreprise (commande /entreprise et salons des entreprises) ----------------------
    async def enterprise_embed(self, guild_id: int, st, nom: str, *, rows: list[dict] | None = None):
        """(embed, fiche de l'API) d'une entreprise, ou (None, None) si elle existe pas."""
        nom = nom.strip()
        data = await st.api.enterprise(nom)
        rows = rows if rows is not None else await st.api.enterprises()
        if data is None:
            close = next((r["name"] for r in rows if str(r.get("name", "")).lower() == nom.lower()), None)
            data = await st.api.enterprise(close) if close else None
        if data is None:
            return None, None
        name = data.get("name") or nom
        embed = discord.Embed(title=f"🏢 {name}", color=GREEN if data.get("is_open") else GREY)
        desc = [f"{entreprises.type_label(data.get('type'))} · {'🟢 ouverte' if data.get('is_open') else '🔒 fermée'}"
                + (f" · fondée le {data['created_on']}" if data.get("created_on") else "")]
        if data.get("description"):
            desc.append(f"*{truncate(str(data['description']), 300)}*")
        if data.get("services"):
            desc.append(f"**Services :** {truncate(str(data['services']), 300)}")
        embed.description = "\n".join(desc)
        owner = data.get("owner") or "?"
        members = await st.members(max_age=300)
        who = owner
        if owner.lower() in members:
            custom = await self._role_names(st)
            role = members[owner.lower()].get("role")
            who += f" ({custom.get(role) or insights_rank(role)} de {st.country_name})"
        if link := await self.store.get_link_by_name(guild_id, owner):
            who += f" · <@{link['discord_id']}>"
        embed.add_field(name="👑 Propriétaire", value=who, inline=False)
        staff = await self.store.employees(guild_id, name)
        if staff:
            embed.add_field(name=f"👷 Employés sur Discord ({len(staff)})", inline=False,
                            value=truncate(", ".join(f"<@{e['discord_id']}>" for e in staff)))
        embed.add_field(name="💰 Chiffre d'affaires", value=f"{veille.fmt(data.get('turnover'))} $")
        embed.add_field(name="🏦 Banque", value=f"{veille.fmt(data.get('bank_balance'))} $")
        embed.add_field(name="👥 Membres", value=str(data.get("members_count", "?")))
        contracts = f"{data.get('contracts_done', 0)} faits · {data.get('contracts_success_pct', '?')} % réussis"
        if data.get("contracts_in_progress_value"):
            contracts += f" · {veille.fmt(data['contracts_in_progress_value'])} $ en cours"
        if data.get("disputes"):
            contracts += f" · ⚠️ {data['disputes']} litige(s)"
        embed.add_field(name="📝 Contrats", value=contracts, inline=False)
        flows = data.get("flows_14d") or {}
        if any(isinstance(flows.get(k), int) for k in ("net", "salaries", "taxes")):
            embed.add_field(name="💸 14 derniers jours", inline=False,
                            value=f"Net {veille.signed(flows.get('net'), ' $')} · salaires {veille.fmt(flows.get('salaries'))} $ · "
                                  f"taxes {veille.fmt(flows.get('taxes'))} $")
        pos, of = entreprises.rank(rows, name, kind=data.get("type"))
        pos_all, of_all = entreprises.rank(rows, name)
        if pos:
            embed.add_field(name="🏆 Rang (chiffre d'affaires)", inline=False,
                            value=f"{pos}e sur {of} de son type · {pos_all}e sur {of_all} au serveur")
        points = await st.api.series("enterprise.turnover", name, "30d")
        summary = veille.series_summary(points)
        if summary:
            embed.add_field(name="📈 CA sur 30 jours", inline=False,
                            value=f"`{summary['spark']}`\n{veille.signed(summary['delta'], ' $')} · min {veille.fmt(summary['min'])} · max {veille.fmt(summary['max'])}")
        extra = []
        if data.get("type") in ("farm", "trader", "bet"):
            extra += entreprises.operations_fields(await st.api.enterprise_operations(name))
        if data.get("type") in ("electric", "petrol"):
            extra += entreprises.energy_fields(await st.api.enterprise_energy(name))
        if data.get("type") == "realestate":
            if field := entreprises.parcels_field(await st.api.enterprise_parcels(name)):
                extra.append(field)
        for fname, value in extra[:5]:
            embed.add_field(name=fname, value=truncate(value), inline=False)
        return embed, data

    # ---- Un salon par entreprise du pays, avec ses stats à jour -------------------------------------
    async def _enterprise_category(self, guild: discord.Guild, st):
        cat = guild.get_channel(st.enterprise_category_id) if st.enterprise_category_id else None
        key = f"ng_cat_ent:{guild.id}"
        if cat is None and (saved := await self.store.get_value(key)):
            cat = guild.get_channel(int(saved))
        if cat is None:
            if not guild.me.guild_permissions.manage_channels:
                if st.should_warn("cat-ent", 3600):
                    log.warning("%s : il me faut « Gérer les salons » pour créer les salons des entreprises", guild.name)
                return None
            overwrites = {guild.default_role: discord.PermissionOverwrite(send_messages=False),
                          guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True)}
            cat = await guild.create_category("🏢 Entreprises", overwrites=overwrites, reason="Salons des entreprises du pays")
            await self.store.set_value(key, str(cat.id))
        return cat

    async def _keep_message(self, channel, key: str, embed: discord.Embed) -> None:
        """Modifie le message gardé à jour dans ce salon, ou en publie un nouveau."""
        saved = await self.store.get_value(key)
        if saved:
            try:
                await channel.get_partial_message(int(saved)).edit(embed=embed)
                return
            except discord.NotFound:
                pass
        message = await channel.send(embed=embed)
        await self.store.set_value(key, str(message.id))

    async def run_enterprise_channels(self, guild: discord.Guild, st) -> int:
        """Crée le salon de chaque entreprise du pays (une seule fois) et met ses stats à jour.
        Retourne le nombre de salons mis à jour. Rien n'est jamais supprimé."""
        rows = await st.api.enterprises()
        members = await st.members(max_age=300)
        ours = entreprises.country_enterprises(rows, members, st.extra_enterprises)
        category = await self._enterprise_category(guild, st)
        if category is None:
            return 0
        done = 0
        # Le résumé de toutes les entreprises du pays
        key = f"ng_salon_ent_resume:{guild.id}"
        summary_channel = guild.get_channel(int(await self.store.get_value(key) or 0))
        if summary_channel is None:
            summary_channel = await category.create_text_channel("📊・entreprises-du-pays", reason="Résumé des entreprises",
                                                                 topic=f"Les entreprises de {st.country_name}, mises à jour aux 30 minutes")
            await self.store.set_value(key, str(summary_channel.id))
        tot = entreprises.totals(ours)
        embed = discord.Embed(title=f"🏢 Les entreprises de {st.country_name}", color=BLUE, timestamp=discord.utils.utcnow(),
                              description=(f"**{tot['count']}** entreprise(s) · **{tot['employees']}** employé(s) en jeu · "
                                           f"CA total **{veille.fmt(tot['turnover'])} $** · banques {veille.fmt(tot['bank'])} $"))
        for r in ours[:20]:
            pos, of = entreprises.rank(rows, r["name"], kind=r.get("type"))
            embed.add_field(name=f"{entreprises.type_label(r.get('type'))} {r['name']}", inline=False,
                            value=f"chef **{r.get('owner') or '?'}** · CA {veille.fmt(r.get('turnover'))} $"
                                  + (f" · {pos}e sur {of}" if pos else ""))
        embed.set_footer(text="Mis à jour aux 30 minutes · un salon par entreprise juste en dessous")
        await self._keep_message(summary_channel, f"ng_msg_ent:{summary_channel.id}", embed)
        done += 1
        # Un salon par entreprise
        current = {r["name"] for r in ours}
        for r in ours:
            ckey = f"ng_salon_ent:{guild.id}:{r['name']}"
            channel = guild.get_channel(int(await self.store.get_value(ckey) or 0))
            if channel is None:
                overwrites = dict(getattr(category, "overwrites", {}) or {})
                role = await self.enterprise_role(guild, st, r, create=False) if st.enterprise_roles else None
                if role is not None:  # le patron et les employés peuvent écrire dans le salon de leur entreprise
                    overwrites[role] = discord.PermissionOverwrite(send_messages=True)
                icon = entreprises.TYPES.get(r.get("type"), ("🏢", ""))[0]
                channel = await category.create_text_channel(
                    f"{icon}・{r['name']}"[:100], overwrites=overwrites, reason=f"Entreprise {r['name']}",
                    topic=f"{entreprises.type_label(r.get('type'))} de {r.get('owner') or '?'} · stats à jour aux 30 minutes")
                await self.store.set_value(ckey, str(channel.id))
            embed, data = await self.enterprise_embed(guild.id, st, r["name"], rows=rows)
            if embed is None:
                continue
            if data.get("owner"):
                embed.set_thumbnail(url=FACE_URL.format(data["owner"]))
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="Mis à jour aux 30 minutes · /entreprise pour la fiche avec le drapeau")
            await self._keep_message(channel, f"ng_msg_ent:{channel.id}", embed)
            done += 1
        # Entreprises qui sont plus au pays : on le dit dans leur salon (une fois), sans rien supprimer
        prefix = f"ng_salon_ent:{guild.id}:"
        for key in await self.store.keys(prefix):
            name = key[len(prefix):]
            if name in current or await self.store.get_value(f"ng_ent_partie:{guild.id}:{name}"):
                continue
            channel = guild.get_channel(int(await self.store.get_value(key) or 0))
            if channel is not None:
                gone = discord.Embed(title=f"🏢 {name}", color=GREY,
                                     description="📉 Cette entreprise est plus au pays (fermée, renommée ou vendue). "
                                                 "Le salon est gardé : supprime-le si tu en veux plus.")
                await self._keep_message(channel, f"ng_msg_ent:{channel.id}", gone)
            await self.store.set_value(f"ng_ent_partie:{guild.id}:{name}", "1")
        return done

    # ---- Rôles des entreprises : un rôle par entreprise du pays (patron + employés) et un rôle « Patron » --
    @staticmethod
    def _holders(guild: discord.Guild, role_id: int) -> list:
        members = guild.members.values() if isinstance(guild.members, dict) else guild.members
        return [m for m in members if any(r.id == role_id for r in m.roles)]

    async def enterprise_role(self, guild: discord.Guild, st, row: dict, *, create: bool) -> discord.Role | None:
        """Le rôle Discord d'une entreprise (créé au besoin si l'option est activée)."""
        key = f"ng_role_ent:{guild.id}:{row['name']}"
        role_id = await self.store.get_value(key)
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            role = next((r for r in getattr(guild, "roles", []) if r.name == entreprises.role_name(row["name"])), None)
        if role is None and create:
            if not guild.me.guild_permissions.manage_roles:
                return None
            try:
                role = await guild.create_role(name=entreprises.role_name(row["name"]), mentionable=True,
                                               colour=discord.Colour(entreprises.TYPE_COLORS.get(row.get("type"), 0x95A5A6)),
                                               reason=f"Entreprise {row['name']} ({st.country_name})")
            except discord.HTTPException as e:
                if st.should_warn(f"role-ent:{row['name']}"):
                    log.warning("%s : rôle de l'entreprise %s pas créé (%s)", guild.name, row["name"], e)
                return None
        if role is not None and str(role.id) != role_id:
            await self.store.set_value(key, str(role.id))
        return role

    async def sync_enterprise_roles(self, guild: discord.Guild, st, rows: list[dict] | None = None) -> int:
        """Ajuste les rôles d'entreprise et le rôle « Patron ». Retourne le nombre de membres modifiés."""
        if not (st.enterprise_roles or st.patron_role_id):
            return 0
        rows = rows if rows is not None else await self.country_enterprises(st)
        links = {link["ng_name"].lower(): link["discord_id"] for link in await self.store.all_links(guild.id)}
        changes: dict[int, SyncPlan] = {}

        def plan(member_id: int) -> SyncPlan:
            return changes.setdefault(member_id, SyncPlan())

        if st.enterprise_roles:
            wanted = entreprises.wanted_holders(rows, links, await self.store.employees(guild.id))
            current = {r["name"] for r in rows}
            for row in rows:
                role = await self.enterprise_role(guild, st, row, create=True)
                if role is None:
                    continue
                holders = {m.id for m in self._holders(guild, role.id)}
                for mid in wanted[row["name"]] - holders:
                    if guild.get_member(mid):
                        plan(mid).add_roles.add(role.id)
                for mid in holders - wanted[row["name"]]:
                    plan(mid).remove_roles.add(role.id)
            # Entreprises plus au pays : on retire leur rôle (sans le supprimer)
            prefix = f"ng_role_ent:{guild.id}:"
            for key in await self.store.keys(prefix):
                name = key[len(prefix):]
                if name in current:
                    continue
                role_id = await self.store.get_value(key)
                if role_id and (role := guild.get_role(int(role_id))):
                    for m in self._holders(guild, role.id):
                        plan(m.id).remove_roles.add(role.id)
        if st.patron_role_id:
            bosses = entreprises.patrons(await st.api.enterprises(), links)
            holders = {m.id for m in self._holders(guild, st.patron_role_id)}
            for mid in bosses - holders:
                if guild.get_member(mid):
                    plan(mid).add_roles.add(st.patron_role_id)
            for mid in holders - bosses:
                plan(mid).remove_roles.add(st.patron_role_id)
        done = 0
        for mid, p in changes.items():
            member = guild.get_member(mid)
            if member is None or p.empty:
                continue
            await self._apply(member, p, st, "NationsGlory : rôles des entreprises")
            done += 1
        return done


    # ---- Marché des céréales -----------------------------------------------------------------------
    async def run_market(self, guild: discord.Guild, st) -> list[str]:
        rows = await st.api.market()
        key = f"ng_marche:{guild.id}"
        lines, ref = veille.market_moves(await self._load(key), rows, st.market_pct)
        await self._save(key, ref)
        if not lines:
            return []
        channel = self._channel(guild, st.market_channel_id, st, "marché des céréales")
        if channel is not None:
            embed = discord.Embed(title=f"🌾 Marché des céréales · {st.api.server.capitalize()}",
                                  description="\n".join(lines), color=GREY)
            embed.set_footer(text=f"Variation d'au moins {st.market_pct} % depuis la dernière alerte")
            await channel.send(embed=embed)
        return lines

    # ---- Diplomatie et pays surveillés (appelé par collect_alerts) ------------------------------------
    async def diplomacy_and_watch(self, guild: discord.Guild, st, card: dict) -> tuple[list[str], bool]:
        lines: list[str] = []
        important = False
        cid = await st.country()
        need_countries = st.diplomacy_alert or st.watchlist
        countries = await st.api.countries(max_age=300) if need_countries else []
        names = {c.get("country_id"): c.get("name") for c in countries}
        if st.diplomacy_alert:
            key = f"ng_diplomatie:{guild.id}"
            now = veille.relations_snapshot(card)
            previous = await self._load(key)
            alliance_names = {}
            if previous is not None and now["alliance"] != previous.get("alliance"):
                for k in {now["alliance"], previous.get("alliance")} - {None}:
                    if data := await st.api.alliance(k):
                        alliance_names[k] = data.get("name")
            new = veille.diplomacy_lines(previous, now, names, alliance_names)
            lines += new
            important |= any(x.startswith(("⚔️", "⛓️")) for x in new)
            await self._save(key, now)
        if st.watchlist:
            watched, missing = veille.resolve_watchlist(st.watchlist, countries, cid)
            if missing and st.should_warn(f"surveilles:{','.join(missing)}", 3600):
                log.warning("%s : pays surveillés introuvables : %s", guild.name, ", ".join(missing))
            key = f"ng_surveilles:{guild.id}"
            saved = await self._load(key) or {}
            power_lines, power_state = veille.watch_power_lines(saved.get("power"), countries, watched)
            lines += power_lines
            important |= bool(power_lines)
            wars = [w for w in await st.api.server_wars()
                    if (w.get("attacker_id") in watched or w.get("defender_id") in watched)
                    and cid not in (w.get("attacker_id"), w.get("defender_id"))]  # nos guerres sont déjà annoncées
            previous_wars = saved.get("guerres")
            for event in alerts.war_events(previous_wars, wars, cid):
                lines.append(veille.neutral_war_line(event))
            await self._save(key, {"power": power_state, "guerres": alerts.war_snapshot(wars)})
        return lines, important

    # ---- Outils ----------------------------------------------------------------------------------
    @staticmethod
    def _today():
        from .cog import paris_today  # même jour que les dates de l'API

        return paris_today()


def insights_rank(role: str | None) -> str:
    from .logic import RANK_LABELS

    return RANK_LABELS.get(role or "", role or "Sans grade")
