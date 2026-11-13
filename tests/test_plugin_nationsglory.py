"""Plugin « Synchro NationsGlory » : client de l'API (faux serveur), logique, et le module dans un bot hôte simulé."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from aiohttp import web
from discord.ext import commands

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # le catalogue : plugins/<id>/ s'importe comme « plugins.<id> »

from plugins.nationsglory.api import NationsGlory, RateLimiter, extract_page, normalize_target  # noqa: E402
from plugins.nationsglory.common import NationsGloryError  # noqa: E402
from plugins.nationsglory.logic import ConfigError, SyncSettings, parse_settings, plan_sync, plan_unlink  # noqa: E402
from plugins.nationsglory.store import Store  # noqa: E402

PLUGIN = ROOT / "plugins" / "nationsglory"
AMOUR = "0b4e7c21-9a3f-4d58-b6e2-5f18c9a07d34"
GRADES = {"LEADER": 1, "OFFICER": 2, "MEMBER": 3, "RECRUIT": 4}


# ---------------------------------------------------------------- manifeste
def test_manifest():
    from botpanel.plugins import validate_manifest

    manifest = validate_manifest(json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8")))
    assert manifest["id"] == "nationsglory" and manifest["enabled_by"] == "ng.active" and manifest["intents"] == ["members"]
    assert [v["key"] for v in manifest["variables"]] == ["NG_API_KEY", "NG_PROXY"]
    assert {p["section"] for p in manifest["pages"]} == {"ng"}


# ---------------------------------------------------------------- logique de synchro
S = SyncSettings(GRADES, country_role_id=10, outside_role_id=20)


def test_plans():
    assert plan_sync({99}, True, "OFFICER", S).add_roles == {2, 10}
    p = plan_sync({4, 10, 99}, True, "MEMBER", S)
    assert p.add_roles == {3} and p.remove_roles == {4}
    p = plan_sync({2, 10, 99}, False, None, S)
    assert p.add_roles == {20} and p.remove_roles == {2, 10}
    assert plan_sync(set(), True, "OFFICER", SyncSettings(GRADES, cumulative=True)).add_roles == {2, 3, 4}
    assert plan_sync({5}, True, "LEADER", SyncSettings({"LEADER": 5, "OFFICER": 5, "MEMBER": 3})).empty
    p = plan_sync({3}, True, "SOMETHING", S)
    assert p.add_roles == {10} and p.remove_roles == {3}
    assert plan_unlink({1, 10, 99}, S).remove_roles == {1, 10}


def test_parse_settings():
    s, warnings = parse_settings({"grades": {"leader": 1, "OFFICER": 0}, "role_pays_id": 10})
    assert s.rank_roles == {"LEADER": 1} and s.country_role_id == 10 and warnings == ["aucun rôle Discord pour le grade Officier"]
    with pytest.raises(ConfigError):
        parse_settings({"grades": {"CAPITAINE": 1}})
    with pytest.raises(ConfigError):
        parse_settings({"grades": {"LEADER": 1}, "role_pays_id": 1})


def test_normalize_target():
    assert normalize_target(" Jade ", " Amour ") == ("jade", "Amour")
    assert normalize_target("blue", "https://nationsglory.fr/servers/jade/countries/C%C3%B4te_Ivoire?x=1") == ("jade", "Côte_Ivoire")


def test_extract_page_shapes():
    assert extract_page([{"a": 1}]) == ([{"a": 1}], None)
    assert extract_page({"data": [{"a": 1}], "next_cursor": "abc"}) == ([{"a": 1}], "abc")
    assert extract_page({"items": [{"a": 1}], "meta": {"next_cursor": None}}) == ([{"a": 1}], None)
    assert extract_page({"data": [], "links": {"next": "/v1/x?cursor=zz&limit=5"}}) == ([], "zz")


# ---------------------------------------------------------------- faux serveur de l'API
MEMBERS = [{"country_id": AMOUR, "username": f"Joueur_{i}", "role": "MEMBER"} for i in range(5)] + [
    {"country_id": AMOUR, "username": "Tristan_QC", "role": "LEADER"},
    {"country_id": AMOUR, "username": "Bob", "role": "OFFICER"},
]
COUNTRIES = [{"country_id": f"00000000-0000-0000-0000-{i:012d}", "name": f"Pays{i}"} for i in range(7)] + [
    {"country_id": AMOUR, "name": "Amour", "power": 42, "level": 3}
]


def paginate(rows, request, page_size_max=3):
    limit = int(request.query.get("limit", 2))
    if limit > page_size_max:
        return web.json_response({"type": "bad_request", "status": 400, "detail": "limit too high"}, status=400)
    start = int(request.query.get("cursor", 0))
    chunk = rows[start: start + limit]
    nxt = str(start + limit) if start + limit < len(rows) else None
    return web.json_response({"data": chunk, "next_cursor": nxt})


@pytest.fixture
async def api_url(unused_tcp_port):
    calls = []
    state = {"busy": 0}

    @web.middleware
    async def auth(request, handler):
        calls.append(str(request.rel_url))
        if request.headers.get("X-API-Key") != "ngp_test":
            return web.json_response({"title": "Unauthorized", "status": 401}, status=401)
        if state["busy"]:
            state["busy"] -= 1
            return web.json_response({"status": 429}, status=429, headers={"Retry-After": "1"})
        return await handler(request)

    async def countries(request):
        return paginate(COUNTRIES, request)

    async def country(request):
        if request.match_info["cid"] != AMOUR:
            return web.json_response({"status": 404}, status=404)
        return web.json_response(COUNTRIES[-1])

    async def members(request):
        if request.match_info["cid"] != AMOUR:
            return web.json_response({"status": 410}, status=410)
        return paginate(MEMBERS, request)

    async def player(request):
        name = request.match_info["name"]
        if name.lower() == "inconnu":
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"username": name, "country_id": None, "role": None})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/v1/servers/jade/countries", countries)
    app.router.add_get("/v1/servers/jade/countries/{cid}", country)
    app.router.add_get("/v1/servers/jade/countries/{cid}/members", members)
    app.router.add_get("/v1/servers/jade/players/{name}", player)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    yield f"http://127.0.0.1:{unused_tcp_port}/v1", calls, state
    await runner.cleanup()


async def test_api_resolve_members_and_caches(api_url):
    url, calls, _ = api_url
    api = NationsGlory("ngp_test", "Jade", base_url=url)
    assert await api.resolve_country("amour") == (AMOUR, "Amour")  # insensible à la casse
    n = len(calls)
    assert await api.resolve_country("Amour") == (AMOUR, "Amour")
    assert len(calls) == n  # liste des pays en cache
    members = await api.members(AMOUR)
    assert len(members) == 7 and members["tristan_qc"] == {"username": "Tristan_QC", "role": "LEADER"}
    assert any("limit=100" in c for c in calls) and any("cursor=" in c for c in calls)
    n = len(calls)
    await api.members(AMOUR, max_age=60)
    assert len(calls) == n  # membres en cache (un cache par pays)
    assert (await api.get_country(AMOUR))["power"] == 42
    assert await api.get_player("Inconnu") is None
    assert (await api.get_player("Steve"))["username"] == "Steve"
    assert await api.resolve_country(AMOUR) == (AMOUR, "Amour")
    with pytest.raises(NationsGloryError) as e:
        await api.resolve_country("Atlantide")
    assert e.value.status == 404 and "options du bot" in e.value.user_message
    with pytest.raises(NationsGloryError):
        await api.members("11111111-1111-1111-1111-111111111111")
    await api.close()


async def test_api_bad_key_and_unreachable(api_url):
    url, _, _ = api_url
    api = NationsGlory("ngp_mauvaise", "jade", base_url=url)
    with pytest.raises(NationsGloryError) as e:
        await api.members(AMOUR)
    assert e.value.status == 401 and "clé" in e.value.user_message
    await api.close()
    with pytest.raises(NationsGloryError) as e:
        await NationsGlory("", "jade", base_url=url).members(AMOUR)
    assert e.value.status == 401 and "pas encore configurée" in e.value.user_message
    api = NationsGlory("ngp_test", "jade", base_url="http://127.0.0.1:1/v1")
    with pytest.raises(NationsGloryError):
        await api.members(AMOUR)
    await api.close()


async def test_api_429_waits_and_retries(api_url):
    url, calls, state = api_url
    limiter = RateLimiter(50)
    api = NationsGlory("ngp_test", "jade", base_url=url, limiter=limiter)
    state["busy"] = 1
    start = time.monotonic()
    assert (await api.get_country(AMOUR))["power"] == 42
    assert time.monotonic() - start >= 0.9 and len(calls) == 2  # attendu Retry-After, puis réessayé
    state["busy"] = 2
    with pytest.raises(NationsGloryError) as e:
        await api.get_country(AMOUR)
    assert e.value.status == 429
    await api.close()


async def test_rate_limiter_shared():
    limiter = RateLimiter(3)
    start = time.monotonic()
    for _ in range(3):
        await limiter.wait()
    assert time.monotonic() - start < 0.5
    limiter.calls[0] -= 59.7  # la 1re requête date de presque une minute
    await limiter.wait()
    assert 0.2 <= time.monotonic() - start < 2


# ---------------------------------------------------------------- stockage
async def test_store(tmp_path):
    store = Store(tmp_path / "ng.db")
    await store.connect()
    await store.add_link(555, 42, "Tristan_QC", 1700000000)
    assert (await store.get_link(555, 42))["linked_at"] == 1700000000
    assert (await store.get_link_by_name(555, "tristan_qc"))["discord_id"] == 42  # sans tenir compte de la casse
    await store.rename_link(555, 42, "TRISTAN_QC")
    assert [r["ng_name"] for r in await store.all_links(555)] == ["TRISTAN_QC"] and await store.all_links(777) == []
    assert await store.remove_link(555, 42) and not await store.remove_link(555, 42)
    await store.set_value("k", "v")
    assert await store.get_value("k") == "v"
    await store.close()


# ---------------------------------------------------------------- le module dans un bot hôte
class FakeRole:
    def __init__(self, rid, position, name=None, managed=False):
        self.id, self.position, self.name, self.managed = rid, position, name or f"r{rid}", managed

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and self.id == other.id

    def __hash__(self):
        return self.id


class FakeMember:
    def __init__(self, uid, guild, roles, nick=None):
        self.id, self.guild, self.roles, self.nick = uid, guild, list(roles), nick
        self.add_roles = AsyncMock(side_effect=lambda *r, reason=None: self.roles.extend(r))
        self.remove_roles = AsyncMock(side_effect=lambda *r, reason=None: [self.roles.remove(x) for x in r])
        self.edit = AsyncMock()
        self.mention = f"<@{uid}>"
        self.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")
        self.name, self.created_at, self.send = f"joueur{uid}", discord.utils.utcnow(), AsyncMock()
        self.guild_permissions = SimpleNamespace(manage_roles=False)

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)

    def __str__(self):
        return f"membre{self.id}"


class FakeResponse:
    def __init__(self):
        self.done, self.sent, self.edits = False, [], []

    def is_done(self):
        return self.done

    async def defer(self, **kw):
        self.done = True

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append(content or kw)

    async def edit_message(self, **kw):
        self.done = True
        self.edits.append(kw)


def fake_guild(gid, role_ids=(1, 2, 3, 4, 10, 20, 99), name="Amour"):
    roles = {i: FakeRole(i, i) for i in role_ids}
    guild = SimpleNamespace(id=gid, name=name, owner_id=1, get_role=roles.get, channels={})
    guild.get_channel = guild.channels.get
    guild.me = FakeMember(999, guild, [FakeRole(0, 0), FakeRole(500, 500, "Bot")])
    guild.me.guild_permissions = SimpleNamespace(manage_roles=True, manage_nicknames=True)
    guild.members = {}
    guild.get_member = lambda i: guild.members.get(i)
    return guild, roles


def add_member(guild, uid, roles=()):
    m = FakeMember(uid, guild, [FakeRole(0, 0), *roles])
    guild.members[uid] = m
    return m


def inter(guild, user):
    return SimpleNamespace(user=user, guild=guild, guild_id=guild.id, response=FakeResponse(),
                           followup=SimpleNamespace(send=AsyncMock()), message=None)


class HostBot(commands.Bot):
    """Le minimum qu'un bot hôte offre aux plugins Bot Panel (bot.plugin_host)."""

    def __init__(self, data: Path) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.none())
        self.configs: dict[int, dict] = {}
        self.plugin_host = SimpleNamespace(
            config=lambda gid, section: self.configs.get(gid, {}).get(section, {}),
            data_dir=lambda pid: data / pid,
            send_log=AsyncMock(),
            refresh_panel=Mock(),
        )

    async def configure(self, gid: int, values: dict) -> None:
        self.configs.setdefault(gid, {}).setdefault("ng", {}).update(values)
        await self.get_cog("NGSync").on_guild_config_update(gid)


