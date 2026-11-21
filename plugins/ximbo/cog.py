"""Ximbo : bouton « Créer mon bot musique », salons de contrôle privés et lecteur Lavalink (Spotify, YouTube, SoundCloud).

Fonctionnement :
  - /ximbo-panneau publie un panneau avec le bouton « Créer mon bot musique » ;
  - chaque membre reçoit un salon privé (catégorie réglable) avec un lecteur à boutons ;
  - dans ce salon, il écrit un titre ou colle un lien : c'est ajouté à la file et le bot rejoint son vocal.
Un bot Discord est dans un seul vocal par serveur : un salon peut prendre le lecteur seulement s'il est libre.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
import time
from collections import defaultdict
from pathlib import Path

import discord
import wavelink
from discord import app_commands
from discord.ext import commands, tasks

from .common import COLOR, SOURCE_LABEL, SPOTIFY_URL_RE, channel_name, fmt_ms, progress_bar, reply, search_order
from .lavalink import LavalinkManager

log = logging.getLogger("plugin.ximbo")

SECTION = "ximbo"
SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    channel_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    owner_id   INTEGER NOT NULL,
    message_id INTEGER,
    volume     INTEGER NOT NULL DEFAULT 60,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS rooms_owner ON rooms (guild_id, owner_id);
"""
LOGO = Path(__file__).resolve().parent / "logo.png"
BRAND: dict = {"icon": None, "name": "Ximbo"}


