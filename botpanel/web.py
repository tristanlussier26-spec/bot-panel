"""Serveur web du panel : API JSON, console en direct (WebSocket), surveillance des bots."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from botpanel import __version__, builder, gitops, kits, options, plugins
from botpanel.auth import Auth, avatar_url
from botpanel.config import Settings
from botpanel.db import Database
from botpanel.files import BotFiles, FileError
from botpanel.runtime import BotRuntime, RuntimeError_

log = logging.getLogger("botpanel.web")
STATIC = Path(__file__).parent / "static"

NAME_RE = re.compile(r"^[^\x00-\x1f]{1,40}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,200}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED_ENV = {"HOME", "PATH", "PYTHONUSERBASE", "PYTHONUNBUFFERED", "NPM_CONFIG_CACHE", "BOT_ID"}
TOKEN_KEYS = ("DISCORD_TOKEN", "BOT_TOKEN", "TOKEN")
USER = web.RequestKey("user", dict)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Panel:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.db = Database(self.settings.data_dir / "panel.db")
        self.runtime = BotRuntime(self.settings)
        self.catalog = plugins.Catalog(self.settings.plugin_catalog, max_bytes=self.settings.max_plugin_mb * 1024 * 1024)
        self.auth = Auth(self)
        self.busy: set[str] = set()  # bots en train de démarrer/s'arrêter (la surveillance y touche pas)
        self._stats: dict[str, tuple[float, dict | None]] = {}
        self._disk: dict[str, tuple[float, int]] = {}
        self._tasks: list[asyncio.Task] = []
        self.docker_ok = False
        self.docker_error = ""

    # ------------------------------------------------------------------------------------------
    # Démarrage
    # ------------------------------------------------------------------------------------------
    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.errors, self.security, self.require_login],
                              client_max_size=self.settings.max_upload_mb * 1024 * 1024)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        self.auth.routes(app)
        r = app.router
        r.add_get("/", self.page_index)
        r.add_get("/login", self.page_login)
        r.add_static("/static/", STATIC, append_version=False)
        r.add_get("/api/me", self.api_me)
        # Bots
        r.add_get("/api/bots", self.list_bots)
        r.add_get("/api/kits", self.list_kits)
        r.add_post("/api/bots/{bot}/kit", self.update_kit)
        r.add_post("/api/bots", self.create_bot)
        r.add_get("/api/bots/{bot}", self.get_bot)
        r.add_patch("/api/bots/{bot}", self.update_bot)
        r.add_delete("/api/bots/{bot}", self.delete_bot)
        r.add_post("/api/bots/{bot}/power", self.power)
        r.add_post("/api/bots/{bot}/command", self.command)
        r.add_get("/api/bots/{bot}/console", self.console)
        r.add_get("/api/bots/{bot}/activity", self.bot_activity)
        r.add_get("/api/shared", self.shared_list)
        r.add_get("/api/bots/{bot}/options", self.options_get)
        r.add_post("/api/bots/{bot}/options", self.options_save)
        r.add_get("/api/bots/{bot}/env", self.get_env)
        r.add_put("/api/bots/{bot}/env", self.put_env)
        r.add_post("/api/bots/{bot}/git", self.git_pull)
        r.add_get("/api/bots/{bot}/plugins", self.plugins_list)
        r.add_post("/api/bots/{bot}/plugins/install", self.plugins_install)
        r.add_post("/api/bots/{bot}/plugins/upload", self.plugins_upload)
        r.add_put("/api/bots/{bot}/plugins/{pid}/variables", self.plugins_variables)
        r.add_delete("/api/bots/{bot}/plugins/{pid}", self.plugins_remove)
        # Fichiers
        r.add_get("/api/bots/{bot}/files", self.files_list)
        r.add_get("/api/bots/{bot}/files/content", self.files_read)
        r.add_put("/api/bots/{bot}/files/content", self.files_write)
        r.add_get("/api/bots/{bot}/files/download", self.files_download)
        r.add_post("/api/bots/{bot}/files/upload", self.files_upload)
        r.add_post("/api/bots/{bot}/files/mkdir", self.files_mkdir)
        r.add_post("/api/bots/{bot}/files/delete", self.files_delete)
        r.add_post("/api/bots/{bot}/files/rename", self.files_rename)
        r.add_post("/api/bots/{bot}/files/extract", self.files_extract)
        # Constructeur de serveurs Discord
        r.add_get("/api/builder/sources", self.builder_sources)
        r.add_get("/api/builder/sources/{bot}", self.builder_guilds)
        r.add_get("/api/builder/templates", self.templates_list)
        r.add_get("/api/builder/templates/{tid}", self.templates_get)
        r.add_post("/api/builder/templates", self.templates_save)
        r.add_delete("/api/builder/templates/{tid}", self.templates_delete)
        r.add_post("/api/builder/validate", self.builder_validate)
        r.add_post("/api/builder/plan", self.builder_plan)
        r.add_post("/api/builder/apply", self.builder_apply)
        r.add_get("/api/builder/jobs/{job}", self.builder_job)
        r.add_post("/api/builder/export", self.builder_export)
        r.add_post("/api/builder/import", self.builder_import)
        # Admin
        r.add_get("/api/admin/users", self.admin_users)
        r.add_post("/api/admin/users", self.admin_add_user)
        r.add_patch("/api/admin/users/{uid}", self.admin_update_user)
        r.add_get("/api/admin/system", self.admin_system)
        return app

    async def startup(self, app: web.Application) -> None:
        self.settings.load_secret()
        self.settings.bots_dir.mkdir(parents=True, exist_ok=True)
        await self.db.connect()
        for uid in self.settings.admin_ids:  # les admins de .env existent d'avance
            s = self.settings
            await self.db.add_user(uid, "admin", (s.default_max_bots, s.default_max_ram, s.default_max_disk))
            await self.db.update_user(uid, role="admin")
        try:
            await self.runtime.connect()
            self.docker_ok = True
        except RuntimeError_ as e:
            self.docker_error = str(e)
            log.error("%s", e)
        if self.docker_ok:
            self._tasks.append(asyncio.create_task(self.watchdog(), name="surveillance"))

    async def cleanup(self, app: web.Application) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.runtime.close()
        await self.db.close()

    # ------------------------------------------------------------------------------------------
    # Middlewares
    # ------------------------------------------------------------------------------------------
    @web.middleware
    async def errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=e.status)
        except (FileError, gitops.FileError, builder.TemplateError, RuntimeError_, options.OptionsError, plugins.PluginError,
                kits.KitError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except builder.DiscordError as e:
            return web.json_response({"error": str(e)}, status=400 if e.status != 401 else 400)
        except OSError as e:
            if not request.path.startswith("/api/"):
                raise
            return web.json_response({"error": f"Erreur de fichier : {e.strerror or e}"}, status=400)
        except json.JSONDecodeError:
            return web.json_response({"error": "Requête invalide"}, status=400)

    @web.middleware
    async def security(self, request: web.Request, handler):
        response = await handler(request)
        host = urlsplit(self.settings.public_url).netloc
        response.headers.update({
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
                f"img-src 'self' data: https://cdn.discordapp.com; connect-src 'self' ws://{host} wss://{host}; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
        })
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.path.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = "no-cache"  # le navigateur vérifie à chaque fois : jamais d'ancienne version après une mise à jour
        return response

    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return False
        allowed = {urlsplit(self.settings.public_url).netloc, request.host}
        return urlsplit(origin).netloc in allowed

    @web.middleware
    async def require_login(self, request: web.Request, handler):
        path = request.path
        if path.startswith(("/static/", "/auth/")) or path in ("/login", "/favicon.ico"):
            return await handler(request)
        user = await self.auth.current_user(request)
        if path.startswith("/api/"):
            if path == "/api/logout":
                return await handler(request)
            if user is None:
                raise ApiError("Connecte-toi d'abord", 401)
            websocket = request.headers.get("Upgrade", "").lower() == "websocket"
            if websocket and not self._same_origin(request):
                raise ApiError("Origine refusée", 403)
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Panel") != "1":
                raise ApiError("Requête refusée (protection CSRF)", 403)
            if path.startswith("/api/admin/") and user["role"] != "admin":
                raise ApiError("Réservé à l'admin", 403)
            request[USER] = user
            return await handler(request)
        if user is None:
            raise web.HTTPFound("/login")
        request[USER] = user
        return await handler(request)

    # ------------------------------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------------------------------
    async def page_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def page_login(self, request: web.Request) -> web.StreamResponse:
        if await self.auth.current_user(request):
            raise web.HTTPFound("/")
        return web.FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})

    async def api_me(self, request: web.Request) -> web.Response:
        user = request[USER]
        return web.json_response({**self._user_public(user), "usage": await self._usage(user),
                                  "docker": self.docker_ok, "docker_error": self.docker_error,
                                  "images": self.settings.allowed_images, "version": __version__,
                                  "defaults": {"python": self.settings.python_image, "node": self.settings.node_image}})

    def _user_public(self, user: dict) -> dict:
        return {"id": str(user["id"]), "name": user["name"], "avatar": avatar_url(user["id"], user.get("avatar")),
                "role": user["role"], "max_bots": user["max_bots"], "max_ram_mb": user["max_ram_mb"],
                "max_disk_mb": user["max_disk_mb"], "created_at": user["created_at"], "last_login": user.get("last_login")}

    # ------------------------------------------------------------------------------------------
    # Outils
    # ------------------------------------------------------------------------------------------
    @staticmethod
    async def body(request: web.Request) -> dict:
        data = await request.json()
        if not isinstance(data, dict):
            raise ApiError("Requête invalide")
        return data

    async def bot_for(self, request: web.Request) -> dict:
        bot = await self.db.get_bot(request.match_info["bot"])
        user = request[USER]
        if bot is None or (bot["owner_id"] != user["id"] and user["role"] != "admin"):
            raise ApiError("Bot introuvable", 404)
        return bot

    def files(self, bot_id: str) -> BotFiles:
        return BotFiles(self.settings.bots_dir / bot_id)

    def need_docker(self) -> None:
        if not self.docker_ok:
            raise ApiError(self.docker_error or "Docker est pas joignable", 503)

    async def disk_bytes(self, bot_id: str, *, fresh: bool = False) -> int:
        cached = self._disk.get(bot_id)
        if cached and not fresh and time.monotonic() - cached[0] < 30:
            return cached[1]
        size = await asyncio.to_thread(self.files(bot_id).size)
        self._disk[bot_id] = (time.monotonic(), size)
        return size

    async def stats(self, bot_id: str) -> dict | None:
        cached = self._stats.get(bot_id)
        if cached and time.monotonic() - cached[0] < 2:
            return cached[1]
        value = await self.runtime.stats(bot_id) if self.docker_ok else None
        self._stats[bot_id] = (time.monotonic(), value)
        return value

    async def _usage(self, user: dict) -> dict:
        bots = await self.db.list_bots(user["id"])
        disk = sum([await self.disk_bytes(b["id"]) for b in bots])
        return {"bots": len(bots), "ram_mb": sum(b["ram_mb"] for b in bots), "disk_mb": round(disk / 1024 / 1024, 1)}

    async def disk_left(self, bot: dict) -> int:
        """Octets qu'il reste au propriétaire du bot (l'admin a pas de limite)."""
        owner = await self.db.get_user(bot["owner_id"])
        if owner is None or owner["role"] == "admin":
            return 1 << 50
        used = sum([await self.disk_bytes(b["id"], fresh=b["id"] == bot["id"]) for b in await self.db.list_bots(owner["id"])])
        return max(owner["max_disk_mb"] * 1024 * 1024 - used, 0)

    async def check_disk(self, bot: dict, adding: int) -> None:
        if adding > await self.disk_left(bot):
            raise ApiError("Plus assez d'espace disque : supprime des fichiers ou demande plus d'espace à l'admin")

    async def bot_public(self, bot: dict, *, details: bool = False) -> dict:
        status = await self.runtime.status(bot["id"]) if self.docker_ok else {"state": "unknown"}
        if bot["id"] in self.busy and status["state"] != "running":
            status["state"] = "starting"
        data = {k: bot[k] for k in ("id", "name", "language", "image", "main_file", "startup_cmd", "ram_mb",
                                    "cpu_percent", "git_url", "git_branch", "created_at", "port")}
        data["web_url"] = f"http://{self.settings.public_host}:{bot['port']}" if bot.get("port") else None
        data.update(auto_restart=bool(bot["auto_restart"]), wanted_running=bool(bot["wanted_running"]),
                    has_git_token=bool(bot["git_token"]), owner_id=str(bot["owner_id"]), status=status,
                    has_options=options.has_schema(self.files(bot["id"])), shared=bool(bot.get("shared")),
                    stats=await self.stats(bot["id"]) if status["state"] == "running" else None,
                    disk_mb=round(await self.disk_bytes(bot["id"]) / 1024 / 1024, 1), kit=self._kit_info(bot))
        if details:
            owner = await self.db.get_user(bot["owner_id"])
            data["owner"] = {"id": str(bot["owner_id"]), "name": owner["name"] if owner else "?"}
        return data

    # ------------------------------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------------------------------
    async def list_bots(self, request: web.Request) -> web.Response:
        user = request[USER]
        everyone = user["role"] == "admin" and request.query.get("tous") == "1"
        bots = await self.db.list_bots(None if everyone else user["id"])
        result = await asyncio.gather(*(self.bot_public(b) for b in bots))
        if everyone:
            names = {u["id"]: u["name"] for u in await self.db.list_users()}
            for item, bot in zip(result, bots):
                item["owner_name"] = names.get(bot["owner_id"], "?")
        return web.json_response(result)

    def _clean_settings(self, data: dict, user: dict, current: dict | None = None) -> dict:
        s = self.settings
        out: dict = {}
        if "name" in data or current is None:
            name = str(data.get("name", "")).strip()
            if not NAME_RE.match(name):
                raise ApiError("Donne un nom au bot (40 caractères max)")
            out["name"] = name
        language = data.get("language", current["language"] if current else "python")
        if language not in ("python", "node", "custom"):
            raise ApiError("Langage invalide")
        out["language"] = language
        if "image" in data or current is None or "language" in data:
            image = str(data.get("image") or "").strip() or (s.python_image if language == "python" else s.node_image if language == "node" else "")
            if not image:
                raise ApiError("Choisis une image Docker")
            if not IMAGE_RE.match(image):
                raise ApiError("Nom d'image Docker invalide")
            if user["role"] != "admin" and image not in s.allowed_images and image != (current or {}).get("image"):
                raise ApiError(f"Image pas permise. Choix : {', '.join(s.allowed_images)}")
            out["image"] = image
        if "main_file" in data or current is None:
            main = str(data.get("main_file") or ("index.js" if language == "node" else "main.py")).strip()
            if not main or len(main) > 200 or ".." in main.split("/") or main.startswith("/") or main.startswith("-"):
                raise ApiError("Fichier principal invalide")
            out["main_file"] = main
        if "startup_cmd" in data:
            cmd = str(data["startup_cmd"] or "").strip()
            if len(cmd) > 1000:
                raise ApiError("Commande de démarrage trop longue")
            out["startup_cmd"] = cmd
        if language == "custom" and not (out.get("startup_cmd") or (current or {}).get("startup_cmd")):
            raise ApiError("Avec une image perso, mets la commande de démarrage")
        for key, lo, hi, default, label in (("ram_mb", 64, 16384, 256, "RAM"), ("cpu_percent", 10, 800, 100, "CPU")):
            if key in data or current is None:
                try:
                    value = int(data.get(key, default))
                except (TypeError, ValueError):
                    raise ApiError(f"{label} invalide") from None
                if not lo <= value <= hi:
                    raise ApiError(f"{label} : entre {lo} et {hi}")
                out[key] = value
        if "auto_restart" in data:
            out["auto_restart"] = int(bool(data["auto_restart"]))
        if "web_port" in data:
            out["web_port"] = bool(data["web_port"])
        if "shared" in data:
            out["shared"] = int(bool(data["shared"]))
        if "git_url" in data:
            url = str(data["git_url"] or "").strip()
            branch = str(data.get("git_branch") or "").strip()
            if url:
                gitops.validate(url, branch)
            out["git_url"], out["git_branch"] = url, branch
        if "git_token" in data and data["git_token"] is not None:
            token = str(data["git_token"]).strip()
            if len(token) > 300 or any(c.isspace() for c in token):
                raise ApiError("Jeton Git invalide")
            out["git_token"] = token
        return out

    async def free_port(self) -> int:
        """Premier port libre de PORT_RANGE (un port par bot)."""
        used = {b["port"] for b in await self.db.list_bots() if b.get("port")}
        port = next((p for p in self.settings.ports if p not in used), None)
        if port is None:
            raise ApiError(f"Plus de port libre (plage {self.settings.port_range} dans .env)")
        return port

    async def _check_limits(self, user: dict, *, ram_mb: int, exclude: str | None = None, creating: bool = False) -> None:
        if user["role"] == "admin":
            return
        bots = await self.db.list_bots(user["id"])
        if creating and len(bots) >= user["max_bots"]:
            raise ApiError(f"Tu as déjà {len(bots)} bot(s) sur {user['max_bots']} permis")
        total = sum(b["ram_mb"] for b in bots if b["id"] != exclude) + ram_mb
        if total > user["max_ram_mb"]:
            raise ApiError(f"RAM : {total} Mo au total dépasserait ta limite de {user['max_ram_mb']} Mo")

    async def list_kits(self, request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(kits.list_kits, self.settings.kits_dir))

    async def create_bot(self, request: web.Request) -> web.Response:
        user = request[USER]
        data = await self.body(request)
        kit = None
        if data.get("kit"):  # modèle de bot : son langage et son fichier principal
            kit = await asyncio.to_thread(kits.get_kit, self.settings.kits_dir, str(data["kit"]))
            data.update(language=kit["language"], main_file=kit["main_file"], starter=False, startup_cmd="")
            if not str(data.get("image") or "").startswith(kit["language"]):
                data["image"] = ""
        fields = self._clean_settings(data, user)
        await self._check_limits(user, ram_mb=fields["ram_mb"], creating=True)
        if fields.pop("web_port", False):
            fields["port"] = await self.free_port()
        bot = await self.db.create_bot(user["id"], **fields)
        files = self.files(bot["id"])
        files.ensure()
        notes = []
        if kit is not None:
            await asyncio.to_thread(kits.install, files, self.settings.kits_dir, kit["id"])
            await self.db.set_kit(bot["id"], kit["id"], kit["version"])
            notes = await self._install_kit_plugins(user, bot, kit)
            bot = await self.db.get_bot(bot["id"])
        elif data.get("starter", True) and bot["language"] in ("python", "node"):
            self._starter(files, bot)
        await self.db.log(user["id"], "Bot créé", bot_id=bot["id"],
                          detail=bot["name"] + (f" (modèle {kit['name']} {kit['version']})" if kit else ""))
        if bot["git_url"]:
            try:
                await gitops.deploy(files, bot["git_url"], bot["git_branch"], bot["git_token"])
                await self.db.log(user["id"], "Dépôt Git cloné", bot_id=bot["id"], detail=bot["git_url"])
            except FileError as e:
                self.runtime.console.publish(bot["id"], f"Clonage du dépôt raté : {e}")
        return web.json_response({**await self.bot_public(bot, details=True), "notes": notes}, status=201)

    async def _install_kit_plugins(self, user: dict, bot: dict, kit: dict) -> list[str]:
        """Les plugins du modèle, depuis le catalogue. Un plugin qui s'installe pas empêche pas la création du bot."""
        notes = []
        files = self.files(bot["id"])
        for pid in kit["plugins"]:
            try:
                entry = await self.catalog.find(pid)
                manifest, contents = await asyncio.to_thread(
                    plugins.read_zip, await self.catalog.download(entry),
                    max_bytes=self.settings.max_plugin_mb * 1024 * 1024, reserved_env=RESERVED_ENV)
                await asyncio.to_thread(plugins.install, files, manifest, contents)
                await self.db.log(user["id"], "Plugin installé", bot_id=bot["id"],
                                  detail=f"{manifest['name']} {manifest['version']} (modèle)")
            except (plugins.PluginError, FileError) as e:
                notes.append(f"Plugin « {pid} » pas installé : {e}. Installe-le dans l'onglet Plugins.")
        return notes

    async def update_kit(self, request: web.Request) -> web.Response:
        """Recopie les fichiers du modèle (nouvelle version). Plugins, données, variables et .env restent."""
        bot = await self.bot_for(request)
        if not bot.get("kit"):
            raise ApiError("Ce bot a pas été créé à partir d'un modèle")
        kit = await asyncio.to_thread(kits.get_kit, self.settings.kits_dir, bot["kit"])
        count = await asyncio.to_thread(kits.install, self.files(bot["id"]), self.settings.kits_dir, kit["id"])
        await self.db.set_kit(bot["id"], kit["id"], kit["version"])
        self._disk.pop(bot["id"], None)
        self.runtime.console.publish(bot["id"], f"Modèle {kit['name']} mis à jour : version {kit['version']} ({count} fichiers)")
        await self.db.log(request[USER]["id"], "Modèle mis à jour", bot_id=bot["id"], detail=f"{kit['name']} {kit['version']}")
        return web.json_response({**await self.bot_public(await self.db.get_bot(bot["id"]), details=True),
                                  "restart_needed": await self._running(bot)})

    def _kit_info(self, bot: dict) -> dict | None:
        if not bot.get("kit"):
            return None
        try:
            latest = kits.get_kit(self.settings.kits_dir, bot["kit"])
        except kits.KitError:
            return {"id": bot["kit"], "name": bot["kit"], "version": bot.get("kit_version") or "", "update": None}
        newer = latest["version"] if latest["version"] and latest["version"] != bot.get("kit_version") else None
        return {"id": bot["kit"], "name": latest["name"], "version": bot.get("kit_version") or "", "update": newer}

    @staticmethod
    def _starter(files: BotFiles, bot: dict) -> None:
        """Petit bot d'exemple pour que le premier démarrage fasse quelque chose."""
        if any(files.root.iterdir()):
            return
        if bot["language"] == "python":
            files.write_text(bot["main_file"], (
                "import os\n\nimport discord\n\n"
                "intents = discord.Intents.default()\nclient = discord.Client(intents=intents)\n\n\n"
                "@client.event\nasync def on_ready():\n    print(f\"Connecté en tant que {client.user}\")\n\n\n"
                "token = os.getenv(\"DISCORD_TOKEN\")\nif not token:\n"
                "    raise SystemExit(\"Ajoute DISCORD_TOKEN dans l'onglet Variables du panel.\")\n"
                "client.run(token)\n"))
            files.write_text("requirements.txt", "discord.py>=2.4\n")
        else:
            files.write_text(bot["main_file"], (
                "const { Client, GatewayIntentBits } = require('discord.js');\n\n"
                "const client = new Client({ intents: [GatewayIntentBits.Guilds] });\n"
                "client.once('ready', () => console.log(`Connecté en tant que ${client.user.tag}`));\n\n"
                "if (!process.env.DISCORD_TOKEN) {\n"
                "  console.error(\"Ajoute DISCORD_TOKEN dans l'onglet Variables du panel.\");\n  process.exit(1);\n}\n"
                "client.login(process.env.DISCORD_TOKEN);\n"))
            files.write_text("package.json", json.dumps(
                {"name": "mon-bot", "version": "1.0.0", "main": bot["main_file"], "dependencies": {"discord.js": "^14.16.0"}},
                indent=2) + "\n")

    async def get_bot(self, request: web.Request) -> web.Response:
        return web.json_response(await self.bot_public(await self.bot_for(request), details=True))

    async def update_bot(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        user = request[USER]
        owner = await self.db.get_user(bot["owner_id"]) if bot["owner_id"] != user["id"] else user
        fields = self._clean_settings(await self.body(request), user, bot)
        if "web_port" in fields:
            wanted = fields.pop("web_port")
            if wanted and not bot.get("port"):
                fields["port"] = await self.free_port()
            elif not wanted and bot.get("port"):
                fields["port"] = None
        if "ram_mb" in fields:
            await self._check_limits(owner, ram_mb=fields["ram_mb"], exclude=bot["id"])
        bot = await self.db.update_bot(bot["id"], **fields)
        await self.db.log(user["id"], "Réglages modifiés", bot_id=bot["id"],
                          detail=", ".join(k for k in fields if k != "git_token") + (" (jeton Git)" if "git_token" in fields else ""))
        return web.json_response(await self.bot_public(bot, details=True))

    async def delete_bot(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        data = await self.body(request)
        if data.get("confirm") != bot["name"]:
            raise ApiError("Écris le nom du bot pour confirmer")
        await self.db.update_bot(bot["id"], wanted_running=0)
        if self.docker_ok:
            await self.runtime.remove(bot["id"])
        await asyncio.to_thread(self._rmtree, self.files(bot["id"]).root)
        await self.db.delete_bot(bot["id"])
        await self.db.log(request[USER]["id"], "Bot supprimé", bot_id=bot["id"], detail=bot["name"])
        return web.json_response({"ok": True})

    @staticmethod
    def _rmtree(path: Path) -> None:
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    async def start_bot(self, bot: dict) -> None:
        env = {e["key"]: e["value"] for e in await self.db.get_env(bot["id"])}
        await self.runtime.start(bot, env)

    async def power(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)  # d'abord les droits, ensuite Docker
        self.need_docker()
        action = (await self.body(request)).get("action")
        if action not in ("start", "stop", "restart", "kill"):
            raise ApiError("Action invalide")
        if bot["id"] in self.busy:
            raise ApiError("Une action est déjà en cours sur ce bot", 409)
        self.busy.add(bot["id"])
        try:
            if action in ("stop", "kill", "restart"):
                await self.db.update_bot(bot["id"], wanted_running=0)
                await self.runtime.stop(bot["id"], kill=action == "kill")
            if action in ("start", "restart"):
                owner = await self.db.get_user(bot["owner_id"])
                if owner and owner["role"] != "admin" and await self.disk_left(bot) <= 0:
                    raise ApiError("Espace disque plein : libère de la place avant de démarrer")
                await self.db.update_bot(bot["id"], wanted_running=1)
                try:
                    await self.start_bot(bot)
                except Exception:
                    await self.db.update_bot(bot["id"], wanted_running=0)
                    raise
        finally:
            self.busy.discard(bot["id"])
        labels = {"start": "Démarrage", "stop": "Arrêt", "restart": "Redémarrage", "kill": "Arrêt forcé"}
        await self.db.log(request[USER]["id"], labels[action], bot_id=bot["id"])
        return web.json_response(await self.bot_public(await self.db.get_bot(bot["id"])))

    async def command(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)  # d'abord les droits, ensuite Docker
        self.need_docker()
        text = str((await self.body(request)).get("command", ""))
        if not text.strip() or len(text) > 2000:
            raise ApiError("Commande vide ou trop longue")
        await self.runtime.send_input(bot["id"], text)
        return web.json_response({"ok": True})

    async def bot_activity(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        return web.json_response([{"action": a["action"], "detail": a["detail"], "at": a["at"], "user": a["user_name"]}
                                  for a in await self.db.activity(bot["id"])])

    # ---- Console en direct --------------------------------------------------------------------
    async def console(self, request: web.Request) -> web.WebSocketResponse:
        bot = await self.bot_for(request)  # d'abord les droits, ensuite Docker
        self.need_docker()
        bot_id = bot["id"]
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8192)
        await ws.prepare(request)
        container, lines, last = await self.runtime.history(bot_id)
        await ws.send_json({"type": "history", "lines": lines})
        queue = self.runtime.console.subscribe(bot_id)

        async def pump_logs() -> None:
            seen, after, first = container, last, True
            while True:
                cid, running = await self.runtime.container_id(bot_id)
                if cid and running:
                    # Nouveau conteneur, ou rien encore écrit au moment de l'historique : tout son journal est nouveau
                    from_start = cid != seen or (first and after is None)
                    async for line in self.runtime.follow_logs(bot_id, from_start=from_start, after=after):
                        await ws.send_json({"type": "line", "text": line})
                    seen, after = cid, None
                elif cid and cid != seen:  # nouveau conteneur déjà arrêté (plantage immédiat)
                    for line in await self.runtime.logs(bot_id, tail=200):
                        await ws.send_json({"type": "line", "text": line})
                    seen = cid
                first = False
                await asyncio.sleep(1)

        async def pump_panel() -> None:
            while True:
                await ws.send_json({"type": "line", "text": await queue.get()})

        async def pump_stats() -> None:
            while True:
                fresh = await self.db.get_bot(bot_id)
                if fresh is None:
                    await ws.close()
                    return
                await ws.send_json({"type": "status", "bot": await self.bot_public(fresh)})
                await asyncio.sleep(2)

        tasks = [asyncio.create_task(f()) for f in (pump_logs, pump_panel, pump_stats)]
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "command" and str(data.get("text", "")).strip():
                    try:
                        await self.runtime.send_input(bot_id, str(data["text"])[:2000])
                    except RuntimeError_ as e:
                        await ws.send_json({"type": "line", "text": f"[Panel] {e}"})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.runtime.console.unsubscribe(bot_id, queue)
        return ws

    # ---- Options du bot (réglages décrits par le bot lui-même) --------------------------------
    async def bot_for_options(self, request: web.Request) -> tuple[dict, bool]:
        """(bot, accès complet). Un bot partagé est accessible aux autres, mais seulement pour leurs serveurs."""
        bot = await self.db.get_bot(request.match_info["bot"])
        user = request[USER]
        if bot is None:
            raise ApiError("Bot introuvable", 404)
        if bot["owner_id"] == user["id"] or user["role"] == "admin":
            return bot, True
        if bot.get("shared"):
            return bot, False
        raise ApiError("Bot introuvable", 404)

    async def shared_list(self, request: web.Request) -> web.Response:
        user = request[USER]
        names = {u["id"]: u["name"] for u in await self.db.list_users()}
        out = []
        for bot in await self.db.list_bots():
            if not bot.get("shared"):
                continue
            mine, invite = await asyncio.to_thread(options.managed_scopes, self.files(bot["id"]), str(user["id"]))
            status = await self.runtime.status(bot["id"]) if self.docker_ok else {"state": "unknown"}
            out.append({"id": bot["id"], "name": bot["name"], "owner_name": names.get(bot["owner_id"], "?"),
                        "running": status["state"] == "running", "invite": invite, "servers": mine,
                        "yours": bot["owner_id"] == user["id"]})
        return web.json_response(out)

    async def options_get(self, request: web.Request) -> web.Response:
        bot, full = await self.bot_for_options(request)
        only = None if full else str(request[USER]["id"])
        data = await asyncio.to_thread(options.load, self.files(bot["id"]), only_for=only)
        status = await self.runtime.status(bot["id"]) if self.docker_ok else {"state": "unknown"}
        return web.json_response({**data, "running": status["state"] == "running", "bot_name": bot["name"], "full": full})

    async def options_save(self, request: web.Request) -> web.Response:
        bot, full = await self.bot_for_options(request)
        data = await self.body(request)
        if not full:
            mine, _ = await asyncio.to_thread(options.managed_scopes, self.files(bot["id"]), str(request[USER]["id"]))
            if str(data.get("scope", "")) not in {sc["id"] for sc in mine}:
                raise ApiError("Tu gères pas ce serveur (il faut la permission « Gérer le serveur » sur Discord)", 403)
        status = await self.runtime.status(bot["id"]) if self.docker_ok else {"state": "unknown"}
        if status["state"] != "running":
            raise ApiError("Démarre le bot pour changer ses options : c'est lui qui les vérifie et les applique")
        user = request[USER]
        answer = await options.send(self.files(bot["id"]), str(data.get("scope", "")), str(data.get("page", "")),
                                    data.get("values"), user)
        if answer["ok"]:
            await self.db.log(user["id"], "Options du bot modifiées", bot_id=bot["id"],
                              detail=f"{data.get('page')} ({data.get('scope')})")
        return web.json_response(answer, status=200 if answer["ok"] else 400)

    # ---- Variables d'environnement ------------------------------------------------------------
    async def get_env(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        return web.json_response([
            {"key": e["key"], "secret": bool(e["secret"]), "value": None if e["secret"] else e["value"], "set": bool(e["value"])}
            for e in await self.db.get_env(bot["id"])
        ])

    async def put_env(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        items = (await self.body(request)).get("variables")
        if not isinstance(items, list) or len(items) > 100:
            raise ApiError("Liste de variables invalide (100 max)")
        old = {e["key"]: e["value"] for e in await self.db.get_env(bot["id"])}
        clean, seen = [], set()
        for item in items:
            key = str(item.get("key", "")).strip()
            if not ENV_KEY_RE.match(key):
                raise ApiError(f"Nom de variable invalide : « {key} » (lettres, chiffres et _)")
            if key in RESERVED_ENV:
                raise ApiError(f"{key} est réservé par le panel")
            if key in seen:
                raise ApiError(f"{key} est là deux fois")
            seen.add(key)
            value = item.get("value")
            if value is None:  # secret pas retouché : on garde l'ancienne valeur
                value = old.get(key, "")
            value = str(value)
            if len(value) > 4096 or "\x00" in value:
                raise ApiError(f"Valeur de {key} trop longue")
            clean.append((key, value, bool(item.get("secret"))))
        await self.db.set_env(bot["id"], clean)
        await self.db.log(request[USER]["id"], "Variables modifiées", bot_id=bot["id"], detail=", ".join(k for k, _, _ in clean))
        status = await self.runtime.status(bot["id"]) if self.docker_ok else {"state": "offline"}
        return web.json_response({"ok": True, "restart_needed": status["state"] == "running"})

    # ---- Plugins --------------------------------------------------------------------------------
    @staticmethod
    def _plugin_status(plugin: dict, host: dict | None) -> tuple[str, str]:
        """(état, explication) d'un plugin installé, d'après ce que le bot a dit à son dernier démarrage."""
        if plugin.get("error"):
            return "error", plugin["error"]
        if host is None:
            return "unknown", "Le bot a pas encore dit s'il supporte les plugins Bot Panel : démarre-le."
        info = host["plugins"].get(plugin["id"])
        if info is None or (info["loaded"] and info["version"] != plugin["version"]):
            return "pending", "Redémarre le bot pour charger cette version."
        if info["error"]:
            return "error", info["error"]
        return "loaded", "Chargé par le bot."

    async def _running(self, bot: dict) -> bool:
        return self.docker_ok and (await self.runtime.status(bot["id"]))["state"] == "running"

    async def plugins_list(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        files = self.files(bot["id"])
        installed = await asyncio.to_thread(plugins.installed, files, reserved_env=RESERVED_ENV)
        host = await asyncio.to_thread(plugins.host_report, files)
        env = {e["key"]: e for e in await self.db.get_env(bot["id"])}
        catalog, catalog_error = [], None
        try:
            catalog = await self.catalog.entries(force=request.query.get("refresh") == "1")
        except plugins.PluginError as e:
            catalog_error = str(e)
        latest = {c["id"]: c["version"] for c in catalog}
        for p in installed:
            p.pop("pages", None)
            p["status"], p["status_text"] = self._plugin_status(p, host)
            p["intents"] = [plugins.INTENTS[i] for i in p.get("intents", [])]
            p["variables"] = [
                {**v, "set": bool(env.get(v["key"], {}).get("value")),
                 "value": None if v["secret"] else env.get(v["key"], {}).get("value", "")}
                for v in p["variables"]
            ]
            newer = latest.get(p["id"])
            p["update"] = newer if newer and p["version"] != "?" and plugins.version_key(newer) > plugins.version_key(p["version"]) else None
        return web.json_response({
            "installed": installed,
            "catalog": [{k: c[k] for k in ("id", "name", "version", "description", "author")} for c in catalog],
            "catalog_enabled": bool(self.catalog.url),
            "catalog_error": catalog_error,
            "host": host is not None,
            "running": await self._running(bot),
        })

    async def _install_plugin(self, request: web.Request, bot: dict, blob: bytes, source: str) -> web.Response:
        max_bytes = self.settings.max_plugin_mb * 1024 * 1024
        manifest, contents = await asyncio.to_thread(plugins.read_zip, blob, max_bytes=max_bytes, reserved_env=RESERVED_ENV)
        if sum(len(v) for v in contents.values()) > await self.disk_left(bot):
            raise ApiError("Plus assez d'espace disque pour ce plugin")
        files = self.files(bot["id"])
        before = {p["id"]: p["version"] for p in await asyncio.to_thread(plugins.installed, files)}
        await asyncio.to_thread(plugins.install, files, manifest, contents)
        self._disk.pop(bot["id"], None)
        what = f"{manifest['name']} {manifest['version']}"
        verb = "mis à jour" if manifest["id"] in before else "installé"
        self.runtime.console.publish(bot["id"], f"Plugin {verb} : {what} ({source})")
        await self.db.log(request[USER]["id"], f"Plugin {verb}", bot_id=bot["id"], detail=f"{what} ({source})")
        return web.json_response({"ok": True, "id": manifest["id"], "name": manifest["name"], "version": manifest["version"],
                                  "restart_needed": await self._running(bot)})

    async def plugins_install(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        entry = await self.catalog.find(str((await self.body(request)).get("id", "")))
        blob = await self.catalog.download(entry)
        return await self._install_plugin(request, bot, blob, "catalogue")

    async def plugins_upload(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        limit = self.settings.max_plugin_mb * 1024 * 1024
        reader = await request.multipart()
        blob = None
        while (part := await reader.next()) is not None:
            if part.name != "file":
                continue
            chunks, size = [], 0
            while chunk := await part.read_chunk(256 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ApiError(f"Plugin trop gros ({self.settings.max_plugin_mb} Mo max)")
                chunks.append(chunk)
            blob = b"".join(chunks)
        if not blob:
            raise ApiError("Choisis le .zip du plugin")
        return await self._install_plugin(request, bot, blob, "zip téléversé")

    async def plugins_variables(self, request: web.Request) -> web.Response:
        """Les variables d'un plugin (ex. sa clé d'API), gardées avec les autres variables du bot."""
        bot = await self.bot_for(request)
        plugin = next((p for p in await asyncio.to_thread(plugins.installed, self.files(bot["id"]), reserved_env=RESERVED_ENV)
                       if p["id"] == request.match_info["pid"] and not p["error"]), None)
        if plugin is None:
            raise ApiError("Plugin introuvable", 404)
        values = (await self.body(request)).get("values")
        if not isinstance(values, dict):
            raise ApiError("Requête invalide")
        declared = {v["key"]: v for v in plugin["variables"]}
        current = [(e["key"], e["value"], bool(e["secret"])) for e in await self.db.get_env(bot["id"])]
        changed = []
        for key, value in values.items():
            if key not in declared or value is None:  # None : secret pas retouché
                continue
            value = str(value).strip()
            if len(value) > 4096 or "\x00" in value:
                raise ApiError(f"Valeur de {key} trop longue")
            current = [item for item in current if item[0] != key]
            if value:
                current.append((key, value, declared[key]["secret"]))
            changed.append(key)
        if len(current) > 100:
            raise ApiError("Trop de variables (100 max)")
        await self.db.set_env(bot["id"], current)
        if changed:
            await self.db.log(request[USER]["id"], "Variables de plugin modifiées", bot_id=bot["id"],
                              detail=f"{plugin['name']} : {', '.join(changed)}")
        return web.json_response({"ok": True, "restart_needed": bool(changed) and await self._running(bot)})

    async def plugins_remove(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        pid = request.match_info["pid"]
        await asyncio.to_thread(plugins.remove, self.files(bot["id"]), pid)
        self._disk.pop(bot["id"], None)
        self.runtime.console.publish(bot["id"], f"Plugin retiré : {pid}")
        await self.db.log(request[USER]["id"], "Plugin retiré", bot_id=bot["id"], detail=pid)
        return web.json_response({"ok": True, "restart_needed": await self._running(bot)})

    # ---- Git ------------------------------------------------------------------------------------
    async def git_pull(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        if not bot["git_url"]:
            raise ApiError("Mets l'adresse du dépôt dans les réglages du bot")
        files = self.files(bot["id"])
        files.ensure()
        message = await gitops.deploy(files, bot["git_url"], bot["git_branch"], bot["git_token"])
        self.runtime.console.publish(bot["id"], f"Git : {message}")
        await self.db.log(request[USER]["id"], "Mise à jour Git", bot_id=bot["id"], detail=message)
        left = await self.disk_left(bot)
        return web.json_response({"message": message, "over_quota": left <= 0})

    # ---- Fichiers ---------------------------------------------------------------------------------
    async def files_list(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        files = self.files(bot["id"])
        files.ensure()
        return web.json_response({"path": request.query.get("path", ""), "entries": files.list(request.query.get("path", ""))})

    async def files_read(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        return web.json_response({"content": self.files(bot["id"]).read_text(request.query.get("path", ""))})

    async def files_write(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        data = await self.body(request)
        content = str(data.get("content", ""))
        files = self.files(bot["id"])
        path = files.path(str(data.get("path", "")))
        previous = path.stat().st_size if path.is_file() else 0
        await self.check_disk(bot, len(content.encode()) - previous)
        files.write_text(str(data.get("path", "")), content)
        self._disk.pop(bot["id"], None)
        await self.db.log(request[USER]["id"], "Fichier modifié", bot_id=bot["id"], detail=str(data.get("path")))
        return web.json_response({"ok": True})

    async def files_download(self, request: web.Request) -> web.StreamResponse:
        bot = await self.bot_for(request)
        path = self.files(bot["id"]).path(request.query.get("path", ""))
        if not path.is_file():
            raise ApiError("Fichier introuvable", 404)
        return web.FileResponse(path, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(path.name)}",
            "Content-Type": "application/octet-stream",
        })

    async def files_upload(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        files = self.files(bot["id"])
        files.ensure()
        reader = await request.multipart()
        directory, saved = "", []
        left = await self.disk_left(bot)
        while (part := await reader.next()) is not None:
            if part.name == "path":
                directory = (await part.text()).strip().strip("/")
                files.path(directory)
                continue
            if part.name != "files" or not part.filename:
                continue
            name = Path(part.filename.replace("\\", "/")).name
            if not name or name in (".", ".."):
                continue
            rel = f"{directory}/{name}" if directory else name
            chunks, size = [], 0
            while chunk := await part.read_chunk(256 * 1024):
                size += len(chunk)
                if size > left:
                    raise ApiError("Plus assez d'espace disque pour ce téléversement")
                chunks.append(chunk)
            files.write_bytes(rel, b"".join(chunks))
            left -= size
            saved.append(rel)
        self._disk.pop(bot["id"], None)
        await self.db.log(request[USER]["id"], "Fichiers téléversés", bot_id=bot["id"], detail=", ".join(saved)[:400])
        return web.json_response({"saved": saved})

    async def files_mkdir(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        self.files(bot["id"]).mkdir(str((await self.body(request)).get("path", "")))
        return web.json_response({"ok": True})

    async def files_delete(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        paths = (await self.body(request)).get("paths")
        if not isinstance(paths, list) or not paths:
            raise ApiError("Rien à supprimer")
        files = self.files(bot["id"])
        for p in paths:
            await asyncio.to_thread(files.delete, str(p))
        self._disk.pop(bot["id"], None)
        await self.db.log(request[USER]["id"], "Fichiers supprimés", bot_id=bot["id"], detail=", ".join(map(str, paths))[:400])
        return web.json_response({"ok": True})

    async def files_rename(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        data = await self.body(request)
        self.files(bot["id"]).rename(str(data.get("from", "")), str(data.get("to", "")))
        return web.json_response({"ok": True})

    async def files_extract(self, request: web.Request) -> web.Response:
        bot = await self.bot_for(request)
        path = str((await self.body(request)).get("path", ""))
        count = await asyncio.to_thread(self.files(bot["id"]).extract_zip, path, max_bytes=await self.disk_left(bot))
        self._disk.pop(bot["id"], None)
        await self.db.log(request[USER]["id"], "Zip décompressé", bot_id=bot["id"], detail=f"{path} ({count} fichiers)")
        return web.json_response({"count": count})

    # ------------------------------------------------------------------------------------------
    # Constructeur de serveurs
    # ------------------------------------------------------------------------------------------
    async def _token(self, request: web.Request, bot_id: str) -> tuple[dict, str]:
        bot = await self.db.get_bot(str(bot_id))
        user = request[USER]
        if bot is None or (bot["owner_id"] != user["id"] and user["role"] != "admin"):
            raise ApiError("Bot introuvable", 404)
        env = {e["key"]: e["value"] for e in await self.db.get_env(bot["id"])}
        token = next((env[k] for k in TOKEN_KEYS if env.get(k)), "")
        if not token:
            raise ApiError("Ce bot a pas de variable DISCORD_TOKEN (onglet Variables)")
        return bot, token

    def api(self, token: str) -> builder.DiscordAPI:
        return builder.DiscordAPI(token, base=self.settings.discord_api)

    async def builder_sources(self, request: web.Request) -> web.Response:
        out = []
        for bot in await self.db.list_bots(request[USER]["id"]):
            keys = {e["key"] for e in await self.db.get_env(bot["id"]) if e["value"]}
            out.append({"id": bot["id"], "name": bot["name"], "has_token": any(k in keys for k in TOKEN_KEYS)})
        return web.json_response(out)

    async def builder_guilds(self, request: web.Request) -> web.Response:
        _, token = await self._token(request, request.match_info["bot"])
        async with self.api(token) as api:
            me = await api.me()
            guilds = await api.guilds()
        return web.json_response({
            "bot": {"id": me["id"], "name": me.get("global_name") or me.get("username")},
            "invite": builder.invite_url(me["id"]),
            "guilds": [{"id": g["id"], "name": g["name"],
                        "icon": f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png?size=64" if g.get("icon") else None}
                       for g in guilds],
        })

    async def templates_list(self, request: web.Request) -> web.Response:
        return web.json_response(await self.db.list_templates(request[USER]["id"]))

    async def templates_get(self, request: web.Request) -> web.Response:
        t = await self.db.get_template(request[USER]["id"], int(request.match_info["tid"]))
        if t is None:
            raise ApiError("Modèle introuvable", 404)
        return web.json_response(t)

    async def templates_save(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        name = str(data.get("name", "")).strip()
        content = str(data.get("content", ""))
        if not NAME_RE.match(name):
            raise ApiError("Donne un nom au modèle (40 caractères max)")
        if len(content) > 200_000:
            raise ApiError("Modèle trop gros")
        tid = await self.db.save_template(request[USER]["id"], name, content, data.get("id"))
        return web.json_response({"id": tid})

    async def templates_delete(self, request: web.Request) -> web.Response:
        await self.db.delete_template(request[USER]["id"], int(request.match_info["tid"]))
        return web.json_response({"ok": True})

    async def builder_validate(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        if data.get("example"):
            return web.json_response({"content": builder.EXAMPLE})
        template = builder.parse_template(str(data.get("content", "")))
        return web.json_response({"ok": True, "summary": template.summary()})

    async def _plan(self, request: web.Request, data: dict):
        _, token = await self._token(request, data.get("bot_id", ""))
        guild_id = str(data.get("guild_id", ""))
        if not guild_id.isdigit():
            raise ApiError("Choisis un serveur Discord")
        template = builder.parse_template(str(data.get("content", "")))
        api = self.api(token)
        await api.__aenter__()
        try:
            state = await builder.load_state(api, guild_id)
            plan = builder.build_plan(template, state, mirror=bool(data.get("mirror")))
        except BaseException:
            await api.__aexit__()
            raise
        return api, state, plan

    async def builder_plan(self, request: web.Request) -> web.Response:
        api, state, plan = await self._plan(request, await self.body(request))
        await api.__aexit__()
        return web.json_response({**plan.public(), "guild": state.guild.get("name")})

    async def builder_apply(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        api, state, plan = await self._plan(request, data)
        user = request[USER]
        job = builder.new_job(user["id"], state.id, plan, bool(data.get("confirm_delete")))
        await self.db.log(user["id"], "Modèle appliqué", detail=f"{state.guild.get('name')} ({len(plan.actions)} actions)")

        async def run() -> None:
            try:
                await builder.apply_plan(api, state, plan, job)
            finally:
                await api.__aexit__()

        self._tasks.append(asyncio.create_task(run()))
        self._tasks = [t for t in self._tasks if not t.done()]
        return web.json_response(job.public())

    async def builder_job(self, request: web.Request) -> web.Response:
        job = builder.JOBS.get(request.match_info["job"])
        if job is None or job.owner_id != request[USER]["id"]:
            raise ApiError("Tâche introuvable", 404)
        return web.json_response(job.public())

    async def builder_export(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        _, token = await self._token(request, data.get("bot_id", ""))
        async with self.api(token) as api:
            text = await builder.export_guild(api, str(data.get("guild_id", "0")))
        return web.json_response({"content": text})

    async def builder_import(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        _, token = await self._token(request, data.get("bot_id", ""))
        async with self.api(token) as api:
            text = await builder.import_discord_template(api, str(data.get("code", "")))
        return web.json_response({"content": text})

    # ------------------------------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------------------------------
    async def admin_users(self, request: web.Request) -> web.Response:
        users = []
        for u in await self.db.list_users():
            users.append({**self._user_public(u), "usage": await self._usage(u),
                          "from_env": u["id"] in self.settings.admin_ids})
        return web.json_response(users)

    async def admin_add_user(self, request: web.Request) -> web.Response:
        uid = str((await self.body(request)).get("id", "")).strip()
        if not uid.isdigit() or not 15 <= len(uid) <= 21:
            raise ApiError("ID Discord invalide (clic droit sur le profil › Copier l'identifiant)")
        s = self.settings
        user = await self.db.add_user(int(uid), "user", (s.default_max_bots, s.default_max_ram, s.default_max_disk))
        if user["role"] == "pending":
            user = await self.db.update_user(int(uid), role="user")
        await self.db.log(request[USER]["id"], "Compte ajouté", detail=uid)
        return web.json_response(self._user_public(user))

    async def admin_update_user(self, request: web.Request) -> web.Response:
        uid = int(request.match_info["uid"])
        target = await self.db.get_user(uid)
        if target is None:
            raise ApiError("Compte introuvable", 404)
        data = await self.body(request)
        fields: dict = {}
        if "role" in data:
            if data["role"] not in ("admin", "user", "pending", "blocked"):
                raise ApiError("Rôle invalide")
            if uid in self.settings.admin_ids and data["role"] != "admin":
                raise ApiError("Ce compte est admin dans .env (ADMIN_IDS) : enlève-le de là d'abord")
            if uid == request[USER]["id"] and data["role"] != "admin":
                raise ApiError("Tu peux pas t'enlever toi-même le rôle admin")
            fields["role"] = data["role"]
        for key, hi in (("max_bots", 100), ("max_ram_mb", 262144), ("max_disk_mb", 1048576)):
            if key in data:
                try:
                    value = int(data[key])
                except (TypeError, ValueError):
                    raise ApiError("Limite invalide") from None
                if not 0 <= value <= hi:
                    raise ApiError(f"Limite hors bornes (0 à {hi})")
                fields[key] = value
        user = await self.db.update_user(uid, **fields)
        if fields.get("role") in ("blocked", "pending"):
            await self.db.delete_user_sessions(uid)
            for bot in await self.db.list_bots(uid):  # un compte bloqué fait plus rouler de bots
                await self.db.update_bot(bot["id"], wanted_running=0)
                if self.docker_ok:
                    await self.runtime.stop(bot["id"])
        await self.db.log(request[USER]["id"], "Compte modifié", detail=f"{target['name']} : {fields}")
        return web.json_response(self._user_public(user))

    async def admin_system(self, request: web.Request) -> web.Response:
        info = {"docker": self.docker_ok, "docker_error": self.docker_error, "version": __version__,
                "host_data_dir": str(self.runtime.host_data_dir), "network": self.settings.bot_network}
        if self.docker_ok:
            v = await self.runtime.docker.version()
            info["docker_version"] = v.get("Version")
        bots = await self.db.list_bots()
        statuses = await asyncio.gather(*(self.runtime.status(b["id"]) for b in bots)) if self.docker_ok else []
        info["bots"] = len(bots)
        info["running"] = sum(1 for s in statuses if s["state"] == "running")
        info["activity"] = [{"action": a["action"], "detail": a["detail"], "at": a["at"], "user": a["user_name"],
                             "bot": a["bot_name"]} for a in await self.db.recent_activity()]
        return web.json_response(info)

    # ------------------------------------------------------------------------------------------
    # Surveillance : redémarre les bots tombés, arrête ceux qui plantent en boucle
    # ------------------------------------------------------------------------------------------
    async def watchdog(self, interval: float = 5) -> None:
        first = True
        tick = 0
        while True:
            try:
                await self._watch_once(boot=first)
                if tick % 12 == 0:
                    await self._check_quotas()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Erreur dans la surveillance des bots")
            first = False
            tick += 1
            await asyncio.sleep(interval)

    async def _watch_once(self, *, boot: bool = False) -> None:
        for bot in await self.db.list_bots():
            if not bot["wanted_running"] or bot["id"] in self.busy:
                continue
            status = await self.runtime.status(bot["id"])
            if status["state"] == "running":
                continue
            console = self.runtime.console
            why = "manque de RAM" if status["oom"] else f"code de sortie {status['exit_code']}"
            if boot or status["exit_code"] is None:
                console.publish(bot["id"], "Démarrage automatique (le panel a redémarré).")
            elif bot["auto_restart"] and self.runtime.crashed(bot["id"]):
                console.publish(bot["id"], f"Le bot s'est arrêté ({why}). Redémarrage automatique…")
            else:
                await self.db.update_bot(bot["id"], wanted_running=0)
                extra = " Il plante en boucle : je le redémarre plus tout seul. Regarde les erreurs plus haut." if bot["auto_restart"] else ""
                console.publish(bot["id"], f"Le bot s'est arrêté ({why}).{extra}")
                await self.db.log(None, "Arrêté (plantage)", bot_id=bot["id"], detail=why)
                continue
            self.busy.add(bot["id"])
            try:
                await self.start_bot(bot)
            except Exception as e:  # noqa: BLE001
                await self.db.update_bot(bot["id"], wanted_running=0)
                console.publish(bot["id"], f"Impossible de démarrer : {e}")
            finally:
                self.busy.discard(bot["id"])

    async def _check_quotas(self) -> None:
        for user in await self.db.list_users():
            if user["role"] == "admin":
                continue
            bots = await self.db.list_bots(user["id"])
            used = sum([await self.disk_bytes(b["id"], fresh=True) for b in bots])
            if used <= user["max_disk_mb"] * 1024 * 1024 * 1.1:
                continue
            for bot in bots:
                if bot["wanted_running"]:
                    await self.db.update_bot(bot["id"], wanted_running=0)
                    self.runtime.console.publish(bot["id"], f"Espace disque dépassé ({used // 1024 // 1024} Mo sur "
                                                            f"{user['max_disk_mb']} Mo) : bot arrêté. Libère de la place.")
                    await self.runtime.stop(bot["id"])


def _quote(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")
