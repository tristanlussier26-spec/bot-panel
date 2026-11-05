"""Le panel au complet : connexion Discord, droits, fichiers, variables, vrais conteneurs Docker, constructeur."""
import asyncio
import io
import json
import os
import zipfile

import aiodocker
import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from botpanel import builder
from botpanel.config import Settings
from botpanel.web import Panel
from tests.fake_discord import GUILD_ID, FakeDiscord

ADMIN, FRIEND, STRANGER = "111111111111111111", "111111111111111112", "111111111111111113"
TEST_IMAGE = os.getenv("BOTPANEL_TEST_IMAGE", "botpanel-test/python:3")
H = {"X-Panel": "1"}


async def docker_ready() -> bool:
    try:
        async with aiodocker.Docker() as d:
            await d.images.inspect(TEST_IMAGE)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def env(tmp_path):
    fake = FakeDiscord()
    discord = TestServer(fake.app())
    await discord.start_server()
    settings = Settings(
        data_dir=tmp_path / "data", host_data_dir=None, public_url="http://127.0.0.1",
        discord_client_id="cid", discord_client_secret="secret", admin_ids={int(ADMIN)},
        discord_api=str(discord.make_url("/api/v10")), bot_network="botpanel-test",
        default_max_bots=2, default_max_ram=512, default_max_disk=1,
    )
    panel = Panel(settings)
    server = TestServer(panel.app())
    clients = []

    async def client(user: str | None = None) -> TestClient:
        c = TestClient(server)
        await c.start_server()
        clients.append(c)
        if user:
            await login(c, user)
        return c

    yield panel, client, fake
    for c in clients:
        await c.close()
    await discord.close()
    if panel.docker_ok:
        async with aiodocker.Docker() as d:
            for c in await d.containers.list(all=True, filters=json.dumps({"label": ["botpanel=1"]})):
                await c.delete(force=True)


async def login(c: TestClient, user: str):
    r = await c.get("/auth/login", allow_redirects=False)
    assert r.status == 302 and "discord.com/oauth2/authorize" in r.headers["Location"]
    state = r.cookies["bp_oauth_state"].value
    return await c.get(f"/auth/callback?code=code-{user[-1]}&state={state}", allow_redirects=False)