class UserError(Exception):
    """Erreur à montrer telle quelle au membre."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = f"❌ {message}"


# ======================================================================
# Lecteur
# ======================================================================
class XimboPlayer(wavelink.Player):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.room_id: int | None = None
        self.filter_name = "Aucun"
        self.empty_since: float | None = None


def _bass(f: wavelink.Filters) -> None:
    f.equalizer.set(bands=[{"band": i, "gain": g} for i, g in enumerate([0.30, 0.25, 0.20, 0.12, 0.05])])


FILTERS: dict[str, tuple[str, object]] = {
    "aucun": ("Aucun", None),
    "bass": ("Bass boost", _bass),
    "nightcore": ("Nightcore", lambda f: f.timescale.set(speed=1.2, pitch=1.2, rate=1.0)),
    "vaporwave": ("Vaporwave", lambda f: f.timescale.set(speed=0.85, pitch=0.85, rate=1.0)),
    "8d": ("8D", lambda f: f.rotation.set(rotation_hz=0.2)),
    "karaoke": ("Karaoké", lambda f: f.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)),
}
LOOP_LABEL = {wavelink.QueueMode.normal: "Désactivée", wavelink.QueueMode.loop: "🔂 Titre", wavelink.QueueMode.loop_all: "🔁 File"}


def requester_of(track: wavelink.Playable) -> int | None:
    return getattr(track.extras, "requester", None)


def player_embed(room: dict, player: XimboPlayer | None) -> discord.Embed:
    active = player is not None and player.room_id == room["channel_id"]
    if not active or player.current is None:
        e = discord.Embed(
            title="🎵 Rien en lecture",
            description=("**Écris le nom d'une chanson** ou colle un lien **Spotify**, YouTube ou SoundCloud "
                         "directement dans ce salon. Playlists et albums acceptés.\n\n"
                         "➜ Va dans un **salon vocal** : je te rejoins tout seul."),
            color=COLOR,
        )
        if active and len(player.queue):
            e.add_field(name="En attente", value=f"{len(player.queue)} titre(s)")
        e.add_field(name="Propriétaire", value=f"<@{room['owner_id']}>")
        e.add_field(name="Volume", value=f"{room['volume']} %")
        if BRAND["icon"]:
            e.set_thumbnail(url=BRAND["icon"])
        e.set_footer(text=f"{BRAND['name']} · ⏮️ ⏯️ ⏭️ ⏹️ 🔀 · volume · boucle · autoplay · file", icon_url=BRAND["icon"])
        return e

    t = player.current
    e = discord.Embed(title=t.title[:256], url=t.uri, color=COLOR)
    e.set_author(name="⏸️ En pause" if player.paused else "▶️ En lecture")
    timeline = "🔴 **EN DIRECT**" if t.is_stream else \
        f"`{fmt_ms(player.position)}` {progress_bar(player.position, t.length)} `{fmt_ms(t.length)}`"
    e.description = f"**{t.author}**\n\n{timeline}"
    if t.artwork:
        e.set_thumbnail(url=t.artwork)
    e.add_field(name="Volume", value=f"{player.volume} %")
    e.add_field(name="Boucle", value=LOOP_LABEL.get(player.queue.mode, "?"))
    e.add_field(name="Autoplay", value="✨ Recommandations" if player.autoplay == wavelink.AutoPlayMode.enabled else "Désactivé")
    e.add_field(name="Filtre", value=player.filter_name)
    e.add_field(name="Source", value=SOURCE_LABEL.get(t.source, t.source))
    req = requester_of(t)
    e.add_field(name="Demandé par", value=f"<@{req}>" if req else "✨ Autoplay")
    upcoming = list(itertools.islice(player.queue, 5))
    if upcoming:
        lines = [f"`{i}.` {tr.title[:60]} — *{tr.author[:30]}* `{fmt_ms(tr.length)}`" for i, tr in enumerate(upcoming, 1)]
        if (rest := len(player.queue) - len(upcoming)) > 0:
            lines.append(f"… et **{rest}** autre(s)")
        e.add_field(name=f"À suivre ({len(player.queue)})", value="\n".join(lines), inline=False)
    total = sum(tr.length for tr in player.queue if not tr.is_stream)
    where = player.channel.name if player.channel else "?"
    e.set_footer(text=f"{BRAND['name']} · 🔊 {where} · File : {fmt_ms(total)}", icon_url=BRAND["icon"])
    return e


def panel_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title="🎧 Ton propre bot musique",
        description=("Clique sur **Créer mon bot musique** pour avoir ton **salon de contrôle privé**.\n\n"
                     "Dedans, tu écris juste un titre ou tu colles un lien **Spotify** et je joue dans ton vocal, "
                     "avec un lecteur à boutons : pause, passer, file, volume, boucle, autoplay, filtres "
                     "(bass boost, nightcore, 8D…) et invitation de tes amis."),
        color=COLOR,
    )
    if BRAND["icon"]:
        e.set_thumbnail(url=BRAND["icon"])
        e.set_author(name=BRAND["name"], icon_url=BRAND["icon"])
    e.set_footer(text="Un salon par membre · je joue dans un seul vocal à la fois sur ce serveur")
    return e


# ======================================================================
# Vues persistantes (custom_id fixes : elles marchent encore après un redémarrage)
# ======================================================================
def _cog(interaction: discord.Interaction) -> "Ximbo":
    return interaction.client.get_cog("Ximbo")  # type: ignore[return-value]


class PanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Créer mon bot musique", emoji="🎵", style=discord.ButtonStyle.success, custom_id="ximbo:create")
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _cog(interaction).create_room(interaction)

    @discord.ui.button(label="Mon salon", emoji="📍", style=discord.ButtonStyle.secondary, custom_id="ximbo:mine")
    async def mine(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        room = await _cog(interaction).room_of_owner(interaction.guild_id, interaction.user.id)
        if room and interaction.guild.get_channel(room["channel_id"]):
            await reply(interaction, f"📍 Ton salon : <#{room['channel_id']}>")
        else:
            await reply(interaction, "Tu as pas encore de salon : clique sur **Créer mon bot musique**.")


class ControlsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _go(self, interaction: discord.Interaction, action: str, value: str | None = None) -> None:
        await _cog(interaction).control(interaction, action, value)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="ximbo:prev", row=0)
    async def prev(self, i, _): await self._go(i, "prev")

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="ximbo:pause", row=0)
    async def pause(self, i, _): await self._go(i, "pause")

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="ximbo:skip", row=0)
    async def skip(self, i, _): await self._go(i, "skip")

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="ximbo:stop", row=0)
    async def stop_(self, i, _): await self._go(i, "stop")

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="ximbo:shuffle", row=0)
    async def shuffle(self, i, _): await self._go(i, "shuffle")

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="ximbo:voldown", row=1)
    async def voldown(self, i, _): await self._go(i, "voldown")

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="ximbo:volup", row=1)
    async def volup(self, i, _): await self._go(i, "volup")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="ximbo:loop", row=1)
    async def loop(self, i, _): await self._go(i, "loop")

    @discord.ui.button(emoji="✨", label="Autoplay", style=discord.ButtonStyle.secondary, custom_id="ximbo:autoplay", row=1)
    async def autoplay(self, i, _): await self._go(i, "autoplay")

    @discord.ui.button(emoji="📜", label="File", style=discord.ButtonStyle.secondary, custom_id="ximbo:queue", row=1)
    async def queue(self, i, _): await self._go(i, "queue")

    @discord.ui.select(custom_id="ximbo:filter", placeholder="🎛️ Filtre audio", row=2,
                       options=[discord.SelectOption(label=label, value=key) for key, (label, _) in FILTERS.items()])
    async def filter_(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        await self._go(interaction, "filter", select.values[0])

    @discord.ui.button(emoji="➕", label="Ajouter", style=discord.ButtonStyle.success, custom_id="ximbo:add", row=3)
    async def add(self, i, _): await self._go(i, "add")

    @discord.ui.button(emoji="👥", label="Inviter", style=discord.ButtonStyle.secondary, custom_id="ximbo:invite", row=3)
    async def invite(self, i, _): await self._go(i, "invite")

    @discord.ui.button(emoji="🗑️", label="Vider la file", style=discord.ButtonStyle.secondary, custom_id="ximbo:clear", row=3)
    async def clear(self, i, _): await self._go(i, "clear")

    @discord.ui.button(emoji="❌", label="Fermer le salon", style=discord.ButtonStyle.danger, custom_id="ximbo:close", row=3)
    async def close(self, i, _): await self._go(i, "close")


class AddModal(discord.ui.Modal, title="Ajouter à la file"):
    query = discord.ui.TextInput(label="Titre ou lien (Spotify, YouTube, SoundCloud)", max_length=300,
                                 placeholder="ex. Les Cowboys Fringants - L'Amérique pleure")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            msg = await _cog(interaction).handle_query(interaction.user, interaction.channel, str(self.query))
        except UserError as e:
            msg = e.user_message
        await interaction.followup.send(msg, ephemeral=True)


class InviteView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=90)
        self.owner_id = owner_id

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choisis des amis (re-choisir = retirer)", min_values=1, max_values=10)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        channel = interaction.channel
        added, removed = [], []
        for user in select.values:
            if user.bot or user.id == self.owner_id or not isinstance(user, discord.Member):
                continue
            if channel.overwrites_for(user).view_channel:
                await channel.set_permissions(user, overwrite=None, reason="Retiré du salon musique")
                removed.append(user.mention)
            else:
                await channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True,
                                              reason="Invité au salon musique")
                added.append(user.mention)
        lines = (["✅ Ajouté(s) : " + ", ".join(added)] if added else []) + (["➖ Retiré(s) : " + ", ".join(removed)] if removed else [])
        await interaction.response.edit_message(content="\n".join(lines) or "Rien changé.", view=None)


class ConfirmClose(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=30)

    @discord.ui.button(label="Oui, supprimer le salon", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="🗑️ Suppression…", view=None)
        await _cog(interaction).close_room(interaction.guild, interaction.channel_id)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Annulé.", view=None)


# ======================================================================
# Cog
# ======================================================================
class Ximbo(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host
        self.db = None
        self.rooms: dict[int, int] = {}  # salon -> serveur
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_edit: dict[int, float] = {}
        self.manager: LavalinkManager | None = None
        self.node_status = "pas encore démarré"
        self._tasks: list[asyncio.Task] = []
        self._started = False
        sid, secret = os.getenv("XIMBO_SPOTIFY_ID", "").strip(), os.getenv("XIMBO_SPOTIFY_SECRET", "").strip()
        self.spotify = (sid, secret) if sid and secret else ("", "")
        self.external = os.getenv("XIMBO_LAVALINK_URI", "").strip()

    @property
    def spotify_ok(self) -> bool:
        return bool(self.spotify[0])

    async def cog_load(self) -> None:
        self.db = await self.host.open_db("ximbo", SCHEMA)
        self.rooms = {r["channel_id"]: r["guild_id"] for r in await self.db.all("SELECT channel_id, guild_id FROM rooms")}
        self.bot.add_view(PanelView())
        self.bot.add_view(ControlsView())
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()
        for vc in list(self.bot.voice_clients):
            if isinstance(vc, XimboPlayer):
                await vc.disconnect()
        for task in self._tasks:
            task.cancel()
        if self.manager is not None:
            await self.manager.stop()
        if hasattr(wavelink.Pool, "close") and self._started:
            try:
                await wavelink.Pool.close()
            except Exception:  # noqa: BLE001
                pass

    def cfg(self, guild_id: int | None) -> dict:
        return self.host.config(guild_id, SECTION)

    # ---------------- démarrage : Lavalink, logo ----------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._started:
            return
        self._started = True
        if self.external:
            uri, password = self.external, os.getenv("XIMBO_LAVALINK_PASSWORD", "youshallnotpass")
            self.node_status = "connexion au Lavalink externe…"
        else:
            self.manager = LavalinkManager(self.host.data_dir("ximbo") / "lavalink", spotify=self.spotify)
            self._tasks.append(asyncio.create_task(self.manager.run()))
            uri, password = self.manager.uri, self.manager.password
            self.node_status = "en attente de Lavalink"
        node = wavelink.Node(identifier="ximbo", uri=uri, password=password, retries=None, inactive_player_timeout=300)
        self._tasks.append(asyncio.create_task(wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)))
        self._tasks.append(asyncio.create_task(self._branding()))

    async def _branding(self) -> None:
        """Dans le modèle Ximbo : le logo devient la photo du bot (seulement quand il change)."""
        if os.getenv("XIMBO_MODELE") == "1" and LOGO.is_file():
            data = LOGO.read_bytes()
            digest = hashlib.sha1(data).hexdigest()
            if await self.db.get_value("avatar") != digest:
                try:
                    await self.bot.user.edit(avatar=data)
                    await self.db.set_value("avatar", digest)
                    log.info("Logo Ximbo appliqué comme photo du bot")
                except discord.HTTPException as e:
                    log.warning("Photo du bot pas changée (Discord limite les changements) : %s", e)
            if not os.getenv("BOT_STATUT", "").strip():
                await asyncio.sleep(3)  # après le statut du bot de base
                await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/ximbo-panneau"))
        BRAND["icon"] = self.bot.user.display_avatar.with_size(256).url
        BRAND["name"] = self.bot.user.display_name

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        self.node_status = "prêt"
        log.info("Connecté à Lavalink (resumed=%s)", payload.resumed)
        self.host.refresh_panel()

    def lavalink_status(self) -> str:
        if self.node_status == "prêt":
            return "✅ prêt"
        if self.manager is not None and self.manager.status != "prêt":
            return f"⏳ {self.manager.status}"
        return f"⏳ {self.node_status}"

    def panel_info(self, guild_id: int) -> list[tuple[str, str]]:
        count = sum(1 for g in self.rooms.values() if g == guild_id)
        spotify = "configuré" if self.spotify_ok else "pas configuré (variables XIMBO_SPOTIFY_ID et XIMBO_SPOTIFY_SECRET)"
        return [("Lavalink", self.lavalink_status()), ("Spotify", spotify), ("Salons musique", str(count))]

    # ---------------- outils ----------------
    async def room(self, channel_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM rooms WHERE channel_id = ?", (channel_id,))

    async def room_of_owner(self, guild_id: int, owner_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM rooms WHERE guild_id = ? AND owner_id = ?", (guild_id, owner_id))

    @staticmethod
    def player_for(guild: discord.Guild, room_id: int) -> XimboPlayer | None:
        vc = guild.voice_client
        return vc if isinstance(vc, XimboPlayer) and vc.room_id == room_id else None

    @staticmethod
    def can_manage(member: discord.Member, room: dict) -> bool:
        return member.id == room["owner_id"] or member.guild_permissions.manage_channels

    def ensure_ready(self) -> None:
        if self.node_status != "prêt":
            raise UserError(f"Le lecteur démarre encore ({self.lavalink_status().lstrip('⏳ ')}). "
                            "Au tout premier démarrage, ça peut prendre quelques minutes.")

    async def refresh(self, guild: discord.Guild | None, room_id: int | None, *, force: bool = True) -> None:
        if guild is None or room_id is None:
            return
        now = time.monotonic()
        if not force and now - self._last_edit.get(room_id, 0) < 10:
            return
        self._last_edit[room_id] = now
        room = await self.room(room_id)
        channel = guild.get_channel(room_id)
        if room is None or not isinstance(channel, discord.TextChannel):
            return
        embed = player_embed(room, self.player_for(guild, room_id))
        try:
            if room["message_id"]:
                await channel.get_partial_message(room["message_id"]).edit(embed=embed)
                return
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("Lecteur du salon %s pas mis à jour : %s", room_id, e)
            return
        msg = await channel.send(embed=embed, view=ControlsView())  # le message du lecteur a été effacé : on le refait
        await self.db.run("UPDATE rooms SET message_id = ? WHERE channel_id = ?", (msg.id, room_id))

    async def search(self, query: str, guild_id: int) -> wavelink.Search:
        query = query.strip()
        if SPOTIFY_URL_RE.match(query) and not self.spotify_ok:
            raise UserError("Les liens Spotify sont pas encore configurés : l'admin du bot doit remplir "
                            "XIMBO_SPOTIFY_ID et XIMBO_SPOTIFY_SECRET (onglet Variables). Écris plutôt le titre.")
        results: wavelink.Search = []
        for source in search_order(query, self.cfg(guild_id).get("recherche", "spotify"), self.spotify_ok):
            try:
                results = await wavelink.Playable.search(query) if source is None else \
                    await wavelink.Playable.search(query, source=source)
            except wavelink.LavalinkLoadException as e:
                if source is None:
                    raise UserError(f"Impossible de charger ce lien ({getattr(e, 'error', e)}).") from e
                continue
            if results:
                break
        return results

    async def ensure_player(self, member: discord.Member, room: dict) -> XimboPlayer:
        if not member.voice or not member.voice.channel:
            raise UserError("Va d'abord dans un salon vocal, puis renvoie ta chanson.")
        target, guild = member.voice.channel, member.guild
        cfg = self.cfg(guild.id)
        vc = guild.voice_client
        if vc is None:
            player: XimboPlayer = await target.connect(cls=XimboPlayer, self_deaf=True)
            player.room_id = room["channel_id"]
            player.autoplay = wavelink.AutoPlayMode.partial
            player.inactive_timeout = int(cfg.get("quitter_minutes", 5)) * 60
            await player.set_volume(room["volume"])
            return player
        if not isinstance(vc, XimboPlayer):
            raise UserError("Je suis déjà dans un vocal pour autre chose sur ce serveur.")
        player = vc
        if player.room_id != room["channel_id"]:
            others = [m for m in player.channel.members if not m.bot and m.id != member.id]
            if player.current is not None and others:
                raise UserError(f"Je joue déjà pour <#{player.room_id}> dans {player.channel.mention}. Réessaie quand je serai libre.")
            old = player.room_id  # le lecteur est libre : il passe à ce salon
            player.queue.reset()
            player.auto_queue.reset()
            await player.stop()
            await player.set_filters(None)
            player.filter_name = "Aucun"
            player.room_id = room["channel_id"]
            await player.set_volume(room["volume"])
            await self.refresh(guild, old)
        if player.channel.id != target.id:
            if player.current is not None and any(not m.bot for m in player.channel.members):
                raise UserError(f"Je suis dans {player.channel.mention}. Viens-y pour ajouter des chansons.")
            await player.move_to(target)
        return player

    # ---------------- ajout de musique ----------------
    async def handle_query(self, member: discord.Member, channel, query: str) -> str:
        room = await self.room(channel.id)
        if room is None:
            raise UserError("Ce salon est pas un salon musique.")
        self.ensure_ready()
        async with self.locks[member.guild.id]:
            player = await self.ensure_player(member, room)
            results = await self.search(query, member.guild.id)
            if not results:
                raise UserError(f"Aucun résultat pour **{query[:80]}**.")
            if isinstance(results, wavelink.Playlist):
                for t in results.tracks:
                    t.extras = {"requester": member.id}
                added = await player.queue.put_wait(results)
                msg = f"📃 **{results.name}** ajoutée : {added} titre(s)"
            else:
                track = results[0]
                track.extras = {"requester": member.id}
                await player.queue.put_wait(track)
                msg = f"➕ **{track.title}** — {track.author}" + (f" (position {len(player.queue)})" if player.playing else "")
            if not player.playing:
                await player.play(player.queue.get(), volume=room["volume"])
        await self.refresh(member.guild, channel.id)
        return msg

    # ---------------- salons ----------------
    async def create_room(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        cfg = self.cfg(guild.id)
        if not cfg.get("active"):
            await reply(interaction, "Ximbo est pas activé sur ce serveur (Bot Panel › Options du bot › Ximbo).")
            return
        if (role_id := cfg.get("role_acces")) and not any(r.id == role_id for r in member.roles) \
                and not member.guild_permissions.manage_channels:
            await reply(interaction, f"Il faut le rôle <@&{role_id}> pour avoir un salon musique.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        existing = await self.room_of_owner(guild.id, member.id)
        if existing:
            if guild.get_channel(existing["channel_id"]):
                await reply(interaction, f"Tu as déjà ton salon : <#{existing['channel_id']}>")
                return
            await self.forget_room(existing["channel_id"])
        if sum(1 for g in self.rooms.values() if g == guild.id) >= int(cfg.get("max_salons", 25)):
            await reply(interaction, "❌ Le nombre maximum de salons musique est atteint sur ce serveur.")
            return
        category = guild.get_channel(cfg.get("categorie") or 0)
        try:
            if not isinstance(category, discord.CategoryChannel):
                category = await guild.create_category(
                    "🎵 Ximbo", overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
                    reason="Catégorie des salons musique Ximbo")
                await self.host.save(guild.id, SECTION, {"categorie": category.id})
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                      manage_messages=True, read_message_history=True, manage_channels=True),
            }
            channel = await guild.create_text_channel(
                channel_name(member.display_name, member.id), category=category, overwrites=overwrites,
                topic=f"Bot musique de {member.display_name} : écris un titre ou colle un lien Spotify ici.",
                reason=f"Salon musique de {member}")
        except discord.Forbidden:
            await reply(interaction, "❌ Il me manque des permissions : **Gérer les salons** et **Gérer les rôles**.")
            return
        welcome = discord.Embed(
            title=f"Bienvenue dans ton bot musique, {member.display_name} !",
            description=("• Va dans un **salon vocal**\n"
                         "• Écris ici un **titre** ou colle un lien **Spotify / YouTube / SoundCloud** (playlists OK)\n"
                         "• Contrôle tout avec le lecteur en dessous\n"
                         "• **👥 Inviter** donne l'accès à tes amis · **❌ Fermer** supprime le salon\n\n"
                         "Tes messages s'effacent tout seuls pour garder le salon propre."),
            color=COLOR)
        await channel.send(content=member.mention, embed=welcome)
        volume = int(cfg.get("volume", 60))
        room = {"channel_id": channel.id, "owner_id": member.id, "volume": volume}
        msg = await channel.send(embed=player_embed(room, None), view=ControlsView())
        await self.db.run("INSERT INTO rooms (channel_id, guild_id, owner_id, message_id, volume) VALUES (?, ?, ?, ?, ?)",
                          (channel.id, guild.id, member.id, msg.id, volume))
        self.rooms[channel.id] = guild.id
        self.host.refresh_panel()
        await reply(interaction, f"✅ Ton bot musique est prêt : {channel.mention}")

    async def forget_room(self, channel_id: int) -> None:
        await self.db.run("DELETE FROM rooms WHERE channel_id = ?", (channel_id,))
        self.rooms.pop(channel_id, None)
        self.host.refresh_panel()

    async def close_room(self, guild: discord.Guild, room_id: int) -> None:
        if player := self.player_for(guild, room_id):
            await player.disconnect()
        await self.forget_room(room_id)
        if channel := guild.get_channel(room_id):
            try:
                await channel.delete(reason="Salon musique fermé")
            except discord.HTTPException:
                pass

    # ---------------- boutons ----------------
    async def control(self, interaction: discord.Interaction, action: str, value: str | None = None) -> None:
        member = interaction.user
        room = await self.room(interaction.channel_id)
        if room is None:
            await reply(interaction, "Ce salon est plus un salon musique.")
            return
        player = self.player_for(interaction.guild, room["channel_id"])
        if action == "add":
            await interaction.response.send_modal(AddModal())
            return
        if action in ("invite", "close") and not self.can_manage(member, room):
            await reply(interaction, "Seul le propriétaire du salon peut faire ça.")
            return
        if action == "invite":
            await interaction.response.send_message("Qui veux-tu ajouter (ou retirer) ?", view=InviteView(room["owner_id"]), ephemeral=True)
            return
        if action == "close":
            await interaction.response.send_message("Supprimer ce salon et arrêter la musique ?", view=ConfirmClose(), ephemeral=True)
            return
        if action == "queue":
            await reply(interaction, embed=self.queue_embed(player))
            return
        try:
            if action in ("voldown", "volup"):
                current = player.volume if player else room["volume"]
                volume = max(0, min(150, current + (10 if action == "volup" else -10)))
                if player:
                    await player.set_volume(volume)
                await self.db.run("UPDATE rooms SET volume = ? WHERE channel_id = ?", (volume, room["channel_id"]))
                room = await self.room(room["channel_id"])
            else:
                if player is None:
                    raise UserError("Rien joue. Écris un titre dans le salon pour commencer.")
                if (not member.voice or member.voice.channel != player.channel) and not self.can_manage(member, room):
                    raise UserError(f"Viens dans {player.channel.mention} pour contrôler la musique.")
                await self.player_action(player, action, value)
        except UserError as e:
            await reply(interaction, e.user_message)
            return
        kwargs: dict = {"embed": player_embed(room, self.player_for(interaction.guild, room["channel_id"]))}
        if action == "filter":
            kwargs["view"] = ControlsView()  # remet le menu des filtres à zéro
        self._last_edit[room["channel_id"]] = time.monotonic()
        await interaction.response.edit_message(**kwargs)

    async def player_action(self, player: XimboPlayer, action: str, value: str | None) -> None:
        if action == "pause":
            await player.pause(not player.paused)
        elif action == "skip":
            if not player.current:
                raise UserError("Rien à passer.")
            await player.skip(force=True)
        elif action == "prev":
            history = player.queue.history
            if player.position > 5000 or history is None or len(history) < 2:
                await player.seek(0)
            else:
                previous = history[-2]
                if player.current:
                    player.queue.put_at(0, player.current)
                await player.play(previous)
        elif action == "stop":
            player.queue.reset()
            await player.disconnect()
        elif action == "shuffle":
            if len(player.queue) < 2:
                raise UserError("Pas assez de titres dans la file pour mélanger.")
            player.queue.shuffle()
        elif action == "loop":
            order = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
            player.queue.mode = order[(order.index(player.queue.mode) + 1) % 3]
        elif action == "autoplay":
            enabled = player.autoplay == wavelink.AutoPlayMode.enabled
            player.autoplay = wavelink.AutoPlayMode.partial if enabled else wavelink.AutoPlayMode.enabled
        elif action == "clear":
            player.queue.clear()
        elif action == "filter":
            label, apply = FILTERS.get(value or "aucun", FILTERS["aucun"])
            if apply is None:
                await player.set_filters(None)
            else:
                filters = wavelink.Filters()
                apply(filters)
                await player.set_filters(filters)
            player.filter_name = label

    @staticmethod
    def queue_embed(player: XimboPlayer | None) -> discord.Embed:
        e = discord.Embed(title="📜 File d'attente", color=COLOR)
        if player is None or (player.current is None and not len(player.queue)):
            e.description = "La file est vide."
            return e
        lines = [f"**▶️ {player.current.title}** — {player.current.author}\n"] if player.current else []
        lines += [f"`{i:>2}.` {t.title[:55]} — *{t.author[:25]}* `{fmt_ms(t.length)}`"
                  for i, t in enumerate(itertools.islice(player.queue, 20), 1)]
        if len(player.queue) > 20:
            lines.append(f"\n… et {len(player.queue) - 20} autre(s)")
        e.description = "\n".join(lines)
        total = sum(t.length for t in player.queue if not t.is_stream)
        e.set_footer(text=f"{len(player.queue)} titre(s) · {fmt_ms(total)}")
        return e

    # ---------------- événements ----------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or message.channel.id not in self.rooms:
            return
        query = message.content.strip()
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if not query:
            if not message.attachments and not message.stickers:  # contenu vide : l'intent est pas activé
                await message.channel.send(
                    "⚠️ Je peux pas lire les messages : active **Message Content Intent** dans le Developer Portal "
                    "(Bot › Privileged Gateway Intents), puis redémarre le bot. En attendant, utilise **➕ Ajouter**.",
                    delete_after=15)
            return
        try:
            async with message.channel.typing():
                msg = await self.handle_query(message.author, message.channel, query)
        except UserError as e:
            msg = e.user_message
        except Exception:  # noqa: BLE001
            log.exception("Ajout raté")
            msg = "❌ Erreur inattendue, réessaie. Les détails sont dans la console du bot."
        await message.channel.send(f"{message.author.mention} {msg}", delete_after=8,
                                   allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        if isinstance(payload.player, XimboPlayer):
            await self.refresh(payload.player.guild, payload.player.room_id)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if isinstance(player, XimboPlayer):
            await asyncio.sleep(1.5)
            if player.current is None:
                await self.refresh(player.guild, player.room_id)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if isinstance(player, XimboPlayer) and (channel := player.guild.get_channel(player.room_id or 0)):
            await channel.send(f"⚠️ Lecture impossible : **{payload.track.title}** (je passe au suivant)", delete_after=10)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        guild, room_id = player.guild, getattr(player, "room_id", None)
        await player.disconnect()
        await self.refresh(guild, room_id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.id in self.rooms:
            if player := self.player_for(channel.guild, channel.id):
                await player.disconnect()
            await self.forget_room(channel.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.run("DELETE FROM rooms WHERE guild_id = ?", (guild.id,))
        self.rooms = {c: g for c, g in self.rooms.items() if g != guild.id}

    @tasks.loop(seconds=15)
    async def ticker(self) -> None:
        """Avance la barre de progression et quitte les vocaux où il reste personne."""
        for vc in list(self.bot.voice_clients):
            if not isinstance(vc, XimboPlayer) or vc.room_id is None or vc.channel is None:
                continue
            try:
                if any(not m.bot for m in vc.channel.members):
                    vc.empty_since = None
                else:
                    vc.empty_since = vc.empty_since or time.monotonic()
                    limit = int(self.cfg(vc.guild.id).get("quitter_minutes", 5)) * 60
                    if time.monotonic() - vc.empty_since > min(limit, 180):
                        guild, room_id = vc.guild, vc.room_id
                        await vc.disconnect()
                        await self.refresh(guild, room_id)
                        continue
                if vc.current and not vc.paused:
                    await self.refresh(vc.guild, vc.room_id, force=False)
            except Exception:  # noqa: BLE001
                log.exception("ticker")

    @ticker.before_loop
    async def _before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    # ---------------- commandes ----------------
    @app_commands.command(name="ximbo-panneau", description="Publie le bouton « Créer mon bot musique »")
    @app_commands.describe(salon="Où publier le panneau (par défaut : ici)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def panneau(self, interaction: discord.Interaction, salon: discord.TextChannel | None = None) -> None:
        salon = salon or interaction.channel
        try:
            await salon.send(embed=panel_embed(interaction.guild), view=PanelView())
        except discord.Forbidden:
            await reply(interaction, f"❌ Je peux pas écrire dans {salon.mention}.")
            return
        await reply(interaction, f"✅ Panneau publié dans {salon.mention}.")

    @app_commands.command(name="ximbo-fermer", description="Ferme ton salon musique (ou celui-ci si tu gères les salons)")
    @app_commands.guild_only()
    async def fermer(self, interaction: discord.Interaction) -> None:
        room = await self.room(interaction.channel_id)
        if room is None or not self.can_manage(interaction.user, room):
            room = await self.room_of_owner(interaction.guild_id, interaction.user.id)
        if room is None:
            await reply(interaction, "Tu as pas de salon musique.")
            return
        await reply(interaction, "🗑️ Salon fermé.")
        await self.close_room(interaction.guild, room["channel_id"])

    @app_commands.command(name="ximbo-statut", description="État du lecteur (Lavalink, Spotify, salons)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def statut(self, interaction: discord.Interaction) -> None:
        e = discord.Embed(title="🎧 Ximbo", color=COLOR)
        for name, value in self.panel_info(interaction.guild_id):
            e.add_field(name=name, value=value, inline=False)
        if self.manager is not None and (versions := self.manager.state.get("versions")):
            e.add_field(name="Versions", value=", ".join(f"{k} {v}" for k, v in versions.items()), inline=False)
        await reply(interaction, embed=e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ximbo(bot))
