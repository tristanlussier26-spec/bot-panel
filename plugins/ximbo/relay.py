"""Lecture YouTube par yt-dlp, relayée à Lavalink.

Le plugin youtube-source de Lavalink se fait refuser l'audio par YouTube (« No supported audio streams available »,
« This video is unavailable »). yt-dlp, avec deno pour les défis JavaScript de YouTube, y arrive : c'est ce que fait
bot LeQueb. Ximbo trouve donc l'audio avec yt-dlp, puis le relaie à Lavalink par un petit serveur local :
Lavalink lit http://127.0.0.1:<port>/yt/<id> comme un simple fichier audio, et garde filtres, volume et avance rapide.

Les pistes données à Lavalink sont des pistes « http » encodées ici (format lavaplayer), avec le vrai titre, l'artiste
et la durée. Côté bot, la piste garde sa source d'origine (« youtube » + l'identifiant de la vidéo), pour l'autoplay.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import struct
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
import wavelink
from aiohttp import web

log = logging.getLogger("plugin.ximbo.relay")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Sources sans audio (LavaSrc) : on cherche la même chanson sur YouTube
MIRRORED = {"spotify", "applemusic", "deezer", "yandexmusic", "vkmusic", "tidal", "qobuz"}
# Audio seul, en téléchargement direct (pas de flux HLS/DASH) : webm/opus d'abord, Lavalink le lit sans réencoder
FORMAT = ("bestaudio[ext=webm][protocol=https]/bestaudio[ext=m4a][protocol=https]/"
          "bestaudio[protocol=https]/bestaudio")
PROBES = {"webm": "matroska/webm", "weba": "matroska/webm", "mka": "matroska/webm", "m4a": "mp4", "mp4": "mp4"}
STREAM_TTL = 5 * 3600     # une adresse googlevideo expire après ~6 h
UPDATE_EVERY = 24 * 3600  # YouTube change souvent : un yt-dlp de quelques semaines finit par se faire refuser
SEARCH_TTL = 24 * 3600
CACHE_SIZE = 300


class RelayError(Exception):
    """Une chanson impossible à trouver ou à lire (message pour les membres)."""


# ---------------------------------------------------------------- pistes lavaplayer
def _java_utf(text: str) -> bytes:
    """DataOutput.writeUTF de Java : UTF-8 « modifié » (paires de substitution, NUL sur 2 octets) précédé de sa taille."""
    data = text.encode("utf-16-be", "surrogatepass")
    out = bytearray()
    for (unit,) in struct.iter_unpack(">H", data):
        if 0x0001 <= unit <= 0x007F:
            out.append(unit)
        elif unit <= 0x07FF:
            out += bytes((0xC0 | unit >> 6, 0x80 | unit & 0x3F))
        else:
            out += bytes((0xE0 | unit >> 12, 0x80 | (unit >> 6) & 0x3F, 0x80 | unit & 0x3F))
    if len(out) > 0xFFFF:
        raise ValueError("texte trop long pour une piste lavaplayer")
    return struct.pack(">H", len(out)) + bytes(out)


def encode_http_track(*, title: str, author: str, length: int, url: str, uri: str | None = None,
                      artwork: str | None = None, isrc: str | None = None, probe: str = "matroska/webm") -> str:
    """Piste « http » de lavaplayer (version 3), comme DefaultAudioPlayerManager.encodeTrack la produit."""
    body = bytearray(b"\x03")
    body += _java_utf(title) + _java_utf(author) + struct.pack(">q", int(length)) + _java_utf(url) + b"\x00"
    for value in (uri, artwork, isrc):
        body += (b"\x01" + _java_utf(value)) if value else b"\x00"
    body += _java_utf("http") + _java_utf(probe) + struct.pack(">q", 0)
    return base64.b64encode(struct.pack(">i", len(body) | 1 << 30) + bytes(body)).decode()


def meta_of(track: wavelink.Playable) -> dict | None:
    """Ce qu'il faut pour jouer la piste par yt-dlp, ou None si Lavalink la joue lui-même (SoundCloud, lien direct…)."""
    raw = track.raw_data or {}
    mine = (raw.get("pluginInfo") or {}).get("ximbo")
    if isinstance(mine, dict):
        return dict(mine)
    if track.is_stream:
        return None
    base = {"title": track.title, "author": track.author, "length": track.length, "uri": track.uri,
            "artwork": track.artwork, "isrc": track.isrc, "origin": track.source}
    if track.source == "youtube" and VIDEO_ID_RE.match(track.identifier or ""):
        return {**base, "video": track.identifier}
    if track.source in MIRRORED:
        return {**base, "id": track.identifier, "query": search_query(track.author, track.title)}
    return None


