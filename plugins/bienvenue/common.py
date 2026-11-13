"""Petits outils du plugin (couleurs, réponses, embeds)."""
from __future__ import annotations

import re
from datetime import timedelta

import discord

GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xE67E22
BLUE = 0x3498DB
YELLOW = 0xF1C40F
PURPLE = 0x9B59B6
GREY = 0x95A5A6


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


_DURATION_PART = re.compile(r"(\d+)(min|[jdhms])?")
_UNITS = {"j": 86400, "d": 86400, "h": 3600, "min": 60, "m": 60, "s": 1, None: 60}


def parse_duration(text: str) -> timedelta | None:
    """Convertit « 10m », « 2h », « 1h30 », « 3j » ou « 45 » (minutes) en timedelta."""
    text = text.lower().replace(" ", "")
    if not text:
        return None
    total, pos = 0, 0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            return None
        total += int(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text) or total <= 0:
        return None
    return timedelta(seconds=total)


def format_duration(td: timedelta) -> str:
    seconds = int(td.total_seconds())
    parts = []
    for label, size in (("j", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        value, seconds = divmod(seconds, size)
        if value:
            parts.append(f"{value} {label}")
    return " ".join(parts) or "0 s"


def hierarchy_error(
    interaction: discord.Interaction, target: discord.Member, *, check_bot: bool = True
) -> str | None:
    """Retourne un message d'erreur si l'auteur (ou le bot) ne peut pas agir sur `target`."""
    guild = interaction.guild
    author = interaction.user
    if target.id == author.id:
        return "Tu peux pas faire ça sur toi-même."
    if target.id == guild.owner_id:
        return "C'est le propriétaire du serveur, oublie ça."
    if target.id == guild.me.id:
        return "Bien essayé 😄"
    if isinstance(author, discord.Member) and author.id != guild.owner_id and target.top_role >= author.top_role:
        return "Ce membre a un rôle égal ou plus haut que le tien."
    if check_bot and target.top_role >= guild.me.top_role:
        return "Ce membre a un rôle égal ou plus haut que le mien. Monte mon rôle dans les paramètres du serveur."
    return None


def fill(template: str, member: discord.Member, **extra) -> str:
    """Remplace les variables {membre}, {pseudo}, {serveur}, {nombre} (et autres) dans un texte."""
    values = {
        "membre": member.mention,
        "pseudo": member.display_name,
        "serveur": member.guild.name,
        "nombre": str(member.guild.member_count or ""),
        **{k: str(v) for k, v in extra.items()},
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template
