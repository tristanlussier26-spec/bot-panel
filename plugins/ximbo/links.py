"""Liens Spotify et Deezer : la liste des chansons, comme bot LeQueb, sans clé d'API.

Spotify et Deezer ne diffusent pas leur audio : on lit le titre et l'artiste de chaque chanson, puis Ximbo la cherche
sur YouTube au moment de la jouer (voir relay.py). Depuis février 2026, l'API officielle de Spotify ne donne plus le
contenu des playlists aux nouvelles apps : spotifyscraper lit plutôt les pages publiques de Spotify (celles qu'on
intègre dans un site), comme le fait bot LeQueb. Deezer a une API publique, sans clé.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import aiohttp

from .relay import search_query

log = logging.getLogger("plugin.ximbo.links")

SPOTIFY_RE = re.compile(r"(?:open|play)\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(track|album|playlist|artist)/([A-Za-z0-9]{22})")
SPOTIFY_URI_RE = re.compile(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})")
SPOTIFY_SHORT_RE = re.compile(r"https?://spotify\.link/\S+")
DEEZER_RE = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist|artist)/(\d+)")
DEEZER_SHORT_RE = re.compile(r"https?://(?:deezer\.page\.link|link\.deezer\.com|dzr\.page\.link)/\S+")
LABEL = {"spotify": "🟢 Spotify", "deezer": "🟣 Deezer"}


class LinkError(Exception):
    """Un lien Spotify/Deezer illisible (message pour les membres)."""


@dataclass
class LinkResult:
    platform: str
    kind: str                      # track, album, playlist, artist
    name: str
    tracks: list[dict] = field(default_factory=list)  # métadonnées : title, author, length, uri, artwork, id, query


def is_link(text: str) -> bool:
    text = text.strip()
    return bool(SPOTIFY_RE.search(text) or SPOTIFY_URI_RE.search(text) or SPOTIFY_SHORT_RE.match(text)
                or DEEZER_RE.search(text) or DEEZER_SHORT_RE.match(text))


def spotifyscraper_available() -> bool:
    try:
        import spotify_scraper  # noqa: F401
    except ImportError:
        return False
    return True


def _image(*groups) -> str | None:
    for images in groups:
        for image in images or ():
            if getattr(image, "url", None):
                return image.url
    return None


def song(platform: str, *, title: str, artists: str, length_ms: int | None, uri: str | None, ident: str,
         artwork: str | None = None, isrc: str | None = None) -> dict:
    return {"title": title or "Sans titre", "author": artists, "length": int(length_ms or 0), "uri": uri,
            "artwork": artwork, "isrc": isrc, "id": ident, "origin": platform, "query": search_query(artists, title)}


# ---------------------------------------------------------------- Spotify
def _spotify_song(t, fallback_artists: str = "", fallback_art: str | None = None) -> dict | None:
    if t is None or not getattr(t, "name", None):
        return None
    artists = ", ".join(a.name for a in (t.artists or ()) if getattr(a, "name", None)) or fallback_artists
    album = getattr(t, "album", None)
    return song("spotify", title=t.name, artists=artists, length_ms=t.duration_ms,
                uri=t.share_url or f"https://open.spotify.com/track/{t.id}", ident=t.id,
                artwork=_image(getattr(t, "images", ()), getattr(album, "images", ())) or fallback_art)


async def spotify(kind: str, ident: str, *, limit: int) -> LinkResult:
    try:
        from spotify_scraper import AsyncSpotifyClient
    except ImportError as e:
        raise LinkError("la librairie spotifyscraper est pas installée (redémarre le bot)") from e
    url = f"https://open.spotify.com/{kind}/{ident}"
    try:
        async with AsyncSpotifyClient(timeout=15) as client:
            if kind == "track":
                t = await client.get_track(url)
                return LinkResult("spotify", kind, t.name, [s for s in [_spotify_song(t)] if s])
            if kind == "album":
                a = await client.get_album(url)
                artists = ", ".join(x.name for x in (a.artists or ()))
                art = _image(a.images)
                songs = [s for s in (_spotify_song(t, artists, art) for t in a.tracks) if s]
                return LinkResult("spotify", kind, f"{a.name} — {artists}" if artists else a.name, songs[:limit])
            if kind == "playlist":
                p = await client.get_playlist(url, max_tracks=limit)
                art = _image(p.images)
                songs = [s for s in (_spotify_song(pt.track, fallback_art=art) for pt in p.tracks) if s]
                return LinkResult("spotify", kind, p.name, songs[:limit])
            a = await client.get_artist(url)
            songs = [s for s in (_spotify_song(t, a.name, _image(a.images)) for t in a.top_tracks) if s]
            return LinkResult("spotify", kind, f"Top de {a.name}", songs[:limit])
    except LinkError:
        raise
    except Exception as e:  # noqa: BLE001  (réseau, page changée, lien privé…)
        log.warning("Lien Spotify %s/%s illisible : %r", kind, ident, e)
        raise LinkError("Spotify refuse ce lien ou il est privé : vérifie que la playlist est **publique**.") from e


# ---------------------------------------------------------------- Deezer
async def _deezer_json(http: aiohttp.ClientSession, url: str) -> dict:
    async with http.get(url) as r:
        data = await r.json(content_type=None)
    if not isinstance(data, dict):
        raise LinkError("Deezer a répondu n'importe quoi")
    if data.get("error"):
        raise LinkError(f"Deezer : {data['error'].get('message', 'lien introuvable')}")
    return data


def _deezer_song(t: dict, art: str | None = None) -> dict:
    album = t.get("album") or {}
    return song("deezer", title=t.get("title") or "", artists=(t.get("artist") or {}).get("name", ""),
                length_ms=int(t.get("duration") or 0) * 1000, uri=t.get("link"), ident=str(t.get("id") or ""),
                artwork=album.get("cover_xl") or album.get("cover_big") or art, isrc=t.get("isrc"))


async def deezer(kind: str, ident: str, *, limit: int) -> LinkResult:
    base = "https://api.deezer.com"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        if kind == "track":
            t = await _deezer_json(http, f"{base}/track/{ident}")
            return LinkResult("deezer", kind, t.get("title", "?"), [_deezer_song(t)])
        if kind == "artist":
            a = await _deezer_json(http, f"{base}/artist/{ident}")
            top = await _deezer_json(http, f"{base}/artist/{ident}/top?limit={min(limit, 50)}")
            return LinkResult("deezer", kind, f"Top de {a.get('name', '?')}",
                              [_deezer_song(t, a.get("picture_xl")) for t in top.get("data", [])])
        info = await _deezer_json(http, f"{base}/{kind}/{ident}")
        art = info.get("cover_xl") or info.get("picture_xl")
        songs = [_deezer_song(t, art) for t in (info.get("tracks") or {}).get("data", [])]
        following = (info.get("tracks") or {}).get("next")
        while following and len(songs) < limit:
            page = await _deezer_json(http, following)
            songs += [_deezer_song(t, art) for t in page.get("data", [])]
            following = page.get("next")
        name = info.get("title", "?")
        if kind == "album" and (artist := (info.get("artist") or {}).get("name")):
            name = f"{name} — {artist}"
        return LinkResult("deezer", kind, name, songs[:limit])


# ---------------------------------------------------------------- point d'entrée
async def _unshorten(url: str) -> str:
    """spotify.link/…, link.deezer.com/… : l'adresse complète derrière le lien court."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.get(url, allow_redirects=True) as r:
                final = str(r.url)
                if SPOTIFY_RE.search(final) or DEEZER_RE.search(final):
                    return final
                page = await r.text(errors="replace")
    except (aiohttp.ClientError, TimeoutError) as e:
        raise LinkError("ce lien court ne répond pas") from e
    found = SPOTIFY_RE.search(page) or DEEZER_RE.search(page)  # certains liens courts redirigent en JavaScript
    if not found:
        raise LinkError("ce lien court mène nulle part")
    return "https://" + found.group(0) if found.re is SPOTIFY_RE else "https://www." + found.group(0)


async def resolve(text: str, *, limit: int = 500) -> LinkResult | None:
    """Le contenu d'un lien Spotify/Deezer, ou None si ce n'en est pas un. Lève LinkError."""
    text = text.strip()
    if SPOTIFY_SHORT_RE.match(text) or DEEZER_SHORT_RE.match(text):
        text = await _unshorten(text.split()[0])
    if m := (SPOTIFY_RE.search(text) or SPOTIFY_URI_RE.search(text)):
        result = await spotify(m.group(1), m.group(2), limit=limit)
    elif m := DEEZER_RE.search(text):
        result = await deezer(m.group(1), m.group(2), limit=limit)
    else:
        return None
    if not result.tracks:
        raise LinkError(f"Aucune chanson dans ce lien {LABEL[result.platform].split(' ', 1)[1]}.")
    return result
