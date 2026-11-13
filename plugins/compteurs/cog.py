"""Salons compteurs : des salons vocaux dont le nom se met à jour tout seul (« 👥 Membres : 46 »)."""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .common import reply

log = logging.getLogger("plugin.compteurs")

# clé du compteur -> (salon dans les réglages, format dans les réglages)
COUNTERS = {
    "membres": ("salon_membres", "format_membres"),
    "humains": ("salon_humains", "format_humains"),
    "bots": ("salon_bots", "format_bots"),
    "boosts": ("salon_boosts", "format_boosts"),
    "pays": ("salon_pays", "format_pays"),
}
EDIT_GAP = 5 * 60  # Discord permet 2 renommages par 10 minutes par salon


def render(fmt: str, value: int | str, **extra) -> str:
    text = fmt.replace("{n}", str(value))
    for key, val in extra.items():
        text = text.replace("{" + key + "}", str(val))
    return text.strip()[:100] or str(value)


class Counters(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self.ng: dict[int, tuple[str, int]] = {}  # serveur -> (pays, nombre de membres), du plugin NationsGlory
        self.last_edit: dict[int, float] = {}
        self.pending: set[int] = set()

    async def cog_load(self) -> None:
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()

    # ---- Valeurs ---------------------------------------------------------------------------------
    def values(self, guild: discord.Guild) -> dict[str, int | None]:
        members = getattr(guild, "members", None) or []
        humans = sum(1 for m in members if not m.bot) if members else None
        return {
            "membres": guild.member_count,
            "humains": humans,
            "bots": len(members) - humans if members else None,
            "boosts": getattr(guild, "premium_subscription_count", 0) or 0,
            "pays": self.ng.get(guild.id, (None, None))[1],
        }

    def names(self, guild: discord.Guild) -> dict[int, str]:
        """Nom voulu de chaque salon compteur réglé : {id du salon: nom}."""
        cfg = self.host.config(guild.id, "compteurs")
        values = self.values(guild)
        country = self.ng.get(guild.id, ("pays", 0))[0]
        wanted = {}
        for key, (channel_key, fmt_key) in COUNTERS.items():
            channel_id = int(cfg.get(channel_key, 0) or 0)
            if channel_id and values[key] is not None:
                wanted[channel_id] = render(str(cfg.get(fmt_key) or "{n}"), values[key], pays=country, serveur=guild.name)
        return wanted

    async def refresh(self, guild: discord.Guild, *, force: bool = False) -> int:
        """Renomme les salons qui ont changé (au plus un renommage aux 5 min par salon). Retourne le nombre renommé."""
        if not self.host.config(guild.id, "compteurs").get("active"):
            return 0
        done = 0
        now = time.monotonic()
        for channel_id, name in self.names(guild).items():
            channel = guild.get_channel(channel_id)
            if channel is None or channel.name == name:
                continue
            if not force and now - self.last_edit.get(channel_id, -1e9) < EDIT_GAP:
                self.pending.add(guild.id)  # on réessaie au prochain tour
                continue
            try:
                await channel.edit(name=name, reason="Salon compteur")
                self.last_edit[channel_id] = now
                done += 1
            except discord.HTTPException as e:
                log.warning("%s : salon compteur %s pas renommé (%s)", guild.name, channel_id, e)
        return done

    @tasks.loop(minutes=5)
    async def loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.refresh(guild)
            except Exception:  # noqa: BLE001
                log.exception("Salons compteurs de %s", guild.name)

    @loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Événements -----------------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ng_country_update(self, guild_id: int, country: str, members: dict) -> None:
        """Du plugin Synchro NationsGlory : les membres du pays après chaque synchro."""
        self.ng[guild_id] = (country, len(members))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.refresh(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.refresh(member.guild)

    async def on_guild_config_update(self, guild_id: int) -> None:
        if guild := self.bot.get_guild(guild_id):
            await self.refresh(guild, force=True)

    # ---- Commande -------------------------------------------------------------------------------
    @app_commands.command(name="compteurs-creer", description="Crée une catégorie de salons compteurs en haut du serveur")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def creer(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)
        cfg = self.host.config(guild.id, "compteurs")
        locked = {guild.default_role: discord.PermissionOverwrite(connect=False),
                  guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)}
        category = await guild.create_category("📊 Statistiques", overwrites=locked, position=0, reason="Salons compteurs")
        values = {"active": True}
        wanted = ["membres", "humains", "boosts"] + (["pays"] if guild.id in self.ng else [])
        for key in wanted:
            channel_key, fmt_key = COUNTERS[key]
            if (value := self.values(guild)[key]) is None:
                continue
            name = render(str(cfg.get(fmt_key) or "{n}"), value, pays=self.ng.get(guild.id, ("pays", 0))[0], serveur=guild.name)
            channel = await category.create_voice_channel(name, reason="Salon compteur")
            values[channel_key] = channel.id
        errors = await self.host.save(guild.id, "compteurs", values)
        if errors:
            return await reply(interaction, "⚠️ Salons créés, mais réglages pas enregistrés : " + " · ".join(errors))
        await reply(interaction, f"✅ {len(values) - 1} salon(s) compteur(s) créé(s) dans **{category.name}**. "
                                 "Ils se mettent à jour tout seuls (Discord permet un renommage aux 5 minutes).")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Counters(bot))
