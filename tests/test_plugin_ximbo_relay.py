"""Ximbo 1.2 : l'audio YouTube vient de yt-dlp (comme bot LeQueb), relayé à Lavalink par un serveur local,
et les liens Spotify/Deezer sont lus sans clé d'API (spotifyscraper, API publique de Deezer)."""
import asyncio
import base64
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.ngbot import Member, ng_bot

wavelink = pytest.importorskip("wavelink")

from plugins.ximbo import links  # noqa: E402
from plugins.ximbo import relay as R  # noqa: E402
from plugins.ximbo.common import source_label  # noqa: E402

# Produites par un vrai Lavalink 4.2.2 (/v4/loadtracks sur un fichier servi en http) : l'encodeur doit les refaire
LAVALINK_WEBM = ("QAAAhQMADVVua25vd24gdGl0bGUADlVua25vd24gYXJ0aXN0AAAAAAAAF3gAHGh0dHA6Ly8xMjcuMC4wLjE6ODA3Ny9hLndlYm0AAQAc"
                 "aHR0cDovLzEyNy4wLjAuMTo4MDc3L2Eud2VibQAAAARodHRwAA1tYXRyb3NrYS93ZWJtAAAAAAAAAAA=")
LAVALINK_M4A = ("QAAAeQMADVVua25vd24gdGl0bGUADlVua25vd24gYXJ0aXN0AAAAAAAAF4cAG2h0dHA6Ly8xMjcuMC4wLjE6ODA3Ny9hLm00YQABABto"
                "dHRwOi8vMTI3LjAuMC4xOjgwNzcvYS5tNGEAAAAEaHR0cAADbXA0AAAAAAAAAAA=")


def lavasrc_track(source="spotify", ident="5ZhmgObwrdS8l5e2Ft6OzD", *, stream=False, title="Mécaniques générales",
                  author="Patrice Michaud", length=197_000):
    return wavelink.Playable({
        "encoded": "QAAA", "pluginInfo": {}, "userData": {"requester": 5},
        "info": {"identifier": ident, "isSeekable": True, "author": author, "length": length, "isStream": stream,
                 "position": 0, "title": title, "uri": f"https://open.spotify.com/track/{ident}",
                 "artworkUrl": "https://i.scdn.co/image/a", "isrc": "CAUM71500001", "sourceName": source}})


def decode_strings(encoded: str) -> list[str]:
    """Les textes d'une piste lavaplayer (titre, artiste, adresse…), pour vérifier ce qu'on donne à Lavalink."""
    data = base64.b64decode(encoded)
    header = struct.unpack(">i", data[:4])[0]
    assert header >> 30 == 1 and header & 0x3FFFFFFF == len(data) - 4 and data[4] == 3
    pos, out = 5, []

    def utf():
        nonlocal pos
        (n,) = struct.unpack(">H", data[pos:pos + 2])
        raw = data[pos + 2:pos + 2 + n]
        pos += 2 + n
        return raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass").encode("utf-16", "surrogatepass").decode("utf-16")

    out += [utf(), utf()]
    pos += 8  # durée
    out.append(utf())
    pos += 1  # direct ?
    for _ in range(3):
        present = data[pos]
        pos += 1
        out.append(utf() if present else None)
    out += [utf(), utf()]
    return out


# ---------------------------------------------------------------- pistes lavaplayer
def test_encoder_matches_lavalink():
    url = "http://127.0.0.1:8077/a.webm"
    assert R.encode_http_track(title="Unknown title", author="Unknown artist", length=6008, url=url, uri=url) == LAVALINK_WEBM
    url = "http://127.0.0.1:8077/a.m4a"
    assert R.encode_http_track(title="Unknown title", author="Unknown artist", length=6023, url=url, uri=url,
                               probe="mp4") == LAVALINK_M4A
    # UTF-8 « modifié » de Java : accents normaux, émoji en paire de substitution, NUL sur 2 octets
    assert R._java_utf("é") == b"\x00\x02\xc3\xa9"
    assert R._java_utf("🎸") == b"\x00\x06\xed\xa0\xbc\xed\xbe\xb8"
    assert R._java_utf("a\x00") == b"\x00\x03a\xc0\x80"
    enc = R.encode_http_track(title="Ça 🎸", author="Émilie", length=1, url="http://x/yt/a", isrc="CA1")
    assert decode_strings(enc) == ["Ça 🎸", "Émilie", "http://x/yt/a", None, None, "CA1", "http", "matroska/webm"]


