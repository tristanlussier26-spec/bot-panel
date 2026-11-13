"""Tickets : panneau avec bouton, salon privé avec le staff, fermeture avec transcription."""
from __future__ import annotations

import io
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from .common import BLUE, GREY, RED, fill, make_embed, reply, truncate

log = logging.getLogger("plugin.tickets")
TOPIC_RE = re.compile(r"^ticket:(\d+)")


class TicketModal(discord.ui.Modal, title="Ouvrir un ticket"):
    sujet = discord.ui.TextInput(label="Sujet", placeholder="Ex. problème de grade, plainte, candidature…", max_length=100)

    def __init__(self, cog: "Tickets") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.open_ticket(interaction, str(self.sujet.value))


class TicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:(?P<action>open|close)"):
    def __init__(self, action: str) -> None:
        self.action = action
        if action == "open":
            button = discord.ui.Button(label="Ouvrir un ticket", emoji="📩", style=discord.ButtonStyle.primary)
        else:
            button = discord.ui.Button(label="Fermer", emoji="🔒", style=discord.ButtonStyle.danger)
        button.custom_id = f"ticket:{action}"
        super().__init__(button)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            return await reply(interaction, "Les tickets sont désactivés.")
        if self.action == "open":
            await interaction.response.send_modal(TicketModal(cog))
        else:
            await cog.ask_close(interaction)


