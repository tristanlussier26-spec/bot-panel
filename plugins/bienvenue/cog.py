"""Accueil : messages de bienvenue/départ, rôle d'arrivée (accès restreint) et panneau de vérification."""
from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from .common import GREEN, ORANGE, fill, reply, truncate

log = logging.getLogger("plugin.bienvenue")


class VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"verif:ok"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(label="J'accepte", emoji="✅", style=discord.ButtonStyle.success, custom_id="verif:ok")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Welcome")
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if cog is None or member is None:
            return await reply(interaction, "Le module d'accueil est désactivé.")
        if cog.host.config(member.guild.id, "arrivee").get("retirer_quand") != "bouton":
            return await reply(interaction, "Ici, l'accès se débloque en liant ton compte NationsGlory avec `/lier`.")
        changed = await cog.verify(member, reason="A accepté le règlement")
        await reply(interaction, "✅ Bienvenue! T'as maintenant accès au serveur." if changed else "T'as déjà accès 😉")


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(VerifyButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(VerifyButton)

    @staticmethod
    def _channel(guild: discord.Guild, channel_id):
        """Un salon de CE serveur (jamais celui d'un autre serveur)."""
        return guild.get_channel(int(channel_id or 0)) if channel_id else None

    def _role(self, guild: discord.Guild, role_id) -> discord.Role | None:
        role = guild.get_role(int(role_id or 0))
        if role is None or role.managed or role >= guild.me.top_role:
            if role is not None:
                log.warning("Je peux pas gérer le rôle « %s » : monte le rôle du bot au-dessus.", role.name)
            return None
        return role

    async def verify(self, member: discord.Member, *, reason: str) -> bool:
        """Retire le rôle d'arrivée et donne le rôle vérifié. Retourne True si quelque chose a changé."""
        changed = False
        cfg = self.host.config(member.guild.id, "arrivee")
        arrival = self._role(member.guild, cfg.get("role_id"))
        verified = self._role(member.guild, cfg.get("role_verifie_id"))
        try:
            if arrival and arrival in member.roles:
                await member.remove_roles(arrival, reason=reason)
                changed = True
            if verified and verified not in member.roles:
                await member.add_roles(verified, reason=reason)
                changed = True
        except discord.HTTPException as e:
            log.warning("Vérification de %s : %s", member, e)
        return changed

    @commands.Cog.listener()
    async def on_ng_linked(self, member: discord.Member) -> None:
        """Événement du plugin NationsGlory : une liaison /lier a été acceptée."""
        if self.host.config(member.guild.id, "arrivee").get("retirer_quand") == "liaison":
            await self.verify(member, reason="Compte NationsGlory lié")

    @staticmethod
    def _welcome_embed(cfg: dict, member: discord.Member) -> discord.Embed:
        embed = discord.Embed(
            title=truncate(fill(cfg.get("titre", ""), member), 256) or None,
            description=truncate(fill(cfg.get("message", ""), member), 4096),
            color=GREEN,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if url := str(cfg.get("image_url") or "").strip():
            if url.startswith(("http://", "https://")):
                embed.set_image(url=url)
        embed.set_footer(text=f"Membre n°{member.guild.member_count}")
        return embed

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.host.config(member.guild.id, "bienvenue")
        arrivee = self.host.config(member.guild.id, "arrivee")
        if not member.bot and (role := self._role(member.guild, arrivee.get("role_id"))):
            try:
                await member.add_roles(role, reason="Rôle d'arrivée")
            except discord.HTTPException as e:
                log.warning("Rôle d'arrivée pour %s : %s", member, e)

        if cfg.get("active") and (channel := self._channel(member.guild, cfg.get("salon_id"))):
            try:
                await channel.send(
                    content=member.mention,
                    embed=self._welcome_embed(cfg, member),
                    allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
                )
            except discord.HTTPException as e:
                log.warning("Message de bienvenue : %s", e)
        if cfg.get("mp_active") and not member.bot:
            try:
                await member.send(truncate(fill(cfg.get("mp_message", ""), member), 2000))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = self.host.config(member.guild.id, "bienvenue")
        if not cfg.get("depart_active"):
            return
        channel = self._channel(member.guild, cfg.get("depart_salon_id")) or self._channel(member.guild, cfg.get("salon_id"))
        if channel is None:
            return
        embed = discord.Embed(description=truncate(fill(cfg.get("depart_message", ""), member), 4096), color=ORANGE)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("Message de départ : %s", e)

    @app_commands.command(name="verification-panneau", description="Publie le panneau « J'accepte le règlement » ici")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def verification_panneau(self, interaction: discord.Interaction) -> None:
        cfg = self.host.config(interaction.guild.id, "arrivee")
        if cfg.get("retirer_quand") != "bouton":
            return await reply(
                interaction,
                "Le panneau sert juste si « Quand retirer ce rôle » est réglé sur le bouton (page Arrivée & accès des options du bot).",
            )
        embed = discord.Embed(
            title="✅ Vérification",
            description=truncate(cfg.get("message_panneau", ""), 4096),
            color=GREEN,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(VerifyButton())
        await interaction.channel.send(embed=embed, view=view)
        await reply(interaction, "Panneau publié.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