@pytest.fixture
async def host(tmp_path, api_url):
    url, calls, _ = api_url
    os.environ["NG_API_KEY"] = "ngp_test"
    bot = HostBot(tmp_path)
    await bot.load_extension("plugins.nationsglory")
    cog = bot.get_cog("NGSync")
    cog.tick.cancel()  # pas de vrai Discord
    real_client = cog.client

    def client(server):
        api = real_client(server)
        api.base_url = url
        return api

    cog.client = client
    yield bot, cog, calls
    await bot.unload_extension("plugins.nationsglory")
    os.environ.pop("NG_API_KEY", None)


async def test_sync_isolation_and_shared_client(host):
    bot, cog, _ = host
    assert cog.state(555) is None  # désactivée par défaut
    await bot.configure(555, {"active": True, "serveur": "jade", "pays": "Amour", "grades": GRADES,
                              "role_pays_id": 10, "role_hors_pays_id": 20})
    st = cog.state(555)
    guild, roles = fake_guild(555)
    tristan = add_member(guild, 42, [roles[99]])
    bob = add_member(guild, 43, [roles[3], roles[10]])
    parti = add_member(guild, 44, [roles[1], roles[10]])
    await cog.store.add_link(555, 42, "tristan_qc")
    await cog.store.add_link(555, 43, "Bob")
    await cog.store.add_link(555, 44, "Inconnu")
    assert await cog.sync_all(guild, st) == (3, 3, 0)
    assert {r.id for r in tristan.roles} == {0, 99, 1, 10}  # Chef + pays
    assert {r.id for r in bob.roles} == {0, 2, 10}  # promu Officier
    assert {r.id for r in parti.roles} == {0, 20}  # hors pays
    assert (await cog.store.get_link(555, 42))["ng_name"] == "Tristan_QC"
    tristan.edit.assert_awaited_with(nick="Tristan_QC", reason="Pseudo NationsGlory")
    assert await cog.sync_all(guild, st) == (3, 0, 0)
    await cog._unlink(bob, await cog.store.get_link(555, 43), st)
    assert {r.id for r in bob.roles} == {0}

    # 2e serveur Discord : réglages et liaisons séparés, même client (cache et limite de la clé partagés)
    await bot.configure(777, {"active": True, "serveur": "Jade", "pays": "https://nationsglory.fr/servers/jade/countries/Amour",
                              "grades": {"LEADER": 5}})
    st2 = cog.state(777)
    assert st2 is not st and st2.api is st.api and st2.country_query == "Amour"
    assert st2.settings.rank_roles == {"LEADER": 5} and await cog.store.all_links(777) == []

    # l'état affiché dans Bot Panel
    await cog.run_sync(guild, st)
    assert cog.panel_info(555) == [("NationsGlory", "✅ Amour sur Jade · 7 membres · 2 compte(s) lié(s)")]
    assert cog.panel_info(888) == []
    bot.plugin_host.refresh_panel.assert_called()