class ConfirmClose(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.button(label="Oui, fermer le ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.close_ticket(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)

    def _cfg(self, guild: discord.Guild) -> dict:
        return self.host.config(guild.id, "tickets")

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(TicketButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(TicketButton)

    # ---- Outils -----------------------------------------------------------------------
    def _staff_role(self, guild: discord.Guild) -> discord.Role | None:
        return guild.get_role(int(self._cfg(guild).get("role_staff_id") or 0))

    def _is_staff(self, member: discord.Member) -> bool:
        role = self._staff_role(member.guild)
        return (role is not None and role in member.roles) or member.guild_permissions.manage_channels

    @staticmethod
    def ticket_owner(channel: discord.abc.GuildChannel) -> int | None:
        match = TOPIC_RE.match(getattr(channel, "topic", None) or "")
        return int(match.group(1)) if match else None

    def open_tickets(self, guild: discord.Guild, user_id: int) -> list[discord.TextChannel]:
        return [c for c in guild.text_channels if self.ticket_owner(c) == user_id]

    # ---- Ouverture ----------------------------------------------------------------------
    async def open_ticket(self, interaction: discord.Interaction, subject: str) -> None:
        guild = interaction.guild
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await reply(interaction, "Membre introuvable.")
        cfg = self._cfg(guild)
        limit = int(cfg.get("max_par_membre", 1) or 1)
        if existing := self.open_tickets(guild, member.id):
            if len(existing) >= limit:
                return await reply(interaction, f"T'as déjà un ticket ouvert : {existing[0].mention}")

        category = guild.get_channel(int(cfg.get("categorie_id") or 0))
        if not isinstance(category, discord.CategoryChannel):
            category = None
        staff = self._staff_role(guild)
        allow = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: allow,
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        if staff is not None:
            overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        await interaction.response.defer(ephemeral=True)
        channel = await guild.create_text_channel(
            name=f"ticket-{member.name}"[:90],
            category=category,
            overwrites=overwrites,
            topic=f"ticket:{member.id} · {subject}"[:1024],
            reason=f"Ticket ouvert par {member}",
        )
        embed = make_embed(f"🎫 {truncate(subject, 200)}", BLUE, user=member,
                           description=truncate(fill(cfg.get("message_ouverture", ""), member), 4096))
        view = discord.ui.View(timeout=None)
        view.add_item(TicketButton("close"))
        mentions = [member.mention] + ([staff.mention] if staff else [])
        await channel.send(
            content=" ".join(mentions),
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[member], roles=[staff] if staff else False),
        )
        await reply(interaction, f"✅ Ton ticket est ouvert : {channel.mention}")
        log_embed = make_embed("🎫 Ticket ouvert", BLUE, user=member, description=f"{channel.mention} · {truncate(subject, 200)}")
        await self.host.send_log(guild.id, log_embed, "moderation")

    # ---- Fermeture -----------------------------------------------------------------------
    async def ask_close(self, interaction: discord.Interaction) -> None:
        owner = self.ticket_owner(interaction.channel)
        member = interaction.guild.get_member(interaction.user.id)
        if owner is None:
            return await reply(interaction, "C'est pas un salon de ticket.")
        if member is None or (member.id != owner and not self._is_staff(member)):
            return await reply(interaction, "⛔ Juste la personne qui a ouvert le ticket ou le staff peut le fermer.")
        await interaction.response.send_message("Fermer ce ticket?", view=ConfirmClose(self), ephemeral=True)

    async def transcript(self, channel: discord.TextChannel) -> tuple[str, int]:
        lines, count = [], 0
        async for message in channel.history(limit=2000, oldest_first=True):
            count += 1
            when = message.created_at.strftime("%Y-%m-%d %H:%M")
            text = message.clean_content
            for embed in message.embeds:
                text += f" [embed: {embed.title or ''} {embed.description or ''}]"
            for attachment in message.attachments:
                text += f" [fichier: {attachment.url}]"
            lines.append(f"[{when}] {message.author}: {text}")
        return "\n".join(lines), count

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        owner_id = self.ticket_owner(channel)
        if owner_id is None:
            return await reply(interaction, "C'est pas un salon de ticket.")
        await interaction.response.send_message("🔒 Fermeture du ticket…")
        text, count = await self.transcript(channel)
        subject = (channel.topic or "").split(" · ", 1)[-1]
        target = interaction.guild.get_channel(int(self._cfg(interaction.guild).get("salon_transcripts_id") or 0))
        if target is not None:
            embed = make_embed("🔒 Ticket fermé", GREY, description=f"`#{channel.name}` · {truncate(subject, 200)}")
            embed.add_field(name="Ouvert par", value=f"<@{owner_id}>")
            embed.add_field(name="Fermé par", value=interaction.user.mention)
            embed.add_field(name="Messages", value=str(count))
            file = discord.File(io.BytesIO(text.encode("utf-8")), filename=f"{channel.name}.txt")
            try:
                await target.send(embed=embed, file=file)
            except discord.HTTPException as e:
                log.warning("Transcription du ticket : %s", e)
        await self.host.send_log(
            interaction.guild.id,
            make_embed("🔒 Ticket fermé", RED, description=f"`#{channel.name}` par {interaction.user.mention}"),
            "moderation",
        )
        try:
            await channel.delete(reason=f"Ticket fermé par {interaction.user}")
        except discord.HTTPException as e:
            log.warning("Suppression du ticket : %s", e)

    # ---- Commandes -------------------------------------------------------------------------
    @app_commands.command(name="ticket-panneau", description="Publie le panneau « Ouvrir un ticket » dans ce salon")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ticket_panneau(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🎫 Support", description=truncate(self._cfg(interaction.guild).get("message_panneau", ""), 4096), color=BLUE
        )
        view = discord.ui.View(timeout=None)
        view.add_item(TicketButton("open"))
        await interaction.channel.send(embed=embed, view=view)
        await reply(interaction, "Panneau publié.")

    @app_commands.command(name="ticket-ajouter", description="Ajoute un membre à ce ticket")
    @app_commands.describe(membre="Le membre à ajouter")
    @app_commands.guild_only()
    async def ticket_ajouter(self, interaction: discord.Interaction, membre: discord.Member) -> None:
        member = interaction.guild.get_member(interaction.user.id)
        if self.ticket_owner(interaction.channel) is None:
            return await reply(interaction, "C'est pas un salon de ticket.")
        if member is None or not self._is_staff(member):
            return await reply(interaction, "⛔ Réservé au staff.")
        await interaction.channel.set_permissions(membre, view_channel=True, send_messages=True, read_message_history=True)
        await reply(interaction, f"➕ {membre.mention} a été ajouté au ticket.", ephemeral=False)

    @app_commands.command(name="ticket-fermer", description="Ferme ce ticket")
    @app_commands.guild_only()
    async def ticket_fermer(self, interaction: discord.Interaction) -> None:
        await self.ask_close(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
