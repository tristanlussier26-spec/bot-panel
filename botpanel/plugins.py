"""Plugins des bots : des modules qu'on ajoute à un bot depuis l'onglet « Plugins ».

Un plugin vient du catalogue (un index.json, sur GitHub par défaut) ou d'un .zip téléversé.
Le panel le décompresse dans plugins/<id>/ du bot ; c'est le bot qui le charge à son démarrage
(il doit supporter les plugins Bot Panel, comme le modèle Bot NG : voir README, « Plugins »).
Le code d'un plugin roule seulement dans le conteneur du bot, jamais dans le panel.

Format d'un plugin (format 1) :
  plugin.json        id, name, version, description, author, runtime, variables, pages, enabled_by
  __init__.py        extension discord.py : async def setup(bot)
  requirements.txt   (optionnel) dépendances pip, installées au démarrage du bot
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import time
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import aiohttp

from botpanel.files import BotFiles, FileError

FORMAT = 1
RUNTIMES = {"discord.py"}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")  # aussi un nom de module Python valide
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}([-+][0-9A-Za-z.]{1,20})?$")
VAR_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
INTENTS = {"members": "Server Members Intent", "message_content": "Message Content Intent", "presences": "Presence Intent"}
# Une ligne de requirements.txt : un paquet de PyPI et des versions, rien d'autre (pas d'URL ni d'option pip)
REQ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(\[[A-Za-z0-9._,-]{1,80}\])?\s*((==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]{1,40}\s*,?\s*){0,4}$")
EXTENSIONS = {".py", ".json", ".txt", ".md", ".toml", ".yml", ".yaml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ttf", ".otf"}
MAX_FILES = 300
MAX_INDEX_BYTES = 512 * 1024
MAX_HOST_BYTES = 64 * 1024
GITHUB_HOSTS = {"raw.githubusercontent.com", "github.com", "objects.githubusercontent.com", "codeload.github.com"}
CATALOG_TTL = 600


class PluginError(Exception):
    """Erreur affichable à l'utilisateur."""


def _text(raw: dict, key: str, limit: int, *, required: bool = True) -> str:
    value = raw.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit or "\x00" in value:
        raise PluginError(f"plugin.json : « {key} » manquant ou invalide")
    return value.strip()