def search_query(author: str | None, title: str | None) -> str:
    return f"{author} - {title} audio" if author else f"{title} audio"


def make_playable(meta: dict, *, url: str, probe: str, source: str, identifier: str,
                  extras: dict | None = None) -> wavelink.Playable:
    title = str(meta.get("title") or "Sans titre")[:300]
    author = str(meta.get("author") or "Inconnu")[:200]
    length = int(meta.get("length") or 0)
    uri, artwork, isrc = meta.get("uri") or None, meta.get("artwork") or None, meta.get("isrc") or None
    return wavelink.Playable({
        "encoded": encode_http_track(title=title, author=author, length=length, url=url, uri=uri, artwork=artwork,
                                     isrc=isrc, probe=probe),
        "info": {"identifier": identifier, "isSeekable": True, "author": author, "length": length, "isStream": False,
                 "position": 0, "title": title, "uri": uri, "artworkUrl": artwork, "isrc": isrc, "sourceName": source},
        "pluginInfo": {"ximbo": meta},
        "userData": dict(extras or {}),
    })


def pending_playable(meta: dict, *, port: int, extras: dict | None = None) -> wavelink.Playable:
    """Une chanson d'une playlist Spotify/Deezer : la vidéo YouTube est cherchée seulement au moment de la jouer."""
    ident = str(meta.get("id") or meta.get("query") or "?")[:200]
    return make_playable(meta, url=f"http://127.0.0.1:{port}/attente", probe="matroska/webm",
                         source=str(meta.get("origin") or "spotify"), identifier=ident, extras=extras)


# ---------------------------------------------------------------- yt-dlp (bloquant : roule dans un thread)
def prepare_deno() -> None:
    """yt-dlp a besoin d'un moteur JavaScript (deno, installé par pip) pour YouTube : on l'ajoute au PATH."""
    try:
        import deno
        folder = os.path.dirname(deno.find_deno_bin())
    except Exception:  # noqa: BLE001  (pas installé : yt-dlp fera sans, moins bien)
        return
    if folder not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def ytdlp_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("yt-dlp")
    except PackageNotFoundError:
        return None


def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def _options(cache_dir: Path, **extra) -> dict:
    return {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "cachedir": str(cache_dir), "source_address": "0.0.0.0", **extra}


def _search(query: str, cache_dir: Path, count: int = 5) -> list[dict]:
    import yt_dlp
    with yt_dlp.YoutubeDL(_options(cache_dir, extract_flat="in_playlist")) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False) or {}
    found = []
    for e in info.get("entries") or []:
        if e and e.get("id"):
            found.append({"id": e["id"], "duration": e.get("duration"), "title": e.get("title"),
                          "live": e.get("live_status") in ("is_live", "is_upcoming") or bool(e.get("is_live"))})
    return found


def _extract(video_id: str, cache_dir: Path) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(_options(cache_dir, format=FORMAT)) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False) or {}


def pick(entries: list[dict], length_ms: int | None) -> str | None:
    """Le premier résultat qui a la bonne durée (à 10 % près), sinon le premier tout court."""
    usable = [e for e in entries if e.get("id") and not e.get("live")]
    if not usable:
        return None
    if length_ms:
        want = length_ms / 1000
        for e in usable:
            if e.get("duration") and abs(float(e["duration"]) - want) <= max(7.0, want * 0.1):
                return e["id"]
    return usable[0]["id"]


@dataclass
class Stream:
    url: str
    headers: dict
    probe: str
    expires: float
    length: int
    title: str
    author: str
    artwork: str | None


