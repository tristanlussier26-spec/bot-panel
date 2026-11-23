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
    # Forme exacte de la vraie API (relevée sur Jade) : le curseur est dans meta.page
    real = {"data": [{"a": 1}], "meta": {"request_id": "x", "generated_at": "2026-11-22T21:27:06Z",
                                         "page": {"limit": 100, "total": 226, "next_cursor": "Y2sBqX0", "has_more": True}}}
    assert extract_page(real) == ([{"a": 1}], "Y2sBqX0")
    last = {"data": [{"a": 1}], "meta": {"page": {"limit": 100, "total": 226, "next_cursor": None, "has_more": False}}}
    assert extract_page(last) == ([{"a": 1}], None)


# ---------------------------------------------------------------- faux serveur de l'API
MEMBERS = [{"country_id": AMOUR, "username": f"Joueur_{i}", "role": "MEMBER"} for i in range(5)] + [
    {"country_id": AMOUR, "username": "Tristan_QC", "role": "LEADER"},
    {"country_id": AMOUR, "username": "Bob", "role": "OFFICER"},
]
ENNEMI = "00000000-0000-0000-0000-000000000001"
COUNTRIES = [{"country_id": f"00000000-0000-0000-0000-{i:012d}", "name": f"Pays{i}", "power": 500, "claims": 100}
             for i in range(7)] + [
    {"country_id": AMOUR, "name": "Amour", "power": 42, "level": 3, "claims": 30,
     "relations": {"allies": ["00000000-0000-0000-0000-000000000002"], "enemies": [ENNEMI]}}
]
# Mêmes formes que les vraies réponses de l'API (relevées sur Jade)
WARS = [
    {"war_id": 6780, "attacker_id": ENNEMI, "attacker_name": "Pays1", "defender_id": AMOUR, "defender_name": "Amour",
     "status": "negotiation", "reason": "KILL_WILDERNESS", "declared_on": "2026-10-22", "started_on": None,
     "ended_on": None, "winner": None},
]
WARZONE = [{"period_start": "2026-11-21", "period_end": "2026-11-22", "rank": i + 1,
            "country_id": COUNTRIES[i]["country_id"], "country_name": COUNTRIES[i]["name"],
            "total": 1000 - i, "boat": 0, "petrol": 0, "mine": 0} for i in range(8)]
PNG = b"\x89PNG fake"
WARZONE_WEEK = [{**r, "period_start": "2026-11-15"} for r in WARZONE]
ONLINE = ["tristan_qc", "Bob", "Quelquun"]
LOGINS = {"tristan_qc": "2026-11-22", "bob": "2026-11-01", "joueur_0": "private"}  # les autres : 404
ALLIANCE = {"alliance_key": "b7f6c07bf21591d4", "name": "Hojo", "color": "red", "opened": True, "tags": [],
            "founded_on": "2026-10-01", "prestige_points": 900, "prestige_rank": 1, "countries_count": 2,
            "total_claims": 5000, "bank": 12345, "description": "La plus forte"}
COUNTRIES[-1]["alliance_key"] = COUNTRIES[3]["alliance_key"] = ALLIANCE["alliance_key"]
COUNTRIES[-1].update(members_count=7, score=999, role_names={"LEADER": "Empereur", "OFFICER": "Conseiller"})
for _c in COUNTRIES[:7]:
    _c["members_count"] = 3
# Formes relevées dans l'extraction réelle de Jade (valeurs fictives)
SERIES = [{"at": f"2026-11-{15 + i:02d}T00:00:00Z", "value": v, "min": v, "max": v}
          for i, v in enumerate([30, 32, None, 35, 40, 38, 42])]
MARKET = [{"cereal": "cereal_avoine", "price": 2000, "trend": "+"}, {"cereal": "cereal_ble", "price": 400, "trend": "-"}]
MARKET_HISTORY = {"cereal": "cereal_ble", "price": 400, "trend": "-",
                  "history": {"price": {"2026-11-20": 380, "2026-11-21": 390, "2026-11-22": 400},
                              "sales": {"2026-11-22": 1200}, "stocks": {}}}
HDV = [{"item": "264:0", "id": 264, "meta": 0, "name": "Diamant", "price_cents": 12800, "last_day_median_cents": 13000,
        "change_bp": 250, "trades_7d": 40, "quantity_7d": 900, "value_7d": 115200, "last_trade_at": "2026-11-22T20:00:00Z",
        "sell_listings": 3, "best_sell_cents": 12500, "buy_listings": 1, "best_buy_cents": 11000},
       {"item": "57:0", "id": 57, "meta": 0, "name": "Bloc de diamant", "price_cents": 115000,
        "last_day_median_cents": 116000, "change_bp": None}]
LOTTERIES = [{"lottery_id": "p30", "state": "past", "starts_at": "2026-04-09T16:30:00Z", "ends_at": "2026-04-12T13:45:00Z",
              "ticket_price": 5000, "cash_prize": 0, "item_prizes": [{"type_id": 16257, "damage": 0}], "tickets_sold": 51,
              "winners_count": 1, "winners": ["Gagnant_1"]}]
ENTERPRISES = [{"name": "AFR", "type": "farm", "owner": "Tristan_QC", "created_on": "2025-11-23", "is_open": True,
                "members_count": 16, "contracts_done": 4, "contracts_success_pct": 75, "turnover": 132664, "bank_balance": 0},
               {"name": "Autre", "type": "trader", "owner": "Inconnu_9", "is_open": False, "members_count": 2, "turnover": 5}]
SKILLS = [{"as_of": "2026-11-22T21:09:34Z", "skill": "miner", "rank": i + 1, "username": u, "score": 1000 - i}
          for i, u in enumerate(["Quelquun", "Bob", "Autre_2"])]
