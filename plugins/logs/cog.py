"""Logs de chaque serveur dans son salon de logs (réglé dans le panel, page Logs)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands

from .common import BLUE, GREEN, GREY, ORANGE, RED, YELLOW, make_embed, truncate

log = logging.getLogger("plugin.logs")

AUDIT_WINDOW = timedelta(seconds=20)


class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)

    # ---- Outils ----------------------------------------------------------------
    def _watch(self, guild_id: int | None, channel_id: int | None = None) -> bool:
        """Faut-il logger? (salon de logs configuré sur ce serveur, salon de l'événement pas ignoré)"""
        if guild_id is None:
            return False
        cfg = self.host.config(guild_id, "logs")
        log_channel = int(cfg.get("salon_id", 0) or 0)
        if not log_channel:
            return False
        ignored = {int(c) for c in cfg.get("salons_ignores", [])}
        return channel_id is None or (channel_id != log_channel and channel_id not in ignored)

    async def _send(self, guild_id: int, embed: discord.Embed, category: str) -> None:
        await self.host.send_log(guild_id, embed, category)

    @commands.Cog.listener()
    async def on_botpanel_log(self, guild_id: int, embed: discord.Embed, category: str) -> None:
        """Un événement à logger (de ce plugin ou d'un autre) : dans le salon de logs, si la catégorie est cochée."""
        cfg = self.host.config(guild_id, "logs")
        channel_id = int(cfg.get("salon_id", 0) or 0)
        if not channel_id or not cfg.get(category, True):
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None or getattr(getattr(channel, "guild", None), "id", guild_id) != guild_id:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("Impossible d'écrire dans le salon de logs : %s", e)

    async def _audit(
        self, guild: discord.Guild, target_id: int, *actions: discord.AuditLogAction
    ) -> discord.AuditLogEntry | None:
        """Cherche dans le journal d'audit qui a fait l'action (ban, kick, sourdine...)."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1.5)  # Discord met une seconde à écrire l'entrée
        oldest = discord.utils.utcnow() - AUDIT_WINDOW
        try:
            async for entry in guild.audit_logs(limit=15):
                if entry.created_at < oldest:
                    break
                if entry.action in actions and entry.target is not None and entry.target.id == target_id:
                    return entry
        except discord.HTTPException:
            pass
        return None

    @staticmethod
    def _add_audit_fields(embed: discord.Embed, entry: discord.AuditLogEntry | None) -> None:
        if entry is None:
            return
        reason = entry.reason or "Aucune raison"
        bot_user = entry.user is not None and entry.user.bot
        # Quand l'action vient du bot, la raison commence par « modérateur : raison ».
        if bot_user and " : " in reason:
            moderator, reason = reason.split(" : ", 1)
            embed.add_field(name="Par", value=f"{moderator} (via le bot)")
        elif entry.user is not None:
            embed.add_field(name="Par", value=entry.user.mention)
        embed.add_field(name="Raison", value=truncate(reason), inline=False)

    # ---- Messages ----------------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not self._watch(payload.guild_id, payload.channel_id):
            return
        msg = payload.cached_message
        if msg is not None:
            if msg.author.bot:
                return
            embed = make_embed("🗑️ Message supprimé", RED, user=msg.author, description=f"Dans <#{payload.channel_id}>")
            if msg.content:
                embed.add_field(name="Contenu", value=truncate(msg.content), inline=False)
            if msg.attachments:
                embed.add_field(name="Pièces jointes", value=truncate("\n".join(a.filename for a in msg.attachments)), inline=False)
        else:
            embed = make_embed(
                "🗑️ Message supprimé", RED,
                description=f"Dans <#{payload.channel_id}>\n-# Message plus vieux que le démarrage du bot, contenu inconnu.",
            )
            embed.set_footer(text=f"ID du message : {payload.message_id}")
        await self._send(payload.guild_id, embed, "messages")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if not self._watch(payload.guild_id, payload.channel_id):
            return
        embed = make_embed(
            "🧹 Messages supprimés en lot", RED,
            description=f"**{len(payload.message_ids)}** messages supprimés dans <#{payload.channel_id}>",
        )
        await self._send(payload.guild_id, embed, "messages")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        if not self._watch(after.guild.id, after.channel.id):
            return
        embed = make_embed(
            "✏️ Message modifié", YELLOW, user=after.author,
            description=f"Dans {after.channel.mention} · [Aller au message]({after.jump_url})",
        )
        embed.add_field(name="Avant", value=truncate(before.content) or "*(vide)*", inline=False)
        embed.add_field(name="Après", value=truncate(after.content) or "*(vide)*", inline=False)
        await self._send(after.guild.id, embed, "messages")

    # ---- Membres -------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._watch(member.guild.id):
            return
        embed = make_embed("📥 Arrivée", GREEN, user=member, description=f"{member.mention} a rejoint le serveur.")
        embed.add_field(name="Compte créé", value=discord.utils.format_dt(member.created_at, "R"))
        embed.add_field(name="Membres", value=str(member.guild.member_count))
        if discord.utils.utcnow() - member.created_at < timedelta(days=7):
            embed.add_field(name="⚠️ Attention", value="Compte créé il y a moins de 7 jours.", inline=False)
        await self._send(member.guild.id, embed, "membres")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if not self._watch(guild.id):
            return
        entry = await self._audit(guild, member.id, discord.AuditLogAction.kick, discord.AuditLogAction.ban)
        if entry is not None and entry.action is discord.AuditLogAction.ban:
            return  # déjà loggé par on_member_ban
        if entry is not None:
            embed = make_embed("👢 Expulsion", ORANGE, user=member, description=f"{member.mention} a été expulsé.")
            self._add_audit_fields(embed, entry)
            return await self._send(guild.id, embed, "moderation")
        embed = make_embed("📤 Départ", ORANGE, user=member, description=f"{member.mention} a quitté le serveur.")
        if member.joined_at:
            embed.add_field(name="Arrivé", value=discord.utils.format_dt(member.joined_at, "R"))
        if roles := " ".join(r.mention for r in reversed(member.roles[1:])):
            embed.add_field(name="Rôles", value=truncate(roles), inline=False)
        await self._send(guild.id, embed, "membres")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        if not self._watch(guild.id):
            return
        embed = make_embed("🔨 Bannissement", RED, user=user, description=f"{user.mention} a été banni.")
        self._add_audit_fields(embed, await self._audit(guild, user.id, discord.AuditLogAction.ban))
        await self._send(guild.id, embed, "moderation")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._watch(guild.id):
            return
        embed = make_embed("✅ Débannissement", GREEN, user=user, description=f"{user.mention} a été débanni.")
        self._add_audit_fields(embed, await self._audit(guild, user.id, discord.AuditLogAction.unban))
        await self._send(guild.id, embed, "moderation")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild_id = after.guild.id
        if not self._watch(guild_id):
            return
        if before.nick != after.nick:
            embed = make_embed("🏷️ Surnom modifié", BLUE, user=after, description=after.mention)
            embed.add_field(name="Avant", value=before.nick or "*(aucun)*")
            embed.add_field(name="Après", value=after.nick or "*(aucun)*")
            await self._send(guild_id, embed, "roles")
        if before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            embed = make_embed("🎭 Rôles modifiés", BLUE, user=after, description=after.mention)
            if added:
                embed.add_field(name="Ajoutés", value=truncate(" ".join(added)), inline=False)
            if removed:
                embed.add_field(name="Retirés", value=truncate(" ".join(removed)), inline=False)
            await self._send(guild_id, embed, "roles")
        if before.timed_out_until != after.timed_out_until:
            entry = await self._audit(after.guild, after.id, discord.AuditLogAction.member_update)
            if after.is_timed_out():
                embed = make_embed("🔇 Sourdine", ORANGE, user=after, description=after.mention)
                embed.add_field(name="Jusqu'à", value=discord.utils.format_dt(after.timed_out_until, "f"))
            else:
                embed = make_embed("🔊 Sourdine retirée", GREEN, user=after, description=after.mention)
            self._add_audit_fields(embed, entry)
            await self._send(guild_id, embed, "moderation")

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if after.bot or (before.name == after.name and before.global_name == after.global_name):
            return
        for guild in after.mutual_guilds:  # le changement de nom est loggé sur chaque serveur commun
            if not self._watch(guild.id):
                continue
            embed = make_embed("🪪 Nom modifié", BLUE, user=after, description=after.mention)
            embed.add_field(name="Avant", value=f"{before.global_name or before.name} (@{before.name})")
            embed.add_field(name="Après", value=f"{after.global_name or after.name} (@{after.name})")
            await self._send(guild.id, embed, "membres")

    # ---- Vocal -----------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or not self._watch(member.guild.id) or before.channel == after.channel:
            return
        if before.channel is None:
            embed = make_embed("🔊 Vocal", GREEN, user=member, description=f"{member.mention} a rejoint {after.channel.mention}")
        elif after.channel is None:
            embed = make_embed("🔇 Vocal", ORANGE, user=member, description=f"{member.mention} a quitté {before.channel.mention}")
        else:
            embed = make_embed(
                "🔀 Vocal", BLUE, user=member,
                description=f"{member.mention} : {before.channel.mention} → {after.channel.mention}",
            )
        await self._send(member.guild.id, embed, "vocal")

    # ---- Salons et rôles -----------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if self._watch(channel.guild.id):
            await self._send(channel.guild.id, make_embed("📁 Salon créé", GREEN, description=f"{channel.mention} (`{channel.name}`)"), "salons")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self._watch(channel.guild.id):
            await self._send(channel.guild.id, make_embed("🗑️ Salon supprimé", RED, description=f"`#{channel.name}`"), "salons")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._watch(role.guild.id):
            await self._send(role.guild.id, make_embed("➕ Rôle créé", GREEN, description=f"{role.mention} (`{role.name}`)"), "salons")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._watch(role.guild.id):
            await self._send(role.guild.id, make_embed("➖ Rôle supprimé", GREY, description=f"`@{role.name}`"), "salons")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(bot))