def test_meta_of_and_playables():
    yt = lavasrc_track("youtube", "dQw4w9WgXcQ", title="Vidéo", author="Chaîne")
    assert R.meta_of(yt)["video"] == "dQw4w9WgXcQ"
    sp = R.meta_of(lavasrc_track())
    assert sp["query"] == "Patrice Michaud - Mécaniques générales audio" and sp["length"] == 197_000 and "video" not in sp
    assert R.meta_of(lavasrc_track("soundcloud")) is None          # Lavalink le joue lui-même
    assert R.meta_of(lavasrc_track("youtube", "dQw4w9WgXcQ", stream=True)) is None  # les directs aussi

    meta = links.song("spotify", title="Titre", artists="Artiste", length_ms=180_000, uri="https://open.spotify.com/track/x",
                      ident="x" * 22, artwork="https://img")
    pending = R.pending_playable(meta, port=2334, extras={"requester": 9})
    assert pending.source == "spotify" and pending.title == "Titre" and pending.length == 180_000
    assert pending.extras.requester == 9 and R.meta_of(pending)["query"] == "Artiste - Titre audio"
    assert source_label(pending) == "🟢 Spotify"


def test_pick_and_stream_info():
    entries = [{"id": "aaaaaaaaaaa", "duration": 400}, {"id": "bbbbbbbbbbb", "duration": 199},
               {"id": "ccccccccccc", "duration": 197, "live": True}]
    assert R.pick(entries, 197_000) == "bbbbbbbbbbb"      # la bonne durée, pas le premier résultat
    assert R.pick(entries, 30_000) == "aaaaaaaaaaa"       # aucune ne colle : le premier
    assert R.pick(entries, None) == "aaaaaaaaaaa" and R.pick([], 1) is None

    s = R.stream_from_info({"url": "https://rr1---sn.googlevideo.com/videoplayback?expire=2000000000&x=1", "ext": "webm",
                            "protocol": "https", "duration": 197.4, "http_headers": {"User-Agent": "UA"},
                            "title": "T", "channel": "C", "thumbnail": "https://i.ytimg.com/x.jpg"}, now=1_999_990_000)
    assert s.probe == "matroska/webm" and s.length == 197_400 and s.headers == {"User-Agent": "UA"}
    assert s.expires == 2_000_000_000 - 600
    assert R.stream_from_info({"url": "https://g/v", "ext": "m4a"}).probe == "mp4"
    with pytest.raises(R.RelayError, match="direct"):
        R.stream_from_info({"url": "https://g/v", "ext": "webm", "is_live": True})
    with pytest.raises(R.RelayError, match="format"):
        R.stream_from_info({"url": "https://g/m.m3u8", "ext": "mp4", "protocol": "m3u8_native"})


def test_ytdlp_format_prefers_direct_opus():
    yt_dlp = pytest.importorskip("yt_dlp")
    formats = [
        {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "vcodec": "none", "protocol": "https", "abr": 129, "url": "u140"},
        {"format_id": "251", "ext": "webm", "acodec": "opus", "vcodec": "none", "protocol": "https", "abr": 135, "url": "u251"},
        {"format_id": "233", "ext": "mp4", "acodec": "mp4a.40.2", "vcodec": "none", "protocol": "m3u8_native", "abr": 192, "url": "u233"},
    ]
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        chosen = list(ydl.build_format_selector(R.FORMAT)({"formats": formats, "has_merged_format": False}))
    assert chosen[0]["format_id"] == "251"
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        chosen = list(ydl.build_format_selector(R.FORMAT)({"formats": [formats[0], formats[2]], "has_merged_format": False}))
    assert chosen[0]["format_id"] == "140"  # pas de webm : le m4a direct, jamais le HLS


