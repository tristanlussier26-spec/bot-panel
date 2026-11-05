"""Constructeur de serveurs : modèle, plan, application (contre un faux Discord), export, import."""
import pytest
import yaml
from aiohttp.test_utils import TestServer

from botpanel import builder as b
from tests.fake_discord import GUILD_ID, FakeDiscord


async def _api(fake: FakeDiscord, token: str = "bon-jeton"):
    server = TestServer(fake.app())
    await server.start_server()
    api = b.DiscordAPI(token, base=str(server.make_url("/api/v10")))
    await api.__aenter__()
    return server, api


async def _apply(api, text, mirror=False, allow_delete=False):
    state = await b.load_state(api, GUILD_ID)
    plan = b.build_plan(b.parse_template(text), state, mirror=mirror)
    job = b.new_job(1, GUILD_ID, plan, allow_delete)
    await b.apply_plan(api, state, plan, job)
    return plan, job


def test_permission_bits_match_discord_py():
    discord = pytest.importorskip("discord")
    for name, bit in b.PERMISSIONS.items():
        assert discord.Permissions.VALID_FLAGS[name] == 1 << bit, name
    assert b.perms_to_int(["read_messages", "send_messages"], "x") == discord.Permissions(view_channel=True, send_messages=True).value


def test_template_errors_are_clear():
    cases = {
        "roles: [{name: A, permissions: [voler]}]": "permission inconnue « voler »",
        "roles: [A, a]": "deux fois",
        "categories: [{name: X, channels: [{name: y, type: lol}]}]": "type « lol » inconnu",
        "roles: [{name: A, color: rouge}]": "couleur invalide",
        "salons: []": "Clé inconnue « salons »",
        "roles: [\n  - name: A\n   bad": "YAML invalide (ligne",
        "categories: [{name: X, channels: [{name: v, type: voice, user_limit: 500}]}]": "entre 0 et 99",
    }
    for text, expected in cases.items():
        with pytest.raises(b.TemplateError, match=expected.replace("(", r"\(")):
            b.parse_template(text)


def test_category_permissions_flow_to_channels():
    t = b.parse_template("""
categories:
  - name: Staff
    private: true
    overwrites: {Modo: {allow: [view_channel]}}
    channels:
      - staff
      - {name: annonces-staff, overwrites: {Modo: {allow: [view_channel, send_messages]}}}
      - {name: ouvert, overwrites: {"@everyone": {}}}
""")
    staff, annonces, ouvert = t.categories[0].channels
    view, send = 1 << 10, 1 << 11
    assert staff.overwrites == {"@everyone": (0, view), "Modo": (view, 0)}
    assert annonces.overwrites["Modo"] == (view | send, 0)
    assert ouvert.overwrites["@everyone"] == (0, 0)  # neutre dans ce salon


async def test_apply_example_then_plan_is_empty():
    fake = FakeDiscord()
    server, api = await _api(fake)
    try:
        plan, job = await _apply(api, b.EXAMPLE)
        assert job.status == "done", job.public()
        assert all(s["state"] == "ok" for s in job.steps), [s for s in job.steps if s["state"] != "ok"]
        assert fake.rate_limited_once  # le 429 a été attendu puis réessayé
        names = [r["name"] for r in sorted(fake.roles, key=lambda r: -r["position"])]
        assert names == ["MonBot", "Admin", "Modo", "Membre", "Nouveau", "@everyone"]
        cats = {c["name"]: c for c in fake.channels if c["type"] == 4}
        assert list(sorted(cats, key=lambda n: cats[n]["position"])) == ["Bienvenue", "Discussion", "Staff"]
        staff_chan = next(c for c in fake.channels if c["name"] == "staff")
        assert staff_chan["parent_id"] == cats["Staff"]["id"]
        modo = next(r for r in fake.roles if r["name"] == "Modo")
        ow = {o["id"]: (int(o["allow"]), int(o["deny"])) for o in staff_chan["permission_overwrites"]}
        assert ow == {GUILD_ID: (0, 1 << 10), modo["id"]: (1 << 10, 0)}
        general = [c for c in fake.channels if c["name"] == "général"]
        assert len(general) == 1 and general[0]["parent_id"] == cats["Discussion"]["id"]  # l'existant est réutilisé
        assert general[0]["topic"] == "On jase de tout"
        assert fake.guild["name"] == "Mon serveur"
        # Deuxième passage : rien à faire
        state = await b.load_state(api, GUILD_ID)
        again = b.build_plan(b.parse_template(b.EXAMPLE), state)
        assert again.actions == [], [a.label for a in again.actions]
    finally:
        await api.__aexit__()
        await server.close()


