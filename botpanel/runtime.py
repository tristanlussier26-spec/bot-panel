"""Exécution des bots dans des conteneurs Docker isolés (un conteneur par bot, comme Pterodactyl/Wings).

Chaque bot :
  - roule avec l'image de son langage (Python, Node ou une image perso) ;
  - voit seulement son dossier, monté dans /home/container ;
  - roule en utilisateur non-root, sans capacités Linux, système de fichiers en lecture seule (sauf son dossier et /tmp) ;
  - a une limite de RAM, de CPU et de processus.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import socket
import time
from datetime import datetime
from collections import defaultdict, deque
from pathlib import Path
from typing import AsyncIterator

import aiodocker
from aiodocker.exceptions import DockerError

from botpanel.config import Settings

log = logging.getLogger("botpanel.runtime")

WORKDIR = "/home/container"
LABEL = "botpanel.bot"
CONSOLE_HISTORY = 200
CRASH_WINDOW = 60  # deux plantages en moins de 60 s = on arrête de redémarrer


class RuntimeError_(Exception):
    """Erreur affichable à l'utilisateur."""


def startup_command(bot: dict) -> str:
    """Commande de démarrage : celle du bot, ou celle par défaut de son langage."""
    if (custom := (bot.get("startup_cmd") or "").strip()):
        return custom
    main = shlex.quote(bot["main_file"])
    if bot["language"] == "node":
        return (
            "if [ -f package.json ]; then echo '[Panel] Installation des dépendances (npm)…'; "
            "npm install --omit=dev --no-audit --no-fund --loglevel=error || exit 1; fi; "
            f"exec node {main}"
        )
    # Les dépendances du bot, puis celles de ses plugins (plugins/<id>/requirements.txt)
    return (
        "for req in requirements.txt plugins/*/requirements.txt; do "
        "if [ -f \"$req\" ]; then echo \"[Panel] Installation des dépendances (pip) : $req\"; "
        "python3 -m pip install --user --disable-pip-version-check --no-warn-script-location -q -r \"$req\" "
        "|| exit 1; fi; done; "
        f"exec python3 -u {main}"
    )


def _stamp(stamp: str) -> tuple[int, int]:
    """Horodatage Docker (RFC 3339, nanosecondes) -> (secondes, nanosecondes), sans perte de précision."""
    try:
        main, _, frac = stamp.rstrip("Z").partition(".")
        return int(datetime.fromisoformat(main + "+00:00").timestamp()), int((frac or "0")[:9].ljust(9, "0"))
    except ValueError:
        return 0, 0


