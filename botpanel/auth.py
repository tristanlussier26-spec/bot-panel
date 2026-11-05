"""Connexion avec Discord (OAuth2) : l'admin est dans ADMIN_IDS, les autres attendent qu'il les approuve."""
from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from botpanel.web import Panel

SESSION_COOKIE = "bp_session"
STATE_COOKIE = "bp_oauth_state"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"


def avatar_url(user_id: int | str, avatar: str | None) -> str:
    if avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(user_id) >> 22) % 6}.png"


class Auth:
    def __init__(self, panel: "Panel") -> None:
        self.panel = panel
        self.settings = panel.settings

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url}/auth/callback"

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/auth/login", self.login)
        app.router.add_get("/auth/callback", self.callback)
        app.router.add_post("/api/logout", self.logout)

    def _cookie(self, response: web.StreamResponse, name: str, value: str, max_age: int) -> None:
        response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="Lax",
                            secure=self.settings.secure_cookies, path="/")

    async def current_user(self, request: web.Request) -> dict | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        user = await self.panel.db.session_user(token)
        if user is None or user["role"] not in ("admin", "user"):
            return None
        return user

    async def login(self, request: web.Request) -> web.StreamResponse:
        if not (self.settings.discord_client_id and self.settings.discord_client_secret):
            raise web.HTTPFound("/login?etat=config")
        state = secrets.token_urlsafe(24)
        query = urlencode({
            "client_id": self.settings.discord_client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": "identify", "state": state, "prompt": "none",
        })
        response = web.HTTPFound(f"{AUTHORIZE_URL}?{query}")
        self._cookie(response, STATE_COOKIE, state, 600)
        raise response

    async def callback(self, request: web.Request) -> web.StreamResponse:
        state, code = request.query.get("state", ""), request.query.get("code", "")
        expected = request.cookies.get(STATE_COOKIE, "")
        if not code or not expected or not hmac.compare_digest(state, expected):
            raise web.HTTPFound("/login?etat=erreur")
        try:
            profile = await self._exchange(code)
        except (aiohttp.ClientError, KeyError, ValueError):
            raise web.HTTPFound("/login?etat=erreur") from None

        uid = int(profile["id"])
        name = profile.get("global_name") or profile.get("username") or str(uid)
        s = self.settings
        limits = (s.default_max_bots, s.default_max_ram, s.default_max_disk)
        existing = await self.panel.db.get_user(uid)
        role = "admin" if uid in s.admin_ids else (existing["role"] if existing else "pending")
        user = await self.panel.db.upsert_login(uid, name, profile.get("avatar"), role, limits)
        if uid in s.admin_ids and user["role"] != "admin":
            user = await self.panel.db.update_user(uid, role="admin")
        if not existing:
            await self.panel.db.log(uid, "Première connexion" + (" (en attente d'approbation)" if role == "pending" else ""))

        if user["role"] == "pending":
            raise web.HTTPFound("/login?etat=attente")
        if user["role"] == "blocked":
            raise web.HTTPFound("/login?etat=bloque")
        token = await self.panel.db.create_session(uid)
        response = web.HTTPFound("/")
        self._cookie(response, SESSION_COOKIE, token, 30 * 86400)
        response.del_cookie(STATE_COOKIE, path="/")
        raise response

    async def _exchange(self, code: str) -> dict:
        s = self.settings
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.post(f"{s.discord_api}/oauth2/token", data={
                "client_id": s.discord_client_id, "client_secret": s.discord_client_secret,
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            }) as resp:
                resp.raise_for_status()
                token = (await resp.json())["access_token"]
            async with http.get(f"{s.discord_api}/users/@me", headers={"Authorization": f"Bearer {token}"}) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def logout(self, request: web.Request) -> web.Response:
        if token := request.cookies.get(SESSION_COOKIE):
            await self.panel.db.delete_session(token)
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response