# ---------------------------------------------------------------- le relais
async def test_prepare_finds_video_and_keeps_metadata(tmp_path):
    rel = R.YoutubeRelay(tmp_path, port=2334, auto_update=False)
    searches = []

    async def fake_search(query, cache_dir, count=5):
        searches.append(query)
        return [{"id": "aaaaaaaaaaa", "duration": 30}, {"id": "bbbbbbbbbbb", "duration": 198}]
    extract = {"url": "https://g/v?expire=9999999999", "ext": "webm", "protocol": "https", "duration": 198,
               "title": "Vidéo YouTube", "channel": "Chaîne", "thumbnail": "https://i.ytimg.com/b.jpg"}

    def run(func, *args):
        return fake_search(*args) if func is R._search else asyncio.sleep(0, extract)
    orig = asyncio.to_thread
    asyncio.to_thread = run
    try:
        ready = await rel.prepare(lavasrc_track())
        again = await rel.prepare(lavasrc_track())   # recherche et adresse en cache
    finally:
        asyncio.to_thread = orig
    assert searches == ["Patrice Michaud - Mécaniques générales audio"]
    assert ready.source == "youtube" and ready.identifier == "bbbbbbbbbbb"
    assert ready.title == "Mécaniques générales" and ready.author == "Patrice Michaud" and ready.length == 197_000
    assert ready.uri == "https://open.spotify.com/track/5ZhmgObwrdS8l5e2Ft6OzD" and ready.extras.requester == 5
    assert decode_strings(ready.encoded)[2] == "http://127.0.0.1:2334/yt/bbbbbbbbbbb"
    assert source_label(ready) == "🟢 Spotify · audio YouTube"
    assert R.meta_of(ready)["video"] == "bbbbbbbbbbb" and again.identifier == "bbbbbbbbbbb"
    assert await rel.prepare(lavasrc_track("soundcloud")) is None


async def test_ytdlp_updates_itself_once_a_day(tmp_path, monkeypatch):
    rel = R.YoutubeRelay(tmp_path, port=0)
    rel.version = "2026.01.01"
    commands = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(R, "ytdlp_version", lambda: "2026.11.20")
    assert await rel.update_ytdlp() == "2026.11.20"
    assert commands[0][1:3] == ("-m", "pip") and "-U" in commands[0] and commands[0][-1] == "yt-dlp[default]"


async def test_relay_serves_ranges_and_renews_expired_urls(tmp_path):
    audio = bytes(range(256)) * 400
    hits = []

    async def googlevideo(request):
        hits.append((request.match_info["name"], request.headers.get("Range"), request.headers.get("User-Agent")))
        if request.match_info["name"] == "expiree":
            return web.Response(status=403)
        start = int(request.headers["Range"].split("=")[1].rstrip("-"))
        return web.Response(status=206, body=audio[start:], content_type="audio/webm",
                            headers={"Content-Range": f"bytes {start}-{len(audio) - 1}/{len(audio)}"})
    app = web.Application()
    app.router.add_get("/{name}", googlevideo)
    async with TestServer(app) as upstream:
        rel = R.YoutubeRelay(tmp_path, port=0, auto_update=False)
        calls = []

        async def stream(video_id, *, refresh=False):
            calls.append(refresh)
            name = "expiree" if not refresh and len(calls) == 1 else "bonne"
            return R.stream_from_info({"url": str(upstream.make_url(f"/{name}")), "ext": "webm",
                                       "http_headers": {"User-Agent": "yt-dlp-UA"}})
        rel.stream = stream
        rel.port = upstream.port + 1
        for _ in range(20):  # un port libre près de celui du faux YouTube
            try:
                await rel.start()
                break
            except OSError:
                rel.port += 1
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(rel.url_for("dQw4w9WgXcQ")) as r:   # ce que Lavalink demande en premier
                    body = await r.read()
                    assert r.status == 200 and body == audio and r.headers["Accept-Ranges"] == "bytes"
                assert calls == [False, True]  # adresse refusée (403) : yt-dlp en redonne une
                async with http.get(rel.url_for("dQw4w9WgXcQ"), headers={"Range": "bytes=1000-"}) as r:  # avance rapide
                    assert r.status == 206 and await r.read() == audio[1000:]
                    assert r.headers["Content-Range"] == f"bytes 1000-{len(audio) - 1}/{len(audio)}"
                async with http.get(f"http://127.0.0.1:{rel.port}/yt/pas-valide") as r:
                    assert r.status == 404
            assert hits[0] == ("expiree", "bytes=0-", "yt-dlp-UA") and hits[-1][1] == "bytes=1000-"
        finally:
            await rel.stop()


