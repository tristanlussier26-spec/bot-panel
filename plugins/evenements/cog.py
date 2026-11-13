"""Événements du pays : guerres, réunions… avec présences (boutons) et rappels automatiques."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .common import BLUE, GREY, RED, reply, truncate

log = logging.getLogger("plugin.evenements")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_ts    INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    reminded    TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS rsvp (
    event_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    status   TEXT NOT NULL,            -- oui, peut, non
    PRIMARY KEY (event_id, user_id)
);
"""
KINDS = {"guerre": ("⚔️", "Guerre", RED), "reunion": ("🏰", "Réunion", BLUE), "evenement": ("🎉", "Événement", 0x9B59B6)}
STATUS = {"oui": ("✅", "Présents"), "peut": ("❔", "Peut-être"), "non": ("❌", "Absents")}
LATE = 15 * 60  # rappels plus envoyés 15 min après le début (ex. bot redémarré en retard)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Toronto")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_when(text: str, tz: ZoneInfo, now: datetime | None = None) -> datetime | None:
    """« 25/11 20:30 », « 25/11/2026 20h », « demain 21h », « 20:30 » (aujourd'hui, ou demain si c'est passé)."""
    now = now or datetime.now(tz)
    text = text.strip().lower().replace(" à ", " ")
    text = re.sub(r"(\d{1,2})h(\d{2})?\b", lambda m: f"{m[1]}:{m[2] or '00'}", text)
    day_offset = None
    if text.startswith("demain"):
        day_offset, text = 1, text[6:].strip()
    elif text.startswith("aujourd'hui"):
        day_offset, text = 0, text[11:].strip()
    m = re.fullmatch(r"(?:(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+)?(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    try:
        if day:
            when = datetime(int(year or now.year), int(month), int(day), int(hour), int(minute), tzinfo=tz)
            if not year and when < now - timedelta(days=1):
                when = when.replace(year=now.year + 1)  # « 05/01 » en décembre : l'année prochaine
        else:
            when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0) + timedelta(days=day_offset or 0)
            if day_offset is None and when <= now:
                when += timedelta(days=1)
    except ValueError:
        return None
    return when


def reminders(cfg: dict) -> list[int]:
    out = set()
    for raw in cfg.get("rappels") or []:
        try:
            minutes = int(str(raw).strip())
        except ValueError:
            continue
        if 0 < minutes <= 7 * 24 * 60:
            out.add(minutes)
    return sorted(out, reverse=True) + [0]  # 0 = au début


class RsvpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"evt:(?P<status>oui|peut|non):(?P<event_id>\d+)"):
    def __init__(self, status: str, event_id: int) -> None:
        self.status, self.event_id = status, event_id
        emoji, label = STATUS[status]
        style = {"oui": discord.ButtonStyle.success, "peut": discord.ButtonStyle.secondary, "non": discord.ButtonStyle.danger}[status]
        super().__init__(discord.ui.Button(label=label.rstrip("s") if status != "peut" else label, emoji=emoji, style=style,
                                           custom_id=f"evt:{status}:{event_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["status"], int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Events")
        if cog is None:
            return await reply(interaction, "Les événements sont désactivés.")
        await cog.answer(interaction, self.event_id, self.status)


class Events(commands.Cog):
    evenement = app_commands.Group(name="evenement", description="Guerres, réunions et événements du pays", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self.db = None

    async def cog_load(self) -> None:
        self.db = await self.host.open_db("evenements", SCHEMA)
        self.bot.add_dynamic_items(RsvpButton)
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()
        self.bot.remove_dynamic_items(RsvpButton)

    def cfg(self, guild_id: int) -> dict:
        return self.host.config(guild_id, "evenements")

    def can_organize(self, member: discord.Member) -> bool:
        role_id = int(self.cfg(member.guild.id).get("role_organisateur_id", 0) or 0)
        if role_id and any(r.id == role_id for r in member.roles):
            return True
        perms = member.guild_permissions
        return bool(getattr(perms, "manage_events", False) or getattr(perms, "manage_guild", False))

    # ---- Affichage -------------------------------------------------------------------------------
    async def answers(self, event_id: int) -> dict[str, list[int]]:
        out = {s: [] for s in STATUS}
        for row in await self.db.all("SELECT user_id, status FROM rsvp WHERE event_id = ? ORDER BY rowid", (event_id,)):
            out.setdefault(row["status"], []).append(row["user_id"])
        return out

    async def embed(self, event: dict) -> discord.Embed:
        emoji, label, color = KINDS.get(event["kind"], KINDS["evenement"])
        ts = event["start_ts"]
        embed = discord.Embed(title=truncate(f"{emoji} {event['title']}", 256),
                              description=truncate(event["description"], 3000) or None,
                              color=GREY if event["cancelled"] else color)
        embed.add_field(name="📅 Quand", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=False)
        for status, users in (await self.answers(event["id"])).items():
            s_emoji, s_label = STATUS[status]
            names = " ".join(f"<@{u}>" for u in users[:40]) + (f" +{len(users) - 40}" if len(users) > 40 else "")
            embed.add_field(name=f"{s_emoji} {s_label} ({len(users)})", value=names or "—", inline=True)
        embed.set_footer(text=f"{label} n°{event['id']}" + (" · ANNULÉ" if event["cancelled"] else " · clique pour dire si tu viens"))
        return embed

    def view(self, event_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for status in STATUS:
            view.add_item(RsvpButton(status, event_id))
        return view

    async def get(self, guild_id: int, event_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM events WHERE id = ? AND guild_id = ?", (event_id, guild_id))

    # ---- Présences ------------------------------------------------------------------------------
    async def answer(self, interaction: discord.Interaction, event_id: int, status: str) -> None:
        event = await self.get(interaction.guild_id or 0, event_id)
        if event is None or event["cancelled"]:
            return await reply(interaction, "Cet événement est annulé ou introuvable.")
        if event["start_ts"] + 6 * 3600 < time.time():
            return await reply(interaction, "Cet événement est fini.")
        await self.db.run("INSERT OR REPLACE INTO rsvp (event_id, user_id, status) VALUES (?, ?, ?)",
                          (event_id, interaction.user.id, status))
        await interaction.response.edit_message(embed=await self.embed(event), view=self.view(event_id))

    # ---- Rappels ----------------------------------------------------------------------------------
    async def due_reminders(self, now: float | None = None) -> list[tuple[dict, int]]:
        """(événement, minutes avant) à envoyer maintenant. Si plusieurs sont dus (bot arrêté), juste le plus proche."""
        now = now or time.time()
        out = []
        for event in await self.db.all("SELECT * FROM events WHERE cancelled = 0 AND start_ts > ?", (int(now) - LATE,)):
            sent = set(json.loads(event["reminded"]))
            due = [m for m in reminders(self.cfg(event["guild_id"])) if m not in sent and now >= event["start_ts"] - m * 60]
            if not due:
                continue
            await self.db.run("UPDATE events SET reminded = ? WHERE id = ?", (json.dumps(sorted(sent | set(due))), event["id"]))
            out.append((event, min(due)))
        return out

    async def send_reminder(self, event: dict, minutes: int) -> None:
        guild = self.bot.get_guild(event["guild_id"])
        channel = guild.get_channel(event["channel_id"]) if guild else None
        if channel is None:
            return
        emoji = KINDS.get(event["kind"], KINDS["evenement"])[0]
        role_id = int(self.cfg(guild.id).get("role_alerte_id", 0) or 0)
        going = (await self.answers(event["id"]))["oui"]
        ping = f"<@&{role_id}> " if role_id else ""
        if minutes:
            text = f"{ping}⏰ {emoji} **{event['title']}** commence <t:{event['start_ts']}:R>!"
        else:
            text = f"{ping}🔔 {emoji} **{event['title']}** commence maintenant!"
        if going:
            text += "\nPrésents : " + " ".join(f"<@{u}>" for u in going[:50])
        await channel.send(truncate(text, 2000), allowed_mentions=discord.AllowedMentions(
            roles=[discord.Object(role_id)] if role_id else False, users=[discord.Object(u) for u in going[:50]]))

    @tasks.loop(seconds=30)
    async def loop(self) -> None:
        for event, minutes in await self.due_reminders():
            try:
                await self.send_reminder(event, minutes)
            except discord.HTTPException as e:
                log.warning("Rappel de l'événement %s : %s", event["id"], e)

    @loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Commandes -----------------------------------------------------------------------------------
    @evenement.command(name="creer", description="Planifie une guerre, une réunion ou un événement")
    @app_commands.describe(titre="Ex. Assaut sur la Bulgarie", quand="Ex. 25/11 20:30, demain 21h, 20h30",
                           type="Le genre d'événement", description="Détails (facultatif)", salon="Où l'annoncer (sinon le salon des options)")
    @app_commands.choices(type=[app_commands.Choice(name=f"{e} {label}", value=k) for k, (e, label, _) in KINDS.items()])
    async def creer(self, interaction: discord.Interaction, titre: app_commands.Range[str, 1, 100], quand: str,
                    type: app_commands.Choice[str], description: app_commands.Range[str, 0, 1500] = "",
                    salon: discord.TextChannel | None = None) -> None:
        guild = interaction.guild
        cfg = self.cfg(guild.id)
        if not cfg.get("active"):
            return await reply(interaction, "Les événements sont désactivés sur ce serveur (options du bot).")
        if not self.can_organize(interaction.user):
            return await reply(interaction, "⛔ Faut le rôle d'organisateur (ou « Gérer les événements ») pour planifier.")
        tz = zone(str(cfg.get("fuseau") or "America/Toronto"))
        when = parse_when(quand, tz)
        if when is None:
            return await reply(interaction, "Je comprends pas la date. Exemples : `25/11 20:30`, `demain 21h`, `20h30`.")
        if when.timestamp() < time.time() - 60:
            return await reply(interaction, "Cette date est passée.")
        channel = salon or guild.get_channel(int(cfg.get("salon_id", 0) or 0)) or interaction.channel
        cur = await self.db.run(
            "INSERT INTO events (guild_id, channel_id, title, kind, description, start_ts, author_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild.id, channel.id, titre.strip(), type.value, description.strip(), int(when.timestamp()), interaction.user.id))
        event = await self.get(guild.id, cur.lastrowid)
        role_id = int(cfg.get("role_alerte_id", 0) or 0)
        message = await channel.send(content=f"<@&{role_id}>" if role_id else None, embed=await self.embed(event),
                                     view=self.view(event["id"]),
                                     allowed_mentions=discord.AllowedMentions(roles=[discord.Object(role_id)] if role_id else False))
        await self.db.run("UPDATE events SET message_id = ? WHERE id = ?", (message.id, event["id"]))
        marks = [m for m in reminders(cfg) if m]
        await reply(interaction, f"✅ {KINDS[type.value][1]} n°{event['id']} planifié(e) pour <t:{event['start_ts']}:F> dans {channel.mention}."
                                 + (f" Rappels : {', '.join(f'{m} min' for m in marks)} avant." if marks else ""))

    @evenement.command(name="annuler", description="Annule un événement")
    @app_commands.describe(numero="Le numéro de l'événement (voir /evenement liste)")
    async def annuler(self, interaction: discord.Interaction, numero: int) -> None:
        event = await self.get(interaction.guild.id, numero)
        if event is None:
            return await reply(interaction, "Événement introuvable.")
        if not self.can_organize(interaction.user) and interaction.user.id != event["author_id"]:
            return await reply(interaction, "⛔ T'as pas le droit d'annuler cet événement.")
        await self.db.run("UPDATE events SET cancelled = 1 WHERE id = ?", (numero,))
        event["cancelled"] = 1
        channel = interaction.guild.get_channel(event["channel_id"])
        if channel is not None and event["message_id"]:
            try:
                await channel.get_partial_message(event["message_id"]).edit(embed=await self.embed(event), view=None)
                await channel.send(f"❌ **{event['title']}** (<t:{event['start_ts']}:f>) est annulé.")
            except discord.HTTPException:
                pass
        await reply(interaction, f"Événement n°{numero} annulé.")

    @evenement.command(name="liste", description="Les prochains événements du pays")
    async def liste(self, interaction: discord.Interaction) -> None:
        rows = await self.db.all("SELECT * FROM events WHERE guild_id = ? AND cancelled = 0 AND start_ts > ? ORDER BY start_ts LIMIT 15",
                                 (interaction.guild.id, int(time.time()) - 3600))
        lines = []
        for e in rows:
            going = len((await self.answers(e["id"]))["oui"])
            lines.append(f"{KINDS.get(e['kind'], KINDS['evenement'])[0]} n°{e['id']} **{truncate(e['title'], 60)}** · <t:{e['start_ts']}:F> · ✅ {going}")
        await reply(interaction, embed=discord.Embed(title="📅 Prochains événements", description="\n".join(lines) or "Rien de prévu.", color=BLUE))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
