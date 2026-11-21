"""Plugin « ximbo » (musique Spotify, salons de contrôle) et modèle « Ximbo » : chargement, Lavalink embarqué, recherche."""
import json
import re
from types import SimpleNamespace

import discord
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from tests.ngbot import Member, ng_bot

pytest.importorskip("wavelink")


# ---------------------------------------------------------------- petites fonctions
def test_helpers():
    from plugins.ximbo.common import channel_name, fmt_ms, progress_bar, search_order

    assert fmt_ms(None) == "--:--" and fmt_ms(65_000) == "1:05" and fmt_ms(3_725_000) == "1:02:05"
    bar = progress_bar(30_000, 180_000, size=12)
    assert bar.count("🔘") == 1 and len(bar) == 12
    assert progress_bar(999_999, 1000).endswith("🔘") and progress_bar(0, 0) == "▬" * 16
    # un lien : joué tel quel ; un titre : la source choisie, puis YouTube Music, YouTube, SoundCloud
    assert search_order("https://open.spotify.com/track/abc", "spotify", True) == [None]
    assert search_order("spotify:track:abc", "spotify", True) == [None]
    assert search_order("les cowboys", "spotify", True) == ["spsearch", "ytmsearch", "ytsearch", "scsearch"]
    assert search_order("les cowboys", "spotify", False)[0] == "ytmsearch"  # pas de clés Spotify
    assert search_order("les cowboys", "soundcloud", True) == ["scsearch", "ytmsearch", "ytsearch"]
    assert channel_name("Lequeb 🇨🇦", 5) == "🎵・lequeb" and channel_name("🎸", 42) == "🎵・42"


def test_lavalink_helpers():
    from plugins.ximbo.lavalink import COMPONENTS, LavalinkError, heap_mb, java_arch, pick_release, render_config

    assert java_arch("x86_64") == "x64" and java_arch("aarch64") == "aarch64"
    with pytest.raises(LavalinkError):
        java_arch("mips")
    releases = [
        {"tag_name": "5.0.0", "assets": [{"name": "Lavalink.jar", "browser_download_url": "u5"}]},          # trop récent
        {"tag_name": "4.2.0-rc1", "prerelease": True, "assets": [{"name": "Lavalink.jar", "browser_download_url": "rc"}]},
        {"tag_name": "4.1.1", "assets": [{"name": "Lavalink-musl.jar", "browser_download_url": "musl"},
                                         {"name": "Lavalink.jar", "browser_download_url": "u411"}]},
    ]
    assert pick_release(releases, COMPONENTS["lavalink"][1], "4.") == ("4.1.1", "u411")
    yt = [{"tag_name": "1.13.3", "assets": [{"name": "youtube-common-1.13.3.jar", "browser_download_url": "c"},
                                            {"name": "youtube-plugin-1.13.3.jar", "browser_download_url": "p"}]}]
    assert pick_release(yt, COMPONENTS["youtube"][1]) == ("1.13.3", "p")
    assert pick_release([], COMPONENTS["lavasrc"][1], "4.") is None
    assert heap_mb(1024) == 409 and heap_mb(256) == 256 and heap_mb(4096) == 768 and heap_mb(None) == 512
    assert heap_mb(1024, "300") == 300 and heap_mb(1024, "abc") == 409

    without = render_config(2333, 'mot"de passe')
    assert "address: 127.0.0.1" in without and 'password: "mot\\"de passe"' in without
    assert "spotify: false" in without and "clientId" not in without
    with_keys = render_config(2444, "pw", "id123", "secret456")
    assert "port: 2444" in with_keys and "spotify: true" in with_keys
    assert 'clientId: "id123"' in with_keys and 'clientSecret: "secret456"' in with_keys
    assert '- "ytmsearch:\\"%ISRC%\\""' in with_keys and "youtube: false" in with_keys
    # Apps Spotify en « Development mode » (février 2026) : playlists par l'API du lecteur web, pas de GET /artists
    yaml = pytest.importorskip("yaml")
    spotify = yaml.safe_load(with_keys)["plugins"]["lavasrc"]["spotify"]
    assert spotify["preferPartnerApi"] is False and spotify["resolveArtistsInSearch"] is False
    assert spotify["clientId"] == "id123"
    assert "preferPartnerApi" not in without