PROFILE = {"username": "Bob", "face_url": "https://files.recube.gg/nationsglory/face/Bob.png", "description": None,
           "verified": True, "online": False, "default_server": "jade", "first_join_on": "2025-01-02",
           "playtime_total_minutes": 6000, "servers_played": 2, "achievement_points": 150,
           "servers": [{"server": "jade", "online": False, "country_id": AMOUR, "country_name": "Amour", "role": "OFFICER",
                        "last_login_on": "2026-11-01", "playtime_minutes": 3000, "grade": "VIP"},
                       {"server": "blue", "online": False, "country_id": None, "country_name": None, "role": None}]}
STATUS = [{"domain": "country", "state": "OK", "as_of": "2026-11-22T21:26:04Z"},
          {"domain": "parcel", "state": "BROKEN", "as_of": None}]
FORUM = {"categories": [{"id": 12, "slug": "annonces", "name": "Annonces"}],
         "threads": [{"id": 100, "title": "Vieille annonce", "url": "https://nationsglory.fr/forum/thread/100", "author": "Staff"}]}


def paginate(rows, request, page_size_max=3):
    limit = int(request.query.get("limit", 2))
    if limit > page_size_max:
        return web.json_response({"type": "bad_request", "status": 400, "detail": "limit too high"}, status=400)
    start = int(request.query.get("cursor", 0))
    chunk = rows[start: start + limit]
    nxt = str(start + limit) if start + limit < len(rows) else None
    # Même enveloppe que la vraie API : le curseur est dans meta.page
    return web.json_response({"data": chunk, "meta": {"request_id": "test", "page": {
        "limit": limit, "total": len(rows), "next_cursor": nxt, "has_more": nxt is not None}}})


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
        row = next((c for c in COUNTRIES if c["country_id"] == request.match_info["cid"]), None)
        if row is None:
            return web.json_response({"status": 404}, status=404)
        return web.json_response(row)

    async def members(request):
        if request.match_info["cid"] not in {c["country_id"] for c in COUNTRIES}:
            return web.json_response({"status": 410}, status=410)
        return paginate(MEMBERS, request)

    async def wars(request):
        rows = [w for w in WARS if request.query.get("country") in (None, w["attacker_id"], w["defender_id"])]
        return paginate(rows, request)

    async def war(request):
        wid = int(request.match_info["wid"])
        w = next((w for w in WARS if w["war_id"] == wid), None)
        if w is None:
            return web.json_response({"status": 410}, status=410)
        return web.json_response({"data": {**w, "assaults": w.get("assaults", [])}})

    async def warzone(request):
        return paginate(WARZONE, request)

    async def image(request):
        if request.match_info["sha"] != "a" * 64:
            return web.json_response({"status": 404}, status=404)
        return web.Response(body=PNG, content_type="image/png")

    async def warzone_week(request):
        return paginate(WARZONE_WEEK, request)

    async def online(request):
        return paginate([{"username": u} for u in ONLINE], request)

    async def last_login(request):
        name = request.match_info["name"]
        value = LOGINS.get(name.lower())
        if value == "private":
            return web.json_response({"code": "profile_private", "status": 403}, status=403)
        if value is None:
            return web.json_response({"code": "player_not_found", "status": 404}, status=404)
        return web.json_response({"data": {"username": name, "last_login_on": value}})

    async def alliance(request):
        if request.match_info["key"] != ALLIANCE["alliance_key"]:
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"data": ALLIANCE})

    async def series(request):
        if request.match_info["metric"] == "country.score" and request.query.get("range") not in ("90d", "1y"):
            return web.json_response({"code": "validation_failed", "status": 422}, status=422)
        return web.json_response({"data": {"metric": request.match_info["metric"], "subject": request.query.get("subject"),
                                           "range": request.query.get("range"), "step_seconds": 3600, "points": SERIES}})

    async def market(request):
        return paginate(MARKET, request)

    async def market_one(request):
        if request.match_info["cereal"] != "cereal_ble":
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"data": MARKET_HISTORY})

    async def hdv_items(request):
        q = request.query.get("q", "").lower()
        return web.json_response({"data": [r for r in HDV if q in r["name"].lower()][: int(request.query.get("limit", 25))]})

    async def hdv_item(request):
        row = next((r for r in HDV if r["item"] == request.match_info["item"]), None)
        if row is None:
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"data": {**row, "week": {"trades": 40, "quantity": 900, "value": 115200},
                                           "month": {"low_cents": 9000, "high_cents": 15000},
                                           "book": {"sell": {"listings": 3, "unit_low_cents": 12500},
                                                    "buy": {"listings": 1, "unit_high_cents": 11000}}}})

    async def lotteries(request):
        return paginate(LOTTERIES, request)

    async def enterprises(request):
        return paginate(ENTERPRISES, request)

    async def warzone_live(request):
        return paginate([{**r, "as_of": "2026-11-22T21:34:34Z"} for r in WARZONE], request)

    async def skills(request):
        return paginate(SKILLS, request)

    async def profile(request):
        if request.match_info["name"].lower() == "cache":
            return web.json_response({"code": "profile_private", "status": 403}, status=403)
        if request.match_info["name"].lower() != "bob":
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"data": PROFILE})

    async def enterprise(request):
        row = next((r for r in ENTERPRISES if r["name"] == request.match_info["name"]), None)
        if row is None:
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"data": {**row, "description": "La ferme de Sakhaline", "services": "Blé et maïs",
                                           "disputes": 0, "contracts_in_progress_value": 5000,
                                           "flows_14d": {"net": 12000, "salaries": -3000, "taxes": -800}}})

    async def enterprise_ops(request):
        if request.match_info["name"] != "AFR":
            return web.json_response({"data": []})
        return web.json_response({"data": [{"enterprise": "AFR", "kind": "farm", "trader": None, "bet": None, "farm": {
            "history": {"wheat": {"2026-11-20": 100, "2026-11-21": 150, "2026-11-22": 90}, "corn": {"2026-11-22": 40}},
            "top": {"wheat": 5000, "corn": 1200}, "today_collected": 130, "total_collected": 6200}}]})

    async def enterprise_energy(request):
        return web.json_response({"status": 404}, status=404)

    async def enterprise_parcels(request):
        return web.json_response({"data": []})

    async def status(request):
        return web.json_response({"data": {"server": "jade", "domains": STATUS}})

    async def forum_categories(request):
        return web.json_response({"data": FORUM["categories"]})

    async def forum_threads(request):
        if request.match_info["cat"] != "12":
            return web.json_response({"status": 404}, status=404)
        return web.json_response({"data": {"category": FORUM["categories"][0], "threads": FORUM["threads"]}})

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
    app.router.add_get("/v1/servers/jade/wars", wars)
    app.router.add_get("/v1/servers/jade/wars/{wid}", war)
    app.router.add_get("/v1/servers/jade/rankings/warzone/daily", warzone)
    app.router.add_get("/v1/images/{sha}.png", image)
    app.router.add_get("/v1/servers/jade/rankings/warzone/weekly", warzone_week)
    app.router.add_get("/v1/servers/jade/online", online)
    app.router.add_get("/v1/servers/jade/players/{name}/last-login", last_login)
    app.router.add_get("/v1/servers/jade/alliances/{key}", alliance)
    app.router.add_get("/v1/servers/jade/series/{metric}", series)
    app.router.add_get("/v1/servers/jade/market", market)
    app.router.add_get("/v1/servers/jade/market/{cereal}", market_one)
    app.router.add_get("/v1/servers/jade/hdv/items", hdv_items)
    app.router.add_get("/v1/servers/jade/hdv/items/{item}", hdv_item)
    app.router.add_get("/v1/servers/jade/lotteries", lotteries)
    app.router.add_get("/v1/servers/jade/enterprises", enterprises)
    app.router.add_get("/v1/servers/jade/enterprises/{name}", enterprise)
    app.router.add_get("/v1/servers/jade/enterprises/{name}/operations", enterprise_ops)
    app.router.add_get("/v1/servers/jade/enterprises/{name}/energy", enterprise_energy)
    app.router.add_get("/v1/servers/jade/enterprises/{name}/parcels", enterprise_parcels)
    app.router.add_get("/v1/servers/jade/rankings/warzone/live", warzone_live)
    app.router.add_get("/v1/servers/jade/rankings/skills/{period}", skills)
    app.router.add_get("/v1/players/{name}", profile)
    app.router.add_get("/v1/servers/jade/status", status)
    app.router.add_get("/v1/forum/categories", forum_categories)
    app.router.add_get("/v1/forum/categories/{cat}/threads", forum_threads)
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
    assert fields["Pays"] == "⚡ Power : 42\n🗺️ Claims : 30\n🏆 Score : 999\nNiveau : 3"

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
    assert {"lier", "delier", "compte", "pays", "joueur", "guerres", "guerre", "enligne", "classement-pays", "alliance",
            "inactifs", "synchro tout", "synchro liste"} <= names
    assert "classement" not in names  # celui du plugin Niveaux
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


