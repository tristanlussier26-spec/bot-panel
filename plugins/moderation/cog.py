"""Commandes de modération.

Les bans, expulsions et sourdines sont loggés par le module logs (même quand c'est fait sans le bot) :
la raison envoyée à Discord commence par le nom du modérateur pour que le journal d'audit le montre.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .common import (
    GREEN,
    ORANGE,
    format_duration,
    hierarchy_error,
    make_embed,
    parse_duration,
    reply,
    truncate,
)

Reason = app_commands.Range[str, 1, 400]
NO_REASON = "Aucune raison"


def audit_reason(interaction: discord.Interaction, raison: str) -> str:
    return truncate(f"{interaction.user} : {raison}", 512)


SCHEMA = """
CREATE TABLE IF NOT EXISTS warns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warns_user ON warns (guild_id, user_id);
"""


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self.db = None

    async def cog_load(self) -> None:
        self.db = await self.host.open_db("moderation", SCHEMA)

    # ---- Avertissements ------------------------------------------------------------------------
    async def add_warn(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        cur = await self.db.run(
            "INSERT INTO warns (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason, int(time.time())),
        )
        return cur.lastrowid

    async def get_warns(self, guild_id: int, user_id: int) -> list[dict]:
        return await self.db.all("SELECT * FROM warns WHERE guild_id = ? AND user_id = ? ORDER BY id DESC", (guild_id, user_id))

    async def delete_warn(self, guild_id: int, warn_id: int) -> bool:
        cur = await self.db.run("DELETE FROM warns WHERE guild_id = ? AND id = ?", (guild_id, warn_id))
        return cur.rowcount > 0

    @commands.Cog.listener()
    async def on_automod_warn(self, member: discord.Member, moderator_id: int, reason: str) -> None:
        """Événement du plugin Auto-modération : avertir un membre."""
        await self.add_warn(member.guild.id, member.id, moderator_id, reason)

    async def _notify(
        self, user: discord.abc.User, guild: discord.Guild, action: str, raison: str, duree: str | None = None
    ) -> None:
        """MP au membre sanctionné (s'il accepte les MP)."""
        if not self.host.config(guild.id, "moderation").get("mp_sanctions", True):
            return
        embed = discord.Embed(title=f"Sanction sur {guild.name}", color=ORANGE)
        embed.add_field(name="Action", value=action)
        if duree:
            embed.add_field(name="Durée", value=duree)
        embed.add_field(name="Raison", value=truncate(raison), inline=False)
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            pass

    # ---- Messages ------------------------------------------------------------
    @app_commands.command(name="clear", description="Supprime des messages dans ce salon")
    @app_commands.describe(nombre="Nombre de messages (1 à 100)", membre="Seulement les messages de ce membre")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.guild_only()
    async def clear(
        self,
        interaction: discord.Interaction,
        nombre: app_commands.Range[int, 1, 100],
        membre: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        count = 0

        def check(message: discord.Message) -> bool:
            nonlocal count
            if membre is not None and message.author.id != membre.id:
                return False
            if count >= nombre:
                return False
            count += 1
            return True

        deleted = await interaction.channel.purge(
            limit=nombre if membre is None else 500,
            check=check,
            reason=f"/clear par {interaction.user}",
        )
        await reply(interaction, f"🧹 {len(deleted)} message(s) supprimé(s).")

    @app_commands.command(name="slowmode", description="Met un délai entre les messages dans ce salon")
    @app_commands.describe(secondes="Délai en secondes (0 = désactivé, max 21600)")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def slowmode(self, interaction: discord.Interaction, secondes: app_commands.Range[int, 0, 21600]) -> None:
        await interaction.channel.edit(slowmode_delay=secondes)
        if secondes:
            await reply(interaction, f"🐢 Mode lent activé : {secondes} s entre chaque message.", ephemeral=False)
        else:
            await reply(interaction, "🐇 Mode lent désactivé.", ephemeral=False)

    # ---- Sanctions -------------------------------------------------------------
    @app_commands.command(name="kick", description="Expulse un membre du serveur")
    @app_commands.describe(membre="Le membre à expulser", raison="La raison")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, membre: discord.Member, raison: Reason = NO_REASON) -> None:
        if err := hierarchy_error(interaction, membre):
            return await reply(interaction, f"⛔ {err}")
        await self._notify(membre, interaction.guild, "Expulsion", raison)
        await membre.kick(reason=audit_reason(interaction, raison))
        await reply(interaction, f"👢 **{membre}** a été expulsé. Raison : {raison}", ephemeral=False)

    @app_commands.command(name="ban", description="Bannit un utilisateur du serveur")
    @app_commands.describe(
        utilisateur="La personne à bannir (membre ou ID)",
        raison="La raison",
        jours_messages="Supprimer ses messages des X derniers jours (0 à 7)",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        utilisateur: discord.User,
        raison: Reason = NO_REASON,
        jours_messages: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        member = interaction.guild.get_member(utilisateur.id)
        if member is not None:
            if err := hierarchy_error(interaction, member):
                return await reply(interaction, f"⛔ {err}")
            await self._notify(member, interaction.guild, "Bannissement", raison)
        await interaction.guild.ban(
            utilisateur,
            reason=audit_reason(interaction, raison),
            delete_message_seconds=jours_messages * 86400,
        )
        await reply(interaction, f"🔨 **{utilisateur}** a été banni. Raison : {raison}", ephemeral=False)

    @app_commands.command(name="unban", description="Débannit un utilisateur")
    @app_commands.describe(utilisateur_id="L'ID Discord de la personne", raison="La raison")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, utilisateur_id: str, raison: Reason = NO_REASON) -> None:
        if not utilisateur_id.strip().isdigit():
            return await reply(interaction, "Ça prend un ID Discord (juste des chiffres).")
        try:
            await interaction.guild.unban(discord.Object(id=int(utilisateur_id)), reason=audit_reason(interaction, raison))
        except discord.NotFound:
            return await reply(interaction, "Cette personne est pas bannie.")
        await reply(interaction, f"✅ <@{utilisateur_id}> a été débanni.", ephemeral=False)

    @app_commands.command(name="timeout", description="Met un membre en sourdine (il peut plus écrire ni parler)")
    @app_commands.describe(membre="Le membre", duree="Ex. 10m, 2h, 1h30, 3j (max 28j)", raison="La raison")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(
        self, interaction: discord.Interaction, membre: discord.Member, duree: str, raison: Reason = NO_REASON
    ) -> None:
        td = parse_duration(duree)
        if td is None:
            return await reply(interaction, "Durée invalide. Exemples : `10m`, `2h`, `1h30`, `3j`.")
        if td > timedelta(days=28):
            return await reply(interaction, "Discord accepte max 28 jours.")
        if err := hierarchy_error(interaction, membre):
            return await reply(interaction, f"⛔ {err}")
        await membre.timeout(td, reason=audit_reason(interaction, raison))
        await self._notify(membre, interaction.guild, "Sourdine", raison, format_duration(td))
        await reply(
            interaction, f"🔇 **{membre}** est en sourdine pour {format_duration(td)}. Raison : {raison}", ephemeral=False
        )

    @app_commands.command(name="untimeout", description="Retire la sourdine d'un membre")
    @app_commands.describe(membre="Le membre")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, membre: discord.Member) -> None:
        if not membre.is_timed_out():
            return await reply(interaction, "Ce membre est pas en sourdine.")
        await membre.timeout(None, reason=audit_reason(interaction, "fin de la sourdine"))
        await reply(interaction, f"🔊 **{membre}** peut reparler.", ephemeral=False)

    # ---- Avertissements --------------------------------------------------------
    @app_commands.command(name="warn", description="Donne un avertissement à un membre")
    @app_commands.describe(membre="Le membre", raison="La raison")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, membre: discord.Member, raison: Reason) -> None:
        if membre.bot:
            return await reply(interaction, "Ça sert à rien d'avertir un bot.")
        if err := hierarchy_error(interaction, membre, check_bot=False):
            return await reply(interaction, f"⛔ {err}")
        warn_id = await self.add_warn(interaction.guild.id, membre.id, interaction.user.id, raison)
        total = len(await self.get_warns(interaction.guild.id, membre.id))
        await self._notify(membre, interaction.guild, "Avertissement", raison)
        await reply(
            interaction,
            f"⚠️ {membre.mention} a reçu un avertissement (#{warn_id}, total : {total}). Raison : {raison}",
            ephemeral=False,
        )
        embed = make_embed("⚠️ Avertissement", ORANGE, user=membre, description=membre.mention)
        embed.add_field(name="Par", value=interaction.user.mention)
        embed.add_field(name="Total", value=str(total))
        embed.add_field(name="Raison", value=truncate(raison), inline=False)
        await self.host.send_log(interaction.guild.id, embed, "moderation")

    @app_commands.command(name="warns", description="Liste les avertissements d'un membre")
    @app_commands.describe(membre="Le membre")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warns(self, interaction: discord.Interaction, membre: discord.Member) -> None:
        rows = await self.get_warns(interaction.guild.id, membre.id)
        if not rows:
            return await reply(interaction, f"✅ {membre.mention} a aucun avertissement.")
        embed = make_embed(
            f"Avertissements de {membre.display_name}", ORANGE, user=membre, description=f"Total : **{len(rows)}**"
        )
        for w in rows[:10]:
            date = discord.utils.format_dt(datetime.fromtimestamp(w["created_at"], tz=timezone.utc), "d")
            embed.add_field(
                name=f"#{w['id']} · {date}",
                value=truncate(f"{w['reason']}\n-# par <@{w['moderator_id']}>"),
                inline=False,
            )
        if len(rows) > 10:
            embed.set_footer(text=f"10 plus récents sur {len(rows)} · ID : {membre.id}")
        await reply(interaction, embed=embed)

    @app_commands.command(name="unwarn", description="Retire un avertissement avec son numéro")
    @app_commands.describe(numero="Le numéro de l'avertissement (voir /warns)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def unwarn(self, interaction: discord.Interaction, numero: int) -> None:
        if await self.delete_warn(interaction.guild.id, numero):
            await reply(interaction, f"🗑️ Avertissement #{numero} retiré.")
            embed = make_embed("🗑️ Avertissement retiré", GREEN, description=f"#{numero} par {interaction.user.mention}")
            await self.host.send_log(interaction.guild.id, embed, "moderation")
        else:
            await reply(interaction, f"Aucun avertissement #{numero}.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