class ConsoleHub:
    """Messages du panel (démarrage, plantages…) diffusés aux consoles ouvertes, avec un petit historique."""

    def __init__(self) -> None:
        self.history: defaultdict[str, deque[tuple[float, str]]] = defaultdict(lambda: deque(maxlen=50))
        self.listeners: defaultdict[str, set[asyncio.Queue]] = defaultdict(set)

    def publish(self, bot_id: str, line: str) -> None:
        text = f"[Panel] {line}"
        self.history[bot_id].append((time.time(), text))
        for queue in list(self.listeners[bot_id]):
            if not queue.full():
                queue.put_nowait(text)

    def subscribe(self, bot_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.listeners[bot_id].add(queue)
        return queue

    def unsubscribe(self, bot_id: str, queue: asyncio.Queue) -> None:
        self.listeners[bot_id].discard(queue)


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.docker: aiodocker.Docker | None = None
        self.host_data_dir: Path = settings.data_dir
        self.console = ConsoleHub()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_crash: dict[str, float] = {}

    # ---- Connexion à Docker ------------------------------------------------------------
    async def connect(self) -> None:
        self.docker = aiodocker.Docker()
        try:
            version = await self.docker.version()
        except Exception as e:
            raise RuntimeError_(f"Docker est pas joignable ({e}). Le panel doit avoir accès à /var/run/docker.sock.") from e
        self.host_data_dir = self.settings.host_data_dir or await self._detect_host_data_dir() or self.settings.data_dir
        await self.ensure_network()
        log.info("Docker %s connecté, dossiers des bots sur l'hôte : %s", version.get("Version"), self.host_data_dir / "bots")

    async def close(self) -> None:
        if self.docker is not None:
            await self.docker.close()

    async def _detect_host_data_dir(self) -> Path | None:
        """Quand le panel roule lui-même dans Docker, trouve le vrai chemin de son dossier data sur l'hôte."""
        if not Path("/.dockerenv").exists():
            return None
        try:
            me = await self.docker.containers.get(socket.gethostname())
            for mount in (await me.show()).get("Mounts", []):
                if Path(mount.get("Destination", "")) == self.settings.data_dir:
                    return Path(mount["Source"])
        except DockerError:
            pass
        log.warning("Chemin du dossier data sur l'hôte introuvable : mets HOST_DATA_DIR dans .env")
        return None

    async def ensure_network(self) -> None:
        name = self.settings.bot_network
        if any(n.get("Name") == name for n in await self.docker.networks.list()):
            return
        await self.docker.networks.create({
            "Name": name, "Driver": "bridge", "Labels": {"botpanel": "1"},
            "Options": {"com.docker.network.bridge.enable_icc": "false"},  # les bots se voient pas entre eux
        })
        log.info("Réseau Docker « %s » créé pour les bots", name)

    async def ensure_image(self, image: str, bot_id: str) -> None:
        try:
            await self.docker.images.inspect(image)
            return
        except DockerError as e:
            if e.status != 404:
                raise
        self.console.publish(bot_id, f"Téléchargement de l'image {image}… (une seule fois, ça peut prendre une minute)")
        try:
            await self.docker.images.pull(image)
        except DockerError as e:
            raise RuntimeError_(f"Impossible de télécharger l'image {image} : {e.message}") from e

    # ---- Conteneurs ----------------------------------------------------------------------
    @staticmethod
    def name(bot_id: str) -> str:
        return f"botpanel-{bot_id}"

    def host_path(self, bot_id: str) -> str:
        return str(self.host_data_dir / "bots" / bot_id)

    def container_config(self, bot: dict, env: dict[str, str]) -> dict:
        base_env = {
            "HOME": WORKDIR,
            "PYTHONUSERBASE": f"{WORKDIR}/.local",
            "PYTHONUNBUFFERED": "1",
            "NPM_CONFIG_CACHE": f"{WORKDIR}/.npm",
            "PATH": f"{WORKDIR}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "BOT_ID": bot["id"],
        }
        port = bot.get("port")
        if port:  # mêmes noms que Pterodactyl : le bot écoute sur SERVER_PORT
            base_env.update(SERVER_PORT=str(port), SERVER_IP="0.0.0.0")
        all_env = {**env, **base_env}
        ram = int(bot["ram_mb"]) * 1024 * 1024
        config = {
            "Image": bot["image"],
            "Cmd": ["/bin/sh", "-c", startup_command(bot)],
            "WorkingDir": WORKDIR,
            "User": "1000:1000",
            "Env": [f"{k}={v}" for k, v in all_env.items()],
            "Labels": {"botpanel": "1", LABEL: bot["id"]},
            "OpenStdin": True,
            "StdinOnce": False,
            "Tty": False,
            "HostConfig": {
                "Binds": [f"{self.host_path(bot['id'])}:{WORKDIR}"],
                "Memory": ram,
                "MemorySwap": ram,
                "NanoCpus": int(int(bot["cpu_percent"]) / 100 * 1e9),
                "PidsLimit": 256,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m"},
                "NetworkMode": self.settings.bot_network,
                "RestartPolicy": {"Name": "no"},  # c'est le panel qui redémarre (détection des plantages en boucle)
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}},
            },
        }
        if port:
            config["ExposedPorts"] = {f"{port}/tcp": {}}
            config["HostConfig"]["PortBindings"] = {f"{port}/tcp": [{"HostPort": str(port)}]}
        return config

    async def _get(self, bot_id: str):
        try:
            return await self.docker.containers.get(self.name(bot_id))
        except DockerError as e:
            if e.status == 404:
                return None
            raise

    async def start(self, bot: dict, env: dict[str, str]) -> None:
        """(Re)crée le conteneur avec la config actuelle du bot, puis le démarre."""
        async with self._locks[bot["id"]]:
            await self.ensure_image(bot["image"], bot["id"])
            old = await self._get(bot["id"])
            if old is not None:
                await old.delete(force=True)
            container = await self.docker.containers.create(self.container_config(bot, env), name=self.name(bot["id"]))
            await container.start()
            self.console.publish(bot["id"], "Bot démarré.")

    async def stop(self, bot_id: str, *, kill: bool = False) -> None:
        async with self._locks[bot_id]:
            container = await self._get(bot_id)
            if container is None:
                return
            info = await container.show()
            if info["State"]["Running"]:
                if kill:
                    await container.kill()
                    self.console.publish(bot_id, "Bot tué (arrêt forcé).")
                else:
                    self.console.publish(bot_id, "Arrêt du bot…")
                    await container.stop(t=10)
                    self.console.publish(bot_id, "Bot arrêté.")

    async def remove(self, bot_id: str) -> None:
        container = await self._get(bot_id)
        if container is not None:
            await container.delete(force=True)

    async def status(self, bot_id: str) -> dict:
        container = await self._get(bot_id)
        if container is None:
            return {"state": "offline", "exit_code": None, "started_at": None, "oom": False}
        state = (await container.show())["State"]
        status = state.get("Status", "unknown")
        return {
            "state": "running" if state.get("Running") else ("offline" if status in ("exited", "created", "dead") else status),
            "exit_code": state.get("ExitCode"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "oom": bool(state.get("OOMKilled")),
        }

    async def stats(self, bot_id: str) -> dict | None:
        container = await self._get(bot_id)
        if container is None:
            return None
        try:
            data = await container.stats(stream=False)
        except DockerError:
            return None
        data = data[0] if isinstance(data, list) else data
        cpu, pre = data.get("cpu_stats", {}), data.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
        mem = data.get("memory_stats", {})
        usage = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", mem.get("stats", {}).get("cache", 0))
        return {
            "cpu_percent": round(cpu_delta / sys_delta * cpus * 100, 1) if sys_delta > 0 and cpu_delta > 0 else 0.0,
            "memory_mb": round(max(usage, 0) / 1024 / 1024, 1),
            "memory_limit_mb": round(mem.get("limit", 0) / 1024 / 1024),
        }

    async def logs(self, bot_id: str, tail: int = 200) -> list[str]:
        container = await self._get(bot_id)
        if container is None:
            return []
        lines = await container.log(stdout=True, stderr=True, tail=tail)
        return [line.rstrip("\n") for line in lines]

    async def history(self, bot_id: str, tail: int = 200) -> tuple[str | None, list[str], tuple[int, int] | None]:
        """Dernières lignes du bot et messages du panel, dans l'ordre.
        Retourne aussi l'ID du conteneur et l'heure de la dernière ligne (pour suivre la suite sans trou)."""
        container = await self._get(bot_id)
        entries: list[tuple[float, str]] = list(self.console.history[bot_id])
        last = None
        if container is not None:
            for raw in await container.log(stdout=True, stderr=True, tail=tail, timestamps=True):
                stamp, _, text = raw.rstrip("\n").partition(" ")
                last = _stamp(stamp)
                entries.append((last[0] + last[1] / 1e9, text))
        entries.sort(key=lambda e: e[0])
        return (container.id if container is not None else None), [text for _, text in entries[-tail:]], last

    async def container_id(self, bot_id: str) -> tuple[str | None, bool]:
        container = await self._get(bot_id)
        if container is None:
            return None, False
        return container.id, bool((await container.show())["State"]["Running"])

    async def follow_logs(self, bot_id: str, *, from_start: bool = False,
                          after: tuple[int, int] | None = None) -> AsyncIterator[str]:
        """Suit les lignes du conteneur en direct (s'arrête quand le conteneur s'arrête).
        from_start : tout le journal (conteneur tout neuf) ; after : les lignes écrites après cette heure ;
        sinon seulement les nouvelles lignes."""
        container = await self._get(bot_id)
        if container is None:
            return
        params: dict = {"tail": "all"} if from_start else {"tail": "all", "since": after[0]} if after else {"tail": 0}
        async for raw in container.log(stdout=True, stderr=True, follow=True, timestamps=True, **params):
            stamp, _, text = raw.rstrip("\n").partition(" ")
            if after and not from_start and _stamp(stamp) <= after:
                continue
            yield text

    async def send_input(self, bot_id: str, text: str) -> None:
        """Envoie une ligne à l'entrée standard du bot (pour les bots qui lisent des commandes)."""
        container = await self._get(bot_id)
        if container is None or not (await container.show())["State"]["Running"]:
            raise RuntimeError_("Le bot roule pas.")
        stream = container.attach(stdin=True, stdout=False, stderr=False)
        async with stream:
            await stream.write_in((text.rstrip("\n") + "\n").encode())

    # ---- Surveillance : redémarrage automatique ------------------------------------------------
    def crashed(self, bot_id: str) -> bool:
        """Note un plantage. Retourne False si le bot plante en boucle (2 fois en moins de 60 s)."""
        now = time.monotonic()
        last = self._last_crash.get(bot_id)
        self._last_crash[bot_id] = now
        return last is None or now - last > CRASH_WINDOW
