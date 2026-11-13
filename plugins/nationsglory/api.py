"""Client de l'API publique NationsGlory (doc : https://nationsglory.fr/developers).

- Base : https://publicapi.nationsglory.fr/v1
- Clé publique (commence par ngp_) dans l'en-tête X-API-Key
- Pagination avec ?limit= et ?cursor=
- Les membres d'un pays sont mis à jour par NationsGlory aux 5 minutes.
"""
from __future__ import annotations

import asyncio
import re
import socket
import time
from collections import deque
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from .common import NationsGloryError

BASE_URL = "https://publicapi.nationsglory.fr/v1"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PAGE_SIZE = 100
MAX_PAGES = 100
SERVER_RE = re.compile(r"^[a-z0-9_-]{2,24}$")
COUNTRIES_MAX_AGE = 3600  # la liste des pays d'un serveur change rarement


def normalize_target(server: str, country: str) -> tuple[str, str]:
    """Nettoie le serveur et le pays saisis. Accepte l'adresse de la page du pays :
    https://nationsglory.fr/servers/jade/countries/Amour -> ("jade", "Amour")."""
    server, country = str(server or "").strip().lower(), str(country or "").strip()
    if match := re.search(r"/servers/([^/?#]+)", country):
        server = unquote(match.group(1)).strip().lower()
    if match := re.search(r"/countries/([^/?#]+)", country):
        country = unquote(match.group(1)).strip()
    return server, country


