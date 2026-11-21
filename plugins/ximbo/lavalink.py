"""Lavalink embarqué : Java, Lavalink et ses plugins (youtube-source, LavaSrc) téléchargés dans le dossier de données
du plugin, puis lancés à côté du bot, dans le même conteneur.

Pourquoi dans le bot : dans Bot Panel, chaque bot est isolé (pas d'accès au réseau local ni aux autres bots), donc un
Lavalink à part serait injoignable. Tout reste dans data/plugins/ximbo/lavalink/ et survit aux mises à jour.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import platform
import re
import secrets
import shutil
import tarfile
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("plugin.ximbo.lavalink")

GITHUB_RELEASES = "https://api.github.com/repos/{repo}/releases?per_page=20"
ADOPTIUM_JRE = "https://api.adoptium.net/v3/binary/latest/21/ga/linux/{arch}/jre/hotspot/normal/eclipse"
# nom -> (dépôt GitHub, fichier à prendre dans la version, préfixe de version voulu)
COMPONENTS: dict[str, tuple[str, re.Pattern, str]] = {
    "lavalink": ("lavalink-devs/Lavalink", re.compile(r"^Lavalink\.jar$"), "4."),
    "youtube": ("lavalink-devs/youtube-source", re.compile(r"^youtube-plugin-[\d.]+\.jar$"), ""),
    "lavasrc": ("topi314/LavaSrc", re.compile(r"^lavasrc-plugin-[\d.]+\.jar$"), "4."),
}
CHECK_EVERY = 12 * 3600  # on regarde s'il y a des mises à jour (YouTube change souvent) au plus deux fois par jour
READY_LINE = "ready to accept connections"
ARCHES = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}


class LavalinkError(Exception):
    pass


# ---- Petites fonctions (testées) ------------------------------------------------------------------
def java_arch(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    if machine not in ARCHES:
        raise LavalinkError(f"processeur pas supporté par Java/Lavalink : {machine}")
    return ARCHES[machine]


def pick_release(releases: list[dict], pattern: re.Pattern, prefix: str = "") -> tuple[str, str] | None:
    """(version, adresse du fichier) de la plus récente version stable qui a le bon fichier."""
    for rel in releases:
        tag = str(rel.get("tag_name") or "").lstrip("v")
        if rel.get("draft") or rel.get("prerelease") or not tag.startswith(prefix):
            continue
        for asset in rel.get("assets") or []:
            if pattern.match(str(asset.get("name") or "")):
                return tag, str(asset["browser_download_url"])
    return None


def container_memory_mb() -> int | None:
    """La limite de RAM du conteneur (cgroup v2 ou v1), si on la trouve."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 50:
            return int(raw) // (1024 * 1024)
    return None


def heap_mb(limit_mb: int | None, wanted: str | None = None) -> int:
    """Mémoire donnée à Java : la variable XIMBO_JAVA_MB, sinon ~45 % de la RAM du bot (entre 256 et 768 Mo)."""
    if wanted and wanted.strip().isdigit():
        return max(128, int(wanted.strip()))
    if not limit_mb:
        return 512
    return max(256, min(768, int(limit_mb * 0.45)))


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # une chaîne JSON est une chaîne YAML valide


