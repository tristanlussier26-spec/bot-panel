"""Commandes d'information NationsGlory : historique, marché, hôtel des ventes, loteries, compétences,
warzone en direct, entreprises, fiche d'un joueur, comparaison de pays, état des données, aperçu du rapport.

Un Cog à part (NGInfos) qui s'appuie sur l'état du Cog NGSync (le pays configuré, le client de l'API partagé).
"""
from __future__ import annotations

import io
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from . import alerts, entreprises, insights, veille
from .common import BLUE, GREEN, GREY, RED, NationsGloryError, reply, truncate
from .logic import RANK_LABELS

ORANGE = 0xE67E22
GOLD = 0xF1C40F
NAME_RE = r"^[A-Za-z0-9_]{1,32}$"

STATS = {  # choix de /historique -> (métrique de l'API, libellé, suffixe)
    "power": ("country.power", "Power", ""),
    "membres": ("country.members", "Membres", ""),
    "claims": ("country.claims", "Claims", ""),
    "banque": ("country.bank", "Banque", " $"),
    "mmr": ("country.mmr", "MMR", ""),
    "score": ("country.score", "Score", ""),
}
PERIODS = {"24h": "24 heures", "7d": "7 jours", "30d": "30 jours", "90d": "90 jours", "1y": "1 an"}
LONG_ONLY = {"score"}  # l'API publie le score seulement sur 90 jours et 1 an
LOTTERY_STATES = {"past": "🏁 Terminée", "running": "🟢 En cours", "active": "🟢 En cours", "upcoming": "⏳ À venir",
                  "pending": "⏳ À venir"}
STATE_ICONS = {"OK": "🟢", "STALE": "🟡", "SILENT": "🟠", "BROKEN": "🔴", "NEVER": "⚪", "DISABLED": "⚫"}
DOMAIN_LABELS = {
    "country": "Pays", "country_member": "Membres", "presence": "En ligne", "alliance": "Alliances", "market": "Marché",
    "lottery": "Loteries", "enterprise": "Entreprises", "war": "Guerres", "warzone_live": "Warzone (direct)",
    "hdv_trade": "HDV transactions", "hdv_book": "HDV annonces", "hdv_variant": "HDV variantes",
    "enterprise_ops": "Opérations d'entreprises", "energy": "Énergie", "parcel": "Parcelles",
    "player_last_login": "Dernières connexions", "skill_ranking_live": "Compétences (direct)",
    "research_catalog": "Catalogue de recherche", "station_module_catalog": "Modules de station",
    "warzone_daily": "Warzone du jour", "warzone_weekly": "Warzone de la semaine",
    "skill_ranking": "Compétences (semaine)", "images": "Images",
}


def money(cents) -> str:
    """Prix de l'hôtel des ventes (en centièmes de $)."""
    if not isinstance(cents, int):
        return "?"
    return f"{cents / 100:,.2f} $".replace(",", " ").replace(".00 $", " $")


def when(value: str | None, style: str = "R") -> str:
    """Horodatage ISO de l'API -> date relative de Discord (« il y a 2 heures »)."""
    try:
        return discord.utils.format_dt(datetime.fromisoformat(str(value).replace("Z", "+00:00")), style)
    except (TypeError, ValueError):
        return str(value or "?")


