"""Faux Discord (API REST) en mémoire, pour tester le constructeur de serveurs sans vrai serveur."""
from __future__ import annotations

import itertools

from aiohttp import web

BOT_ID = "900"
GUILD_ID = "1000"


class FakeDiscord:
    def __init__(self, *, admin: bool = True) -> None:
        self.ids = itertools.count(2000)
        self.calls: list[tuple[str, str]] = []
        self.rate_limited_once = False
        self.guild = {"id": GUILD_ID, "name": "Serveur test", "owner_id": "1"}
        self.roles = [
            {"id": GUILD_ID, "name": "@everyone", "permissions": "104324673", "position": 0, "color": 0,
             "hoist": False, "mentionable": False, "managed": False},
            {"id": "950", "name": "MonBot", "permissions": "8" if admin else str(1 << 28 | 1 << 4 | 1 << 10 | 1 << 11),
             "position": 1, "color": 0, "hoist": False, "mentionable": False, "managed": True},
        ]
        self.channels: list[dict] = [
            {"id": "1100", "name": "général", "type": 0, "position": 0, "parent_id": None, "topic": None,
             "nsfw": False, "rate_limit_per_user": 0, "permission_overwrites": []},
        ]
        self.template = {
            "code": "abc123", "name": "Gaming",
            "serialized_source_guild": {
                "name": "Gaming",
                "roles": [
                    {"id": 0, "name": "@everyone", "permissions": "104324673", "color": 0, "hoist": False, "mentionable": False},
                    {"id": 1, "name": "Joueur", "permissions": "0", "color": 3447003, "hoist": False, "mentionable": True},
                    {"id": 2, "name": "Chef", "permissions": "8", "color": 15277667, "hoist": True, "mentionable": False},
                ],
                "channels": [
                    {"id": 10, "name": "Texte", "type": 4, "position": 0, "parent_id": None, "permission_overwrites": []},
                    {"id": 11, "name": "salon", "type": 0, "position": 0, "parent_id": 10, "topic": "Salut",
                     "permission_overwrites": []},
                    {"id": 12, "name": "Privé", "type": 4, "position": 1, "parent_id": None,
                     "permission_overwrites": [{"id": 0, "type": 0, "allow": "0", "deny": "1024"},
                                               {"id": 2, "type": 0, "allow": "1024", "deny": "0"}]},
                    {"id": 13, "name": "chefs", "type": 0, "position": 0, "parent_id": 12,
                     "permission_overwrites": [{"id": 0, "type": 0, "allow": "0", "deny": "1024"},
                                               {"id": 2, "type": 0, "allow": "1024", "deny": "0"}]},
                    {"id": 14, "name": "Vocal", "type": 2, "position": 0, "parent_id": 10, "user_limit": 4,
                     "permission_overwrites": []},
                ],
            },
        }

    # ---- utilitaires ---------------------------------------------------------------------------
    def bot_top(self) -> int:
        return next(r["position"] for r in self.roles if r["id"] == "950")

    def _forbidden(self):
        return web.json_response({"message": "Missing Permissions", "code": 50013}, status=403)

    def _role(self, rid):
        return next((r for r in self.roles if r["id"] == rid), None)

    def _channel(self, cid):
        return next((c for c in self.channels if c["id"] == cid), None)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        r = app.router
        r.add_get("/api/v10/users/@me", self.me)
        r.add_get("/api/v10/users/@me/guilds", self.my_guilds)
        r.add_post("/api/v10/oauth2/token", self.oauth_token)
        r.add_get("/api/v10/guilds/templates/{code}", self.get_template)
        r.add_get("/api/v10/guilds/{gid}", self.get_guild)
        r.add_patch("/api/v10/guilds/{gid}", self.patch_guild)
        r.add_get("/api/v10/guilds/{gid}/roles", self.get_roles)
        r.add_post("/api/v10/guilds/{gid}/roles", self.create_role)
        r.add_patch("/api/v10/guilds/{gid}/roles", self.role_positions)
        r.add_patch("/api/v10/guilds/{gid}/roles/{rid}", self.patch_role)
        r.add_delete("/api/v10/guilds/{gid}/roles/{rid}", self.delete_role)
        r.add_get("/api/v10/guilds/{gid}/channels", self.get_channels)
        r.add_post("/api/v10/guilds/{gid}/channels", self.create_channel)
        r.add_patch("/api/v10/guilds/{gid}/channels", self.channel_positions)
        r.add_get("/api/v10/guilds/{gid}/members/{uid}", self.get_member)
        r.add_patch("/api/v10/channels/{cid}", self.patch_channel)
        r.add_delete("/api/v10/channels/{cid}", self.delete_channel)
        return app

    # Connexion des humains (OAuth2) : le code « code-<id> » donne l'utilisateur <id>
    oauth_users = {"1": "Admin", "2": "Ami", "3": "Inconnu"}

    async def oauth_token(self, request):
        form = await request.post()
        if form.get("client_secret") != "secret" or not str(form.get("code", "")).startswith("code-"):
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response({"access_token": "user-" + form["code"][5:], "token_type": "Bearer"})

    async def me(self, request):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer user-"):
            uid = auth.removeprefix("Bearer user-")
            return web.json_response({"id": uid.rjust(18, "1"), "username": self.oauth_users.get(uid, "x"), "avatar": None})
        return web.json_response({"id": BOT_ID, "username": "MonBot", "bot": True})

    async def my_guilds(self, request):
        return web.json_response([{"id": GUILD_ID, "name": self.guild["name"], "icon": None, "owner": False, "permissions": "8"}])

    async def get_guild(self, request):
        return web.json_response({**self.guild, "roles": self.roles})

    async def get_roles(self, request):
        return web.json_response(self.roles)

    async def get_channels(self, request):
        return web.json_response(self.channels)

    async def get_member(self, request):
        return web.json_response({"roles": ["950"]})

    @web.middleware
    async def _auth(self, request, handler):
        self.calls.append((request.method, request.path))
        if request.path in ("/api/v10/oauth2/token",) or request.headers.get("Authorization", "").startswith("Bearer user-"):
            return await handler(request)
        if request.headers.get("Authorization") != "Bot bon-jeton":
            return web.json_response({"message": "401: Unauthorized", "code": 0}, status=401)
        if request.match_info.get("gid") not in (None, GUILD_ID):
            return web.json_response({"message": "Unknown Guild", "code": 10004}, status=404)
        return await handler(request)

    async def get_template(self, request):
        if request.match_info["code"] != "abc123":
            return web.json_response({"message": "Unknown Guild Template", "code": 10057}, status=404)
        return web.json_response(self.template)

    async def patch_guild(self, request):
        self.guild.update(name=(await request.json())["name"])
        return web.json_response(self.guild)

    async def create_role(self, request):
        body = await request.json()
        for r in self.roles:
            if r["position"] >= 1:
                r["position"] += 1
        role = {"id": str(next(self.ids)), "name": body.get("name", "new role"), "permissions": body.get("permissions", "0"),
                "color": body.get("color", 0), "hoist": body.get("hoist", False),
                "mentionable": body.get("mentionable", False), "position": 1, "managed": False}
        self.roles.append(role)
        return web.json_response(role)

    async def patch_role(self, request):
        role = self._role(request.match_info["rid"])
        if role is None:
            return web.json_response({"message": "Unknown Role", "code": 10011}, status=404)
        if role["id"] != GUILD_ID and role["position"] >= self.bot_top():
            return self._forbidden()
        role.update(await request.json())
        return web.json_response(role)

    async def delete_role(self, request):
        role = self._role(request.match_info["rid"])
        if role["position"] >= self.bot_top() or role.get("managed"):
            return self._forbidden()
        self.roles.remove(role)
        for ch in self.channels:
            ch["permission_overwrites"] = [o for o in ch["permission_overwrites"] if o["id"] != role["id"]]
        return web.Response(status=204)

    async def role_positions(self, request):
        for item in await request.json():
            role = self._role(item["id"])
            if item["position"] >= self.bot_top() or role["position"] >= self.bot_top():
                return self._forbidden()
            role["position"] = item["position"]
        return web.json_response(self.roles)

    async def create_channel(self, request):
        if not self.rate_limited_once:
            self.rate_limited_once = True
            return web.json_response({"message": "You are being rate limited.", "retry_after": 0.05, "global": False}, status=429)
        body = await request.json()
        siblings = [c for c in self.channels if c.get("parent_id") == body.get("parent_id") and (c["type"] == 4) == (body["type"] == 4)]
        name = body["name"]
        if body["type"] in (0, 5, 15):
            name = name.lower().replace(" ", "-")
        ch = {"id": str(next(self.ids)), "name": name, "type": body["type"], "parent_id": body.get("parent_id"),
              "position": body.get("position", len(siblings)), "topic": body.get("topic"), "nsfw": body.get("nsfw", False),
              "rate_limit_per_user": body.get("rate_limit_per_user", 0), "user_limit": body.get("user_limit", 0),
              "permission_overwrites": [{**o, "allow": str(o["allow"]), "deny": str(o["deny"])} for o in body.get("permission_overwrites", [])]}
        self.channels.append(ch)
        return web.json_response(ch)

    async def patch_channel(self, request):
        ch = self._channel(request.match_info["cid"])
        body = await request.json()
        if "name" in body and ch["type"] in (0, 5, 15):
            body["name"] = body["name"].lower().replace(" ", "-")
        ch.update(body)
        return web.json_response(ch)

    async def delete_channel(self, request):
        ch = self._channel(request.match_info["cid"])
        self.channels.remove(ch)
        for c in self.channels:
            if c.get("parent_id") == ch["id"]:
                c["parent_id"] = None
        return web.json_response(ch)

    async def channel_positions(self, request):
        for item in await request.json():
            self._channel(item["id"])["position"] = item["position"]
        return web.Response(status=204)