async def test_export_round_trip_and_mirror():
    fake = FakeDiscord()
    server, api = await _api(fake)
    try:
        await _apply(api, b.EXAMPLE)
        exported = await b.export_guild(api, GUILD_ID)
        assert "Admin" in exported and "règles" in exported
        state = await b.load_state(api, GUILD_ID)
        assert b.build_plan(b.parse_template(exported), state).actions == []
        # Miroir : on retire « Jeux » et le rôle « Nouveau » (et ses permissions) du modèle
        data = yaml.safe_load(exported)
        data["roles"] = [r for r in data["roles"] if r["name"] != "Nouveau"]
        for cat in data["categories"]:
            cat["channels"] = [c for c in cat["channels"] if c["name"] != "Jeux"]
            cat.get("overwrites", {}).pop("Nouveau", None)
            for c in cat["channels"]:
                c.get("overwrites", {}).pop("Nouveau", None)
        text = yaml.safe_dump(data, allow_unicode=True)
        state = await b.load_state(api, GUILD_ID)
        plan = b.build_plan(b.parse_template(text), state, mirror=True)
        labels = [a.label for a in plan.actions if a.destructive]
        assert labels == ["Supprimer le rôle « Nouveau »", "Supprimer le salon « Jeux »"], labels
        # Sans confirmation : rien de supprimé
        _, job = await _apply(api, text, mirror=True, allow_delete=False)
        assert any(c["name"] == "Jeux" for c in fake.channels)
        _, job = await _apply(api, text, mirror=True, allow_delete=True)
        assert job.status == "done", job.public()
        assert not any(c["name"] == "Jeux" for c in fake.channels)
        assert not any(r["name"] == "Nouveau" for r in fake.roles)
        assert any(r["name"] == "MonBot" for r in fake.roles)  # le rôle du bot est jamais supprimé
    finally:
        await api.__aexit__()
        await server.close()


async def test_import_discord_new_template():
    fake = FakeDiscord()
    server, api = await _api(fake)
    try:
        text = await b.import_discord_template(api, "https://discord.new/abc123")
        t = b.parse_template(text)
        assert [r.name for r in t.roles] == ["Chef", "Joueur"]
        assert t.categories[1].name == "Privé" and t.categories[1].channels[0].overwrites["Chef"] == (1 << 10, 0)
        assert "overwrites" not in yaml.safe_load(text)["categories"][1]["channels"][0]  # synchronisé : pas répété
        _, job = await _apply(api, text)
        assert job.status == "done" and all(s["state"] == "ok" for s in job.steps), job.public()
        with pytest.raises(b.DiscordError, match="introuvable"):
            await b.import_discord_template(api, "zzz999")
    finally:
        await api.__aexit__()
        await server.close()


async def test_limited_bot_gets_blocked_actions_not_crash():
    fake = FakeDiscord(admin=False)
    server, api = await _api(fake)
    try:
        state = await b.load_state(api, GUILD_ID)
        plan = b.build_plan(b.parse_template(b.EXAMPLE), state)
        blocked = {a.label: a.blocked for a in plan.actions if a.blocked}
        assert "Créer le rôle « Admin »" in blocked and "pas" in blocked["Créer le rôle « Admin »"]
        assert plan.warnings  # conseil : Administrateur
        bad_server, bad_api = await _api(FakeDiscord(), token="mauvais")
        with pytest.raises(b.DiscordError, match="Jeton du bot invalide"):
            await b.load_state(bad_api, GUILD_ID)
        await bad_api.__aexit__()
        await bad_server.close()
    finally:
        await api.__aexit__()
        await server.close()


def test_unknown_role_in_overwrites():
    t = b.parse_template("categories: [{name: X, overwrites: {Fantome: {allow: [view_channel]}}, channels: [a]}]")
    state = b.GuildState(guild={"id": "1", "name": "s"}, roles=[{"id": "1", "name": "@everyone", "permissions": "0", "position": 0}],
                         channels=[], bot_id="9", bot_top=5, bot_perms=8)
    with pytest.raises(b.TemplateError, match="Fantome"):
        b.build_plan(t, state)