class NGInfos(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _st(self, interaction: discord.Interaction):
        sync = self.bot.get_cog("NGSync")
        if sync is None:
            await reply(interaction, "La synchro NationsGlory est désactivée.")
            return None, None
        return sync, await sync._st(interaction)

    async def _country(self, interaction: discord.Interaction, st, name: str | None) -> tuple[str, str] | None:
        """(country_id, nom) : le pays demandé, ou celui du serveur Discord. None (et réponse) si introuvable."""
        if not name or name.strip().lower() == st.country_name.lower():
            return await st.country(), st.country_name
        try:
            return await st.api.resolve_country(name)
        except NationsGloryError as e:
            if e.status == 404:
                await reply(interaction, f"Je trouve pas le pays **{name}** sur {st.api.server.capitalize()}.")
                return None
            raise

    # ---- /historique ------------------------------------------------------------------------------
    @app_commands.command(name="historique", description="L'évolution du pays : power, membres, claims, banque…")
    @app_commands.describe(stat="Ce qu'on regarde", periode="Sur combien de temps", pays="Un autre pays (le tien par défaut)")
    @app_commands.choices(
        stat=[app_commands.Choice(name=label, value=key) for key, (_, label, _) in STATS.items()],
        periode=[app_commands.Choice(name=label, value=key) for key, label in PERIODS.items()],
    )
    @app_commands.guild_only()
    async def historique(self, interaction: discord.Interaction, stat: str = "power", periode: str = "7d",
                         pays: app_commands.Range[str, 1, 64] | None = None) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        if stat not in STATS or periode not in PERIODS:
            return await reply(interaction, "Choix invalide.")
        if stat in LONG_ONLY and periode not in ("90d", "1y"):
            periode = "90d"
        await interaction.response.defer()
        target = await self._country(interaction, st, pays)
        if target is None:
            return
        cid, name = target
        metric, label, suffix = STATS[stat]
        points = await st.api.series(metric, cid, periode)
        summary = veille.series_summary(points)
        embed = discord.Embed(title=f"📈 {label} de {name} · {PERIODS[periode]}", color=BLUE)
        if summary is None:
            embed.description = "Pas encore de données pour cette période."
        else:
            embed.description = f"```\n{summary['spark']}\n```"
            embed.add_field(name="Maintenant", value=veille.fmt(summary["last"], suffix))
            embed.add_field(name="Variation", value=veille.signed(summary["delta"], suffix))
            embed.add_field(name="Min · Max", value=f"{veille.fmt(summary['min'], suffix)} · {veille.fmt(summary['max'], suffix)}")
        embed.set_footer(text="Séries historiques de l'API NationsGlory")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /comparer --------------------------------------------------------------------------------
    @app_commands.command(name="comparer", description="Compare deux pays côte à côte")
    @app_commands.describe(pays="Le pays à comparer", avec="L'autre pays (le tien par défaut)")
    @app_commands.guild_only()
    async def comparer(self, interaction: discord.Interaction, pays: app_commands.Range[str, 1, 64],
                       avec: app_commands.Range[str, 1, 64] | None = None) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        a = await self._country(interaction, st, avec)
        b = await self._country(interaction, st, pays) if a else None
        if not a or not b:
            return
        cards = [await st.api.get_country(cid, max_age=120) or {} for cid, _ in (a, b)]
        countries = await st.api.countries(max_age=300)
        rows = []
        for key, label, suffix in veille.REPORT_STATS:
            va, vb = cards[0].get(key), cards[1].get(key)
            if not isinstance(va, int) and not isinstance(vb, int):
                continue
            mark_a = "🏆 " if isinstance(va, int) and isinstance(vb, int) and va > vb else ""
            mark_b = "🏆 " if isinstance(va, int) and isinstance(vb, int) and vb > va else ""
            rows.append((label, f"{mark_a}{veille.fmt(va, suffix)}", f"{mark_b}{veille.fmt(vb, suffix)}"))
        embed = discord.Embed(title=f"⚖️ {a[1]} contre {b[1]}", color=ORANGE)
        embed.add_field(name="\u200b", value="\n".join(r[0] for r in rows) or "—")
        embed.add_field(name=a[1], value="\n".join(r[1] for r in rows) or "—")
        embed.add_field(name=b[1], value="\n".join(r[2] for r in rows) or "—")
        ranks = []
        for (cid, cname), card in zip((a, b), cards):
            r = veille.rank_of(countries, cid, "power")
            margin = (card.get("power") or 0) - (card.get("claims") or 0)
            status = "🚨 attaquable" if alerts.power_level(card, 0) == alerts.POWER_UNDER else f"marge de power {margin}"
            ranks.append(f"**{cname}** : {r or '?'}e en power · {status}")
        embed.add_field(name="Situation", value="\n".join(ranks), inline=False)
        rel = (cards[0].get("relations") or {})
        if b[0] in (rel.get("allies") or []):
            embed.description = f"🤝 {b[1]} est un allié de {a[1]}."
        elif b[0] in (rel.get("enemies") or []):
            embed.description = f"⚔️ {b[1]} est un ennemi de {a[1]}."
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /joueur ----------------------------------------------------------------------------------
    @app_commands.command(name="joueur", description="La fiche d'un joueur NationsGlory : pays, temps de jeu, connexions")
    @app_commands.describe(pseudo="Le pseudo NationsGlory")
    @app_commands.guild_only()
    async def joueur(self, interaction: discord.Interaction, pseudo: app_commands.Range[str, 1, 32]) -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        pseudo = pseudo.strip()
        if not re.match(NAME_RE, pseudo):
            return await reply(interaction, "Pseudo invalide (lettres, chiffres et _ seulement).")
        await interaction.response.defer(ephemeral=True)
        data = await st.api.get_player(pseudo)
        profile = await st.api.profile(pseudo)
        if data is None and profile is None:
            return await reply(interaction, f"Je trouve pas **{pseudo}** sur NationsGlory.")
        name = (data or {}).get("username") or (profile or {}).get("username") or pseudo
        embed = discord.Embed(title=f"🎮 {name}", color=BLUE)
        embed.set_thumbnail(url=(profile or {}).get("face_url") or f"https://files.recube.gg/nationsglory/face/{name}.png")
        here = next((x for x in (profile or {}).get("servers") or [] if isinstance(x, dict) and x.get("server") == st.api.server), {})
        cid = (data or {}).get("country_id") or here.get("country_id")
        role = (data or {}).get("role") or here.get("role")
        if cid:
            names = {c.get("country_id"): c.get("name") for c in await st.api.countries()}
            country = st.country_name if cid == st.country_id else names.get(cid) or "Pays inconnu"
            custom = await sync._role_names(st) if cid == st.country_id else {}
            embed.add_field(name=f"Pays sur {st.api.server.capitalize()}", value=f"{country} · {custom.get(role) or alerts_rank(role)}")
        else:
            embed.add_field(name=f"Pays sur {st.api.server.capitalize()}", value="Aucun")
        if profile and profile.get("private"):
            embed.description = "🔒 Profil privé : le reste de sa fiche est caché."
        elif profile:
            embed.add_field(name="Statut", value="🟢 En ligne" if profile.get("online") else "⚫ Hors ligne")
            if isinstance(profile.get("playtime_total_minutes"), int):
                embed.add_field(name="Temps de jeu", value=f"{profile['playtime_total_minutes'] // 60:,} h".replace(",", " "))
            if profile.get("first_join_on"):
                embed.add_field(name="Première connexion", value=str(profile["first_join_on"]))
            if isinstance(profile.get("achievement_points"), int):
                embed.add_field(name="Points de succès", value=veille.fmt(profile["achievement_points"]))
            if profile.get("verified"):
                embed.add_field(name="Compte", value="✅ Vérifié")
            servers = [s for s in profile.get("servers") or [] if isinstance(s, dict)]
            here = next((s for s in servers if s.get("server") == st.api.server), None)
            if here:
                if here.get("last_login_on"):
                    embed.add_field(name="Dernière connexion ici", value=str(here["last_login_on"]))
                if isinstance(here.get("playtime_minutes"), int):
                    embed.add_field(name=f"Temps de jeu sur {st.api.server.capitalize()}", value=f"{here['playtime_minutes'] // 60} h")
                if here.get("grade"):
                    embed.add_field(name="Grade boutique", value=str(here["grade"]))
            others = [f"{s.get('server', '?').capitalize()} : {s.get('country_name') or 'sans pays'}"
                      for s in servers if s.get("server") != st.api.server]
            if others:
                embed.add_field(name="Autres serveurs", value=truncate("\n".join(others)), inline=False)
            if profile.get("description"):
                embed.description = truncate(str(profile["description"]), 300)
        if link := await sync.store.get_link_by_name(interaction.guild.id, name):
            embed.add_field(name="Discord", value=f"<@{link['discord_id']}>")
        embed.set_footer(text="Fiche publique NationsGlory")
        await reply(interaction, embed=embed)

    # ---- /warzone ---------------------------------------------------------------------------------
    @app_commands.command(name="warzone", description="Le classement warzone de la journée en cours")
    @app_commands.guild_only()
    async def warzone(self, interaction: discord.Interaction) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        cid = await st.country()
        rows = await st.api.warzone_live()
        _, top, ours = alerts.warzone_summary(rows, cid)
        embed = discord.Embed(title=f"🏁 Warzone en direct · {st.api.server.capitalize()}", color=ORANGE,
                              description="\n".join(top) or "Personne a encore marqué de points aujourd'hui.")
        mine = next((r for r in rows if r.get("country_id") == cid), None)
        if mine:
            detail = f"⛵ Bateau {mine.get('boat', 0)} · 🛢️ Pétrole {mine.get('petrol', 0)} · ⛏️ Mine {mine.get('mine', 0)}"
            embed.add_field(name=st.country_name, value=(ours + "\n" if ours else "") + detail, inline=False)
        if rows and rows[0].get("as_of"):
            embed.add_field(name="Calculé", value=when(rows[0]["as_of"]), inline=False)
        embed.set_footer(text="La journée se termine à 20 h, heure de Paris (14 h au Québec)")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /competences -----------------------------------------------------------------------------
    @app_commands.command(name="competences", description="Classement des compétences, avec les membres du pays")
    @app_commands.describe(competence="Une compétence (toutes par défaut)", semaine="La dernière semaine close au lieu de la semaine en cours")
    @app_commands.choices(competence=[app_commands.Choice(name=label, value=key) for key, label in veille.SKILLS.items()])
    @app_commands.guild_only()
    async def competences(self, interaction: discord.Interaction, competence: str | None = None, semaine: bool = False) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        rows = await st.api.skills("weekly" if semaine else "live")
        members = await st.members(max_age=300)
        title = "la dernière semaine" if semaine else "cette semaine"
        if competence:
            skill_rows = sorted((r for r in rows if r.get("skill") == competence), key=lambda r: r.get("rank") or 99)
            lines = []
            for r in skill_rows[:10]:
                name = str(r.get("username") or "?")
                star = " ⭐" if name.lower() in members else ""
                lines.append(f"`{r.get('rank')}.` **{name}**{star} · {veille.fmt(r.get('score'))}")
            ours = [r for r in skill_rows[10:] if str(r.get("username") or "").lower() in members]
            embed = discord.Embed(title=f"🛠️ {veille.SKILLS.get(competence, competence)} · {title}", color=GREEN,
                                  description="\n".join(lines) or "Pas de classement publié.")
            if ours:
                embed.add_field(name=f"Aussi dans le top 50 ({st.country_name})",
                                value=truncate(", ".join(f"{r['username']} ({r['rank']}e)" for r in ours)), inline=False)
        else:
            embed = discord.Embed(title=f"🛠️ {st.country_name} dans les tops compétences · {title}", color=GREEN)
            for key, label in veille.SKILLS.items():
                ours = sorted((r for r in rows if r.get("skill") == key and str(r.get("username") or "").lower() in members),
                              key=lambda r: r.get("rank") or 99)
                if ours:
                    embed.add_field(name=f"{label} ({len(ours)})", inline=False,
                                    value=truncate(", ".join(f"**{r['username']}** {r['rank']}e" for r in ours)))
            if not embed.fields:
                embed.description = "Aucun membre du pays dans les tops 50 pour l'instant."
        embed.set_footer(text="⭐ = membre du pays · top 50 publié par NationsGlory")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /marche ----------------------------------------------------------------------------------
    @app_commands.command(name="marche", description="Les prix du marché des céréales")
    @app_commands.describe(cereale="Une céréale pour voir son historique (ex. ble, wheat, corn)")
    @app_commands.guild_only()
    async def marche(self, interaction: discord.Interaction, cereale: app_commands.Range[str, 1, 64] | None = None) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        rows = await st.api.market()
        if cereale:
            wanted = cereale.strip().lower()
            row = next((r for r in rows if wanted in (str(r.get("cereal")).lower(), veille.cereal_name(r.get("cereal")).lower(),
                                                       str(r.get("cereal")).lower().removeprefix("cereal_"))), None)
            if row is None:
                return await reply(interaction, f"Céréale **{cereale}** inconnue. Essaie `/marche` pour la liste.")
            data = await st.api.market_history(row["cereal"]) or {}
            history = (data.get("history") or {}).get("price") or {}
            days = sorted(history)
            embed = discord.Embed(title=f"🌾 {veille.cereal_name(row['cereal'])} · {veille.fmt(row.get('price'))} $", color=GOLD)
            if days:
                values = [history[d] for d in days]
                summary = veille.series_summary([{"value": v} for v in values])
                embed.description = f"```\n{summary['spark']}\n```"
                embed.add_field(name=f"Sur {len(days)} jours", value=veille.signed(summary["delta"], " $"))
                embed.add_field(name="Min · Max", value=f"{veille.fmt(summary['min'])} · {veille.fmt(summary['max'])} $")
                sales = (data.get("history") or {}).get("sales") or {}
                if sales:
                    embed.add_field(name="Vendu hier", value=veille.fmt(sales.get(days[-1])))
            else:
                embed.description = "Pas d'historique publié pour cette céréale."
            return await reply(interaction, embed=embed, ephemeral=False)
        lines = [f"{'📈' if r.get('trend') == '+' else '📉'} **{veille.cereal_name(r.get('cereal'))}** · {veille.fmt(r.get('price'))} $"
                 for r in sorted(rows, key=lambda r: -(r.get("price") or 0))]
        embed = discord.Embed(title=f"🌾 Marché des céréales · {st.api.server.capitalize()}", color=GOLD,
                              description="\n".join(lines) or "Marché vide.")
        embed.set_footer(text="📈/📉 = sens de la dernière variation · /marche <céréale> pour l'historique")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /hdv -------------------------------------------------------------------------------------
    @app_commands.command(name="hdv", description="Le prix d'un objet à l'hôtel des ventes")
    @app_commands.describe(objet="Le nom de l'objet (ex. diamant)")
    @app_commands.guild_only()
    async def hdv(self, interaction: discord.Interaction, objet: app_commands.Range[str, 1, 64]) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        rows = await st.api.hdv_search(objet, limit=6)
        if not rows:
            return await reply(interaction, f"Aucun objet « {objet} » échangé ces 30 derniers jours.")
        exact = next((r for r in rows if str(r.get("name") or "").lower() == objet.strip().lower()), rows[0])
        item = await st.api.hdv_item(str(exact.get("item"))) or exact
        embed = discord.Embed(title=f"🏪 {item.get('name') or item.get('item')}", color=GOLD)
        embed.add_field(name="Prix affiché", value=money(item.get("last_day_median_cents")))
        embed.add_field(name="Cours sur 7 jours", value=money(item.get("price_cents")))
        if isinstance(item.get("change_bp"), int):
            embed.add_field(name="Variation 7 j", value=f"{item['change_bp'] / 100:+.1f} %")
        week = item.get("week") or {}
        if week:
            embed.add_field(name="Échangé en 7 jours",
                            value=f"{veille.fmt(week.get('quantity'))} unités · {veille.fmt(week.get('value'))} $ · {week.get('trades', 0)} ventes")
        month = item.get("month") or {}
        if month.get("low_cents") is not None:
            embed.add_field(name="30 jours (min · max)", value=f"{money(month.get('low_cents'))} · {money(month.get('high_cents'))}")
        book = item.get("book") or {}
        sell, buy = book.get("sell") or {}, book.get("buy") or {}
        if sell or buy:
            embed.add_field(name="En vente", value=f"{sell.get('listings', 0)} annonces · dès {money(sell.get('unit_low_cents'))}")
            embed.add_field(name="Demandes d'achat", value=f"{buy.get('listings', 0)} · jusqu'à {money(buy.get('unit_high_cents'))}")
        elif item.get("best_sell_cents") is not None:
            embed.add_field(name="Moins cher en vente", value=money(item["best_sell_cents"]))
        if item.get("last_trade_at"):
            embed.add_field(name="Dernière vente", value=when(item["last_trade_at"]))
        others = [r for r in rows if r is not exact][:5]
        if others:
            embed.add_field(name="Autres résultats", inline=False,
                            value="\n".join(f"{r.get('name') or r.get('item')} · {money(r.get('last_day_median_cents'))}" for r in others))
        embed.set_footer(text=f"Objet {item.get('item')} · hôtel des ventes de {st.api.server.capitalize()}")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /loteries --------------------------------------------------------------------------------
    @app_commands.command(name="loteries", description="Les loteries en cours sur le serveur")
    @app_commands.guild_only()
    async def loteries(self, interaction: discord.Interaction) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        rows = await st.api.lotteries()
        live = [x for x in rows if x.get("state") != "past"]
        past = sorted((x for x in rows if x.get("state") == "past"), key=lambda x: str(x.get("ends_at") or ""), reverse=True)
        rows = live + past[:3]  # les loteries finies : juste les 3 dernières
        embed = discord.Embed(title=f"🎟️ Loteries · {st.api.server.capitalize()}", color=GOLD)
        for lot in rows[:10]:
            state = lot.get("state")
            parts = [f"État : {LOTTERY_STATES.get(state, state or '?')}"]
            if isinstance(lot.get("cash_prize"), int):
                parts.append(f"💰 {veille.fmt(lot['cash_prize'])} $ à gagner")
            prizes = [p for p in lot.get("item_prizes") or [] if p]
            if prizes:
                parts.append(f"🎁 {len(prizes)} objet(s) en prix")
            if isinstance(lot.get("ticket_price"), int):
                parts.append(f"Ticket : {veille.fmt(lot['ticket_price'])} $")
            if lot.get("tickets_sold") is not None:
                parts.append(f"{lot['tickets_sold']} ticket(s) vendu(s)")
            if lot.get("ends_at"):
                parts.append(f"Fin {when(lot['ends_at'])}")
            winners = [str(w.get("username") if isinstance(w, dict) else w) for w in lot.get("winners") or []]
            if winners:
                parts.append("🏆 " + ", ".join(winners[:5]))
            embed.add_field(name=f"Loterie {lot.get('lottery_id')}", value=truncate("\n".join(parts)), inline=False)
        if not rows:
            embed.description = "Aucune loterie pour l'instant."
        elif not live:
            embed.description = "Aucune loterie en cours. Les dernières :"
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- Autocomplétion ---------------------------------------------------------------------------
    async def _member_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        sync = self.bot.get_cog("NGSync")
        st = sync.state(interaction.guild_id) if sync else None
        if st is None:
            return []
        try:
            members = await st.members(max_age=300)
        except Exception:  # noqa: BLE001 - l'autocomplétion doit jamais planter
            return []
        cur = current.lower()
        names = sorted((m["username"] for k, m in members.items() if cur in k), key=lambda n: (not n.lower().startswith(cur), n.lower()))
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    async def _enterprise_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        sync = self.bot.get_cog("NGSync")
        st = sync.state(interaction.guild_id) if sync else None
        if st is None:
            return []
        try:
            rows = await st.api.enterprises()
            ours = {r["name"] for r in await sync.country_enterprises(st)}
        except Exception:  # noqa: BLE001
            return []
        cur = current.lower()
        names = [str(r.get("name")) for r in rows if cur in str(r.get("name") or "").lower()]
        names.sort(key=lambda n: (n not in ours, not n.lower().startswith(cur), n.lower()))
        return [app_commands.Choice(name=("⭐ " if n in ours else "") + n, value=n) for n in names[:25]]

    # ---- /entreprises -----------------------------------------------------------------------------
    @app_commands.command(name="entreprises", description="Toutes les entreprises du pays : chiffre d'affaires, banque, rangs")
    @app_commands.describe(joueur="Les entreprises d'un joueur précis",
                           type="Le top du serveur pour un type d'entreprise (ferme, électricité…)")
    @app_commands.choices(type=[app_commands.Choice(name=label, value=key)
                                for key, (_, label) in list(entreprises.TYPES.items())[:25]])
    @app_commands.guild_only()
    async def entreprises_cmd(self, interaction: discord.Interaction, joueur: app_commands.Range[str, 1, 32] | None = None,
                              type: str | None = None) -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        rows = await st.api.enterprises()
        members = await st.members(max_age=300)
        ours = {r["name"] for r in entreprises.country_enterprises(rows, members, st.extra_enterprises)}
        if type:  # top du serveur pour un type
            pool = sorted((r for r in rows if r.get("type") == type), key=lambda r: -(r.get("turnover") or 0))
            lines = [f"`{i + 1}.` {'⭐ ' if r['name'] in ours else ''}**{r['name']}** · {r.get('owner') or '?'} · "
                     f"CA {veille.fmt(r.get('turnover'))} $" for i, r in enumerate(pool[:15])]
            embed = discord.Embed(title=f"🏆 {entreprises.type_label(type)} · top de {st.api.server.capitalize()}",
                                  description="\n".join(lines) or "Aucune entreprise de ce type.", color=GOLD)
            embed.set_footer(text=f"{len(pool)} entreprise(s) de ce type · ⭐ = du pays")
            return await reply(interaction, embed=embed, ephemeral=False)
        if joueur:
            mine = [r for r in rows if str(r.get("owner") or "").lower() == joueur.strip().lower()]
            title = f"🏢 Entreprises de {joueur}"
        else:
            mine = entreprises.country_enterprises(rows, members, st.extra_enterprises)
            title = f"🏢 Entreprises de {st.country_name}"
        mine.sort(key=lambda r: -(r.get("turnover") or 0))
        tot = entreprises.totals(mine)
        embed = discord.Embed(title=title, color=BLUE)
        if not mine:
            embed.description = "Aucune entreprise trouvée. (Seul le propriétaire d'une entreprise est publié par l'API.)"
            return await reply(interaction, embed=embed, ephemeral=False)
        embed.description = (f"**{tot['count']}** entreprise(s) · **{tot['employees']}** employé(s) · "
                             f"CA total **{veille.fmt(tot['turnover'])} $** · banques {veille.fmt(tot['bank'])} $\n"
                             + " · ".join(f"{entreprises.type_label(k)} {n}" for k, n in tot["by_type"].items()))
        custom = await sync._role_names(st)
        for r in mine[:15]:
            pos, of = entreprises.rank(rows, r["name"], kind=r.get("type"))
            owner = r.get("owner") or "?"
            role = (members.get(owner.lower()) or {}).get("role")
            bits = [f"{entreprises.type_label(r.get('type'))} · chef **{owner}**" + (f" ({custom.get(role) or role_label(role)})" if role else ""),
                    f"CA {veille.fmt(r.get('turnover'))} $ · banque {veille.fmt(r.get('bank_balance'))} $ · 👥 {r.get('members_count', '?')}"]
            if pos:
                bits.append(f"{pos}e sur {of} {entreprises.TYPES.get(r.get('type'), ('', 'entreprises'))[1].lower()} du serveur")
            if r.get("contracts_done"):
                bits.append(f"{r['contracts_done']} contrat(s), {r.get('contracts_success_pct', '?')} % réussis")
            embed.add_field(name=f"{'🟢' if r.get('is_open') else '🔒'} {r.get('name')}", value="\n".join(bits), inline=False)
        footer = "/entreprise <nom> pour la fiche complète"
        if len(mine) > 15:
            footer = f"… et {len(mine) - 15} autres · " + footer
        embed.set_footer(text=footer)
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /entreprise ------------------------------------------------------------------------------
    @app_commands.command(name="entreprise", description="La fiche complète d'une entreprise : finances, activité, historique")
    @app_commands.describe(nom="Le nom exact de l'entreprise")
    @app_commands.guild_only()
    async def entreprise(self, interaction: discord.Interaction, nom: app_commands.Range[str, 1, 64]) -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        nom = nom.strip()
        data = await st.api.enterprise(nom)
        rows = await st.api.enterprises()
        if data is None:
            close = next((r["name"] for r in rows if str(r.get("name", "")).lower() == nom.lower()), None)
            data = await st.api.enterprise(close) if close else None
        if data is None:
            return await reply(interaction, f"Je trouve pas l'entreprise **{nom}** sur {st.api.server.capitalize()}.")
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
            custom = await sync._role_names(st)
            role = members[owner.lower()].get("role")
            who += f" ({custom.get(role) or role_label(role)} de {st.country_name})"
        if link := await sync.store.get_link_by_name(interaction.guild.id, owner):
            who += f" · <@{link['discord_id']}>"
        embed.add_field(name="👑 Propriétaire", value=who, inline=False)
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
        files = []
        try:
            if png := await st.api.image(data.get("flag_sha256")):
                files.append(discord.File(io.BytesIO(png), filename="drapeau.png"))
                embed.set_thumbnail(url="attachment://drapeau.png")
        except NationsGloryError:
            pass
        embed.set_footer(text=f"Entreprise de {st.api.server.capitalize()} · l'API nomme pas les employés")
        await interaction.followup.send(embed=embed, files=files)

    @entreprise.autocomplete("nom")
    async def _entreprise_nom(self, interaction: discord.Interaction, current: str):
        return await self._enterprise_choices(interaction, current)

    # ---- /membre ----------------------------------------------------------------------------------
    @app_commands.command(name="membre", description="La fiche complète d'un membre du pays")
    @app_commands.describe(pseudo="Le pseudo NationsGlory (ou laisse vide pour toi si ton compte est lié)")
    @app_commands.guild_only()
    async def membre(self, interaction: discord.Interaction, pseudo: app_commands.Range[str, 1, 32] | None = None) -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        if not pseudo:
            link = await sync.store.get_link(interaction.guild.id, interaction.user.id)
            if link is None:
                return await reply(interaction, "Donne un pseudo, ou lie ton compte avec `/lier`.")
            pseudo = link["ng_name"]
        await interaction.response.defer()
        members = await st.members(max_age=300)
        key = pseudo.strip().lower()
        m = members.get(key)
        if m is None:
            return await reply(interaction, f"**{pseudo}** est pas membre de {st.country_name}. Essaie `/joueur {pseudo}`.")
        name = m["username"]
        custom = await sync._role_names(st)
        online = await st.api.online()
        logins = await sync._logins(st)
        entry = logins.get(key)
        profile = await st.api.profile(name)
        if profile and not profile.get("private"):
            entry = insights.profile_entry(profile, st.api.server, insights.entry_field(entry, 0), 0)
        rows = await st.api.enterprises()
        owned = [r for r in rows if str(r.get("owner") or "").lower() == key]
        row = insights.member_rows({key: m}, {key: entry} if entry else {}, online, {key: owned}, sync._today())[0]

        rank = m.get("role")
        embed = discord.Embed(title=f"{RANK_EMOJI.get(rank, '👤')} {name}", color=BLUE,
                              description=f"**{custom.get(rank) or role_label(rank)}** de **{st.country_name}**")
        embed.set_thumbnail(url=(profile or {}).get("face_url") or f"https://files.recube.gg/nationsglory/face/{name}.png")
        if row["online"]:
            status = "🟢 Connecté en jeu"
        elif row["login"] == "private":
            status = "🔒 Profil privé"
        elif row["login"] == "none":
            status = "😴 Pas venu depuis plus de 90 jours"
        elif row["days"] is not None:
            status = "⚫ Venu aujourd'hui" if row["days"] == 0 else f"{'😴' if row['days'] >= 7 else '⚫'} Vu il y a {row['days']} jour(s) ({row['login']})"
        else:
            status = "❔ Activité pas encore vérifiée"
        embed.add_field(name="Activité", value=status, inline=False)
        if isinstance(row["playtime"], int):
            embed.add_field(name=f"⏱️ Temps de jeu sur {st.api.server.capitalize()}", value=insights.hours(row["playtime"]))
        total = insights.entry_field(entry, 6)
        if isinstance(total, int):
            embed.add_field(name="⏱️ Temps de jeu total", value=insights.hours(total))
        if row["first_join"]:
            embed.add_field(name="📅 Première connexion", value=str(row["first_join"]))
        if isinstance(row["achievements"], int):
            embed.add_field(name="🏅 Succès", value=veille.fmt(row["achievements"]))
        if row["grade"]:
            embed.add_field(name="💎 Grade boutique", value=str(row["grade"]))
        if owned:
            embed.add_field(name=f"🏢 Entreprises ({len(owned)})", inline=False, value=truncate("\n".join(
                f"{entreprises.type_label(r.get('type'))} **{r['name']}** · CA {veille.fmt(r.get('turnover'))} $" for r in owned)))
        try:
            skills = [r for r in await st.api.skills("live") if str(r.get("username") or "").lower() == key]
            if skills:
                embed.add_field(name="🛠️ Tops compétences (cette semaine)", inline=False, value=", ".join(
                    f"{veille.SKILLS.get(r['skill'], r['skill'])} {r['rank']}e" for r in sorted(skills, key=lambda r: r["rank"])))
        except NationsGloryError:
            pass
        if link := await sync.store.get_link_by_name(interaction.guild.id, name):
            embed.add_field(name="Discord", value=f"<@{link['discord_id']}>")
        embed.set_footer(text="Fiche publique NationsGlory · activité au jour près")
        await reply(interaction, embed=embed, ephemeral=False)

    @membre.autocomplete("pseudo")
    async def _membre_pseudo(self, interaction: discord.Interaction, current: str):
        return await self._member_choices(interaction, current)

    # ---- /membres ---------------------------------------------------------------------------------
    @app_commands.command(name="membres", description="Tous les membres du pays : grade, activité, temps de jeu")
    @app_commands.describe(tri="Comment les trier", page="Page (40 membres par page)")
    @app_commands.choices(tri=[app_commands.Choice(name="Par grade", value="grade"),
                               app_commands.Choice(name="Par activité (connectés d'abord)", value="activite"),
                               app_commands.Choice(name="Par temps de jeu", value="temps"),
                               app_commands.Choice(name="Par succès", value="succes"),
                               app_commands.Choice(name="Par ancienneté", value="anciennete")])
    @app_commands.guild_only()
    async def membres(self, interaction: discord.Interaction, tri: str = "grade", page: app_commands.Range[int, 1, 50] = 1) -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        members = await st.members(max_age=300)
        online = await st.api.online()
        logins = await sync._logins(st)
        owners = entreprises.owners_index(await st.api.enterprises())
        rows = insights.sort_members(insights.member_rows(members, logins, online, owners, sync._today()), tri)
        per_page = 40
        pages = max(1, -(-len(rows) // per_page))
        page = min(page, pages)
        chunk = rows[(page - 1) * per_page: page * per_page]
        custom = await sync._role_names(st)
        lines, current = [], None
        for r in chunk:
            if tri == "grade" and r["role"] != current:
                current = r["role"]
                lines.append(f"\n__{RANK_EMOJI.get(current, '👤')} {custom.get(current) or role_label(current)}__")
            lines.append(insights.member_line(r))
        on = sum(1 for r in rows if r["online"])
        checked = sum(1 for r in rows if r["login"] is not None)
        embed = discord.Embed(title=f"👥 {st.country_name} · {len(rows)} membres · 🟢 {on} en jeu", color=BLUE,
                              description=truncate("\n".join(lines).strip(), 4000))
        embed.set_footer(text=f"Page {page}/{pages} · activité vérifiée pour {checked}/{len(rows)} membres · 🏢 = propriétaire d'entreprise")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /classement-membres ----------------------------------------------------------------------
    @app_commands.command(name="classement-membres", description="Le top des membres du pays : temps de jeu, succès, ancienneté")
    @app_commands.choices(critere=[app_commands.Choice(name="Temps de jeu", value="temps"),
                                   app_commands.Choice(name="Succès", value="succes"),
                                   app_commands.Choice(name="Ancienneté", value="anciennete"),
                                   app_commands.Choice(name="Chiffre d'affaires des entreprises", value="entreprises")])
    @app_commands.guild_only()
    async def classement_membres(self, interaction: discord.Interaction, critere: str = "temps") -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer()
        members = await st.members(max_age=300)
        logins = await sync._logins(st)
        owners = entreprises.owners_index(await st.api.enterprises())
        rows = insights.member_rows(members, logins, set(), owners, sync._today())
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        if critere == "entreprises":
            scored = [(r, sum(e.get("turnover") or 0 for e in r["enterprises"])) for r in rows if r["enterprises"]]
            scored.sort(key=lambda x: -x[1])
            lines = [f"{medals.get(i + 1, f'`{i + 1}.`')} **{r['name']}** · {veille.fmt(v)} $ ({len(r['enterprises'])} entreprise(s))"
                     for i, (r, v) in enumerate(scored[:15])]
            title = "💼 Les plus gros entrepreneurs"
        else:
            field = {"temps": "playtime", "succes": "achievements", "anciennete": "first_join"}[critere]
            ranked = [r for r in insights.sort_members(rows, critere) if r[field]]
            fmt_value = {"temps": lambda r: insights.hours(r["playtime"]), "succes": lambda r: f"{veille.fmt(r['achievements'])} pts",
                         "anciennete": lambda r: f"depuis le {r['first_join']}"}[critere]
            lines = [f"{medals.get(i + 1, f'`{i + 1}.`')} **{r['name']}** · {fmt_value(r)}" for i, r in enumerate(ranked[:15])]
            title = {"temps": "⏱️ Temps de jeu", "succes": "🏅 Succès", "anciennete": "📅 Les plus anciens"}[critere]
        checked = sum(1 for r in rows if r["login"] is not None)
        embed = discord.Embed(title=f"{title} · {st.country_name}", color=GOLD,
                              description="\n".join(lines) or "Pas encore de données : le bot lit les fiches des membres petit à petit.")
        embed.set_footer(text=f"Fiches lues : {checked}/{len(rows)} membres (une aux 30 secondes, relues aux 12 h)")
        await reply(interaction, embed=embed, ephemeral=False)

    # ---- /statut-ng -------------------------------------------------------------------------------
    @app_commands.command(name="statut-ng", description="L'état des données NationsGlory (ce qui est à jour ou en panne)")
    @app_commands.guild_only()
    async def statut_ng(self, interaction: discord.Interaction) -> None:
        _, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        domains = await st.api.status()
        lines = []
        for d in domains:
            icon = STATE_ICONS.get(d.get("state"), "❔")
            label = DOMAIN_LABELS.get(d.get("domain"), d.get("domain"))
            lines.append(f"{icon} {label} · {when(d.get('as_of'))}" if d.get("as_of") else f"{icon} {label}")
        bad = [d for d in domains if d.get("state") not in ("OK", "STALE")]
        embed = discord.Embed(title=f"🩺 Données NationsGlory · {st.api.server.capitalize()}",
                              description=truncate("\n".join(lines), 4000) or "Aucune donnée.",
                              color=GREEN if not bad else RED)
        embed.set_footer(text="🟢 à jour · 🟡 en retard · 🟠 serveur muet · 🔴 en panne · ⚪ pas encore publié")
        await reply(interaction, embed=embed)

    # ---- /rapport ---------------------------------------------------------------------------------
    @app_commands.command(name="rapport", description="Aperçu du rapport du jour du pays, maintenant")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def rapport(self, interaction: discord.Interaction) -> None:
        sync, st = await self._st(interaction)
        if st is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await sync.build_report(interaction.guild, st, save=False)
        await reply(interaction, embed=embed)


def alerts_rank(role: str | None) -> str:
    return RANK_LABELS.get(role or "", role or "Sans grade")


role_label = alerts_rank
RANK_EMOJI = {"LEADER": "👑", "OFFICER": "⭐", "MEMBER": "🛡️", "RECRUIT": "🌱"}