async def test_player_skips_unplayable_songs(tmp_path, monkeypatch):
    from plugins.ximbo.cog import RelayError, XimboPlayer  # le même module que le cog (les tests le rechargent)

    played = []

    async def base_play(self, track, **kwargs):
        played.append(track.title)
        return track
    monkeypatch.setattr(wavelink.Player, "play", base_play)
    good, bad = lavasrc_track(title="Bonne"), lavasrc_track(title="Introuvable")
    relay = SimpleNamespace(running=True)

    async def prepare(track):
        if track.title == "Introuvable":
            raise RelayError("rien trouvé sur YouTube")
        return track
    relay.prepare = prepare
    cog = SimpleNamespace(relay=relay, announce=AsyncMock(), refresh=AsyncMock())
    player = object.__new__(XimboPlayer)
    player.client = SimpleNamespace(get_cog=lambda name: cog)
    player.queue = wavelink.Queue()
    player._guild, player.room_id = None, 77
    player.queue.put(good)
    assert (await player.play(bad)).title == "Bonne" and played == ["Bonne"]
    assert "Introuvable" in cog.announce.call_args.args[1]
    assert await player.play(bad) is None and played == ["Bonne"]  # file vide : on s'arrête là
    relay.running = False
    await player.play(bad)
    assert played == ["Bonne", "Introuvable"]  # sans relais : Lavalink comme avant


# ---------------------------------------------------------------- liens Spotify / Deezer
def test_link_detection():
    assert links.is_link("https://open.spotify.com/playlist/6ilsrg2otbHiOGgHyI1chD?si=c55ac0e3fc7b4ed1")
    assert links.is_link("https://open.spotify.com/intl-fr/album/6ilsrg2otbHiOGgHyI1chD")
    assert links.is_link("spotify:track:6ilsrg2otbHiOGgHyI1chD") and links.is_link("https://spotify.link/AbCdEf")
    assert links.is_link("https://www.deezer.com/fr/playlist/908622995") and links.is_link("https://link.deezer.com/s/30x")
    assert not links.is_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ") and not links.is_link("les cowboys fringants")


async def test_spotify_playlist_without_api_key(monkeypatch):
    import spotify_scraper

    img = SimpleNamespace(url="https://i.scdn.co/image/pl", width=640, height=640)

    def t(name, artists, ms, tid):
        return SimpleNamespace(name=name, artists=tuple(SimpleNamespace(name=a) for a in artists), duration_ms=ms,
                               id=tid, share_url=None, images=(), album=None)
    asked = []

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_playlist(self, url, max_tracks=100):
            asked.append((url, max_tracks))
            return SimpleNamespace(name="All queb", images=(img,), tracks=(
                SimpleNamespace(track=t("J'ai besoin d'un break", ["Jay Scott"], 189_000, "a" * 22)),
                SimpleNamespace(track=None),
                SimpleNamespace(track=t("Un peu trop vite", ["Jay Scott", "Lil Tom"], 193_000, "b" * 22))))
    monkeypatch.setattr(spotify_scraper, "AsyncSpotifyClient", Client)
    result = await links.resolve("https://open.spotify.com/playlist/6ilsrg2otbHiOGgHyI1chD?si=x", limit=500)
    assert asked == [("https://open.spotify.com/playlist/6ilsrg2otbHiOGgHyI1chD", 500)]
    assert (result.platform, result.kind, result.name) == ("spotify", "playlist", "All queb")
    first, second = result.tracks
    assert first["query"] == "Jay Scott - J'ai besoin d'un break audio" and first["length"] == 189_000
    assert first["uri"] == "https://open.spotify.com/track/" + "a" * 22 and first["artwork"] == "https://i.scdn.co/image/pl"
    assert second["author"] == "Jay Scott, Lil Tom"

    class Broken(Client):
        async def get_playlist(self, url, max_tracks=100):
            raise RuntimeError("404")
    monkeypatch.setattr(spotify_scraper, "AsyncSpotifyClient", Broken)
    with pytest.raises(links.LinkError, match="publique"):
        await links.resolve("spotify:playlist:6ilsrg2otbHiOGgHyI1chD")
    assert await links.resolve("pas un lien") is None