# ---------------------------------------------------------------- guerres, power, warzone
from plugins.nationsglory import alerts  # noqa: E402


def test_war_events_logic():
    war = dict(WARS[0])
    assert alerts.war_events(None, [war], AMOUR) == []  # 1er tour : on prend note
    events = alerts.war_events({}, [war], AMOUR)
    assert [e.kind for e in events] == ["declared"] and events[0].important  # on se fait attaquer
    assert "nous déclare la guerre" in alerts.war_line(events[0], AMOUR) and "kill en wilderness" in alerts.war_line(events[0], AMOUR)
    snap = alerts.war_snapshot([war])
    started = {**war, "status": "in_progress"}
    assert [e.kind for e in alerts.war_events(snap, [started], AMOUR)] == ["started"]
    ended = {**war, "status": "finished", "winner": "defender"}
    events = alerts.war_events(alerts.war_snapshot([started]), [ended], AMOUR)
    assert alerts.war_line(events[0], AMOUR).startswith("🏆 Victoire")
    assert alerts.war_line(alerts.war_events({}, [{**ended, "winner": None}], AMOUR) or
                           alerts.war_events(snap, [{**ended, "winner": None}], AMOUR)[0], AMOUR).startswith("🕊️")
    assert alerts.war_events({}, [ended], AMOUR) == []  # finie avant qu'on la voie


def test_assault_events_logic():
    war = {**WARS[0], "status": "in_progress", "assaults": [{"on": "d", "winner": "attacker"}, {"on": "d", "winner": "defender"}]}
    assert alerts.assault_events(war, None, AMOUR) == ([], 2)
    events, n = alerts.assault_events(war, 0, AMOUR)
    assert n == 2 and [alerts.war_line(e, AMOUR)[:1] for e in events] == ["❌", "✅"]  # on défend


def test_power_logic():
    assert alerts.power_level({"power": 99, "claims": 100}, 50) == "sous"
    assert alerts.power_level({"power": 120, "claims": 100}, 50) == "bas"
    assert alerts.power_level({"power": 200, "claims": 100}, 50) == "ok"
    assert alerts.power_level({"power": None, "claims": 100}, 50) is None
    card = {"power": 99, "claims": 100}
    assert alerts.power_alert(None, "sous", card, "Amour") == (None, False)  # 1er tour
    msg, mention = alerts.power_alert("ok", "sous", card, "Amour")
    assert "sous le power" in msg and mention
    assert alerts.power_alert("ok", "bas", card, "Amour")[0].startswith("⚠️")
    assert alerts.power_alert("bas", "ok", card, "Amour")[0].startswith("✅")
    assert alerts.power_alert("bas", "bas", card, "Amour") == (None, False)
    rows = [{"country_id": "x", "name": "X", "power": 5, "claims": 9}, {"country_id": "y", "name": "Y", "power": 9, "claims": 5}]
    now = alerts.attackable_enemies(["x", "y"], rows)
    assert list(now) == ["x"] and alerts.enemy_lines(None, now) == [] and alerts.enemy_lines(["x"], now) == []
    assert "**X**" in alerts.enemy_lines([], now)[0]