class RateLimiter:
    """Limite de requêtes partagée par tous les clients qui utilisent la même clé (60/min chez NationsGlory)."""

    def __init__(self, per_minute: int = 50) -> None:
        self.per_minute = max(1, per_minute)
        self.calls: deque[float] = deque()
        self.paused_until = 0.0
        self._lock: asyncio.Lock | None = None

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    async def wait(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:  # une requête à la fois passe le contrôle, dans l'ordre d'arrivée
            while True:
                now = time.monotonic()
                if now < self.paused_until:
                    await asyncio.sleep(self.paused_until - now)
                    continue
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                await asyncio.sleep(60 - (now - self.calls[0]) + 0.05)


def unwrap(data: Any) -> Any:
    """Certaines réponses emballent l'objet dans {"data": {...}}."""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


def extract_page(data: Any) -> tuple[list[dict], str | None]:
    """Sort (éléments, curseur suivant) d'une réponse paginée, peu importe la forme exacte."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)], None
    if not isinstance(data, dict):
        return [], None
    items: list = []
    for key in ("data", "items", "results", "members", "countries"):
        if isinstance(data.get(key), list):
            items = data[key]
            break
    for container in (data, data.get("meta"), data.get("pagination"), data.get("page"), data.get("links")):
        if not isinstance(container, dict):
            continue
        for key in ("next_cursor", "nextCursor", "cursor_next", "next"):
            value = container.get(key)
            if isinstance(value, str) and value:
                if value.startswith(("http://", "https://", "/")):
                    value = (parse_qs(urlparse(value).query).get("cursor") or [None])[0]
                if value:
                    return [d for d in items if isinstance(d, dict)], value
    return [d for d in items if isinstance(d, dict)], None


def country_name(row: dict) -> str | None:
    return row.get("name") or row.get("country_name")


def country_id(row: dict) -> str | None:
    return row.get("country_id") or row.get("id")


class NationsGlory:
    def __init__(
        self,
        api_key: str,
        server: str,
        *,
        base_url: str = BASE_URL,
        proxy: str | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.server = server.strip().lower()
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy or None  # ex. un VPS avec IP fixe : http://ip-du-vps:3128
        self._session: aiohttp.ClientSession | None = None
        self.limiter = limiter or RateLimiter()
        self._page_size: int | None = PAGE_SIZE
        self._members_cache: dict[str, tuple[float, dict[str, dict]]] = {}
        self._countries: tuple[float, list[dict]] | None = None
        self._countries_lock = asyncio.Lock()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(family=socket.AF_INET),  # IPv4 seulement : une seule IP à autoriser
                headers={"User-Agent": "BotPanel-NationsGlory/1.0"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self.api_key:
            raise NationsGloryError(
                "NG_API_KEY est vide : mets la clé dans l'onglet Plugins de Bot Panel",
                status=401,
                user_message="🔑 La clé de l'API NationsGlory est pas encore configurée par l'hébergeur du bot.",
            )
        for attempt in range(2):
            await self.limiter.wait()
            try:
                async with self._http().get(
                    f"{self.base_url}{path}",
                    params=params,
                    headers={"X-API-Key": self.api_key, "Accept": "application/json"},
                    proxy=self.proxy,
                ) as resp:
                    if resp.status in (404, 410):  # 410 = pays disparu
                        return None
                    if resp.status == 429 and attempt == 0:
                        # Trop de requêtes : tout le monde attend, puis on réessaie une fois
                        try:
                            delay = float(resp.headers.get("Retry-After", 30))
                        except ValueError:
                            delay = 30.0
                        self.limiter.pause(min(max(delay, 1.0), 120.0))
                        continue
                    if resp.status >= 400:
                        detail = (await resp.text())[:300]
                        raise NationsGloryError(
                            f"API NationsGlory : HTTP {resp.status} sur {path} : {detail}", status=resp.status
                        )
                    return await resp.json(content_type=None)
            except NationsGloryError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                raise NationsGloryError(f"API NationsGlory injoignable ({e!r})") from e
        raise NationsGloryError(f"API NationsGlory : trop de requêtes sur {path}", status=429)

    async def _paginate(self, path: str, params: dict | None = None) -> list[dict] | None:
        """Toutes les pages d'une liste. None si la ressource existe pas (404)."""
        items: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            query = dict(params or {})
            if self._page_size:
                query["limit"] = self._page_size
            if cursor:
                query["cursor"] = cursor
            try:
                data = await self._get(path, query)
            except NationsGloryError as e:
                if e.status == 400 and self._page_size and not cursor:
                    self._page_size = None  # limite de 100 refusée : on garde celle par défaut
                    continue
                raise
            if data is None:
                return None if not items else items
            page, cursor = extract_page(data)
            items.extend(page)
            if not cursor or not page:
                break
        return items

    # ---- Pays -------------------------------------------------------------------
    async def resolve_country(self, value: str) -> tuple[str, str]:
        """Accepte un nom de pays (« Amour ») ou un country_id. Retourne (country_id, nom)."""
        value = value.strip()
        if UUID_RE.match(value):
            data = unwrap(await self._get(f"/servers/{self.server}/countries/{value}"))
            if not isinstance(data, dict):
                raise self._country_not_found(value)
            return value, country_name(data) or value
        if not SERVER_RE.match(self.server):
            raise self._country_not_found(value)
        for max_age in (COUNTRIES_MAX_AGE, 300):  # pas trouvé : on relit la liste si elle a plus de 5 min (pays récent)
            for row in await self.countries(max_age=max_age):
                name = country_name(row)
                if name and name.lower() == value.lower() and country_id(row):
                    return str(country_id(row)), name
        raise self._country_not_found(value)

    async def countries(self, *, max_age: float = COUNTRIES_MAX_AGE) -> list[dict]:
        """Tous les pays du serveur, gardés en cache (partagé par les serveurs Discord du même serveur NG)."""
        async with self._countries_lock:
            cached = self._countries
            if cached and time.monotonic() - cached[0] < max_age:
                return cached[1]
            rows = await self._paginate(f"/servers/{self.server}/countries") or []
            self._countries = (time.monotonic(), rows)
            return rows

    def _country_not_found(self, value: str) -> NationsGloryError:
        return NationsGloryError(
            f"Pays « {value} » introuvable sur le serveur {self.server}",
            status=404,
            user_message="⚙️ Le pays configuré est introuvable sur NationsGlory. Vérifie le serveur et le pays dans les options du bot.",
        )

    async def get_country(self, cid: str) -> dict | None:
        data = unwrap(await self._get(f"/servers/{self.server}/countries/{cid}"))
        return data if isinstance(data, dict) else None

    async def members(self, cid: str, *, max_age: float = 0) -> dict[str, dict]:
        """Membres du pays : {pseudo en minuscules: {"username": ..., "role": "LEADER"|...}}."""
        now = time.monotonic()
        cache = self._members_cache.get(cid)
        if max_age and cache and now - cache[0] < max_age:
            return cache[1]
        rows = await self._paginate(f"/servers/{self.server}/countries/{cid}/members")
        if rows is None:
            raise self._country_not_found(cid)
        result: dict[str, dict] = {}
        for row in rows:
            name = str(row.get("username") or "").strip()
            if name:
                role = row.get("role")
                result[name.lower()] = {"username": name, "role": str(role).upper() if role else None}
        self._members_cache[cid] = (now, result)
        return result

    # ---- Joueurs ------------------------------------------------------------------
    async def get_player(self, username: str) -> dict | None:
        data = unwrap(await self._get(f"/servers/{self.server}/players/{username}"))
        return data if isinstance(data, dict) else None