async def test_deezer_album(monkeypatch):
    pages = {
        "https://api.deezer.com/album/302127": {
            "title": "Discovery", "artist": {"name": "Daft Punk"}, "cover_xl": "https://cdn/cover.jpg",
            "tracks": {"data": [{"id": 1, "title": "One More Time", "duration": 320, "artist": {"name": "Daft Punk"},
                                 "link": "https://www.deezer.com/track/1", "album": {}}],
                       "next": "https://api.deezer.com/album/302127/tracks?index=1"}},
        "https://api.deezer.com/album/302127/tracks?index=1": {
            "data": [{"id": 2, "title": "Aerodynamic", "duration": 207, "artist": {"name": "Daft Punk"}, "album": {}}]},
    }

    async def fake_json(http, url):
        return pages[url]
    monkeypatch.setattr(links, "_deezer_json", fake_json)
    result = await links.resolve("https://www.deezer.com/fr/album/302127")
    assert result.name == "Discovery — Daft Punk" and [s["title"] for s in result.tracks] == ["One More Time", "Aerodynamic"]
    assert result.tracks[0]["length"] == 320_000 and result.tracks[1]["artwork"] == "https://cdn/cover.jpg"


# ---------------------------------------------------------------- dans le cog
async def test_spotify_link_queues_songs_found_at_play_time(tmp_path, monkeypatch):
    import sys

    from tests.test_plugin_ximbo_on import Channel, FakePlayer, make_guild

    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        cogmod = sys.modules[type(cog).__module__]  # les modules que ce cog utilise vraiment
        voice, room_channel = Channel(500), Channel(77)
        guild = make_guild({500: voice, 77: room_channel})
        owner = Member(5, guild)
        owner.voice = SimpleNamespace(channel=voice)
        await cog.db.run("INSERT INTO rooms (channel_id, guild_id, owner_id, message_id, volume) VALUES (77, 1, 5, 88, 60)")
        cog.rooms[77] = 1
        room = await cog.room(77)
        player = FakePlayer(voice, 77)
        cog.node_status = "prêt"
        cog.ensure_player = AsyncMock(return_value=player)
        cog.refresh, cog.refresh_card = AsyncMock(), AsyncMock()
        cog.relay = SimpleNamespace(running=True)
        songs = [links.song("spotify", title=f"Chanson {n}", artists="Jay Scott", length_ms=180_000, uri=None,
                            ident=str(n) * 22) for n in range(3)]
        monkeypatch.setattr(cogmod.links, "resolve",
                            AsyncMock(return_value=cogmod.links.LinkResult("spotify", "playlist", "All queb", songs)))
        msg = await cog.enqueue(owner, room, "https://open.spotify.com/playlist/6ilsrg2otbHiOGgHyI1chD")
        assert msg.startswith("🟢 Spotify · 📃 **All queb** ajoutée : **3** titres")
        assert player.current.title == "Chanson 0" and player.current.extras.requester == 5
        assert R.meta_of(player.current)["query"] == "Jay Scott - Chanson 0 audio"
        assert [t.title for t in player.queue] == ["Chanson 1", "Chanson 2"]
