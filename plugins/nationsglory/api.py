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
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import aiohttp

from .common import NationsGloryError

BASE_URL = "https://publicapi.nationsglory.fr/v1"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PAGE_SIZE = 100
MAX_PAGES = 100
SERVER_RE = re.compile(r"^[a-z0-9_-]{2,24}$")
COUNTRIES_MAX_AGE = 3600  # la liste des pays d'un serveur change rarement
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_CACHE_MAX = 64


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
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    # La vraie API met le curseur dans meta.page : {"next_cursor": "...", "has_more": true}
    for container in (meta.get("page"), data, meta, data.get("pagination"), data.get("page"), data.get("links")):
        if not isinstance(container, dict):
            continue
        if container.get("has_more") is False:
            break  # dernière page, même si un curseur traîne
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
        self._images: dict[str, bytes] = {}
        self._cached: dict[str, tuple[float, object]] = {}
        self._cached_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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

    async def _get(self, path: str, params: dict | None = None, *, raw: bool = False) -> Any:
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
                    headers={"X-API-Key": self.api_key, "Accept": "*/*" if raw else "application/json"},
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
                    if raw:
                        return await resp.read()
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

    async def get_country(self, cid: str, *, max_age: float = 0) -> dict | None:
        """Fiche complète d'un pays. Avec `max_age`, la fiche est partagée (stats, alertes, /pays)."""
        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/countries/{cid}"))
            return data if isinstance(data, dict) else None
        if not max_age:
            return await fetch()
        return await self._shared(f"card:{cid}", max_age, fetch)

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

    # ---- Données partagées (un cache par serveur NG, commun à tous les serveurs Discord) ----------
    async def _shared(self, key: str, max_age: float, fetch) -> Any:
        """Garde une réponse `max_age` secondes. Deux serveurs Discord qui demandent la même chose
        en même temps font une seule requête."""
        async with self._cached_locks[key]:
            hit = self._cached.get(key)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
            value = await fetch()
            self._cached[key] = (time.monotonic(), value)
            return value

    # ---- Guerres (domaine war, mis à jour aux 10 minutes) -----------------------------------
    async def wars(self, cid: str, *, max_age: float = 300) -> list[dict]:
        """Guerres d'un pays : en négociation, en cours et finies depuis moins de 30 jours."""
        async def fetch():
            return await self._paginate(f"/servers/{self.server}/wars", {"country": cid}) or []
        return await self._shared(f"wars:{cid}", max_age, fetch)

    async def war(self, war_id: int, *, max_age: float = 300) -> dict | None:
        """Fiche complète d'une guerre (assauts, missiles). None si elle existe plus."""
        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/wars/{int(war_id)}"))
            return data if isinstance(data, dict) else None
        return await self._shared(f"war:{int(war_id)}", max_age, fetch)

    # ---- Classements --------------------------------------------------------------------------
    async def warzone_daily(self, *, max_age: float = 900) -> list[dict]:
        """Classement warzone de la dernière journée close (publié à 20 h, heure de Paris)."""
        return await self.warzone("daily", max_age=max_age)

    async def warzone(self, period: str, *, max_age: float = 900) -> list[dict]:
        """Classement warzone clos : « daily » (chaque jour à 20 h) ou « weekly » (le dimanche à 20 h)."""
        if period not in ("daily", "weekly"):
            raise ValueError(period)
        path = f"/servers/{self.server}/rankings/warzone/{period}"

        async def fetch():
            # Une seule requête pour voir si la période a changé ; tout le classement juste dans ce cas
            first, _ = extract_page(await self._get(path, {"limit": 1}))
            day = first[0].get("period_end") if first else None
            known = self._cached.get(f"warzone_{period}:full")
            if known and known[1] and known[1][0].get("period_end") == day:
                return known[1]
            rows = await self._paginate(path) or []
            self._cached[f"warzone_{period}:full"] = (time.monotonic(), rows)
            return rows
        return await self._shared(f"warzone_{period}", max_age, fetch)

    # ---- Joueurs en ligne et activité ---------------------------------------------------------
    async def online(self, *, max_age: float = 240) -> set[str]:
        """Pseudos (en minuscules) des joueurs connectés au serveur NG (domaine presence, aux 5 min)."""
        async def fetch():
            rows = await self._paginate(f"/servers/{self.server}/online") or []
            return {str(r.get("username") or "").lower() for r in rows if r.get("username")}
        return await self._shared("online", max_age, fetch)

    async def last_login(self, username: str) -> str | None:
        """Jour de la dernière connexion (AAAA-MM-JJ, heure de Paris).
        None : aucune date publiée (pas venu depuis plus de 90 jours). « private » : profil privé."""
        try:
            data = unwrap(await self._get(f"/servers/{self.server}/players/{quote(username, safe='')}/last-login"))
        except NationsGloryError as e:
            if e.status == 403 and "profile_private" in str(e):
                return "private"  # pas un problème de clé : le joueur cache sa fiche
            raise
        if isinstance(data, dict) and isinstance(data.get("last_login_on"), str):
            return data["last_login_on"]
        return None

    # ---- Alliances ------------------------------------------------------------------------------
    async def alliance(self, key: str, *, max_age: float = 300) -> dict | None:
        if not re.fullmatch(r"[0-9a-f]{16}", str(key or "")):
            return None

        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/alliances/{key}"))
            return data if isinstance(data, dict) else None
        return await self._shared(f"alliance:{key}", max_age, fetch)

    async def country_members(self, cid: str, *, max_age: float = 240) -> dict[str, dict]:
        """Membres de n'importe quel pays (pour /pays <nom>), gardés en cache quelques minutes."""
        return await self.members(cid, max_age=max_age)

    # ---- Images (drapeaux) : la clé est exigée, donc Discord peut pas les lire par leur adresse ----
    async def image(self, sha256: str | None) -> bytes | None:
        sha = str(sha256 or "").lower()
        if not SHA_RE.match(sha):
            return None
        if sha not in self._images:
            data = await self._get(f"/images/{sha}.png", raw=True)
            if not data:
                return None
            if len(self._images) >= IMAGE_CACHE_MAX:
                self._images.pop(next(iter(self._images)))
            self._images[sha] = data
        return self._images[sha]

    # ---- Séries historiques (graphiques) ------------------------------------------------------
    async def series(self, metric: str, subject: str | None, range_: str, *, max_age: float = 600) -> list[dict]:
        """Points {at, value, min, max} d'une métrique (ex. country.power sur 7d). [] si rien de publié."""
        params = {"range": range_}
        if subject:
            params["subject"] = subject

        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/series/{metric}", params))
            points = data.get("points") if isinstance(data, dict) else None
            return [p for p in points or [] if isinstance(p, dict)]
        return await self._shared(f"series:{metric}:{subject}:{range_}", max_age, fetch)

    # ---- Économie -------------------------------------------------------------------------------
    async def market(self, *, max_age: float = 240) -> list[dict]:
        """Céréales et leur prix actuel (domaine market, aux 5 min)."""
        async def fetch():
            return await self._paginate(f"/servers/{self.server}/market") or []
        return await self._shared("market", max_age, fetch)

    async def market_history(self, cereal: str, *, days: int = 30, max_age: float = 1800) -> dict | None:
        """Une céréale avec son historique quotidien (history.price, sales, stocks)."""
        if not re.fullmatch(r"[a-z_]{2,64}", cereal or ""):
            return None

        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/market/{cereal}", {"history_days": max(1, min(90, days))}))
            return data if isinstance(data, dict) else None
        return await self._shared(f"market:{cereal}:{days}", max_age, fetch)

    async def hdv_search(self, query: str, *, limit: int = 10, max_age: float = 300) -> list[dict]:
        """Objets de l'hôtel des ventes dont le nom contient `query` (sans tenir compte des accents)."""
        q = str(query or "").strip()[:64]
        if not q:
            return []

        async def fetch():
            data = await self._get(f"/servers/{self.server}/hdv/items", {"q": q, "limit": max(1, min(limit, 25))})
            rows, _ = extract_page(data)
            return rows
        return await self._shared(f"hdv:q:{q.lower()}:{limit}", max_age, fetch)

    async def hdv_item(self, item: str, *, max_age: float = 300) -> dict | None:
        if not re.fullmatch(r"\d{1,5}:\d{1,5}", item or ""):
            return None

        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/hdv/items/{item}"))
            return data if isinstance(data, dict) else None
        return await self._shared(f"hdv:item:{item}", max_age, fetch)

    async def lotteries(self, *, max_age: float = 300) -> list[dict]:
        async def fetch():
            return await self._paginate(f"/servers/{self.server}/lotteries") or []
        return await self._shared("lotteries", max_age, fetch)

    async def enterprises(self, *, max_age: float = 600) -> list[dict]:
        """Toutes les entreprises du serveur (domaine enterprise, aux 10 min)."""
        async def fetch():
            return await self._paginate(f"/servers/{self.server}/enterprises") or []
        return await self._shared("enterprises", max_age, fetch)

    # ---- Classements --------------------------------------------------------------------------
    async def warzone_live(self, *, max_age: float = 600) -> list[dict]:
        """Warzone de la journée en cours (domaine warzone_live, aux 15 min)."""
        async def fetch():
            return await self._paginate(f"/servers/{self.server}/rankings/warzone/live") or []
        return await self._shared("warzone_live", max_age, fetch)

    async def skills(self, period: str = "live", *, max_age: float = 1800) -> list[dict]:
        """Top 50 de chaque compétence : « live » (semaine en cours) ou « weekly » (dernière semaine close)."""
        if period not in ("live", "weekly"):
            raise ValueError(period)

        async def fetch():
            return await self._paginate(f"/servers/{self.server}/rankings/skills/{period}") or []
        return await self._shared(f"skills:{period}", max_age, fetch)

    # ---- Guerres de tout le serveur (pour les pays surveillés) ----------------------------------
    async def server_wars(self, *, max_age: float = 300) -> list[dict]:
        async def fetch():
            return await self._paginate(f"/servers/{self.server}/wars") or []
        return await self._shared("wars:all", max_age, fetch)

    # ---- Joueurs ------------------------------------------------------------------------------
    async def profile(self, username: str, *, max_age: float = 300) -> dict | None:
        """Fiche publique d'un joueur (tous serveurs). {"private": True} si son profil est privé."""
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username or ""):
            return None

        async def fetch():
            try:
                data = unwrap(await self._get(f"/players/{username}"))
            except NationsGloryError as e:
                if e.status == 403 and "profile_private" in str(e):
                    return {"private": True, "username": username}
                raise
            return data if isinstance(data, dict) else None
        return await self._shared(f"profile:{username.lower()}", max_age, fetch)

    async def status(self, *, max_age: float = 120) -> list[dict]:
        """État et fraîcheur de chaque domaine du serveur NG."""
        async def fetch():
            data = unwrap(await self._get(f"/servers/{self.server}/status"))
            domains = data.get("domains") if isinstance(data, dict) else None
            return [d for d in domains or [] if isinstance(d, dict)]
        return await self._shared("status", max_age, fetch)

    # ---- Forum (annonces officielles) -----------------------------------------------------------
    async def forum_category_id(self, value: str) -> int | None:
        """Identifiant d'une catégorie du forum à partir de son numéro, de son slug ou de son nom."""
        value = str(value or "").strip()
        if value.isdigit():
            return int(value)
        if match := re.search(r"/forum/category/([^/?#]+)", value):
            value = unquote(match.group(1))

        async def fetch():
            rows, _ = extract_page(await self._get("/forum/categories"))
            return rows
        rows = await self._shared("forum:categories", 3600, fetch)
        wanted = value.lower()
        for row in rows:
            if wanted in (str(row.get("slug") or "").lower(), str(row.get("name") or "").lower()):
                return int(row["id"]) if str(row.get("id") or "").isdigit() else None
        return None

    async def forum_threads(self, category: int, *, limit: int = 10, max_age: float = 240) -> list[dict]:
        async def fetch():
            data = unwrap(await self._get(f"/forum/categories/{int(category)}/threads", {"limit": max(1, min(limit, 50))}))
            threads = (data.get("threads") or data.get("data")) if isinstance(data, dict) else data
            return [t for t in threads or [] if isinstance(t, dict)]
        return await self._shared(f"forum:{int(category)}:{limit}", max_age, fetch)
