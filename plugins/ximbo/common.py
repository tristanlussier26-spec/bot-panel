"""Petits outils du plugin Ximbo : temps, barre de progression, recherche, textes."""
from __future__ import annotations

import re

import discord

COLOR = 0x1DB954
URL_RE = re.compile(r"^https?://", re.I)
SPOTIFY_URL_RE = re.compile(r"^https?://(open\.|play\.)?spotify\.com/|^spotify:", re.I)
SEARCH_SOURCES = {"spotify": "spsearch", "youtube_music": "ytmsearch", "soundcloud": "scsearch"}
SOURCE_LABEL = {
    "spotify": "🟢 Spotify", "youtube": "🔴 YouTube", "soundcloud": "🟠 SoundCloud", "deezer": "🟣 Deezer",
    "applemusic": "🍎 Apple Music", "twitch": "🟪 Twitch", "bandcamp": "🔵 Bandcamp", "http": "🌐 Lien direct",
}


def fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    s = int(ms // 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def progress_bar(pos: int, length: int, size: int = 16) -> str:
    if not length:
        return "▬" * size
    filled = max(0, min(size - 1, int(size * pos / length)))
    return "▬" * filled + "🔘" + "▬" * (size - filled - 1)


def search_order(query: str, preferred: str, spotify_ok: bool) -> list[str | None]:
    """Les sources à essayer pour une recherche, dans l'ordre. None = un lien, joué tel quel."""
    if URL_RE.match(query) or query.lower().startswith("spotify:"):
        return [None]
    first = SEARCH_SOURCES.get(preferred, "spsearch")
    if first == "spsearch" and not spotify_ok:
        first = "ytmsearch"
    order = [first] + [s for s in ("ytmsearch", "ytsearch", "scsearch") if s != first]
    return order


def channel_name(display_name: str, user_id: int) -> str:
    slug = re.sub(r"[^a-z0-9à-ÿ_-]+", "-", display_name.lower()).strip("-")[:80]
    return f"🎵・{slug or user_id}"


async def reply(interaction: discord.Interaction, content: str | None = None, *, embed: discord.Embed | None = None,
                ephemeral: bool = True) -> None:
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
