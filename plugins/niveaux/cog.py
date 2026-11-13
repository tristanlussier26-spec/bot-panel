"""Niveaux : XP en écrivant, annonce dans le général quand on monte de niveau, rôles en récompense."""
from __future__ import annotations

import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from .common import BLUE, fill, reply, truncate

log = logging.getLogger("plugin.niveaux")


def xp_for_next(level: int) -> int:
    """XP pour passer du niveau `level` au suivant (même courbe que la plupart des bots)."""
    return 5 * level * level + 50 * level + 100


def level_info(total_xp: int) -> tuple[int, int, int]:
    """(niveau, XP dans le niveau actuel, XP pour le prochain niveau)."""
    level, xp = 0, max(0, total_xp)
    while xp >= xp_for_next(level):
        xp -= xp_for_next(level)
        level += 1
    return level, xp, xp_for_next(level)


def total_xp_for(level: int) -> int:
    return sum(xp_for_next(lvl) for lvl in range(level))


def progress_bar(current: int, needed: int, size: int = 14) -> str:
    filled = round(size * current / needed) if needed else size
    return "▰" * filled + "▱" * (size - filled)


SCHEMA = """
CREATE TABLE IF NOT EXISTS xp (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    xp       INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
"""


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self._last_gain: dict[tuple[int, int], float] = {}
        self.db = None

    async def cog_load(self) -> None:
        self.db = await self.host.open_db("niveaux", SCHEMA)

    def validate_guild_config(self, config: dict) -> list[str]:
        niveaux = config.get("niveaux", {})
        if int(niveaux.get("xp_max", 25)) < int(niveaux.get("xp_min", 15)):
            return ["Niveaux : l'XP maximum doit être plus grand que le minimum"]
        return []

    # ---- Base de données ---------------------------------------------------------------------
    async def get_xp(self, guild_id: int, user_id: int) -> dict:
        row = await self.db.one("SELECT * FROM xp WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return row or {"guild_id": guild_id, "user_id": user_id, "xp": 0, "messages": 0}

    async def add_xp(self, guild_id: int, user_id: int, amount: int) -> int:
        """Ajoute de l'XP et retourne le nouveau total."""
        await self.db.run(
            "INSERT INTO xp (guild_id, user_id, xp, messages) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp, messages = messages + 1",
            (guild_id, user_id, amount),
        )
        return (await self.get_xp(guild_id, user_id))["xp"]

    async def set_xp(self, guild_id: int, user_id: int, amount: int) -> None:
        await self.db.run(
            "INSERT INTO xp (guild_id, user_id, xp, messages) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp",
            (guild_id, user_id, amount),
        )

    async def xp_rank(self, guild_id: int, user_id: int) -> int:
        me = await self.get_xp(guild_id, user_id)
        row = await self.db.one("SELECT COUNT(*) AS n FROM xp WHERE guild_id = ? AND xp > ?", (guild_id, me["xp"]))
        return row["n"] + 1

    async def top_xp(self, guild_id: int, limit: int = 10) -> list[dict]:
        return await self.db.all("SELECT * FROM xp WHERE guild_id = ? AND xp > 0 ORDER BY xp DESC LIMIT ?", (guild_id, limit))

    @staticmethod
    def rewards(cfg: dict) -> dict[int, int]:
        return {int(lvl): int(role) for lvl, role in (cfg.get("recompenses") or {}).items() if int(role)}

    async def apply_rewards(self, member: discord.Member, level: int) -> None:
        guild = member.guild
        cfg = self.host.config(guild.id, "niveaux")
        rewards = self.rewards(cfg)
        if not rewards:
            return
        earned = [lvl for lvl in rewards if lvl <= level]
        if cfg.get("cumuler_recompenses", True):
            wanted = {rewards[lvl] for lvl in earned}
        else:
            wanted = {rewards[max(earned)]} if earned else set()
        current = {r.id for r in member.roles}
        manageable = lambda rid: (r := guild.get_role(rid)) is not None and r < guild.me.top_role and not r.managed  # noqa: E731
        to_add = [guild.get_role(rid) for rid in wanted - current if manageable(rid)]
        to_remove = [guild.get_role(rid) for rid in (set(rewards.values()) & current) - wanted if manageable(rid)]
        try:
            if to_add:
                await member.add_roles(*to_add, reason=f"Récompense niveau {level}")
            if to_remove:
                await member.remove_roles(*to_remove, reason=f"Récompense niveau {level}")
        except discord.HTTPException as e:
            log.warning("Récompenses de %s : %s", member, e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        guild_id = message.guild.id
        cfg = self.host.config(guild_id, "niveaux")
        if not cfg.get("active"):
            return
        ignored = {int(c) for c in cfg.get("salons_ignores", [])}
        if message.channel.id in ignored or getattr(message.channel, "category_id", None) in ignored:
            return
        now = time.monotonic()
        key = (guild_id, message.author.id)
        if now - self._last_gain.get(key, -1e9) < int(cfg.get("cooldown_secondes", 60)):
            return
        self._last_gain[key] = now

        xp_min = int(cfg.get("xp_min", 15))
        gain = random.randint(xp_min, max(xp_min, int(cfg.get("xp_max", 25))))
        before = (await self.get_xp(guild_id, message.author.id))["xp"]
        after = await self.add_xp(guild_id, message.author.id, gain)
        old_level, new_level = level_info(before)[0], level_info(after)[0]
        if new_level <= old_level:
            return

        member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
        if member is None:
            return
        await self.apply_rewards(member, new_level)
        channel = message.guild.get_channel(int(cfg.get("salon_annonce_id") or 0)) or message.channel
        text = truncate(fill(cfg.get("message", "{membre} passe au niveau {niveau}!"), member, niveau=new_level), 2000)
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
        except discord.HTTPException as e:
            log.warning("Annonce de niveau : %s", e)

    @app_commands.command(name="rang", description="Ton niveau et ton XP (ou celui d'un membre)")
    @app_commands.describe(membre="Le membre (toi par défaut)")
    @app_commands.guild_only()
    async def rang(self, interaction: discord.Interaction, membre: discord.Member | None = None) -> None:
        target = membre or interaction.user
        data = await self.get_xp(interaction.guild.id, target.id)
        level, current, needed = level_info(data["xp"])
        position = await self.xp_rank(interaction.guild.id, target.id)
        embed = discord.Embed(color=target.color if target.color.value else BLUE)
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="Niveau", value=f"**{level}**")
        embed.add_field(name="Classement", value=f"#{position}")
        embed.add_field(name="Messages", value=str(data["messages"]))
        embed.add_field(name=f"Progression : {current} / {needed} XP", value=progress_bar(current, needed), inline=False)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="classement", description="Les 10 membres avec le plus d'XP")
    @app_commands.guild_only()
    async def classement(self, interaction: discord.Interaction) -> None:
        rows = await self.top_xp(interaction.guild.id, 10)
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{row['user_id']}> · niveau **{level_info(row['xp'])[0]}** · {row['xp']} XP"
            for i, row in enumerate(rows)
        ]
        embed = discord.Embed(title="🏆 Classement", description="\n".join(lines) or "Personne a d'XP encore.", color=BLUE)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="niveau-definir", description="Change le niveau d'un membre")
    @app_commands.describe(membre="Le membre", niveau="Le nouveau niveau")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def niveau_definir(
        self, interaction: discord.Interaction, membre: discord.Member, niveau: app_commands.Range[int, 0, 1000]
    ) -> None:
        await self.set_xp(interaction.guild.id, membre.id, total_xp_for(niveau))
        await self.apply_rewards(membre, niveau)
        await reply(interaction, f"✅ {membre.mention} est maintenant niveau **{niveau}**.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Levels(bot))
