"""Erreurs et petits outils du plugin (le plugin dépend de rien d'autre que discord.py et du bot hôte)."""
from __future__ import annotations

import discord

GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB
GREY = 0x95A5A6


class ServiceError(Exception):
    """Erreur prévue : le bot hôte montre `user_message` au membre et le détail dans la console."""

    user_message = "❌ Un service externe répond pas. Réessaie plus tard."


class NationsGloryError(ServiceError):
    def __init__(self, message: str, *, status: int | None = None, user_message: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        if user_message:
            self.user_message = user_message
        elif status in (401, 403):
            self.user_message = "🔑 L'API NationsGlory refuse la clé du bot. Préviens l'hébergeur du bot."
        elif status == 429:
            self.user_message = "⏳ Trop de requêtes à l'API NationsGlory, réessaie dans une minute."
        else:
            self.user_message = "🌐 L'API NationsGlory répond pas pour l'instant. Réessaie plus tard."


async def reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = True,
) -> None:
    """Répond à une interaction, qu'elle ait déjà été « defer » ou pas."""
    kwargs: dict = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def truncate(text: str | None, limit: int = 1024) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def make_embed(
    title: str,
    color: int,
    *,
    description: str | None = None,
    user: discord.abc.User | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    if user is not None:
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_footer(text=f"ID : {user.id}")
    return embed