async def test_lavalink_install_and_update(tmp_path, monkeypatch):
    """Téléchargement des jars depuis un faux GitHub, puis mise à jour quand une nouvelle version sort."""
    from plugins.ximbo import lavalink

    versions = {"lavalink-devs/Lavalink": "4.1.1", "lavalink-devs/youtube-source": "1.13.3", "topi314/LavaSrc": "4.7.0"}
    names = {"lavalink-devs/Lavalink": "Lavalink.jar", "lavalink-devs/youtube-source": "youtube-plugin-{v}.jar",
             "topi314/LavaSrc": "lavasrc-plugin-{v}.jar"}
    hits = []

    async def releases(request):
        repo = f"{request.match_info['owner']}/{request.match_info['repo']}"
        hits.append(repo)
        v = versions[repo]
        name = names[repo].format(v=v)
        return web.json_response([{"tag_name": v, "assets": [
            {"name": "autre.jar", "browser_download_url": f"{base}/dl/autre"},
            {"name": name, "browser_download_url": f"{base}/dl/{v}/{name}"}]}])

    async def download(request):
        return web.Response(body=(request.match_info["name"] * 200).encode())

    app = web.Application()
    app.router.add_get("/repos/{owner}/{repo}/releases", releases)
    app.router.add_get("/dl/{v}/{name}", download)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(lavalink, "GITHUB_RELEASES", base + "/repos/{repo}/releases")
    try:
        manager = lavalink.LavalinkManager(tmp_path / "ll", port=2555, spotify=("a", "b"))
        assert manager.uri == "http://127.0.0.1:2555" and len(manager.password) > 20
        async with ClientSession() as http:
            await manager._install_components(http)
            folder = tmp_path / "ll"
            assert (folder / "Lavalink.jar").read_bytes().startswith(b"Lavalink.jar")
            assert (folder / "plugins" / "youtube-plugin-1.13.3.jar").is_file()
            assert (folder / "plugins" / "lavasrc-plugin-4.7.0.jar").is_file()
            assert manager.state["versions"] == {"lavalink": "4.1.1", "youtube": "1.13.3", "lavasrc": "4.7.0"}

            # Vérifié il y a moins de 12 h : GitHub est pas redemandé
            hits.clear()
            await manager._install_components(http)
            assert hits == []

            # 12 h plus tard, YouTube a une nouvelle version : le vieux jar est remplacé, le reste est gardé
            manager.state["checked"] = 0
            versions["lavalink-devs/youtube-source"] = "1.14.0"
            await manager._install_components(http)
            plugins = sorted(p.name for p in (folder / "plugins").iterdir())
            assert plugins == ["lavasrc-plugin-4.7.0.jar", "youtube-plugin-1.14.0.jar"]
            assert json.loads((folder / "state.json").read_text())["versions"]["youtube"] == "1.14.0"

            # GitHub en panne : on garde ce qu'on a
            manager.state["checked"] = 0
            monkeypatch.setattr(lavalink, "GITHUB_RELEASES", base + "/rien/{repo}")
            await manager._install_components(http)
            assert manager.state["versions"]["youtube"] == "1.14.0"

        # Le mot de passe est gardé d'un démarrage à l'autre
        again = lavalink.LavalinkManager(tmp_path / "ll")
        assert again.password == manager.password
    finally:
        await server.close()