def render_config(port: int, password: str, spotify_id: str = "", spotify_secret: str = "", country: str = "CA") -> str:
    spotify = bool(spotify_id and spotify_secret)
    lines = [
        "server:",
        f"  port: {port}",
        "  address: 127.0.0.1",
        "lavalink:",
        "  plugins: []",
        "  pluginsDir: ./plugins",
        "  server:",
        f"    password: {_q(password)}",
        "    sources:",
        "      youtube: false",  # remplacé par le plugin youtube-source
        "      bandcamp: true",
        "      soundcloud: true",
        "      twitch: true",
        "      vimeo: true",
        "      http: true",
        "      local: false",
        "      nico: false",
        "    bufferDurationMs: 400",
        "    frameBufferDurationMs: 5000",
        "    opusEncodingQuality: 10",
        "    resamplingQuality: MEDIUM",
        "    trackStuckThresholdMs: 10000",
        "    youtubePlaylistLoadLimit: 6",
        "    playerUpdateInterval: 5",
        "    youtubeSearchEnabled: true",
        "    soundcloudSearchEnabled: true",
        "plugins:",
        "  youtube:",
        "    enabled: true",
        "    allowSearch: true",
        "    allowDirectVideoIds: true",
        "    allowDirectPlaylistIds: true",
        '    clients: ["MUSIC", "ANDROID_VR", "WEB", "WEBEMBEDDED"]',
        "  lavasrc:",
        "    providers:",  # Spotify diffuse pas l'audio : LavaSrc trouve le même titre ailleurs, dans cet ordre
        "      - " + _q('ytmsearch:"%ISRC%"'),
        "      - " + _q("ytmsearch:%QUERY%"),
        "      - " + _q("ytsearch:%QUERY%"),
        "      - " + _q("scsearch:%QUERY%"),
        "    sources:",
        f"      spotify: {'true' if spotify else 'false'}",
        "      applemusic: false",
        "      deezer: false",
        "      yandexmusic: false",
        "      youtube: false",
    ]
    if spotify:
        lines += [
            "    spotify:",
            f"      clientId: {_q(spotify_id)}",
            f"      clientSecret: {_q(spotify_secret)}",
            f"      countryCode: {_q(country)}",
            "      playlistLoadLimit: 10",
            "      albumLoadLimit: 6",
            # Depuis février 2026, une app Spotify en « Development mode » reçoit plus le contenu des playlists
            # (seulement celles de l'utilisateur connecté, et Ximbo en connecte aucun) : playlists, albums et
            # recommandations (autoplay) passent par l'API du lecteur web, avec retour à l'API officielle si elle lâche.
            "      preferPartnerApi: true",
            "      preferV1SearchApi: true",  # la recherche reste sur l'API officielle (plus rapide)
            "      resolveArtistsInSearch: false",  # GET /artists?ids= retiré pour ces apps : ferait planter la recherche
        ]
    lines += ["logging:", "  level:", "    root: WARN", "    lavalink: INFO"]
    return "\n".join(lines) + "\n"


