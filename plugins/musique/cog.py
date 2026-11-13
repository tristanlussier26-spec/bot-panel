"""Musique : un lecteur dans les salons vocaux (liens audio, radios en ligne, fichiers), avec file d'attente."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .common import BLUE, reply, truncate

log = logging.getLogger("plugin.musique")

AUDIO_EXT = {".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m4a", ".aac", ".webm", ".mka"}
BLOCKED_SITES = {  # leurs conditions interdisent de jouer leur musique ailleurs que dans leur appli
    "youtube.com": "YouTube", "youtu.be": "YouTube", "music.youtube.com": "YouTube Music",
    "spotify.com": "Spotify", "deezer.com": "Deezer", "music.apple.com": "Apple Music", "tidal.com": "Tidal",
}
RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
MAX_QUEUE = 50


@dataclass
class Track:
    title: str
    url: str
    requester_id: int
    live: bool = False  # radio : pas de fin


@dataclass
class Player:
    guild_id: int
    queue: deque = field(default_factory=deque)
    current: Track | None = None
    voice: discord.VoiceClient | None = None
    volume: float = 0.5
    idle_since: float = field(default_factory=time.monotonic)
    text_channel_id: int = 0


def site_of(host: str) -> str | None:
    host = host.lower().removeprefix("www.").removeprefix("m.")
    for domain, name in BLOCKED_SITES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def title_from_url(url: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    return truncate(name or urlsplit(url).netloc, 100)


def parse_radios(lines) -> dict[str, str]:
    """Lignes « Nom | https://… » des options -> {nom: adresse}."""
    radios = {}
    for line in lines or []:
        name, sep, url = str(line).partition("|")
        if sep and name.strip() and url.strip().startswith(("http://", "https://")):
            radios[name.strip()[:60]] = url.strip()
    return radios


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self.players: dict[int, Player] = {}
        self._ffmpeg: str | None = None

    async def cog_load(self) -> None:
        self.idle_check.start()

    async def cog_unload(self) -> None:
        self.idle_check.cancel()
        for player in self.players.values():
            if player.voice is not None:
                await player.voice.disconnect(force=True)

    def cfg(self, guild_id: int) -> dict:
        return self.host.config(guild_id, "musique")

    # ---- Sources ------------------------------------------------------------------------------------
    def ffmpeg(self) -> str:
        if self._ffmpeg is None:
            try:
                import imageio_ffmpeg  # ffmpeg installé par pip (requirements.txt du plugin)
                self._ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:  # noqa: BLE001
                self._ffmpeg = shutil.which("ffmpeg") or ""
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg introuvable : redémarre le bot pour installer les dépendances du plugin")
        return self._ffmpeg

    def make_source(self, track: Track, volume: float) -> discord.AudioSource:
        audio = discord.FFmpegPCMAudio(track.url, executable=self.ffmpeg(), before_options=RECONNECT, options="-vn")
        return discord.PCMVolumeTransformer(audio, volume=volume)

    async def resolve(self, host: str) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        return [info[4][0] for info in infos]

    async def check_url(self, url: str) -> str | None:
        """Problème avec cette adresse, ou None si on peut la jouer."""
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "Donne un lien qui commence par http:// ou https://."
        if name := site_of(parts.hostname):
            return f"{name} est pas supporté : leurs conditions interdisent de jouer leur musique ailleurs que dans leur appli. Utilise un lien vers un fichier audio, une radio en ligne ou un fichier."
        try:
            addresses = await self.resolve(parts.hostname)
        except OSError:
            return "Je trouve pas ce site."
        if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
            return "Ce lien pointe vers un réseau privé : refusé."
        return None

    # ---- Lecteur --------------------------------------------------------------------------------------
    def player(self, guild_id: int) -> Player:
        if guild_id not in self.players:
            volume = max(0, min(100, int(self.cfg(guild_id).get("volume", 50) or 0))) / 100
            self.players[guild_id] = Player(guild_id, volume=volume)
        return self.players[guild_id]

    def play_next(self, guild_id: int) -> Track | None:
        player = self.players.get(guild_id)
        if player is None or player.voice is None or not player.voice.is_connected():
            return None
        if not player.queue:
            player.current, player.idle_since = None, time.monotonic()
            return None
        track = player.queue.popleft()
        player.current = track
        try:
            source = self.make_source(track, player.volume)
        except Exception as e:  # noqa: BLE001
            log.warning("Lecture de %s impossible : %s", track.url, e)
            return self.play_next(guild_id)
        loop = asyncio.get_running_loop()
        player.voice.play(source, after=lambda error: loop.call_soon_threadsafe(self._after, guild_id, error))
        return track

    def _after(self, guild_id: int, error: Exception | None) -> None:
        if error:
            log.warning("Musique (serveur %s) : %s", guild_id, error)
        self.play_next(guild_id)

    async def enqueue(self, player: Player, track: Track) -> str:
        if len(player.queue) >= MAX_QUEUE:
            return f"La file est pleine ({MAX_QUEUE} morceaux)."
        player.queue.append(track)
        if player.voice is not None and not player.voice.is_playing() and not player.voice.is_paused():
            self.play_next(player.guild_id)
            return f"▶️ Je joue **{track.title}**."
        return f"➕ **{track.title}** ajouté à la file (position {len(player.queue)})."

    def dj(self, member: discord.Member) -> bool:
        role_id = int(self.cfg(member.guild.id).get("role_dj_id", 0) or 0)
        if member.guild_permissions.manage_channels:
            return True
        return any(r.id == role_id for r in member.roles) if role_id else True

    async def connect(self, interaction: discord.Interaction) -> Player | None:
        """Rejoint le salon vocal du membre. Retourne le lecteur, ou None (et répond) si c'est pas possible."""
        member = interaction.user
        if not self.cfg(interaction.guild.id).get("active"):
            await reply(interaction, "La musique est désactivée sur ce serveur (options du bot).")
            return None
        channel = getattr(member.voice, "channel", None)
        if channel is None:
            await reply(interaction, "🔊 Va dans un salon vocal d'abord.")
            return None
        player = self.player(interaction.guild.id)
        player.text_channel_id = interaction.channel_id or 0
        voice = interaction.guild.voice_client
        if voice is not None and voice.channel != channel:
            others = [m for m in voice.channel.members if not m.bot]
            if others and not self.dj(member):
                await reply(interaction, f"Je joue déjà dans {voice.channel.mention}.")
                return None
            await voice.move_to(channel)
        elif voice is None:
            try:
                voice = await channel.connect(self_deaf=True)
            except (discord.ClientException, asyncio.TimeoutError, RuntimeError) as e:
                log.warning("Connexion vocale : %s", e)
                await reply(interaction, "⚠️ Je peux pas rejoindre le salon vocal (permissions « Se connecter » et « Parler »?).")
                return None
        player.voice = voice
        return player

    async def controls(self, interaction: discord.Interaction) -> Player | None:
        player = self.players.get(interaction.guild.id)
        if player is None or player.voice is None:
            await reply(interaction, "Je joue rien en ce moment.")
            return None
        if not self.dj(interaction.user):
            await reply(interaction, "⛔ Faut le rôle DJ pour ça.")
            return None
        return player

    # ---- Commandes -----------------------------------------------------------------------------------------
    @app_commands.command(name="jouer", description="Joue un lien audio, une radio en ligne ou un fichier dans ton salon vocal")
    @app_commands.describe(lien="Lien vers un fichier audio (.mp3, .ogg…) ou un flux de radio", fichier="Un fichier audio")
    @app_commands.guild_only()
    async def jouer(self, interaction: discord.Interaction, lien: str | None = None, fichier: discord.Attachment | None = None) -> None:
        if fichier is not None:
            ext = PurePosixPath(fichier.filename).suffix.lower()
            if not ((fichier.content_type or "").startswith("audio/") or ext in AUDIO_EXT):
                return await reply(interaction, "Ce fichier est pas un fichier audio.")
            track = Track(truncate(fichier.filename, 100), fichier.url, interaction.user.id)
        elif lien:
            if problem := await self.check_url(lien):
                return await reply(interaction, problem)
            track = Track(title_from_url(lien), lien.strip(), interaction.user.id,
                          live=PurePosixPath(urlsplit(lien).path).suffix.lower() not in AUDIO_EXT)
        else:
            return await reply(interaction, "Donne un lien ou un fichier. Pour une radio de la liste : /radio.")
        await interaction.response.defer()
        if (player := await self.connect(interaction)) is not None:
            await reply(interaction, await self.enqueue(player, track), ephemeral=False)

    async def _radio_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        radios = parse_radios(self.cfg(interaction.guild_id or 0).get("radios"))
        return [app_commands.Choice(name=n, value=n) for n in radios if current.lower() in n.lower()][:25]

    @app_commands.command(name="radio", description="Joue une des radios réglées dans les options du bot")
    @app_commands.describe(station="Le nom de la radio")
    @app_commands.autocomplete(station=_radio_names)
    @app_commands.guild_only()
    async def radio(self, interaction: discord.Interaction, station: str) -> None:
        radios = parse_radios(self.cfg(interaction.guild.id).get("radios"))
        if station not in radios:
            names = ", ".join(radios) or "aucune (ajoute-en dans les options du bot, page Musique)"
            return await reply(interaction, f"Radio inconnue. Radios : {names}.")
        if problem := await self.check_url(radios[station]):
            return await reply(interaction, problem)
        await interaction.response.defer()
        if (player := await self.connect(interaction)) is not None:
            await reply(interaction, await self.enqueue(player, Track(f"📻 {station}", radios[station], interaction.user.id, live=True)),
                        ephemeral=False)

    @app_commands.command(name="pause", description="Met la musique sur pause")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        if (player := await self.controls(interaction)) and player.voice.is_playing():
            player.voice.pause()
            await reply(interaction, "⏸️ Pause.", ephemeral=False)
        elif player:
            await reply(interaction, "C'est déjà sur pause.")

    @app_commands.command(name="reprendre", description="Reprend la musique")
    @app_commands.guild_only()
    async def reprendre(self, interaction: discord.Interaction) -> None:
        if (player := await self.controls(interaction)) and player.voice.is_paused():
            player.voice.resume()
            await reply(interaction, "▶️ C'est reparti.", ephemeral=False)
        elif player:
            await reply(interaction, "C'est pas sur pause.")

    @app_commands.command(name="passer", description="Passe au morceau suivant")
    @app_commands.guild_only()
    async def passer(self, interaction: discord.Interaction) -> None:
        if player := await self.controls(interaction):
            skipped = player.current.title if player.current else "?"
            player.voice.stop()  # le morceau suivant part tout seul (après la fin de celui-ci)
            await reply(interaction, f"⏭️ **{skipped}** passé.", ephemeral=False)

    @app_commands.command(name="arreter", description="Arrête la musique, vide la file et quitte le salon")
    @app_commands.guild_only()
    async def arreter(self, interaction: discord.Interaction) -> None:
        if player := await self.controls(interaction):
            player.queue.clear()
            player.current = None
            await player.voice.disconnect(force=False)
            player.voice = None
            await reply(interaction, "⏹️ Musique arrêtée.", ephemeral=False)

    @app_commands.command(name="volume", description="Change le volume (0 à 100)")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, niveau: app_commands.Range[int, 0, 100]) -> None:
        if player := await self.controls(interaction):
            player.volume = niveau / 100
            if isinstance(getattr(player.voice, "source", None), discord.PCMVolumeTransformer):
                player.voice.source.volume = player.volume
            await reply(interaction, f"🔊 Volume : {niveau} %.", ephemeral=False)

    @app_commands.command(name="file", description="Ce qui joue et la file d'attente")
    @app_commands.guild_only()
    async def file(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild.id)
        if player is None or (player.current is None and not player.queue):
            return await reply(interaction, "La file est vide.")
        lines = [f"▶️ **{player.current.title}** (<@{player.current.requester_id}>)"] if player.current else []
        lines += [f"`{i}.` {t.title} (<@{t.requester_id}>)" for i, t in enumerate(list(player.queue)[:20], 1)]
        if len(player.queue) > 20:
            lines.append(f"… et {len(player.queue) - 20} autres")
        await reply(interaction, embed=discord.Embed(title="🎶 File d'attente", description="\n".join(lines), color=BLUE))

    # ---- Départ automatique ---------------------------------------------------------------------------------
    @tasks.loop(seconds=60)
    async def idle_check(self) -> None:
        now = time.monotonic()
        for player in list(self.players.values()):
            voice = player.voice
            if voice is None:
                continue
            if not voice.is_connected():
                player.voice = None
                continue
            minutes = int(self.cfg(player.guild_id).get("quitter_minutes", 5) or 5)
            playing = voice.is_playing() or voice.is_paused()
            listeners = any(not m.bot for m in voice.channel.members)
            if playing and listeners:
                player.idle_since = now  # quelqu'un écoute : on reste
            elif now - player.idle_since >= minutes * 60:
                player.queue.clear()
                player.current = None
                await voice.disconnect(force=False)
                player.voice = None

    @idle_check.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
