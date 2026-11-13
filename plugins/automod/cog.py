"""Auto-modération : spam, invitations, liens, mots interdits, mentions en masse, alerte anti-raid."""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands

from .common import ORANGE, RED, make_embed, truncate

log = logging.getLogger("plugin.automod")

INVITE_RE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg)/[\w-]+", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)([^\s/<>]+)", re.I)


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    domain = domain.lower().split(":")[0].removeprefix("www.")
    return any(domain == a or domain.endswith("." + a) for a in (x.lower().removeprefix("www.") for x in allowed))


def find_violation(content: str, cfg: dict) -> str | None:
    """Raison de la violation (sans le spam ni les mentions), ou None."""
    for word in cfg.get("mots_interdits", []):
        if word and re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", content, re.I):
            return "mot interdit"
    if cfg.get("anti_invitations", True) and INVITE_RE.search(content):
        return "invitation Discord"
    if cfg.get("anti_liens", False):
        for match in URL_RE.finditer(content):
            if not domain_allowed(match.group(1), cfg.get("liens_autorises", [])):
                return "lien non autorisé"
    return None


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self._messages: defaultdict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._joins: defaultdict[int, deque[float]] = defaultdict(deque)
        self._raid_alert_at: dict[int, float] = {}

    @staticmethod
    def _exempt(message: discord.Message, cfg: dict) -> bool:
        member = message.author
        if member.bot or not hasattr(member, "guild_permissions"):
            return True
        exempt_channels = {int(c) for c in cfg.get("salons_exemptes", [])}
        if message.channel.id in exempt_channels or getattr(message.channel, "category_id", None) in exempt_channels:
            return True
        if member.guild_permissions.manage_messages or member.id == message.guild.owner_id:
            return True
        exempt_roles = {int(r) for r in cfg.get("roles_exemptes", [])}
        return any(r.id in exempt_roles for r in member.roles)

    def _is_spam(self, guild_id: int, user_id: int, cfg: dict) -> bool:
        now = time.monotonic()
        window = int(cfg.get("spam_secondes", 5))
        stamps = self._messages[(guild_id, user_id)]
        stamps.append(now)
        while stamps and now - stamps[0] > window:
            stamps.popleft()
        return len(stamps) >= int(cfg.get("spam_messages", 5))

    async def punish(self, message: discord.Message, reason: str, cfg: dict) -> None:
        member: discord.Member = message.author
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        sanction = cfg.get("sanction", "supprimer")
        detail = "Message supprimé"
        try:
            if sanction == "avertir":
                self.bot.dispatch("automod_warn", member, self.bot.user.id, f"Auto-mod : {reason}")
                detail = "Message supprimé + avertissement"
            elif sanction == "sourdine":
                minutes = int(cfg.get("duree_sourdine", 10))
                await member.timeout(timedelta(minutes=minutes), reason=f"Auto-mod : {reason}")
                detail = f"Message supprimé + sourdine {minutes} min"
        except discord.HTTPException as e:
            log.warning("Sanction auto-mod pour %s : %s", member, e)
        try:
            await member.send(f"🛡️ Ton message sur **{message.guild.name}** a été supprimé : {reason}.")
        except discord.HTTPException:
            pass
        embed = make_embed("🛡️ Auto-modération", ORANGE, user=member, description=f"{member.mention} dans {message.channel.mention}")
        embed.add_field(name="Raison", value=reason)
        embed.add_field(name="Action", value=detail)
        if message.content:
            embed.add_field(name="Message", value=truncate(message.content), inline=False)
        await self.host.send_log(message.guild.id, embed, "moderation")

    async def _check(self, message: discord.Message, *, edited: bool = False) -> None:
        if message.guild is None:
            return
        cfg = self.host.config(message.guild.id, "automod")
        if not cfg.get("active") or self._exempt(message, cfg):
            return
        reason = find_violation(message.content or "", cfg)
        max_mentions = int(cfg.get("max_mentions", 0) or 0)
        if reason is None and max_mentions and len(message.raw_mentions) + len(message.raw_role_mentions) > max_mentions:
            reason = "trop de mentions"
        if reason is None and not edited and cfg.get("anti_spam", True) and self._is_spam(message.guild.id, message.author.id, cfg):
            reason = "spam"
        if reason:
            await self.punish(message, reason, cfg)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self._check(after, edited=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild_id = member.guild.id
        cfg = self.host.config(guild_id, "automod")
        if not (cfg.get("active") and cfg.get("anti_raid", True)):
            return
        now = time.monotonic()
        window = int(cfg.get("raid_secondes", 30))
        joins = self._joins[guild_id]
        joins.append(now)
        while joins and now - joins[0] > window:
            joins.popleft()
        if len(joins) >= int(cfg.get("raid_arrivees", 8)) and now - self._raid_alert_at.get(guild_id, -1e9) > window:
            self._raid_alert_at[guild_id] = now
            embed = make_embed(
                "🚨 Raid possible",
                RED,
                description=f"**{len(joins)}** arrivées en moins de {window} secondes. "
                "Vérifie les nouveaux membres, et active au besoin le mode de vérification de Discord.",
            )
            await self.host.send_log(guild_id, embed, "moderation")
            log.warning("Raid possible sur %s : %d arrivées en %d s", member.guild.name, len(joins), window)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