async def test_link_flow(host):
    bot, cog, _ = host
    await bot.configure(555, {"active": True, "pays": "Amour", "serveur": "jade", "grades": {"LEADER": 1},
                              "salon_validation_id": 777})
    guild, roles = fake_guild(555)
    channel = SimpleNamespace(send=AsyncMock())
    guild.channels[777] = channel
    tristan = add_member(guild, 42)
    officer = add_member(guild, 50)
    officer.guild_permissions = SimpleNamespace(manage_roles=True)
    rando = add_member(guild, 51)

    i = inter(guild, tristan)
    await cog.lier.callback(cog, i, "Inconnu")
    assert "trouve pas" in i.followup.send.call_args.kwargs["content"]

    i = inter(guild, tristan)
    await cog.lier.callback(cog, i, "tristan_qc")
    sent = channel.send.call_args.kwargs
    assert "Tristan_QC" in sent["embed"].description
    assert [c.custom_id for c in sent["view"].children] == ["ngl:ok:42:Tristan_QC", "ngl:no:42:Tristan_QC"]
    assert await cog.store.get_link(555, 42) is None

    i = inter(guild, rando)
    i.message = SimpleNamespace(embeds=[sent["embed"]])
    await cog.handle_decision(i, "ok", 42, "Tristan_QC")
    assert await cog.store.get_link(555, 42) is None  # pas le droit

    i = inter(guild, officer)
    i.message = SimpleNamespace(embeds=[sent["embed"]])
    await cog.handle_decision(i, "ok", 42, "Tristan_QC")
    assert i.response.edits[0]["view"] is None
    assert (await cog.store.get_link(555, 42))["ng_name"] == "Tristan_QC"
    assert 1 in {r.id for r in tristan.roles}
    assert "lié" in tristan.send.call_args.args[0]
    bot.plugin_host.send_log.assert_awaited()

    i = inter(guild, tristan)
    await cog.delier.callback(cog, i)
    assert 1 not in {r.id for r in tristan.roles} and await cog.store.get_link(555, 42) is None

    other, _ = fake_guild(888)
    i = inter(other, add_member(other, 42))
    await cog.lier.callback(cog, i, "Bob")
    assert "pas activée" in i.response.sent[0]