# ---------------------------------------------------------------- dans un vrai Bot NG
async def test_ximbo_plugin_loads_in_bot_ng(tmp_path):
    from botng.config import PAGES

    async with ng_bot(tmp_path, "ximbo") as bot:
        plugin = bot.plugin_host.plugins[0]
        assert plugin.id == "ximbo" and plugin.loaded and not plugin.error, plugin.error
        assert bot.intents.message_content and not bot.intents.members
        assert [p.section for p in PAGES] == ["ximbo"]
        cfg = bot.cfg(1, "ximbo")
        assert cfg["active"] is False and cfg["recherche"] == "spotify" and cfg["max_salons"] == 25
        names = {c.qualified_name for c in bot.tree.walk_commands()}
        assert {"ximbo-panneau", "ximbo-fermer", "ximbo-statut"} <= names
        assert bot.optional_module("plugins.ximbo.cog") == ("ximbo", "active")

        cog = bot.get_cog("Ximbo")
        info = dict(cog.panel_info(1))
        assert info["Salons musique"] == "0" and info["Recherche Spotify"].startswith("pas configurée")
        assert "Lecture YouTube" in info
        assert "⏳" in info["Lavalink"]
        # Pas encore prêt : message clair au lieu d'une erreur
        from plugins.ximbo.cog import UserError
        with pytest.raises(UserError, match="démarre encore"):
            cog.ensure_ready()
        # Lien Spotify sans les clés : refusé avant même d'appeler Lavalink
        with pytest.raises(UserError, match="XIMBO_SPOTIFY_ID"):
            await cog.search("https://open.spotify.com/track/abc", 1)

        # Un salon musique gardé dans la base du plugin
        await cog.db.run("INSERT INTO rooms (channel_id, guild_id, owner_id, message_id, volume) VALUES (10, 1, 5, 11, 60)")
        cog.rooms[10] = 1
        assert dict(cog.panel_info(1))["Salons musique"] == "1"
        room = await cog.room_of_owner(1, 5)
        assert room["channel_id"] == 10
        from plugins.ximbo.cog import player_embed
        embed = player_embed(room, None)
        assert embed.title == "🎵 Rien en lecture" and "<@5>" in embed.fields[0].value
        member = SimpleNamespace(id=5, guild_permissions=SimpleNamespace(manage_channels=False))
        assert cog.can_manage(member, room)
        assert not cog.can_manage(SimpleNamespace(id=6, guild_permissions=SimpleNamespace(manage_channels=False)), room)
        await cog.forget_room(10)
        assert await cog.room(10) is None and cog.rooms == {}


# ---------------------------------------------------------------- modèle
def test_ximbo_kit_extends_bot_ng():
    from botpanel import kits
    from botpanel.config import Settings

    found = {k["id"]: k for k in kits.list_kits(Settings().kits_dir)}
    kit = found["ximbo"]
    assert (kit["base"], kit["ram_mb"], kit["plugins"], kit["main_file"]) == ("bot-ng", 1024, ["ximbo"], "bot.py")
    assert found["bot-ng"]["base"] is None and found["bot-ng"]["ram_mb"] is None
    files = kits.kit_files(Settings().kits_dir, "ximbo")
    assert {"botng/bot.py", "botng/plugins.py", "requirements.txt", "bot.py", "README.md"} <= set(files)
    assert b"XIMBO_MODELE" in files["bot.py"] and files["README.md"].startswith(b"# Ximbo")  # par-dessus Bot NG
    assert not any(name.startswith(("data/", "plugins/")) or name == "template.json" for name in files)


def test_kit_base_rules(tmp_path):
    from botpanel import kits

    def make(kid, **extra):
        (tmp_path / kid).mkdir()
        (tmp_path / kid / "main.py").write_text(f"# {kid}\n")
        (tmp_path / kid / "template.json").write_text(json.dumps(
            {"format": 1, "id": kid, "name": kid, "version": "1.0.0", "language": "python", "main_file": "main.py", **extra}))

    make("base")
    (tmp_path / "base" / "lib.py").write_text("x = 1\n")
    make("enfant", base="base", ram_mb=999999)
    make("petit-enfant", base="enfant")
    make("lui-meme", base="lui-meme")
    found = {k["id"]: k for k in kits.list_kits(tmp_path)}
    assert "lui-meme" not in found and found["enfant"]["ram_mb"] is None  # RAM hors limites : ignorée
    files = kits.kit_files(tmp_path, "enfant")
    assert files == {"main.py": b"# enfant\n", "lib.py": b"x = 1\n"}
    with pytest.raises(kits.KitError):
        kits.kit_files(tmp_path, "petit-enfant")
    make("orphelin", base="absent")
    with pytest.raises(kits.KitError):
        kits.kit_files(tmp_path, "orphelin")
    assert re.match(r"^\d", found["base"]["version"])


