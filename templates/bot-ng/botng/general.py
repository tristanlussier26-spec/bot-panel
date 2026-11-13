"""Commandes générales : ping, aide, infos serveur/membre, avatar, sondage."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from botng.helpers import BLUE, PURPLE, reply, truncate

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Vérifie que le bot répond")
    async def ping(self, interaction: discord.Interaction) -> None:
        await reply(interaction, f"🏓 Pong! Latence : {round(self.bot.latency * 1000)} ms")

    @app_commands.command(name="aide", description="Liste des commandes que tu peux utiliser")
    async def aide(self, interaction: discord.Interaction) -> None:
        lines = []
        for cmd in self.bot.tree.walk_commands():
            if isinstance(cmd, app_commands.Group):
                continue
            required = (cmd.root_parent or cmd).default_permissions
            if required is not None and not required <= interaction.permissions:
                continue  # on cache les commandes que le membre peut pas utiliser
            lines.append(f"`/{cmd.qualified_name}` — {cmd.description}")
        lines.sort()
        embed = discord.Embed(title="📖 Commandes", description=truncate("\n".join(lines), 4096), color=BLUE)
        await reply(interaction, embed=embed)

    @app_commands.command(name="serveur", description="Infos sur le serveur Discord")
    @app_commands.guild_only()
    async def serveur(self, interaction: discord.Interaction) -> None:
        g = interaction.guild
        humans = sum(1 for m in g.members if not m.bot)
        embed = discord.Embed(title=g.name, description=g.description, color=BLUE)
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Propriétaire", value=f"<@{g.owner_id}>")
        embed.add_field(name="Créé", value=discord.utils.format_dt(g.created_at, "D"))
        embed.add_field(name="Membres", value=f"{g.member_count} ({humans} humains)")
        embed.add_field(name="Salons", value=f"{len(g.text_channels)} texte · {len(g.voice_channels)} vocaux")
        embed.add_field(name="Rôles", value=str(len(g.roles) - 1))
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (niveau {g.premium_tier})")
        embed.set_footer(text=f"ID : {g.id}")
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="utilisateur", description="Infos sur un membre")
    @app_commands.describe(membre="Le membre (toi par défaut)")
    @app_commands.guild_only()
    async def utilisateur(self, interaction: discord.Interaction, membre: discord.Member | None = None) -> None:
        m = membre or interaction.user
        roles = [r.mention for r in reversed(m.roles[1:])]
        embed = discord.Embed(title=m.display_name, color=m.color if m.color.value else BLUE)
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="Nom", value=str(m))
        embed.add_field(name="Compte créé", value=discord.utils.format_dt(m.created_at, "R"))
        if m.joined_at:
            embed.add_field(name="Arrivé sur le serveur", value=discord.utils.format_dt(m.joined_at, "R"))
        embed.add_field(name=f"Rôles ({len(roles)})", value=truncate(" ".join(roles), 1024) or "Aucun", inline=False)
        embed.set_footer(text=f"ID : {m.id}")
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="avatar", description="Affiche l'avatar d'un membre en grand")
    @app_commands.describe(membre="Le membre (toi par défaut)")
    async def avatar(self, interaction: discord.Interaction, membre: discord.User | None = None) -> None:
        user = membre or interaction.user
        embed = discord.Embed(title=f"Avatar de {user.display_name}", color=BLUE)
        embed.set_image(url=user.display_avatar.with_size(1024).url)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="sondage", description="Crée un sondage avec des réactions")
    @app_commands.describe(question="La question", choix="Choix séparés par des ; (vide = oui/non, max 10)")
    @app_commands.guild_only()
    async def sondage(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 250],
        choix: str | None = None,
    ) -> None:
        options = [c.strip() for c in (choix or "").split(";") if c.strip()]
        if len(options) == 1:
            return await reply(interaction, "Mets au moins 2 choix séparés par `;`, ou aucun pour un oui/non.")
        if len(options) > 10:
            return await reply(interaction, "Maximum 10 choix.")

        if options:
            emojis = NUMBER_EMOJIS[: len(options)]
            description = "\n".join(f"{e} {truncate(o, 200)}" for e, o in zip(emojis, options))
        else:
            emojis, description = ["👍", "👎"], None

        embed = discord.Embed(title=f"📊 {question}", description=description, color=PURPLE)
        embed.set_footer(text=f"Sondage de {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        for emoji in emojis:
            await message.add_reaction(emoji)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
