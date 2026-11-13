"""Onglet « Plugins » : catalogue (faux GitHub), téléversement d'un .zip, zips piégés, variables, retrait."""
import hashlib
import io
import json
import zipfile

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestServer

from botpanel import plugins
from tests.test_web import ADMIN, FRIEND, STRANGER, H, env  # noqa: F401  (fixture)

MANIFEST = {
    "format": 1, "id": "salut", "name": "Salut", "version": "1.0.0", "description": "Dit bonjour.", "author": "moi",
    "runtime": "discord.py",
    "variables": [{"key": "SALUT_CLE", "label": "Clé", "secret": True, "required": True},
                  {"key": "SALUT_MOT", "label": "Mot", "secret": False}],
    "pages": [{"id": "salut", "section": "salut", "title": "Salut", "fields": [{"key": "active", "label": "Actif", "type": "bool"}]}],
}


def make_zip(files: dict[str, bytes | str], *, folder: str = "", symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in files.items():
            z.writestr(folder + name, data)
        if symlink:
            info = zipfile.ZipInfo(folder + symlink)
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def plugin_zip(version="1.0.0", **extra) -> bytes:
    manifest = {**MANIFEST, "version": version}
    return make_zip({"plugin.json": json.dumps(manifest), "__init__.py": "async def setup(bot):\n    pass\n",
                     "requirements.txt": "requests>=2.31\n# commentaire\n", **extra})


@pytest.fixture
async def catalog(env):  # noqa: F811
    panel, _, _ = env
    state = {"zip": plugin_zip(), "sha": None, "redirect": None}

    async def index(request):
        if state["redirect"]:
            raise web.HTTPFound(state["redirect"])
        data = state["zip"]
        entry = {**{k: MANIFEST[k] for k in ("id", "name", "description", "author", "runtime")}, "version": state.get("version", "1.0.0"),
                 "download": "dist/salut.zip", "sha256": state["sha"] or hashlib.sha256(data).hexdigest()}
        broken = {"id": "BRISÉ", "name": "x", "version": "1", "runtime": "discord.py", "download": "x", "sha256": "z"}
        return web.json_response({"format": 1, "plugins": [entry, broken]})

    async def dist(request):
        return web.Response(body=state["zip"], content_type="application/zip")

    app = web.Application()
    app.router.add_get("/catalogue/index.json", index)
    app.router.add_get("/catalogue/dist/salut.zip", dist)
    server = TestServer(app)
    await server.start_server()
    panel.catalog = plugins.Catalog(str(server.make_url("/catalogue/index.json")), max_bytes=5 * 1024 * 1024)
    yield state
    await server.close()


async def test_manifest_and_zip_checks():
    with pytest.raises(plugins.PluginError, match="Format"):
        plugins.validate_manifest({**MANIFEST, "format": 2})
    with pytest.raises(plugins.PluginError, match="id"):
        plugins.validate_manifest({**MANIFEST, "id": "../x"})
    with pytest.raises(plugins.PluginError, match="réservé"):
        plugins.validate_manifest({**MANIFEST, "variables": [{"key": "BOT_ID", "label": "x"}]}, reserved_env={"BOT_ID"})
    with pytest.raises(plugins.PluginError, match="runtime"):
        plugins.validate_manifest({**MANIFEST, "runtime": "node"})
    big = 1024 * 1024
    manifest, files = plugins.read_zip(plugin_zip(), max_bytes=big)
    assert manifest["id"] == "salut" and set(files) == {"plugin.json", "__init__.py", "requirements.txt"}
    # zip fait en compressant le dossier : le dossier du haut est enlevé
    manifest, files = plugins.read_zip(make_zip({"plugin.json": json.dumps(MANIFEST), "__init__.py": "", "a/b.py": ""},
                                                folder="salut-main/"), max_bytes=big)
    assert set(files) == {"plugin.json", "__init__.py", "a/b.py"}
    bad = {
        "zip-slip": make_zip({"plugin.json": json.dumps(MANIFEST), "__init__.py": "", "../../evil.py": ""}),
        "binaire": plugin_zip(**{"lib.so": b"\x7fELF"}),
        "lien": make_zip({"plugin.json": json.dumps(MANIFEST), "__init__.py": ""}, symlink="passwd.txt"),
        "pip": plugin_zip(**{"requirements.txt": "--index-url https://pirate.example/simple\n"}),
        "pip-url": plugin_zip(**{"requirements.txt": "paquet @ https://pirate.example/p.whl\n"}),
        "sans init": make_zip({"plugin.json": json.dumps(MANIFEST)}),
        "sans manifeste": make_zip({"__init__.py": ""}),
        "pas un zip": b"bonjour",
    }
    for name, data in bad.items():
        with pytest.raises(plugins.PluginError):
            plugins.read_zip(data, max_bytes=big)
            pytest.fail(name)
    with pytest.raises(plugins.PluginError, match="trop gros"):
        plugins.read_zip(plugin_zip(**{"gros.txt": "x" * 5000}), max_bytes=4000)
    assert plugins.version_key("1.10.0") > plugins.version_key("1.9.3") > plugins.version_key("1.9.3-beta")


async def test_catalog_install_update_variables_remove(env, catalog):  # noqa: F811
    panel, client, _ = env
    admin = await client(ADMIN)
    await admin.post("/api/admin/users", json={"id": FRIEND}, headers=H)
    friend = await client(FRIEND)
    bot = await (await friend.post("/api/bots", json={"name": "Mon bot", "language": "python", "ram_mb": 256}, headers=H)).json()
    bid = bot["id"]

    data = await (await friend.get(f"/api/bots/{bid}/plugins")).json()
    assert data["installed"] == [] and [c["id"] for c in data["catalog"]] == ["salut"]  # l'entrée brisée est ignorée
    assert data["catalog_error"] is None and data["host"] is False

    r = await friend.post(f"/api/bots/{bid}/plugins/install", json={"id": "salut"}, headers=H)
    assert r.status == 200, await r.text()
    files = panel.files(bid)
    assert files.path("plugins/salut/__init__.py").is_file() and files.path("plugins/salut/requirements.txt").is_file()
    assert not files.path("plugins/.salut.nouveau").exists()
    data = await (await friend.get(f"/api/bots/{bid}/plugins")).json()
    p = data["installed"][0]
    assert (p["id"], p["status"], p["update"]) == ("salut", "unknown", None)
    assert [(v["key"], v["set"]) for v in p["variables"]] == [("SALUT_CLE", False), ("SALUT_MOT", False)]
    assert "pages" not in p

    # Le bot a démarré et dit ce qu'il a chargé (.botpanel/host.json)
    files.write_text(".botpanel/host.json", json.dumps({"format": 1, "runtime": "discord.py",
                                                         "plugins": {"salut": {"version": "1.0.0", "loaded": True, "error": None}}}))
    p = (await (await friend.get(f"/api/bots/{bid}/plugins")).json())["installed"][0]
    assert p["status"] == "loaded"

    # Variables du plugin : gardées avec les autres variables du bot (le secret reste caché)
    await friend.put(f"/api/bots/{bid}/env", json={"variables": [{"key": "DISCORD_TOKEN", "value": "jeton", "secret": True}]}, headers=H)
    r = await friend.put(f"/api/bots/{bid}/plugins/salut/variables",
                         json={"values": {"SALUT_CLE": "ngp_secret", "SALUT_MOT": "allo", "DISCORD_TOKEN": "pirate"}}, headers=H)
    assert r.status == 200
    env_vars = {e["key"]: e for e in await panel.db.get_env(bid)}
    assert env_vars["DISCORD_TOKEN"]["value"] == "jeton"  # pas une variable du plugin : pas touchée
    assert env_vars["SALUT_CLE"]["value"] == "ngp_secret" and env_vars["SALUT_CLE"]["secret"]
    p = (await (await friend.get(f"/api/bots/{bid}/plugins")).json())["installed"][0]
    assert p["variables"][0] == {**p["variables"][0], "set": True, "value": None}
    assert p["variables"][1]["value"] == "allo"
    await friend.put(f"/api/bots/{bid}/plugins/salut/variables", json={"values": {"SALUT_CLE": None, "SALUT_MOT": ""}}, headers=H)
    env_vars = {e["key"]: e["value"] for e in await panel.db.get_env(bid)}
    assert env_vars["SALUT_CLE"] == "ngp_secret" and "SALUT_MOT" not in env_vars  # None = gardé, vide = retiré

    # Nouvelle version dans le catalogue : « Mettre à jour », puis « Redémarre » tant que le bot a l'ancienne
    catalog["zip"], catalog["version"] = plugin_zip("1.1.0"), "1.1.0"
    p = (await (await friend.get(f"/api/bots/{bid}/plugins?refresh=1")).json())["installed"][0]
    assert p["update"] == "1.1.0"
    assert (await friend.post(f"/api/bots/{bid}/plugins/install", json={"id": "salut"}, headers=H)).status == 200
    p = (await (await friend.get(f"/api/bots/{bid}/plugins")).json())["installed"][0]
    assert (p["version"], p["update"], p["status"]) == ("1.1.0", None, "pending")

    # zip modifié après coup (le catalogue en cache a l'ancien sha256) : refusé, rien n'est touché
    catalog["zip"] = plugin_zip("1.1.0", **{"pirate.py": "import os"})
    r = await friend.post(f"/api/bots/{bid}/plugins/install", json={"id": "salut"}, headers=H)
    assert r.status == 400 and "sha256" in (await r.json())["error"]
    assert not files.path("plugins/salut/pirate.py").exists()
    catalog["sha"] = "0" * 64
    await friend.get(f"/api/bots/{bid}/plugins?refresh=1")
    r = await friend.post(f"/api/bots/{bid}/plugins/install", json={"id": "salut"}, headers=H)
    assert r.status == 400 and "sha256" in (await r.json())["error"]
    assert (await friend.post(f"/api/bots/{bid}/plugins/install", json={"id": "inconnu"}, headers=H)).status == 400

    # Les autres comptes touchent pas aux plugins de ce bot
    await admin.post("/api/admin/users", json={"id": STRANGER}, headers=H)
    other = await client(STRANGER)
    assert (await other.get(f"/api/bots/{bid}/plugins")).status == 404
    assert (await other.delete(f"/api/bots/{bid}/plugins/salut", headers=H)).status == 404

    assert (await friend.delete(f"/api/bots/{bid}/plugins/salut", headers=H)).status == 200
    assert not files.path("plugins/salut").exists()
    assert (await friend.delete(f"/api/bots/{bid}/plugins/salut", headers=H)).status == 400
    activity = [a["action"] for a in await panel.db.activity(bid)]
    assert {"Plugin installé", "Plugin mis à jour", "Plugin retiré", "Variables de plugin modifiées"} <= set(activity)


async def test_upload_and_broken_plugins(env, catalog):  # noqa: F811
    panel, client, _ = env
    admin = await client(ADMIN)
    bot = await (await admin.post("/api/bots", json={"name": "Bot", "language": "python"}, headers=H)).json()
    bid = bot["id"]

    def form(data: bytes) -> FormData:
        f = FormData()
        f.add_field("file", data, filename="plugin.zip", content_type="application/zip")
        return f

    r = await admin.post(f"/api/bots/{bid}/plugins/upload", data=form(plugin_zip()), headers=H)
    assert r.status == 200 and (await r.json())["name"] == "Salut"
    r = await admin.post(f"/api/bots/{bid}/plugins/upload",
                         data=form(make_zip({"plugin.json": json.dumps(MANIFEST), "__init__.py": "", "../x.py": ""})), headers=H)
    assert r.status == 400 and "Chemin invalide" in (await r.json())["error"]
    assert not panel.files(bid).path("x.py").exists()

    # Un plugin brisé dans le dossier (modifié à la main) : listé avec son erreur, et on peut le retirer
    panel.files(bid).write_text("plugins/brise/plugin.json", "{pas du json")
    data = await (await admin.get(f"/api/bots/{bid}/plugins")).json()
    broken = next(p for p in data["installed"] if p["id"] == "brise")
    assert broken["status"] == "error" and broken["variables"] == []
    assert (await admin.put(f"/api/bots/{bid}/plugins/brise/variables", json={"values": {}}, headers=H)).status == 404
    assert (await admin.delete(f"/api/bots/{bid}/plugins/brise", headers=H)).status == 200

    # Le catalogue redirige ailleurs : refusé ; catalogue désactivé : juste le téléversement
    catalog["redirect"] = "http://pirate.example/index.json"
    data = await (await admin.get(f"/api/bots/{bid}/plugins?refresh=1")).json()
    assert data["catalog"] == [] and "pas permis" in data["catalog_error"]
    panel.catalog = plugins.Catalog("", max_bytes=1)
    data = await (await admin.get(f"/api/bots/{bid}/plugins")).json()
    assert data["catalog_enabled"] is False and data["catalog"] == [] and data["catalog_error"] is None


def test_startup_installs_plugin_requirements():
    from botpanel.runtime import startup_command
    cmd = startup_command({"language": "python", "main_file": "bot.py"})
    assert "plugins/*/requirements.txt" in cmd and cmd.endswith("exec python3 -u bot.py")


def test_catalog_is_up_to_date():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "scripts/build_catalog.py", "--check"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