# ---- Le gestionnaire ------------------------------------------------------------------------------
class LavalinkManager:
    """Installe Lavalink au besoin, le lance, le surveille et le relance s'il plante."""

    def __init__(self, folder: Path, *, port: int = 2333, spotify: tuple[str, str] = ("", "")) -> None:
        self.folder = folder
        self.port = port
        self.spotify = spotify
        self.status = "en attente du démarrage du bot"
        self.process: asyncio.subprocess.Process | None = None
        self.last_lines: collections.deque[str] = collections.deque(maxlen=15)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        if not self.state.get("password"):
            self.state["password"] = secrets.token_urlsafe(24)
            self._save_state()

    # état gardé entre les démarrages
    @property
    def _state_file(self) -> Path:
        return self.folder / "state.json"

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        self._state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    @property
    def password(self) -> str:
        return self.state["password"]

    @property
    def uri(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def java(self) -> Path:
        return self.folder / "jre" / "bin" / "java"

    # ---- boucle principale ----
    async def run(self) -> None:
        failures = 0
        while True:
            started = time.monotonic()
            try:
                await self.install()
                await self._start()
                code = await self.process.wait()
                self.status = f"arrêté (code {code}), redémarrage…"
                log.warning("Lavalink s'est arrêté (code %s). Dernières lignes :\n%s", code, "\n".join(self.last_lines))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status = f"erreur : {e}"
                log.error("Lavalink : %s", e, exc_info=not isinstance(e, (LavalinkError, aiohttp.ClientError)))
            failures = 0 if time.monotonic() - started > 600 else failures + 1
            await asyncio.sleep(min(300, 5 * 2 ** min(failures, 6)))

    async def stop(self) -> None:
        proc, self.process = self.process, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    # ---- installation ----
    async def install(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Ximbo-BotPanel"}) as http:
            await self._install_java(http)
            await self._install_components(http)

    async def _install_java(self, http: aiohttp.ClientSession) -> None:
        if self.java.is_file():
            return
        self.status = "téléchargement de Java (~50 Mo)…"
        log.info("Premier démarrage : téléchargement de Java 21 (une seule fois)")
        archive = self.folder / "jre.tar.gz"
        await self._download(http, ADOPTIUM_JRE.format(arch=java_arch()), archive)
        self.status = "installation de Java…"
        await asyncio.to_thread(self._extract_java, archive)
        archive.unlink(missing_ok=True)
        if not self.java.is_file():
            raise LavalinkError("Java mal installé (java introuvable après l'extraction)")

    def _extract_java(self, archive: Path) -> None:
        tmp = self.folder / "jre.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:  # vieux Python sans « filter »
                tar.extractall(tmp)  # noqa: S202
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        if len(tops) != 1:
            raise LavalinkError("archive de Java inattendue")
        shutil.rmtree(self.folder / "jre", ignore_errors=True)
        tops[0].rename(self.folder / "jre")
        shutil.rmtree(tmp, ignore_errors=True)
        self.java.chmod(0o755)

    def _target(self, name: str, filename: str) -> Path:
        return self.folder / ("Lavalink.jar" if name == "lavalink" else Path("plugins") / filename)

    async def _install_components(self, http: aiohttp.ClientSession) -> None:
        versions: dict = self.state.setdefault("versions", {})
        files: dict = self.state.setdefault("files", {})
        have_all = all(name in files and (self.folder / files[name]).is_file() for name in COMPONENTS)
        if have_all and time.time() - self.state.get("checked", 0) < CHECK_EVERY:
            return
        (self.folder / "plugins").mkdir(exist_ok=True)
        for name, (repo, pattern, prefix) in COMPONENTS.items():
            try:
                async with http.get(GITHUB_RELEASES.format(repo=repo)) as resp:
                    resp.raise_for_status()
                    found = pick_release(await resp.json(), pattern, prefix)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if name in files and (self.folder / files[name]).is_file():
                    log.warning("Mise à jour de %s pas vérifiée (%s) : on garde la version %s", name, e, versions.get(name))
                    continue
                raise LavalinkError(f"impossible de télécharger {name} ({e})") from e
            if found is None:
                if name in files:
                    continue
                raise LavalinkError(f"aucune version de {name} trouvée sur GitHub")
            tag, url = found
            filename = url.rsplit("/", 1)[-1]
            target = self._target(name, filename)
            if versions.get(name) == tag and target.is_file():
                continue
            self.status = f"téléchargement de {name} {tag}…"
            log.info("Téléchargement de %s %s", name, tag)
            await self._download(http, url, target)
            old = files.get(name)
            if old and old != target.relative_to(self.folder).as_posix():
                (self.folder / old).unlink(missing_ok=True)
            versions[name] = tag
            files[name] = target.relative_to(self.folder).as_posix()
        self.state["checked"] = time.time()
        self._save_state()

    async def _download(self, http: aiohttp.ClientSession, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        async with http.get(url) as resp:
            resp.raise_for_status()
            with part.open("wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    f.write(chunk)
        if part.stat().st_size < 1024:
            part.unlink(missing_ok=True)
            raise LavalinkError(f"fichier téléchargé vide : {url}")
        part.replace(dest)

    # ---- lancement ----
    async def _start(self) -> None:
        (self.folder / "application.yml").write_text(
            render_config(self.port, self.password, *self.spotify), encoding="utf-8")
        tmp = self.folder / "tmp"
        tmp.mkdir(exist_ok=True)
        heap = heap_mb(container_memory_mb(), os.getenv("XIMBO_JAVA_MB"))
        args = [str(self.java), f"-Xmx{heap}m", "-Xss512k", "-XX:+UseG1GC", "-XX:MaxGCPauseMillis=50",
                "-XX:-UsePerfData", "-XX:ActiveProcessorCount=2", f"-Djava.io.tmpdir={tmp}", "-jar", "Lavalink.jar"]
        self.status = "démarrage de Lavalink…"
        log.info("Démarrage de Lavalink %s (mémoire Java : %d Mo)", self.state.get("versions", {}).get("lavalink", "?"), heap)
        self.process = await asyncio.create_subprocess_exec(
            *args, cwd=self.folder, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "JAVA_HOME": str(self.folder / "jre")},
        )
        self._pump_task = asyncio.create_task(self._pump(self.process))

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        """Lit la sortie de Lavalink : garde les dernières lignes, montre les erreurs, repère « prêt »."""
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self.last_lines.append(text)
            if READY_LINE in text:
                self.status = "prêt"
                log.info("Lavalink est prêt")
            elif " ERROR " in text or " WARN " in text or "Exception" in text:
                log.warning("[Lavalink] %s", text[-400:])