def validate_manifest(raw: object, *, reserved_env: set[str] = frozenset()) -> dict:
    """Vérifie plugin.json et retourne une version propre (ce que le panel affiche et utilise)."""
    if not isinstance(raw, dict):
        raise PluginError("plugin.json invalide")
    if raw.get("format") != FORMAT:
        raise PluginError(f"Format de plugin pas supporté (format {FORMAT} attendu)")
    plugin_id = _text(raw, "id", 32)
    if not ID_RE.match(plugin_id):
        raise PluginError("plugin.json : l'id doit être en minuscules (lettres, chiffres, _), 2 à 32 caractères")
    version = _text(raw, "version", 30)
    if not VERSION_RE.match(version):
        raise PluginError("plugin.json : la version doit ressembler à 1.2.3")
    runtime = _text(raw, "runtime", 30)
    if runtime not in RUNTIMES:
        raise PluginError(f"plugin.json : runtime « {runtime} » pas supporté ({', '.join(sorted(RUNTIMES))})")
    variables = []
    for var in raw.get("variables") or []:
        if not isinstance(var, dict):
            raise PluginError("plugin.json : variable invalide")
        key = _text(var, "key", 64)
        if not VAR_KEY_RE.match(key) or key in reserved_env:
            raise PluginError(f"plugin.json : nom de variable invalide ou réservé : {key}")
        variables.append({"key": key, "label": _text(var, "label", 80), "help": _text(var, "help", 300, required=False),
                          "secret": bool(var.get("secret")), "required": bool(var.get("required"))})
    if len(variables) > 20 or len({v["key"] for v in variables}) != len(variables):
        raise PluginError("plugin.json : variables en double ou trop nombreuses (20 max)")
    pages = raw.get("pages") or []
    if not isinstance(pages, list) or len(pages) > 20 or not all(isinstance(p, dict) and isinstance(p.get("fields"), list) for p in pages):
        raise PluginError("plugin.json : « pages » invalide")
    intents = raw.get("intents") or []
    if not isinstance(intents, list) or not set(intents) <= set(INTENTS):
        raise PluginError(f"plugin.json : « intents » invalide (permis : {', '.join(INTENTS)})")
    enabled_by = raw.get("enabled_by") or ""
    if enabled_by and not re.match(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", str(enabled_by)):
        raise PluginError("plugin.json : enabled_by doit ressembler à « section.cle »")
    return {
        "format": FORMAT, "id": plugin_id, "name": _text(raw, "name", 60), "version": version,
        "description": _text(raw, "description", 500, required=False), "author": _text(raw, "author", 60, required=False),
        "runtime": runtime, "variables": variables, "pages": pages, "enabled_by": enabled_by,
        "intents": sorted(set(intents)),
    }


def version_key(version: str) -> tuple:
    main, _, extra = version.partition("-")
    parts = tuple(int(x) for x in re.findall(r"\d+", main)[:3])
    return parts + ((1,) if not extra else (0, extra))  # 1.0.0 passe après 1.0.0-beta


def read_zip(data: bytes, *, max_bytes: int, reserved_env: set[str] = frozenset()) -> tuple[dict, dict[str, bytes]]:
    """Lit et vérifie un plugin en .zip. Retourne (manifeste, {chemin dans le plugin: contenu})."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PluginError("C'est pas un fichier .zip valide") from None
    with z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if not members:
            raise PluginError("Le zip est vide")
        if len(members) > MAX_FILES:
            raise PluginError(f"Trop de fichiers dans le plugin ({MAX_FILES} max)")
        if sum(m.file_size for m in members) > max_bytes:
            raise PluginError(f"Plugin trop gros une fois décompressé ({max_bytes // 1024 // 1024} Mo max)")
        names = [m.filename.replace("\\", "/") for m in members]
        # plugin.json à la racine du zip, ou dans un seul dossier (zip fait en compressant le dossier du plugin)
        prefix = ""
        if "plugin.json" not in names:
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) == 1 and f"{next(iter(tops))}/plugin.json" in names:
                prefix = next(iter(tops)) + "/"
            else:
                raise PluginError("plugin.json introuvable à la racine du zip")
        files: dict[str, bytes] = {}
        for member, name in zip(members, names):
            if stat.S_ISLNK(member.external_attr >> 16):
                raise PluginError("Les liens symboliques sont pas permis dans un plugin")
            if not name.startswith(prefix):
                raise PluginError("Tous les fichiers du plugin doivent être dans le même dossier")
            rel = name[len(prefix):]
            parts = PurePosixPath(rel).parts
            if not parts or name.startswith("/") or any(p in ("..", ".", "") for p in parts) or len(rel) > 200:
                raise PluginError(f"Chemin invalide dans le zip : {name}")
            if "__pycache__" in parts or rel.endswith((".pyc", ".DS_Store")):
                continue
            if PurePosixPath(rel).suffix.lower() not in EXTENSIONS:
                raise PluginError(f"Type de fichier pas permis dans un plugin : {rel}")
            files[rel] = z.read(member)
    try:
        raw = json.loads(files["plugin.json"].decode("utf-8"))
    except ValueError as e:  # UnicodeDecodeError aussi
        raise PluginError(f"plugin.json illisible ({e})") from None
    manifest = validate_manifest(raw, reserved_env=reserved_env)
    if "__init__.py" not in files:
        raise PluginError("__init__.py manquant (c'est lui que le bot charge)")
    if "requirements.txt" in files:
        for line in files["requirements.txt"].decode("utf-8", "replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not REQ_RE.match(line):
                raise PluginError(f"requirements.txt : ligne pas permise « {line[:60]} » (seulement des paquets PyPI)")
    return manifest, files


# ---- Dans le dossier d'un bot ------------------------------------------------------------------
def _read_json(files: BotFiles, rel: str, limit: int) -> object | None:
    try:
        path = files.path(rel)
    except FileError:
        return None
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed(files: BotFiles, *, reserved_env: set[str] = frozenset()) -> list[dict]:
    """Plugins dans plugins/ du bot (un plugin brisé est listé avec son erreur, pour pouvoir le retirer)."""
    try:
        folder = files.path("plugins")
    except FileError:
        return []
    if not folder.is_dir() or folder.is_symlink():
        return []
    found = []
    for sub in sorted(folder.iterdir(), key=lambda p: p.name):
        if not sub.is_dir() or sub.is_symlink() or sub.name.startswith((".", "_")) or not ID_RE.match(sub.name):
            continue
        raw = _read_json(files, f"plugins/{sub.name}/plugin.json", MAX_HOST_BYTES)
        try:
            manifest = validate_manifest(raw, reserved_env=reserved_env)
            if manifest["id"] != sub.name:
                raise PluginError("l'id de plugin.json est pas le nom du dossier")
            found.append({**manifest, "error": None})
        except PluginError as e:
            found.append({"id": sub.name, "name": sub.name, "version": "?", "description": "", "author": "",
                          "variables": [], "pages": [], "error": str(e)})
    return found


def host_report(files: BotFiles) -> dict | None:
    """Ce que le bot dit de ses plugins à son dernier démarrage (.botpanel/host.json), ou None s'il en sait rien."""
    raw = _read_json(files, ".botpanel/host.json", MAX_HOST_BYTES)
    if not isinstance(raw, dict) or not isinstance(raw.get("plugins"), dict):
        return None
    plugins = {}
    for pid, info in list(raw["plugins"].items())[:100]:
        if isinstance(pid, str) and ID_RE.match(pid) and isinstance(info, dict):
            plugins[pid] = {"loaded": bool(info.get("loaded")), "version": str(info.get("version") or "")[:30],
                            "error": str(info["error"])[:300] if info.get("error") else None}
    return {"runtime": str(raw.get("runtime") or "")[:30], "plugins": plugins}


def install(files: BotFiles, manifest: dict, contents: dict[str, bytes]) -> None:
    """Installe (ou remplace) plugins/<id>/. Les données du plugin sont ailleurs : rien n'est perdu à la mise à jour."""
    pid = manifest["id"]
    files.ensure()
    files.mkdir("plugins")
    tmp, old, target = f"plugins/.{pid}.nouveau", f"plugins/.{pid}.ancien", f"plugins/{pid}"
    for leftover in (tmp, old):
        if files.path(leftover, allow_link=True).exists() or files.path(leftover, allow_link=True).is_symlink():
            files.delete(leftover)
    for rel, data in contents.items():
        files.write_bytes(f"{tmp}/{rel}", data)
    target_path = files.path(target, allow_link=True)
    if target_path.exists() or target_path.is_symlink():
        files.rename(target, old)
    files.rename(tmp, target)
    if files.path(old, allow_link=True).exists():
        files.delete(old)


def remove(files: BotFiles, plugin_id: str) -> None:
    if not ID_RE.match(plugin_id):
        raise PluginError("Plugin introuvable")
    path = files.path(f"plugins/{plugin_id}", allow_link=True)
    if not (path.exists() or path.is_symlink()):
        raise PluginError("Plugin introuvable")
    files.delete(f"plugins/{plugin_id}")


# ---- Catalogue -------------------------------------------------------------------------------------
class Catalog:
    """Le catalogue de plugins : un index.json (sur GitHub par défaut) qui pointe vers des .zip avec leur sha256."""

    def __init__(self, url: str, *, max_bytes: int) -> None:
        self.url = url
        self.max_bytes = max_bytes
        self._cache: tuple[float, list[dict]] | None = None

    def _allowed(self, url: str) -> bool:
        """Un .zip vient du même site que le catalogue, ou de GitHub en https."""
        target, index = urlsplit(url), urlsplit(self.url)
        if target.scheme == "https" and target.hostname in GITHUB_HOSTS:
            return True
        return (target.scheme, target.netloc) == (index.scheme, index.netloc) and target.scheme in ("http", "https")

    async def _fetch(self, url: str, limit: int) -> bytes:
        """Télécharge en suivant au plus 3 redirections, chacune vérifiée (jamais vers un site pas permis)."""
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "BotPanel"}) as http:
                for _ in range(4):
                    async with http.get(url, allow_redirects=False) as resp:
                        if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                            url = urljoin(url, resp.headers["Location"])
                            if not self._allowed(url):
                                raise PluginError("Le catalogue redirige vers un site pas permis")
                            continue
                        if resp.status != 200:
                            raise PluginError(f"Le catalogue répond HTTP {resp.status}")
                        # Le fichier arrive en morceaux : on lit jusqu'au bout (read(n) donne juste ce qui est arrivé)
                        chunks, size = [], 0
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise PluginError("Fichier du catalogue trop gros")
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        break
                else:
                    raise PluginError("Trop de redirections dans le catalogue")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PluginError(f"Catalogue injoignable ({type(e).__name__})") from None
        return data

    async def entries(self, *, force: bool = False) -> list[dict]:
        if not self.url:
            return []
        if not force and self._cache and time.monotonic() - self._cache[0] < CATALOG_TTL:
            return self._cache[1]
        try:
            raw = json.loads(await self._fetch(self.url, MAX_INDEX_BYTES))
        except ValueError:
            raise PluginError("index.json du catalogue illisible") from None
        if not isinstance(raw, dict) or raw.get("format") != FORMAT or not isinstance(raw.get("plugins"), list):
            raise PluginError("index.json du catalogue invalide")
        entries = []
        for item in raw["plugins"][:200]:
            try:
                manifest = validate_manifest({**item, "format": FORMAT})
                download = urljoin(self.url, _text(item, "download", 500))
                sha = _text(item, "sha256", 64).lower()
                if not SHA_RE.match(sha) or not self._allowed(download):
                    raise PluginError("téléchargement invalide")
            except PluginError:
                continue  # une entrée brisée cache pas les autres
            entries.append({k: manifest[k] for k in ("id", "name", "version", "description", "author", "runtime")}
                           | {"download": download, "sha256": sha})
        self._cache = (time.monotonic(), entries)
        return entries

    async def find(self, plugin_id: str) -> dict:
        entry = next((e for e in await self.entries() if e["id"] == plugin_id), None)
        if entry is None:
            raise PluginError("Ce plugin est pas dans le catalogue")
        return entry

    async def download(self, entry: dict) -> bytes:
        data = await self._fetch(entry["download"], self.max_bytes)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise PluginError("Le .zip téléchargé correspond pas au catalogue (sha256) : installation annulée")
        return data