def test_warzone_summary():
    day, top, ours = alerts.warzone_summary(WARZONE, AMOUR, top=3)
    assert day == "2026-11-22" and top[0].startswith("🥇") and len(top) == 3
    assert ours.startswith("`8.`") and "__**Amour**__" in ours  # hors du top : ajouté à part
    assert alerts.warzone_summary(WARZONE, "pas-classé", top=3)[2] is None
    assert alerts.warzone_summary(WARZONE, AMOUR)[2] is None  # dans le top 10
    assert alerts.warzone_summary([], AMOUR) == (None, [], None)


async def test_alerts_in_bot(host):
    bot, cog, calls = host
    await bot.configure(555, {"active": True, "serveur": "jade", "pays": "Amour", "grades": GRADES,
                              "salon_guerres_id": 80, "role_alerte_id": 81, "salon_warzone_id": 82, "marge_power": 20})
    st = cog.state(555)
    guild, _ = fake_guild(555)
    chan = SimpleNamespace(id=80, send=AsyncMock())
    wz = SimpleNamespace(id=82, send=AsyncMock())
    guild.channels.update({80: chan, 82: wz})

    saved_wars = list(WARS)
    WARS.clear()  # pas de guerre au 1er tour
    try:
        assert await cog.run_alerts(guild, st) == []  # 1er tour : on prend note, rien à publier
        assert await cog.store.get_value("ng_power:555") == "bas"  # power 42, claims 30, marge 20

        # Une guerre nous est déclarée et un ennemi passe sous le power
        WARS.extend(saved_wars)
        COUNTRIES[1]["power"] = 10
        for c in cog.clients.values():
            c._cached.clear()
            c._countries = None
        lines = await cog.run_alerts(guild, st)
    finally:
        COUNTRIES[1]["power"] = 500
        WARS[:] = saved_wars
    assert any("nous déclare la guerre" in x for x in lines) and any("Pays1" in x and "attaquer" in x for x in lines)
    sent = chan.send.call_args
    assert sent.args[0].startswith("<@&81>") and sent.kwargs["allowed_mentions"].roles[0].id == 81

    # Warzone : publié une fois par journée close
    assert await cog.run_warzone(guild, st) and not await cog.run_warzone(guild, st)
    embed = wz.send.call_args_list[0].kwargs["embed"]
    assert embed.title == "🏁 Warzone du 2026-11-22 · Jade" and "__**Amour**__" in embed.description

    # Drapeau : la clé est exigée, le bot le télécharge
    api = st.api
    assert await api.image("a" * 64) == PNG and await api.image("pas-un-sha") is None
    n = len(calls)
    await api.image("a" * 64)
    assert len(calls) == n  # en cache



# ---------------------------------------------------------------- classements, en ligne, activité
from datetime import date  # noqa: E402

from plugins.nationsglory import insights  # noqa: E402


def test_ranking_logic():
    rows = insights.ranking(COUNTRIES + [{"country_id": "w", "name": "Wilderness", "power": 999999, "members_count": 0}], "power")
    assert rows[0]["name"] == "Pays0" and all(r["name"] != "Wilderness" for r in rows)  # pays vides écartés
    lines, ours = insights.ranking_lines(rows, "power", AMOUR, top=3)
    assert len(lines) == 3 and lines[0].startswith("🥇") and ours.startswith("`8.`") and "__**Amour**__" in ours
    lines, ours = insights.ranking_lines(insights.ranking(COUNTRIES, "banque"), "banque", AMOUR)
    assert lines == [] and ours is None  # personne a de banque dans ces données


def test_online_and_inactivity_logic():
    members = {"a": {"username": "A", "role": "RECRUIT"}, "b": {"username": "B", "role": "LEADER"},
               "c": {"username": "C", "role": "MEMBER"}, "d": {"username": "D", "role": "MEMBER"}, "e": {"username": "E"}}
    assert [m["username"] for m in insights.online_members(members, {"a", "b", "zz"})] == ["B", "A"]
    logins = {"a": ["2026-11-01", 0], "b": ["2026-11-21", 0], "c": ["none", 0], "d": ["private", 0]}
    report = insights.inactivity(members, logins, date(2026, 11, 22), 7)
    assert [(m["username"], n) for m, n in report["inactifs"]] == [("A", 21)]
    assert [m["username"] for m in report["absents"]] == ["C"] and [m["username"] for m in report["prives"]] == ["D"]
    assert [m["username"] for m in report["inconnus"]] == ["E"] and report["verifies"] == 4
    assert insights.next_to_check(members, logins, 100.0, 50) == "E"  # jamais vérifié d'abord
    logins["e"] = ["2026-11-22", 90.0]
    assert insights.next_to_check(members, logins, 100.0, 50) == "A"  # puis le plus vieux
    assert insights.next_to_check(members, {k: [v, 99.0] for k, v in logins.items()}, 100.0, 50) is None


def test_war_detail_lines():
    war = {**WARS[0], "assaults": [{"winner": "attacker"}, {"winner": "attacker"}, {"winner": "defender"}],
           "missiles": {"attacker": {"Conventional": 4, "Nuclear": 1}, "defender": {}},
           "agreements_signed": [{"type": "peace", "signed_on": "2026-10-30", "duration_days": 7, "status": "active"}]}
    fields = dict(insights.war_detail_lines(war, {}))
    assert fields["Assauts"] == "🗡️ Pays1 : 2 · 🛡️ Amour : 1"
    assert fields["Missiles"] == "**Pays1** : 4× Conventional, 1× Nuclear" and "peace" in fields["Accords"]


async def _setup_guild(bot, cog, extra=None):
    await bot.configure(555, {"active": True, "serveur": "jade", "pays": "Amour", "grades": GRADES, **(extra or {})})
    guild, _ = fake_guild(555)
    return cog.state(555), guild


def said(i):
    call = i.followup.send.call_args
    return call.kwargs.get("embed") or call.kwargs.get("content")