async def test_stats_and_announcements(host):
    bot, cog, _ = host
    await bot.configure(555, {"active": True, "pays": "Amour", "serveur": "jade", "grades": GRADES,
                              "salon_stats_id": 666, "salon_annonces_id": 70, "annoncer_departs": True})
    st = cog.state(555)
    guild, _ = fake_guild(555)
    sent, edited = [], []

    class Partial:
        def __init__(self, mid):
            self.mid = mid

        async def edit(self, **kw):
            if self.mid not in [m.id for m in sent]:
                raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "inconnu")
            edited.append(kw)

    async def send(**kw):
        msg = SimpleNamespace(id=1000 + len(sent), **kw)
        sent.append(msg)
        return msg

    announce = SimpleNamespace(id=70, send=AsyncMock())
    guild.channels.update({666: SimpleNamespace(id=666, send=send, get_partial_message=Partial), 70: announce})
    await cog.run_status(guild, st)
    await cog.run_status(guild, st)
    assert len(sent) == 1 and len(edited) == 1  # le même message est mis à jour
    embed = sent[0].embed
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "📊 Amour · Jade"
    assert fields["Membres"] == "👥 **7** membres\n👑 Chef : 1\n⭐ Officier : 1\n🛡️ Membre : 5"
    assert fields["Pays"] == "⚡ Power : 42\nNiveau : 3"

    before = {"bob": {"username": "Bob", "role": "RECRUIT"}, "zed": {"username": "Zed_", "role": "RECRUIT"}}
    assert await cog.announce_country_changes(guild, st, before) == []  # 1er tour : on prend note
    await cog.store.add_link(555, 42, "Bob")
    after = {"bob": {"username": "Bob", "role": "OFFICER"}, "new": {"username": "Nouveau", "role": "RECRUIT"}}
    lines = await cog.announce_country_changes(guild, st, after)
    assert "🎉 **Bob** (<@42>) passe **Officier**!" in lines
    assert "👋 **Nouveau** rejoint **Amour** comme Recrue!" in lines
    assert "🚪 **Zed\\_** a quitté Amour." in lines
    assert announce.send.await_count == 1