async def test_login_roles_and_csrf(env):
    panel, client, _ = env
    anon = await client()
    assert (await anon.get("/api/me")).status == 401
    r = await anon.get("/", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/login"
    # Mauvais « state » : refusé
    r = await anon.get("/auth/callback?code=code-1&state=pirate", allow_redirects=False)
    assert r.headers["Location"] == "/login?etat=erreur"

    admin = await client()
    r = await login(admin, ADMIN)
    assert r.headers["Location"] == "/"
    me = await (await admin.get("/api/me")).json()
    assert me["role"] == "admin" and me["name"] == "Admin"

    friend = await client()
    r = await login(friend, FRIEND)
    assert r.headers["Location"] == "/login?etat=attente"  # en attente d'approbation
    assert (await friend.get("/api/me")).status == 401
    assert (await friend.get("/api/admin/users")).status == 401

    users = await (await admin.get("/api/admin/users")).json()
    assert {u["id"]: u["role"] for u in users}[FRIEND] == "pending"
    # Sans l'en-tête anti-CSRF : refusé
    assert (await admin.patch(f"/api/admin/users/{FRIEND}", json={"role": "user"})).status == 403
    assert (await admin.patch(f"/api/admin/users/{FRIEND}", json={"role": "user"}, headers=H)).status == 200
    r = await login(friend, FRIEND)
    assert r.headers["Location"] == "/"
    me = await (await friend.get("/api/me")).json()
    assert me["role"] == "user"
    assert (await friend.get("/api/admin/users")).status == 403
    # L'admin peut pas s'enlever son rôle
    r = await admin.patch(f"/api/admin/users/{ADMIN}", json={"role": "user"}, headers=H)
    assert r.status == 400
    # Bloquer coupe la session tout de suite
    await admin.patch(f"/api/admin/users/{FRIEND}", json={"role": "blocked"}, headers=H)
    assert (await friend.get("/api/me")).status == 401
    r = await login(friend, FRIEND)
    assert r.headers["Location"] == "/login?etat=bloque"
    # En-têtes de sécurité
    r = await admin.get("/api/me")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


async def test_limits_isolation_files_env(env):
    panel, client, _ = env
    admin = await client(ADMIN)
    await admin.post("/api/admin/users", json={"id": FRIEND}, headers=H)
    friend = await client(FRIEND)
    stranger_admin_added = await admin.post("/api/admin/users", json={"id": STRANGER}, headers=H)
    assert stranger_admin_added.status == 200
    other = await client(STRANGER)

    r = await friend.post("/api/bots", json={"name": "Gros", "language": "python", "ram_mb": 1024}, headers=H)
    assert r.status == 400 and "RAM" in (await r.json())["error"]
    r = await friend.post("/api/bots", json={"name": "Perso", "language": "custom", "image": "alpine:3", "startup_cmd": "sh"}, headers=H)
    assert r.status == 400 and "pas permise" in (await r.json())["error"]
    r = await friend.post("/api/bots", json={"name": "Mon bot", "language": "python", "ram_mb": 256}, headers=H)
    assert r.status == 201
    bot = await r.json()
    bid = bot["id"]
    assert bot["image"] == panel.settings.python_image
    # Bot d'exemple créé, appartenant à l'utilisateur 1000
    files = await (await friend.get(f"/api/bots/{bid}/files")).json()
    assert {e["name"] for e in files["entries"]} == {"main.py", "requirements.txt"}
    assert (panel.settings.bots_dir / bid / "main.py").stat().st_uid == 1000
    await friend.post("/api/bots", json={"name": "Deux", "language": "node", "ram_mb": 128}, headers=H)
    r = await friend.post("/api/bots", json={"name": "Trois", "language": "node", "ram_mb": 64}, headers=H)
    assert r.status == 400 and "2 bot(s) sur 2" in (await r.json())["error"]

    # Un autre compte voit pas ce bot ; l'admin, oui
    assert (await other.get(f"/api/bots/{bid}")).status == 404
    assert (await other.get(f"/api/bots/{bid}/files")).status == 404
    assert (await admin.get(f"/api/bots/{bid}")).status == 200
    assert len(await (await admin.get("/api/bots?tous=1")).json()) == 2

    # Fichiers
    base = f"/api/bots/{bid}/files"
    r = await friend.put(f"{base}/content", json={"path": "cogs/hello.py", "content": "print('salut')"}, headers=H)
    assert r.status == 200
    assert (await (await friend.get(f"{base}/content?path=cogs/hello.py")).json())["content"] == "print('salut')"
    for bad in ("../../panel.db", "/../secret.key"):
        r = await friend.get(f"{base}/content?path={bad}")
        assert r.status == 400, bad
    r = await friend.get(f"{base}/download?path=cogs/hello.py")
    assert r.status == 200 and await r.read() == b"print('salut')"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("projet/bot.py", "x = 1\n")
    form = FormData()
    form.add_field("path", "")
    form.add_field("files", buf.getvalue(), filename="projet.zip")
    r = await friend.post(f"{base}/upload", data=form, headers=H)
    assert (await r.json())["saved"] == ["projet.zip"]
    r = await friend.post(f"{base}/extract", json={"path": "projet.zip"}, headers=H)
    assert (await r.json())["count"] == 1
    await friend.post(f"{base}/rename", json={"from": "projet/bot.py", "to": "projet/main.py"}, headers=H)
    await friend.post(f"{base}/delete", json={"paths": ["projet.zip"]}, headers=H)
    names = {e["name"] for e in (await (await friend.get(f"{base}?path=projet")).json())["entries"]}
    assert names == {"main.py"}
    # Quota disque (1 Mo pour ce compte)
    form = FormData()
    form.add_field("files", b"\0" * (2 * 1024 * 1024), filename="gros.bin")
    r = await friend.post(f"{base}/upload", data=form, headers=H)
    assert r.status == 400 and "espace disque" in (await r.json())["error"]

    # Variables : le secret est jamais renvoyé, et gardé si on le retouche pas
    r = await friend.put(f"/api/bots/{bid}/env", json={"variables": [
        {"key": "DISCORD_TOKEN", "value": "bon-jeton", "secret": True}, {"key": "PREFIX", "value": "!", "secret": False}]}, headers=H)
    assert r.status == 200
    got = await (await friend.get(f"/api/bots/{bid}/env")).json()
    assert {"key": "DISCORD_TOKEN", "secret": True, "value": None, "set": True} in got
    await friend.put(f"/api/bots/{bid}/env", json={"variables": [
        {"key": "DISCORD_TOKEN", "value": None, "secret": True}, {"key": "PREFIX", "value": "?", "secret": False}]}, headers=H)
    stored = {e["key"]: e["value"] for e in await panel.db.get_env(bid)}
    assert stored == {"DISCORD_TOKEN": "bon-jeton", "PREFIX": "?"}
    r = await friend.put(f"/api/bots/{bid}/env", json={"variables": [{"key": "PATH", "value": "/x"}]}, headers=H)
    assert r.status == 400
    r = await friend.put(f"/api/bots/{bid}/env", json={"variables": [{"key": "1MAUVAIS", "value": "x"}]}, headers=H)
    assert r.status == 400

    # Suppression : il faut écrire le nom
    r = await friend.delete(f"/api/bots/{bid}", json={"confirm": "mauvais"}, headers=H)
    assert r.status == 400
    r = await friend.delete(f"/api/bots/{bid}", json={"confirm": "Mon bot"}, headers=H)
    assert r.status == 200 and not (panel.settings.bots_dir / bid).exists()


async def test_real_container_console_and_watchdog(env):
    if not await docker_ready():
        pytest.skip(f"Docker ou l'image {TEST_IMAGE} indisponible")
    panel, client, _ = env
    admin = await client(ADMIN)
    r = await admin.post("/api/bots", json={"name": "Écho", "language": "python", "image": TEST_IMAGE, "starter": False,
                                            "ram_mb": 128, "auto_restart": True}, headers=H)
    bid = (await r.json())["id"]
    base = f"/api/bots/{bid}"
    await admin.put(f"{base}/files/content", json={"path": "main.py", "content": (
        "import os, sys\n"
        "print('prêt, jeton :', 'oui' if os.getenv('DISCORD_TOKEN') else 'non', flush=True)\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if line == 'plante':\n"
        "        sys.exit(3)\n"
        "    print('reçu :', line, flush=True)\n")}, headers=H)
    await admin.put(f"{base}/env", json={"variables": [{"key": "DISCORD_TOKEN", "value": "x", "secret": True}]}, headers=H)

    r = await admin.post(f"{base}/power", json={"action": "start"}, headers=H)
    assert r.status == 200, await r.text()
    assert (await r.json())["status"]["state"] == "running"

    # Origine étrangère refusée pour la console
    r = await admin.get(f"{base}/console", headers={"Origin": "http://pirate.example", "Upgrade": "websocket", "Connection": "Upgrade",
                                                    "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
    assert r.status == 403

    ws = await admin.ws_connect(f"{base}/console", headers={"Origin": "http://127.0.0.1"})
    seen: list[str] = []

    async def until(pred, timeout=20):
        async def loop():
            while True:
                msg = await ws.receive_json()
                if msg["type"] == "history":
                    seen.extend(msg["lines"])
                elif msg["type"] == "line":
                    seen.append(msg["text"])
                if pred():
                    return
        try:
            await asyncio.wait_for(loop(), timeout)
        except TimeoutError:
            raise AssertionError(f"attendu pas vu ; reçu : {seen[-15:]}") from None

    await until(lambda: any("prêt, jeton : oui" in s for s in seen))
    await ws.send_json({"type": "command", "text": "bonjour"})
    await until(lambda: any("reçu : bonjour" in s for s in seen))

    # Plantage : la surveillance redémarre une fois, puis arrête si ça replante tout de suite
    await ws.send_json({"type": "command", "text": "plante"})
    await asyncio.sleep(1.5)
    await panel._watch_once()
    await until(lambda: any("Redémarrage automatique" in s for s in seen))
    await until(lambda: sum("prêt, jeton" in s for s in seen) >= 2)
    await ws.send_json({"type": "command", "text": "plante"})
    await asyncio.sleep(1.5)
    await panel._watch_once()
    await until(lambda: any("plante en boucle" in s for s in seen))
    bot = await panel.db.get_bot(bid)
    assert bot["wanted_running"] == 0
    await ws.close()

    # Redémarrer puis arrêter à la main
    r = await admin.post(f"{base}/power", json={"action": "restart"}, headers=H)
    assert (await r.json())["status"]["state"] == "running"
    r = await admin.post(f"{base}/power", json={"action": "stop"}, headers=H)
    assert (await r.json())["status"]["state"] == "offline"
    await panel._watch_once()  # voulu arrêté : rien ne redémarre
    assert (await panel.runtime.status(bid))["state"] == "offline"


async def test_builder_endpoints(env):
    panel, client, fake = env
    admin = await client(ADMIN)
    r = await admin.post("/api/bots", json={"name": "Constructeur", "language": "python"}, headers=H)
    bid = (await r.json())["id"]
    sources = await (await admin.get("/api/builder/sources")).json()
    assert sources == [{"id": bid, "name": "Constructeur", "has_token": False}]
    r = await admin.get(f"/api/builder/sources/{bid}")
    assert r.status == 400 and "DISCORD_TOKEN" in (await r.json())["error"]
    await admin.put(f"/api/bots/{bid}/env", json={"variables": [{"key": "DISCORD_TOKEN", "value": "bon-jeton", "secret": True}]}, headers=H)
    data = await (await admin.get(f"/api/builder/sources/{bid}")).json()
    assert data["guilds"][0]["id"] == GUILD_ID and "permissions=8" in data["invite"]

    r = await admin.post("/api/builder/validate", json={"content": "roles: [{name: A, permissions: [voler]}]"}, headers=H)
    assert r.status == 400 and "voler" in (await r.json())["error"]
    plan = await (await admin.post("/api/builder/plan", json={"bot_id": bid, "guild_id": GUILD_ID, "content": builder.EXAMPLE}, headers=H)).json()
    assert plan["counts"]["create"] > 10 and plan["counts"]["delete"] == 0
    job = await (await admin.post("/api/builder/apply", json={"bot_id": bid, "guild_id": GUILD_ID, "content": builder.EXAMPLE}, headers=H)).json()
    for _ in range(100):
        job = await (await admin.get(f"/api/builder/jobs/{job['id']}")).json()
        if job["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert job["status"] == "done" and all(s["state"] == "ok" for s in job["steps"]), job
    assert any(c["name"] == "règles" for c in fake.channels)
    exported = (await (await admin.post("/api/builder/export", json={"bot_id": bid, "guild_id": GUILD_ID}, headers=H)).json())["content"]
    plan = await (await admin.post("/api/builder/plan", json={"bot_id": bid, "guild_id": GUILD_ID, "content": exported}, headers=H)).json()
    assert plan["actions"] == []
    imported = await (await admin.post("/api/builder/import", json={"bot_id": bid, "code": "abc123"}, headers=H)).json()
    assert "Chef" in imported["content"]
    tid = (await (await admin.post("/api/builder/templates", json={"name": "Copie", "content": exported}, headers=H)).json())["id"]
    assert (await (await admin.get(f"/api/builder/templates/{tid}")).json())["content"] == exported
    # Le modèle d'un autre compte est invisible
    await admin.post("/api/admin/users", json={"id": FRIEND}, headers=H)
    friend = await client(FRIEND)
    assert (await friend.get(f"/api/builder/templates/{tid}")).status == 404
    r = await friend.post("/api/builder/plan", json={"bot_id": bid, "guild_id": GUILD_ID, "content": exported}, headers=H)
    assert r.status == 404  # peut pas utiliser le bot (et le jeton) d'un autre