async def test_consultation_commands(host):
    bot, cog, _ = host
    st, guild = await _setup_guild(bot, cog)
    user = add_member(guild, 42)

    i = inter(guild, user)
    await cog.enligne.callback(cog, i)
    embed = said(i)
    assert embed.title == "🟢 Amour : 2 en ligne" and "3 joueurs connectés" in embed.description
    assert [f.name for f in embed.fields] == ["👑 Empereur", "⭐ Conseiller"]  # noms de grades du pays

    i = inter(guild, user)
    await cog.classement.callback(cog, i, "score")
    embed = said(i)
    assert embed.title == "🏆 Classement Score · Jade" and embed.description.startswith("🥇 __**Amour**__ · 999")

    i = inter(guild, user)
    await cog.pays.callback(cog, i, "pays3")
    embed = i.followup.send.call_args.kwargs["embed"]
    assert embed.title == "🏳️ Pays3" and "**7** membres" in embed.description  # même faux membres
    i = inter(guild, user)
    await cog.pays.callback(cog, i, "Atlantide")
    assert "trouve pas le pays" in said(i)
    i = inter(guild, user)
    await cog.pays.callback(cog, i, None)
    embed = i.followup.send.call_args.kwargs["embed"]
    names = [f.name for f in embed.fields]
    assert embed.title == "🏳️ Amour" and "👑 Empereur (1)" in names and "⚔️ Ennemis (1)" in names

    i = inter(guild, user)
    await cog.guerre.callback(cog, i, 6780)
    assert said(i).title == "⚔️ Guerre n°6780 : Pays1 contre Amour"
    i = inter(guild, user)
    await cog.guerre.callback(cog, i, 1)
    assert "existe pas" in said(i)

    i = inter(guild, user)
    await cog.alliance.callback(cog, i, None)
    embed = said(i)
    assert embed.title == "🛡️ Alliance Hojo" and {"Pays (2)", "Banque", "Candidatures"} <= {f.name for f in embed.fields}
    i = inter(guild, user)
    await cog.alliance.callback(cog, i, "Pays5")
    assert "aucune alliance" in said(i)


async def test_activity_sweep_and_inactifs(host, monkeypatch):
    bot, cog, calls = host
    st, guild = await _setup_guild(bot, cog)
    monkeypatch.setattr(sys.modules["plugins.nationsglory.cog"], "paris_today", lambda: date(2026, 11, 22))
    checked = []
    for _ in range(9):
        cog._last_activity.clear()  # pas besoin d'attendre 30 s entre deux vérifications
        checked.append(await cog.check_activity(st))
    assert sorted(n for n in checked if n) == sorted(m["username"] for m in MEMBERS) and checked[-2:] == [None, None]
    n = len(calls)
    assert await cog.check_activity(st) is None and len(calls) == n  # moins de 30 s : rien

    i = inter(guild, add_member(guild, 42))
    await cog.inactifs.callback(cog, i, 7)
    embed = said(i)
    assert embed.description == "Bob (Conseiller) · 21 j"
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Profil privé (1)"] == "Joueur_0" and fields["Plus de 90 jours (4)"].count("Joueur_") == 4
    assert embed.footer.text.startswith("7/7 membres vérifiés")
    # Un 403 « profil privé » doit pas mettre le bot en pause comme une clé refusée
    assert st.paused_until == 0


async def test_weekly_warzone_and_stats_extras(host):
    bot, cog, _ = host
    st, guild = await _setup_guild(bot, cog, {"salon_warzone_id": 82, "salon_stats_id": 666})
    wz = SimpleNamespace(id=82, send=AsyncMock())
    sent = []

    async def send(**kw):
        sent.append(kw)
        return SimpleNamespace(id=1)

    guild.channels.update({82: wz, 666: SimpleNamespace(id=666, send=send, get_partial_message=None)})
    assert await cog.run_warzone(guild, st)
    titles = [c.kwargs["embed"].title for c in wz.send.call_args_list]
    assert titles == ["🏁 Warzone du 2026-11-22 · Jade", "🏆 Warzone de la semaine du 2026-11-15 au 2026-11-22 · Jade"]
    assert not await cog.run_warzone(guild, st)
    await cog.run_status(guild, st)
    fields = {f.name: f.value for f in sent[0]["embed"].fields}
    assert fields["En ce moment"] == "🟢 En ligne : 2\n⚔️ Guerres en cours : 0 (+1 en négociation)"


async def test_online_dispatched_for_counters(host):
    bot, cog, _ = host
    st, guild = await _setup_guild(bot, cog)
    seen = []
    bot.dispatch = lambda name, *args: seen.append((name, args))
    await cog.run_sync(guild, st)
    assert ("ng_online_update", (555, "Amour", 2)) in seen


# ---------------------------------------------------------------- veille : logique pure
from plugins.nationsglory import veille  # noqa: E402


def test_sparkline_and_series():
    assert veille.sparkline([1, 2, None, 8]) == "▁▂ █"
    assert veille.sparkline([5, 5]) == "▄▄" and veille.sparkline([None]) == ""
    assert len(veille.compress(list(range(100)), 10)) == 10 and veille.compress([1, 2], 10) == [1, 2]
    summary = veille.series_summary(SERIES)
    assert summary["first"] == 30 and summary["last"] == 42 and summary["delta"] == 12 and summary["min"] == 30
    assert veille.series_summary([{"value": None}]) is None
    assert veille.signed(-1500, " $") == "−1 500 $" and veille.signed(0) == "="


def test_diplomacy_logic():
    names = {"a": "Alpha", "b": "Beta", "c": "Gamma"}
    before = veille.relations_snapshot({"relations": {"allies": ["a"], "enemies": []}, "alliance_key": None})
    after = veille.relations_snapshot({"relations": {"allies": ["b"], "enemies": ["c"]}, "colonies": ["a"],
                                       "colonized_by": None, "alliance_key": "k1"})
    assert veille.diplomacy_lines(None, after, names, {}) == []
    lines = veille.diplomacy_lines(before, after, names, {"k1": "Hojo"})
    assert lines == ["🤝 **Beta** devient notre allié.", "💔 **Alpha** est plus notre allié.",
                     "⚔️ **Gamma** devient notre ennemi.", "🏴 **Alpha** devient notre colonie.",
                     "🛡️ On rejoint l'alliance **Hojo**."]
    colonised = dict(after, colonized_by="c")
    assert "⛓️ On est maintenant une colonie de **Gamma**!" in veille.diplomacy_lines(after, colonised, names, {})