async def test_host_hooks(host):
    bot, cog, calls = host
    ok = {"ng": {"active": True, "serveur": "jade", "pays": "Amour", "grades": GRADES}}
    assert cog.validate_guild_config(ok) == []
    assert cog.validate_guild_config({"ng": {"active": False}}) == []
    assert cog.validate_guild_config({"ng": {"active": True, "serveur": "", "pays": ""}}) == ["NationsGlory : remplis le serveur et le pays"]
    assert "NationsGlory : " in cog.validate_guild_config({"ng": {"active": True, "serveur": "jade", "pays": "A",
                                                                   "grades": {"CAPITAINE": 1}}})[0]

    guild = SimpleNamespace(id=555)
    values = {"active": True, "serveur": "Blue", "pays": "https://nationsglory.fr/servers/jade/countries/amour"}
    await cog.before_panel_save(guild, "ng", values)
    assert values["serveur"] == "jade" and values["pays"] == "Amour"  # nettoyé + nom exact
    with pytest.raises(ValueError, match="Atlantide.*introuvable"):
        await cog.before_panel_save(guild, "ng", {"active": True, "serveur": "jade", "pays": "Atlantide"})
    n = len(calls)
    await cog.before_panel_save(guild, "niveaux", {"pays": "x"})  # pas sa section
    await cog.before_panel_save(guild, "ng", {"active": False, "serveur": "jade", "pays": "Atlantide"})
    assert len(calls) == n