async def test_create_room_and_messages(tmp_path):
    """Bouton « Créer mon bot musique » : salon privé, lecteur, un seul salon par membre ; messages du salon."""
    from unittest.mock import AsyncMock

    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        channels = {}
        sent = AsyncMock(return_value=SimpleNamespace(id=88))
        room_channel = SimpleNamespace(id=77, mention="<#77>", send=sent)
        class Me(str):  # guild.me : sert de clé dans les permissions du salon
            guild_permissions = discord.Permissions.all()

        guild = SimpleNamespace(
            id=1, name="Serveur", default_role="everyone", me=Me("moi"), voice_client=None, get_channel=lambda cid: channels.get(cid),
            create_category=AsyncMock(return_value=SimpleNamespace(id=66)),
            create_text_channel=AsyncMock(side_effect=lambda *a, **k: channels.setdefault(77, room_channel)))
        member = Member(5, guild)
        member.display_name = "Lequeb"

        def interaction(user=member):
            followup = AsyncMock()
            response = SimpleNamespace(defer=AsyncMock(), is_done=lambda: True, send_message=AsyncMock())
            return SimpleNamespace(guild=guild, guild_id=1, user=user, response=response,
                                   followup=SimpleNamespace(send=followup))

        # Pas activé sur ce serveur
        i = interaction()
        i.response.is_done = lambda: False
        await cog.create_room(i)
        assert "pas activé" in i.response.send_message.call_args.kwargs["content"]

        assert await bot.save_section(1, "ximbo", {"active": True, "role_acces": 9}) == []
        i = interaction()
        i.response.is_done = lambda: False
        await cog.create_room(i)
        assert "<@&9>" in i.response.send_message.call_args.kwargs["content"]  # il faut le rôle

        assert await bot.save_section(1, "ximbo", {"role_acces": 0}) == []
        # Mon rôle a pas tout ce qu'il faut : je dis lesquelles, sans rien créer
        Me.guild_permissions = discord.Permissions(view_channel=True, send_messages=True, manage_channels=True)
        i = interaction()
        await cog.create_room(i)
        said = i.followup.send.call_args.kwargs["content"]
        assert "**Gérer les rôles**" in said and "**Intégrer des liens**" in said and "Gérer les salons" not in said
        assert not guild.create_category.called
        # Discord refuse quand même (ex. la catégorie l'interdit)
        Me.guild_permissions = discord.Permissions.all()
        guild.create_category.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        i = interaction()
        await cog.create_room(i)
        assert "Discord refuse" in i.followup.send.call_args.kwargs["content"]
        guild.create_category.side_effect = None

        i = interaction()
        await cog.create_room(i)
        assert "<#77>" in i.followup.send.call_args.kwargs["content"]
        kwargs = guild.create_text_channel.call_args.kwargs
        assert guild.create_text_channel.call_args.args[0] == "🎵・lequeb" and kwargs["category"].id == 66
        assert not kwargs["overwrites"]["everyone"].view_channel and kwargs["overwrites"][member].view_channel
        assert bot.cfg(1, "ximbo")["categorie"] == 66  # la catégorie créée est gardée dans les réglages
        assert sent.await_count == 2  # bienvenue + lecteur
        room = await cog.room(77)
        assert (room["owner_id"], room["message_id"], room["volume"]) == (5, 88, 60) and cog.rooms == {77: 1}

        i = interaction()
        await cog.create_room(i)
        assert "déjà ton salon" in i.followup.send.call_args.kwargs["content"]

        # Un message dans le salon : effacé, et réponse claire tant que Lavalink est pas prêt
        message = SimpleNamespace(author=SimpleNamespace(bot=False, id=5, mention="<@5>", guild=guild), guild=guild,
                                  channel=SimpleNamespace(id=77, send=AsyncMock(), typing=lambda: _NoCtx()),
                                  content="les cowboys fringants", delete=AsyncMock(), attachments=[], stickers=[])
        await cog.on_message(message)
        message.delete.assert_awaited()
        assert "démarre encore" in message.channel.send.call_args.args[0]
        # Contenu vide (Message Content Intent pas activé) : on explique quoi faire
        message.content = ""
        await cog.on_message(message)
        assert "Message Content Intent" in message.channel.send.call_args.args[0]


class _NoCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