def test_watchlist_logic():
    watched, missing = veille.resolve_watchlist(["pays1", "Atlantide", "Amour"], COUNTRIES, AMOUR)
    assert watched == {ENNEMI: "Pays1"} and missing == ["Atlantide"]  # son propre pays est ignoré
    lines, state = veille.watch_power_lines(None, COUNTRIES, watched)
    assert lines == [] and state == {ENNEMI: "ok"}
    weak = [dict(c, power=1) if c["country_id"] == ENNEMI else c for c in COUNTRIES]
    lines, state = veille.watch_power_lines(state, weak, watched)
    assert "sous le power" in lines[0] and state[ENNEMI] == "sous"
    lines, _ = veille.watch_power_lines(state, [c for c in COUNTRIES if c["country_id"] != ENNEMI], watched)
    assert "disparu" in lines[0]
    event = alerts.WarEvent("declared", WARS[0])
    assert veille.neutral_war_line(event).startswith("👀 **Pays1** déclare la guerre à **Amour**")


def test_forum_and_market_logic():
    threads = [{"id": 3}, {"id": 1}, {"id": 2}]
    assert veille.new_threads(None, threads) == [] and [t["id"] for t in veille.new_threads([1], threads)] == [2, 3]
    lines, ref = veille.market_moves(None, MARKET, 10)
    assert lines == [] and ref == {"cereal_avoine": 2000, "cereal_ble": 400}
    lines, ref = veille.market_moves(ref, [{"cereal": "cereal_ble", "price": 460}, {"cereal": "cereal_avoine", "price": 2100}], 10)
    assert lines == ["📈 **Blé** : 400 $ → 460 $ (+15 %)"] and ref["cereal_ble"] == 460 and ref["cereal_avoine"] == 2000
    assert veille.cereal_name("cereal_mais") == "Maïs" and veille.cereal_name("wheat") == "Blé"


def test_report_logic():
    members = {m["username"].lower(): m for m in MEMBERS}
    card = {"power": 42, "claims": 30, "bank_balance": 1000, "score": 999, "mmr": 5}
    now = veille.report_snapshot(card, members, COUNTRIES, AMOUR)
    assert now["members_count"] == 7 and now["ranks"]["power"] == 8
    fields = dict(veille.report_fields(None, now))
    assert "**42**" in fields["Le pays"] and "Mouvements" not in fields
    before = dict(now, power=50, members=now["members"][1:] + ["Parti"], ranks={"power": 9, "score": 1, "members_count": 8})
    fields = dict(veille.report_fields(before, now))
    assert "⚡ Power : **42** (−8)" in fields["Le pays"] and "8e en power (⬆️ 1)" in fields["Classements"]
    assert "Arrivées (1)" in fields["Mouvements"] and "Départs (1) : Parti" in fields["Mouvements"]


# ---------------------------------------------------------------- veille dans le bot
async def test_report_forum_market_in_bot(host, monkeypatch):
    bot, cog, _ = host
    st, guild = await _setup_guild(bot, cog, {"salon_rapport_id": 90, "salon_forum_id": 91, "salon_marche_id": 92,
                                              "heure_rapport": 0, "seuil_marche": 10})
    chans = {i: SimpleNamespace(id=i, send=AsyncMock()) for i in (90, 91, 92)}
    guild.channels.update(chans)
    assert cog.report_due(st)  # heure 0 : c'est l'heure
    assert await cog.run_report(guild, st) and not await cog.run_report(guild, st)
    embed = chans[90].send.call_args.kwargs["embed"]
    assert embed.title == "📰 Rapport du jour · Amour" and "Premier rapport" in embed.description
    names = [f.name for f in embed.fields]
    assert names[:2] == ["Le pays", "Classements"] and "En bref" in names
    st.last_report_day = None
    assert not await cog.run_report(guild, st)  # déjà fait aujourd'hui (gardé en base, survit à un redémarrage)

    assert await cog.run_forum(guild, st) == 0  # 1er tour : on prend note
    FORUM["threads"].insert(0, {"id": 101, "title": "Mise à jour 2.0", "url": "https://x", "author": "Staff", "pinned": True})
    for c in cog.clients.values():
        c._cached.pop("forum:12:10", None)
    try:
        assert await cog.run_forum(guild, st) == 1
    finally:
        FORUM["threads"].pop(0)
    assert chans[91].send.call_args.kwargs["embed"].title == "Mise à jour 2.0"

    assert await cog.run_market(guild, st) == []
    MARKET[1]["price"] = 500
    for c in cog.clients.values():
        c._cached.pop("market", None)
    try:
        lines = await cog.run_market(guild, st)
    finally:
        MARKET[1]["price"] = 400
    assert lines == ["📈 **Blé** : 400 $ → 500 $ (+25 %)"] and chans[92].send.await_count == 1


async def test_diplomacy_and_watchlist_in_bot(host):
    bot, cog, _ = host
    st, guild = await _setup_guild(bot, cog, {"salon_guerres_id": 80, "pays_surveilles": ["Pays3"], "alerte_power": False,
                                              "alerte_ennemis": False, "annoncer_assauts": False})
    guild.channels[80] = SimpleNamespace(id=80, send=AsyncMock())
    assert await cog.run_alerts(guild, st) == []  # 1er tour
    third = COUNTRIES[3]["country_id"]
    saved = dict(COUNTRIES[-1]["relations"])
    COUNTRIES[-1]["relations"] = {"allies": saved["allies"] + [third], "enemies": saved["enemies"]}
    COUNTRIES[3]["power"] = 50
    WARS.append({"war_id": 7000, "attacker_id": third, "attacker_name": "Pays3", "defender_id": COUNTRIES[4]["country_id"],
                 "defender_name": "Pays4", "status": "negotiation", "reason": "SCAM", "declared_on": "2026-11-22"})
    for c in cog.clients.values():
        c._cached.clear()
        c._countries = None
    try:
        lines = await cog.run_alerts(guild, st)
    finally:
        COUNTRIES[-1]["relations"] = saved
        COUNTRIES[3]["power"] = 500
        WARS.pop()
    assert "🤝 **Pays3** devient notre allié." in lines
    assert any("(surveillé) est **sous le power**" in x for x in lines)
    assert "👀 **Pays3** déclare la guerre à **Pays4** (arnaque)." in lines