async def test_without_key_waits_for_host(host):
    bot, cog, calls = host
    os.environ.pop("NG_API_KEY")
    cog.clients.clear()
    await bot.configure(555, {"active": True, "pays": "Amour", "serveur": "jade", "grades": GRADES})
    guild, _ = fake_guild(555)
    bot._connection._guilds = {555: guild}  # bot.guilds
    n = len(calls)
    await cog.tick.coro(cog)
    await cog.tick.coro(cog)  # en pause : pas de 2e essai
    assert len(calls) == n  # aucune requête sans clé
    assert cog.panel_info(555)[0][1].startswith("🔑 En attente de la clé API NationsGlory")
    assert cog.state(555).paused_until > time.monotonic()
    values = {"active": True, "serveur": "jade", "pays": "Atlantide"}
    await cog.before_panel_save(SimpleNamespace(id=555), "ng", values)  # sans clé : on peut pas vérifier, on laisse passer
    assert values["pays"] == "Atlantide"


async def test_commands_are_registered(host):
    bot, cog, _ = host
    names = {c.qualified_name for c in bot.tree.walk_commands()}
    assert {"lier", "delier", "compte", "pays", "joueur", "synchro tout", "synchro liste"} <= names
    assert not names & {"enligne", "guerres"}  # pas dans l'API publique
    assert all(c.module.startswith("plugins.nationsglory") for c in bot.tree.get_commands())


async def test_concurrent_requests_share_the_limit(api_url):
    url, calls, _ = api_url
    limiter = RateLimiter(50)
    a = NationsGlory("ngp_test", "jade", base_url=url, limiter=limiter)
    b = NationsGlory("ngp_test", "jade", base_url=url, limiter=limiter)
    await asyncio.gather(a.get_country(AMOUR), b.get_country(AMOUR), a.get_player("x"))
    assert len(limiter.calls) == 3
    await a.close()
    await b.close()