def stream_from_info(info: dict, now: float | None = None) -> Stream:
    now = time.time() if now is None else now
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise RelayError("c'est un direct (pas pris en charge)")
    url, ext, protocol = info.get("url"), str(info.get("ext") or ""), str(info.get("protocol") or "https")
    probe = PROBES.get(ext)
    if not url or not probe or protocol not in ("https", "http"):
        raise RelayError(f"format audio pas pris en charge ({ext or '?'}, {protocol})")
    expires = now + STREAM_TTL
    expire = parse_qs(urlsplit(url).query).get("expire", [""])[0]
    if expire.isdigit():
        expires = min(expires, int(expire) - 600)
    return Stream(url=url, headers=dict(info.get("http_headers") or {}), probe=probe, expires=expires,
                  length=int(float(info.get("duration") or 0) * 1000), title=str(info.get("title") or ""),
                  author=str(info.get("channel") or info.get("uploader") or ""), artwork=info.get("thumbnail"))


def _short(e: BaseException) -> str:
    text = str(e).replace("ERROR: ", "").strip()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # couleurs du terminal de yt-dlp
    return text.splitlines()[0][:200] if text else type(e).__name__


# ---------------------------------------------------------------- le relais
class YoutubeRelay:
    def __init__(self, cache_dir: Path, *, port: int = 2334, workers: int = 2, auto_update: bool = True) -> None:
        self.cache_dir = cache_dir
        self.auto_update = auto_update
        self.port = port
        self.running = False
        self._streams: OrderedDict[str, Stream] = OrderedDict()
        self._found: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._workers = asyncio.Semaphore(workers)  # yt-dlp + deno prennent de la mémoire : pas trop à la fois
        self._runner: web.AppRunner | None = None
        self._http: aiohttp.ClientSession | None = None
        self._updater: asyncio.Task | None = None
        self.version = ytdlp_version()

    def url_for(self, video_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/yt/{video_id}"

    async def start(self) -> None:
        prepare_deno()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        app = web.Application()
        app.router.add_get("/yt/{vid}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, "127.0.0.1", self.port).start()
        except OSError:
            await self._runner.cleanup()
            self._runner = None
            raise
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        self._http = aiohttp.ClientSession(timeout=timeout, auto_decompress=False)
        self.running = True
        if self.auto_update:
            self._updater = asyncio.create_task(self._keep_fresh())
        log.info("Lecture YouTube par yt-dlp %s : relais prêt sur 127.0.0.1:%d", self.version or "?", self.port)

    async def stop(self) -> None:
        self.running = False
        if self._updater is not None:
            self._updater.cancel()
            self._updater = None
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _keep_fresh(self) -> None:
        """Une fois par jour : la dernière version de yt-dlp (elle sert au prochain redémarrage du bot)."""
        stamp = self.cache_dir / "derniere-mise-a-jour"
        while True:
            try:
                if not stamp.exists() or time.time() - stamp.stat().st_mtime > UPDATE_EVERY:
                    await self.update_ytdlp()
                    stamp.touch()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  (pas de réseau, pip absent… on réessaiera)
                log.debug("Mise à jour de yt-dlp ratée : %s", e)
            await asyncio.sleep(6 * 3600)

    async def update_ytdlp(self) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--user", "--disable-pip-version-check", "--no-warn-script-location",
            "-q", "-U", "yt-dlp[default]", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 600)
        if proc.returncode:
            log.warning("Mise à jour de yt-dlp ratée : %s", err.decode(errors="replace").strip()[-300:])
            return None
        new = ytdlp_version()
        if new and new != self.version:
            log.info("yt-dlp mis à jour : %s → %s (utilisé au prochain redémarrage du bot)", self.version, new)
        return new

    # ---- recherche et extraction (avec cache) ----
    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            if len(self._locks) > CACHE_SIZE * 2:
                for k in [k for k, lock in self._locks.items() if not lock.locked()]:
                    del self._locks[k]
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def find(self, query: str, length_ms: int | None = None) -> str:
        key = f"{query.casefold()}|{(length_ms or 0) // 5000}"
        hit = self._found.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        async with self._lock("q:" + key):
            hit = self._found.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            async with self._workers:
                try:
                    entries = await asyncio.to_thread(_search, query, self.cache_dir)
                except Exception as e:  # noqa: BLE001  (yt-dlp lève toutes sortes d'erreurs)
                    log.warning("Recherche YouTube ratée pour %r : %s", query, _short(e))
                    raise RelayError(f"recherche YouTube ratée ({_short(e)})") from e
            video = pick(entries, length_ms)
            if video is None:
                raise RelayError("rien trouvé sur YouTube")
            self._found[key] = (time.time() + SEARCH_TTL, video)
            while len(self._found) > CACHE_SIZE:
                self._found.popitem(last=False)
            return video

    async def stream(self, video_id: str, *, refresh: bool = False) -> Stream:
        if not VIDEO_ID_RE.match(video_id):
            raise RelayError("vidéo YouTube invalide")
        async with self._lock("v:" + video_id):
            cached = self._streams.get(video_id)
            if cached and not refresh and cached.expires > time.time():
                self._streams.move_to_end(video_id)
                return cached
            async with self._workers:
                try:
                    info = await asyncio.to_thread(_extract, video_id, self.cache_dir)
                except Exception as e:  # noqa: BLE001
                    log.warning("yt-dlp n'a pas pu lire %s : %s", video_id, _short(e))
                    raise RelayError(_short(e)) from e
            stream = stream_from_info(info)
            self._streams[video_id] = stream
            while len(self._streams) > CACHE_SIZE:
                self._streams.popitem(last=False)
            return stream

    async def prepare(self, track: wavelink.Playable) -> wavelink.Playable | None:
        """La piste à donner à Lavalink (relayée), ou None s'il la joue lui-même. Lève RelayError."""
        meta = meta_of(track)
        if meta is None:
            return None
        video = meta.get("video")
        if not video:
            video = await self.find(str(meta.get("query") or search_query(meta.get("author"), meta.get("title"))),
                                    meta.get("length"))
            meta["video"] = video
        stream = await self.stream(video)
        meta["length"] = meta.get("length") or stream.length
        meta["title"] = meta.get("title") or stream.title
        meta["author"] = meta.get("author") or stream.author
        meta["artwork"] = meta.get("artwork") or stream.artwork
        meta["uri"] = meta.get("uri") or f"https://www.youtube.com/watch?v={video}"
        meta.setdefault("origin", "youtube")
        return make_playable(meta, url=self.url_for(video), probe=stream.probe, source="youtube", identifier=video,
                             extras=dict(track.extras))

    async def warm(self, track: wavelink.Playable) -> None:
        """Prépare la prochaine chanson pendant que la courante joue (sinon ~3 s de silence entre les deux)."""
        try:
            meta = meta_of(track)
            if meta is None:
                return
            video = meta.get("video") or await self.find(
                str(meta.get("query") or search_query(meta.get("author"), meta.get("title"))), meta.get("length"))
            await self.stream(video)
        except Exception as e:  # noqa: BLE001  (on réessaiera au moment de la jouer)
            log.debug("Préparation de %s ratée : %s", track.title, e)

    # ---- le serveur que Lavalink lit ----
    async def _handle(self, request: web.Request) -> web.StreamResponse:
        video_id = request.match_info["vid"]
        if not VIDEO_ID_RE.match(video_id) or self._http is None:
            return web.Response(status=404)
        wanted = request.headers.get("Range")
        for attempt in range(2):
            try:
                stream = await self.stream(video_id, refresh=attempt > 0)
            except RelayError as e:
                return web.Response(status=502, text=str(e))
            headers = {**stream.headers, "Range": wanted or "bytes=0-", "Accept-Encoding": "identity"}
            try:
                upstream = await self._http.get(stream.url, headers=headers)
            except (aiohttp.ClientError, TimeoutError) as e:
                log.warning("YouTube ne répond pas pour %s : %s", video_id, e)
                return web.Response(status=502)
            if upstream.status in (401, 403, 404, 410) and attempt == 0:
                upstream.release()  # adresse expirée ou refusée : on en redemande une
                continue
            if upstream.status not in (200, 206):
                log.warning("YouTube répond HTTP %d pour %s", upstream.status, video_id)
                upstream.release()
                return web.Response(status=502)
            response = web.StreamResponse(status=upstream.status if wanted else 200)
            response.headers["Content-Type"] = upstream.headers.get("Content-Type", "application/octet-stream")
            response.headers["Accept-Ranges"] = "bytes"
            if wanted and upstream.headers.get("Content-Range"):
                response.headers["Content-Range"] = upstream.headers["Content-Range"]
            if (size := upstream.headers.get("Content-Length", "")).isdigit():
                response.content_length = int(size)
            try:
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(1 << 16):
                    await response.write(chunk)
            except (ConnectionResetError, aiohttp.ClientError, TimeoutError):
                pass  # Lavalink a fermé (titre passé, avance rapide) ou YouTube a coupé : Lavalink se reconnecte
            finally:
                upstream.release()
            return response
        return web.Response(status=502)