async def test_ingame_and_inactive_roles(host):
    bot, cog, _ = host
    bad = {"ng": {"active": True, "serveur": "jade", "pays": "Amour", "grades": GRADES, "role_en_jeu_id": 3}}
    assert "déjà utilisé pour un grade" in cog.validate_guild_config(bad)[0]
    st, guild = await _setup_guild(bot, cog, {"role_en_jeu_id": 10, "role_inactif_id": 20, "jours_inactif": 14})
    online = add_member(guild, 42)
    away = add_member(guild, 43, [FakeRole(10, 10)])
    await cog.store.add_link(555, 42, "Tristan_QC")
    await cog.store.add_link(555, 43, "Joueur_1")
    await st.country()
    await cog._save(cog._activity_key(st), {"joueur_1": ["none", 0], "tristan_qc": ["2026-11-22", 0]})
    await cog.run_sync(guild, st)
    assert 10 in {r.id for r in online.roles} and 20 not in {r.id for r in online.roles}
    assert 20 in {r.id for r in away.roles} and 10 not in {r.id for r in away.roles}  # hors ligne + inactif


# ---------------------------------------------------------------- commandes d'infos
async def test_info_commands(host):
    bot, cog, _ = host
    infos = bot.get_cog("NGInfos")
    st, guild = await _setup_guild(bot, cog)
    user = add_member(guild, 42)

    async def run(command, *args):
        i = inter(guild, user)
        await command.callback(infos, i, *args)
        return said(i)

    embed = await run(infos.historique, "power", "7d", None)
    assert embed.title == "📈 Power de Amour · 7 jours" and "+12" in [f.value for f in embed.fields][1]
    embed = await run(infos.historique, "score", "24h", None)  # l'API veut 90d pour le score
    assert embed.title.endswith("90 jours")
    embed = await run(infos.comparer, "Pays3", None)
    assert embed.title == "⚖️ Amour contre Pays3"
    embed = await run(infos.joueur, "Bob")
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Pays sur Jade"] == "Amour · Conseiller" and fields["Temps de jeu"] == "100 h" and fields["Grade boutique"] == "VIP"
    embed = await run(infos.joueur, "Cache")
    assert "Profil privé" in embed.description
    embed = await run(infos.warzone)
    assert "__**Amour**__" in embed.description and "Bateau" in embed.fields[0].value
    embed = await run(infos.competences, None, False)
    assert embed.fields[0].name == "Mineur (1)" and "**Bob** 2e" in embed.fields[0].value
    embed = await run(infos.competences, "miner", True)
    assert "**Bob** ⭐" in embed.description
    embed = await run(infos.marche, None)
    assert embed.description.splitlines()[0] == "📈 **Avoine** · 2 000 $"
    embed = await run(infos.marche, "blé")
    assert embed.title == "🌾 Blé · 400 $" and embed.fields[0].value == "+20 $"
    assert "inconnue" in await run(infos.marche, "patate")
    embed = await run(infos.hdv, "diamant")
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🏪 Diamant" and fields["Prix affiché"] == "130 $" and fields["Variation 7 j"] == "+2.5 %"
    assert "Bloc de diamant" in fields["Autres résultats"]
    assert "Aucun objet" in await run(infos.hdv, "licorne")
    embed = await run(infos.loteries)
    assert embed.fields[0].name == "Loterie p30" and "Gagnant_1" in embed.fields[0].value
    embed = await run(infos.entreprises_cmd, None, None)
    assert [f.name for f in embed.fields] == ["🟢 AFR"] and "132 664" in embed.description
    i = inter(guild, user)
    await infos.statut_ng.callback(infos, i)
    assert "🔴 Parcelles" in said(i).description
    i = inter(guild, user)
    await infos.rapport.callback(infos, i)
    assert said(i).title == "📰 Rapport du jour · Amour"
    assert await cog.store.get_value("ng_rapport:555") is None  # un aperçu change pas le prochain rapport


# ---------------------------------------------------------------- entreprises et membres
from plugins.nationsglory import entreprises  # noqa: E402


def test_enterprises_logic():
    members = {m["username"].lower(): m for m in MEMBERS}
    ours = entreprises.country_enterprises(ENTERPRISES, members, ["autre"])
    assert [r["name"] for r in ours] == ["AFR", "Autre"]  # AFR par son propriétaire, Autre ajoutée à la main
    tot = entreprises.totals(ours)
    assert tot["count"] == 2 and tot["turnover"] == 132669 and tot["by_type"] == {"farm": 1, "trader": 1}
    assert entreprises.rank(ENTERPRISES, "AFR") == (1, 2) and entreprises.rank(ENTERPRISES, "AFR", kind="farm") == (1, 1)
    assert entreprises.enterprise_events(None, ours) == []
    before = entreprises.snapshot(ours)
    after = [dict(ENTERPRISES[0], owner="Bob"), {"name": "Neuve", "type": "electric", "owner": "Joueur_1"}]
    lines = entreprises.enterprise_events(before, after)
    assert "🏢 **Joueur_1** a fondé l'entreprise **Neuve** (⚡ Électricité)!" in lines
    assert any("**Autre**" in x and "plus au pays" in x for x in lines)
    assert "🔁 **AFR** change de propriétaire : Tristan_QC → **Bob**." in lines
    assert entreprises.type_label("inconnu") == "🏢 Inconnu"


def test_enterprise_activity_fields():
    farm = [{"kind": "farm", "farm": {"history": {"wheat": {"2026-11-21": 10, "2026-11-22": 30}}, "top": {"wheat": 40},
                                      "today_collected": 30, "total_collected": 40}}]
    name, value = entreprises.operations_fields(farm)[0]
    assert name == "🌾 Récoltes" and "**30**" in value and "Blé 40" in value
    trader = [{"kind": "trader", "trader": {"history": {"2026-11-22": 120}, "today_bp": 120, "total_generated": 9000,
                                            "total_investment": 50000, "investors_count": 4}}]
    assert "+1.20 %" in entreprises.operations_fields(trader)[0][1]
    energy = [{"kind": "electricity", "base_price": 12, "total_generated": 900,
               "history_generated": {"2026-11-22": 40}, "history_collected": {"2026-11-22": 30}}]
    assert entreprises.energy_fields(energy)[0][0] == "⚡ Électricité"
    parcels = [{"parcel": "P1", "status": "to_rent", "price": 100, "price_unit": "per_day"}, {"parcel": "P2", "status": "rented"}]
    title, value = entreprises.parcels_field(parcels)
    assert title == "🏠 Parcelles (2)" and "P1 : à louer 100 $/jour" in value and entreprises.parcels_field([]) is None


def test_member_logic():
    entry = insights.profile_entry(PROFILE, "jade", None, 5.0)
    assert entry == ["2026-11-01", 5.0, 3000, 150, "VIP", "2025-01-02", 6000]
    assert insights.profile_entry({"private": True}, "jade", None, 1.0) == ["private", 1.0]
    assert insights.profile_entry(None, "jade", "2026-11-20", 1.0) == ["2026-11-20", 1.0]
    members = {"bob": {"username": "Bob", "role": "OFFICER"}, "zed": {"username": "Zed", "role": "RECRUIT"}}
    rows = insights.member_rows(members, {"bob": entry}, {"zed"}, {"bob": [ENTERPRISES[0]]}, date(2026, 11, 22))
    by = {r["name"]: r for r in rows}
    assert by["Bob"]["days"] == 21 and by["Bob"]["playtime"] == 3000 and by["Zed"]["online"]
    assert [r["name"] for r in insights.sort_members(rows, "activite")] == ["Zed", "Bob"]
    assert [r["name"] for r in insights.sort_members(rows, "temps")] == ["Bob", "Zed"]
    assert insights.member_line(by["Bob"]) == "😴 **Bob** · il y a 21 j · 50 h · 🏢"
    assert insights.member_line(by["Zed"]) == "🟢 **Zed** · en jeu"


async def test_enterprise_and_member_commands(host, monkeypatch):
    bot, cog, _ = host
    infos = bot.get_cog("NGInfos")
    st, guild = await _setup_guild(bot, cog)
    user = add_member(guild, 42)
    monkeypatch.setattr(sys.modules["plugins.nationsglory.cog"], "paris_today", lambda: date(2026, 11, 22))

    async def run(command, *args):
        i = inter(guild, user)
        await command.callback(infos, i, *args)
        return i

    i = await run(infos.entreprise, "AFR")
    embed = i.followup.send.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🏢 AFR" and "Tristan_QC (Empereur de Amour)" in fields["👑 Propriétaire"]
    assert fields["💸 14 derniers jours"].startswith("Net +12 000 $") and "1er sur" not in fields["🏆 Rang (chiffre d'affaires)"]
    assert "🌾 Récoltes" in fields and "📈 CA sur 30 jours" in fields
    i = await run(infos.entreprise, "afr")  # sans tenir compte de la casse
    assert i.followup.send.call_args.kwargs["embed"].title == "🏢 AFR"
    assert "trouve pas" in said(await run(infos.entreprise, "Licorne Inc"))

    embed = said(await run(infos.entreprises_cmd, None, "farm"))
    assert embed.title.startswith("🏆 🌾 Ferme") and "⭐ **AFR**" in embed.description
    choices = await infos._enterprise_choices(inter(guild, user), "a")
    assert choices[0].name == "⭐ AFR" and choices[0].value == "AFR"

    for _ in range(8):
        cog._last_activity.clear()
        await cog.check_activity(st)
    logins = await cog._logins(st)
    assert logins["bob"][:3] == ["2026-11-01", logins["bob"][1], 3000]  # lu dans sa fiche publique

    embed = said(await run(infos.membre, "bob"))
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "⭐ Bob" and "**Conseiller** de **Amour**" in embed.description
    assert fields["⏱️ Temps de jeu sur Jade"] == "50 h" and fields["💎 Grade boutique"] == "VIP"
    assert "Mineur 2e" in fields["🛠️ Tops compétences (cette semaine)"]
    assert "pas membre" in said(await run(infos.membre, "Inconnu_9"))
    assert [c.value for c in await infos._member_choices(inter(guild, user), "jou")][:2] == ["Joueur_0", "Joueur_1"]

    embed = said(await run(infos.membres, "grade", 1))
    assert embed.title == "👥 Amour · 7 membres · 🟢 2 en jeu" and "__👑 Empereur__" in embed.description
    assert "🟢 **Tristan_QC** · en jeu" in embed.description and "🏢" in embed.description
    embed = said(await run(infos.classement_membres, "temps"))
    assert embed.description.startswith("🥇 **Bob** · 50 h")
    embed = said(await run(infos.classement_membres, "entreprises"))
    assert embed.description.startswith("🥇 **Tristan_QC** · 132 664 $")


async def test_enterprise_announcements(host):
    bot, cog, _ = host
    st, guild = await _setup_guild(bot, cog, {"salon_annonces_id": 70})
    chan = SimpleNamespace(id=70, send=AsyncMock())
    guild.channels[70] = chan
    assert await cog.run_enterprises(guild, st) == []  # 1er tour
    ENTERPRISES.append({"name": "SakhaElec", "type": "electric", "owner": "Bob", "is_open": True, "members_count": 5, "turnover": 10})
    for c in cog.clients.values():
        c._cached.pop("enterprises", None)
    try:
        lines = await cog.run_enterprises(guild, st)
    finally:
        ENTERPRISES.pop()
    assert lines == ["🏢 **Bob** a fondé l'entreprise **SakhaElec** (⚡ Électricité)!"] and chan.send.await_count == 1
