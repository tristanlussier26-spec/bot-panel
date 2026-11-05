"""Constructeur de serveurs Discord : un modèle (YAML) décrit les rôles, catégories, salons et permissions ;
le panel compare avec le serveur, montre le plan, puis l'applique avec le jeton d'un des bots.

Modèle :
    name: Mon serveur            # facultatif : renomme le serveur
    everyone:
      permissions: [view_channel, send_messages, read_message_history]
    roles:                       # du plus haut au plus bas
      - name: Staff
        color: "#e91e63"
        hoist: true              # affiché à part dans la liste des membres
        permissions: [manage_messages, kick_members]
    categories:
      - name: Infos
        overwrites:
          "@everyone": {deny: [send_messages]}
        channels:
          - {name: règles, topic: "Lis-moi"}
          - {name: annonces, type: announcement}
      - name: Staff
        private: true            # = @everyone ne voit pas
        overwrites:
          Staff: {allow: [view_channel]}
        channels:
          - name: staff
          - {name: Réunion, type: voice, user_limit: 10}
    channels: []                 # salons hors catégorie

Les salons héritent des permissions de leur catégorie ; les « overwrites » d'un salon s'ajoutent par-dessus.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp
import yaml

from botpanel import __version__

API_BASE = "https://discord.com/api/v10"

# Bits des permissions (vérifiés contre discord.py 2.7 dans les tests)
PERMISSIONS: dict[str, int] = {
    "create_instant_invite": 0, "kick_members": 1, "ban_members": 2, "administrator": 3, "manage_channels": 4,
    "manage_guild": 5, "add_reactions": 6, "view_audit_log": 7, "priority_speaker": 8, "stream": 9,
    "view_channel": 10, "send_messages": 11, "send_tts_messages": 12, "manage_messages": 13, "embed_links": 14,
    "attach_files": 15, "read_message_history": 16, "mention_everyone": 17, "use_external_emojis": 18,
    "view_guild_insights": 19, "connect": 20, "speak": 21, "mute_members": 22, "deafen_members": 23,
    "move_members": 24, "use_voice_activation": 25, "change_nickname": 26, "manage_nicknames": 27,
    "manage_roles": 28, "manage_webhooks": 29, "manage_expressions": 30, "use_application_commands": 31,
    "request_to_speak": 32, "manage_events": 33, "manage_threads": 34, "create_public_threads": 35,
    "create_private_threads": 36, "use_external_stickers": 37, "send_messages_in_threads": 38,
    "use_embedded_activities": 39, "moderate_members": 40, "view_creator_monetization_analytics": 41,
    "use_soundboard": 42, "create_expressions": 43, "create_events": 44, "use_external_sounds": 45,
    "send_voice_messages": 46, "set_voice_channel_status": 48, "send_polls": 49, "use_external_apps": 50,
    "pin_messages": 51, "bypass_slowmode": 52,
}
ALIASES = {
    "read_messages": "view_channel", "external_emojis": "use_external_emojis", "external_stickers": "use_external_stickers",
    "manage_permissions": "manage_roles", "manage_emojis": "manage_expressions",
    "manage_emojis_and_stickers": "manage_expressions", "create_polls": "send_polls", "manage_server": "manage_guild",
    "timeout_members": "moderate_members", "read_message_history_": "read_message_history",
}
ALL_PERMISSIONS = sum(1 << b for b in PERMISSIONS.values())
ADMIN = 1 << PERMISSIONS["administrator"]
MANAGE_CHANNELS = 1 << PERMISSIONS["manage_channels"]
MANAGE_ROLES = 1 << PERMISSIONS["manage_roles"]
MANAGE_GUILD = 1 << PERMISSIONS["manage_guild"]
VIEW_CHANNEL = 1 << PERMISSIONS["view_channel"]

CHANNEL_TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5, "news": 5, "stage": 13, "forum": 15}
TYPE_NAMES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum"}
TEXT_LIKE = {0, 5, 15}
VOICE_LIKE = {2, 13}
EVERYONE = "@everyone"

# Permissions recommandées pour inviter un bot qui construit le serveur
INVITE_PERMISSIONS = ADMIN


class TemplateError(Exception):
    """Modèle invalide : message prêt à afficher."""


class DiscordError(Exception):
    def __init__(self, status: int, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.status, self.code = status, code


# ---------------------------------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------------------------------
def perms_to_int(names: Any, where: str) -> int:
    if names is None:
        return 0
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise TemplateError(f"{where} : les permissions doivent être une liste")
    value = 0
    for name in names:
        key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
        key = ALIASES.get(key, key)
        if key == "all":
            value |= ALL_PERMISSIONS
            continue
        if key not in PERMISSIONS:
            raise TemplateError(f"{where} : permission inconnue « {name} »")
        value |= 1 << PERMISSIONS[key]
    return value


def int_to_perms(value: int) -> list[str]:
    return [name for name, bit in PERMISSIONS.items() if value >> bit & 1]


def parse_color(value: Any, where: str) -> int:
    if value in (None, "", 0):
        return 0
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", value.strip()):
        return int(value.strip().lstrip("#"), 16)
    raise TemplateError(f"{where} : couleur invalide « {value} » (format #rrggbb)")


def channel_key(name: str, ctype: int) -> str:
    """Comparaison des noms comme Discord les affiche : les salons texte sont en minuscules, avec des tirets."""
    name = name.strip().lower()
    if ctype in TEXT_LIKE:
        name = re.sub(r"\s+", "-", name)
    return name


# ---------------------------------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------------------------------
Overwrites = dict[str, tuple[int, int]]  # nom du rôle (ou @everyone) -> (allow, deny)


@dataclass
class TRole:
    name: str
    permissions: int | None = None
    color: int | None = None
    hoist: bool | None = None
    mentionable: bool | None = None


@dataclass
class TChannel:
    name: str
    type: int = 0
    category: str | None = None
    topic: str | None = None
    nsfw: bool | None = None
    slowmode: int | None = None
    user_limit: int | None = None
    overwrites: Overwrites | None = None  # permissions finales (catégorie + salon)


@dataclass
class TCategory:
    name: str
    overwrites: Overwrites | None = None
    channels: list[TChannel] = field(default_factory=list)


@dataclass
class Template:
    name: str | None = None
    everyone: int | None = None
    roles: list[TRole] = field(default_factory=list)
    categories: list[TCategory] = field(default_factory=list)
    channels: list[TChannel] = field(default_factory=list)  # hors catégorie

    def all_channels(self) -> list[TChannel]:
        return [c for cat in self.categories for c in cat.channels] + self.channels

    def summary(self) -> dict:
        return {"roles": len(self.roles), "categories": len(self.categories), "channels": len(self.all_channels())}


def _name(value: Any, where: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise TemplateError(f"{where} : il manque le nom")
    name = str(value).strip()
    if len(name) > 100:
        raise TemplateError(f"{where} : nom trop long (100 caractères max)")
    return name


def _bool(value: Any, where: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise TemplateError(f"{where} : mets true ou false")


def _int_range(value: Any, lo: int, hi: int, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise TemplateError(f"{where} : nombre entre {lo} et {hi} attendu")
    return value


def _overwrites(data: dict, where: str) -> Overwrites | None:
    raw = data.get("overwrites")
    private = _bool(data.get("private"), f"{where} › private")
    if raw is None and not private:
        return None
    if raw is not None and not isinstance(raw, dict):
        raise TemplateError(f"{where} › overwrites : il faut « nom du rôle: {{allow: [...], deny: [...]}} »")
    result: Overwrites = {}
    if private:
        result[EVERYONE] = (0, VIEW_CHANNEL)
    for role, spec in (raw or {}).items():
        role_name = EVERYONE if str(role).strip().lower() in ("@everyone", "everyone") else _name(role, f"{where} › overwrites")
        spec = spec or {}
        if not isinstance(spec, dict):
            raise TemplateError(f"{where} › overwrites › {role_name} : il faut allow et/ou deny")
        unknown = set(spec) - {"allow", "deny"}
        if unknown:
            raise TemplateError(f"{where} › overwrites › {role_name} : clé inconnue « {', '.join(sorted(unknown))} »")
        allow = perms_to_int(spec.get("allow"), f"{where} › {role_name} › allow")
        deny = perms_to_int(spec.get("deny"), f"{where} › {role_name} › deny")
        prev_allow, prev_deny = result.get(role_name, (0, 0))
        allow |= prev_allow & ~deny
        deny |= prev_deny & ~allow
        result[role_name] = (allow, deny & ~allow)
    return result


def merge_overwrites(base: Overwrites | None, extra: Overwrites | None) -> Overwrites | None:
    """Permissions d'un salon : celles de la catégorie, et pour chaque rôle nommé dans le salon, celles du salon.
    Un rôle mis à {} dans le salon = aucune permission spéciale pour ce rôle dans ce salon."""
    if base is None and extra is None:
        return None
    return {**(base or {}), **(extra or {})}


CHANNEL_KEYS = {"name", "type", "topic", "nsfw", "slowmode", "user_limit", "overwrites", "private"}


def _channel(data: Any, where: str, category: TCategory | None) -> TChannel:
    if isinstance(data, str):
        data = {"name": data}
    if not isinstance(data, dict):
        raise TemplateError(f"{where} : un salon doit avoir au moins un nom")
    unknown = set(data) - CHANNEL_KEYS
    if unknown:
        raise TemplateError(f"{where} : clé inconnue « {', '.join(sorted(unknown))} »")
    name = _name(data.get("name"), where)
    where = f"{where} « {name} »"
    type_name = str(data.get("type", "text")).lower()
    if type_name not in CHANNEL_TYPES or type_name == "category":
        raise TemplateError(f"{where} : type « {type_name} » inconnu (text, voice, announcement, stage, forum)")
    ctype = CHANNEL_TYPES[type_name]
    topic = data.get("topic")
    if topic is not None and (not isinstance(topic, str) or len(topic) > 1024):
        raise TemplateError(f"{where} : le sujet doit être un texte de 1024 caractères max")
    own = _overwrites(data, where)
    return TChannel(
        name=name, type=ctype, category=category.name if category else None,
        topic=topic if ctype in TEXT_LIKE else None,
        nsfw=_bool(data.get("nsfw"), f"{where} › nsfw"),
        slowmode=_int_range(data.get("slowmode"), 0, 21600, f"{where} › slowmode"),
        user_limit=_int_range(data.get("user_limit"), 0, 99, f"{where} › user_limit") if ctype in VOICE_LIKE else None,
        overwrites=merge_overwrites(category.overwrites if category else None, own),
    )


def parse_template(text: str) -> Template:
    try:
        data = yaml.safe_load(text or "")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f" (ligne {mark.line + 1})" if mark else ""
        raise TemplateError(f"YAML invalide{line} : {getattr(e, 'problem', e)}") from None
    if data is None:
        raise TemplateError("Le modèle est vide")
    if not isinstance(data, dict):
        raise TemplateError("Le modèle doit commencer par des clés (name, roles, categories, channels)")
    unknown = set(data) - {"name", "everyone", "roles", "categories", "channels"}
    if unknown:
        raise TemplateError(f"Clé inconnue « {', '.join(sorted(unknown))} »")

    t = Template()
    if data.get("name") is not None:
        t.name = _name(data["name"], "name")
        if len(t.name) < 2:
            raise TemplateError("name : 2 caractères minimum")
    if data.get("everyone") is not None:
        ev = data["everyone"]
        if not isinstance(ev, dict) or set(ev) - {"permissions"}:
            raise TemplateError("everyone : seulement « permissions: [...] »")
        t.everyone = perms_to_int(ev.get("permissions"), "everyone")

    seen: set[str] = set()
    for i, r in enumerate(data.get("roles") or [], 1):
        where = f"roles › n° {i}"
        if isinstance(r, str):
            r = {"name": r}
        if not isinstance(r, dict):
            raise TemplateError(f"{where} : un rôle doit avoir au moins un nom")
        unknown = set(r) - {"name", "permissions", "color", "hoist", "mentionable"}
        if unknown:
            raise TemplateError(f"{where} : clé inconnue « {', '.join(sorted(unknown))} »")
        name = _name(r.get("name"), where)
        if name.lower() in ("@everyone", "everyone"):
            raise TemplateError(f"{where} : @everyone se règle avec la clé « everyone », pas dans roles")
        if name.lower() in seen:
            raise TemplateError(f"{where} : le rôle « {name} » est là deux fois")
        seen.add(name.lower())
        where = f"rôle « {name} »"
        t.roles.append(TRole(
            name=name,
            permissions=perms_to_int(r["permissions"], where) if "permissions" in r else None,
            color=parse_color(r["color"], where) if "color" in r else None,
            hoist=_bool(r.get("hoist"), f"{where} › hoist"),
            mentionable=_bool(r.get("mentionable"), f"{where} › mentionable"),
        ))

    cat_seen: set[str] = set()
    for i, c in enumerate(data.get("categories") or [], 1):
        where = f"categories › n° {i}"
        if not isinstance(c, dict):
            raise TemplateError(f"{where} : une catégorie doit avoir un nom et des salons")
        unknown = set(c) - {"name", "overwrites", "private", "channels"}
        if unknown:
            raise TemplateError(f"{where} : clé inconnue « {', '.join(sorted(unknown))} »")
        name = _name(c.get("name"), where)
        if name.lower() in cat_seen:
            raise TemplateError(f"{where} : la catégorie « {name} » est là deux fois")
        cat_seen.add(name.lower())
        cat = TCategory(name=name, overwrites=_overwrites(c, f"catégorie « {name} »"))
        channels = c.get("channels") or []
        if not isinstance(channels, list):
            raise TemplateError(f"catégorie « {name} » › channels : il faut une liste")
        if len(channels) > 50:
            raise TemplateError(f"catégorie « {name} » : 50 salons max par catégorie (limite de Discord)")
        cat.channels = [_channel(ch, f"catégorie « {name} » › salon n° {j}", cat) for j, ch in enumerate(channels, 1)]
        t.categories.append(cat)
    t.channels = [_channel(ch, f"channels › n° {j}", None) for j, ch in enumerate(data.get("channels") or [], 1)]

    if len(t.roles) > 250:
        raise TemplateError("250 rôles max (limite de Discord)")
    if len(t.all_channels()) + len(t.categories) > 500:
        raise TemplateError("500 salons max, catégories comprises (limite de Discord)")
    keys = [(c.category or "", channel_key(c.name, c.type), c.type in VOICE_LIKE) for c in t.all_channels()]
    for key in keys:
        if keys.count(key) > 1:
            raise TemplateError(f"Le salon « {key[1]} » est là deux fois dans la même catégorie")
    return t


def referenced_roles(t: Template) -> set[str]:
    """Rôles utilisés dans les permissions des catégories et des salons."""
    return {name for ow in [c.overwrites for c in t.categories] + [c.overwrites for c in t.all_channels()] if ow
            for name in ow if name != EVERYONE}


# ---------------------------------------------------------------------------------------------------
# Client REST Discord (avec gestion des limites de débit)
# ---------------------------------------------------------------------------------------------------
class DiscordAPI:
    def __init__(self, token: str, base: str = API_BASE) -> None:
        self.base = base.rstrip("/")
        self.headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": f"DiscordBot (https://github.com/botpanel, {__version__})",
            "X-Audit-Log-Reason": quote("Bot Panel : application d'un modèle"),
        }
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "DiscordAPI":
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc) -> None:
        await self.session.close()

    async def request(self, method: str, path: str, json: Any = None) -> Any:
        for attempt in range(6):
            async with self.session.request(method, self.base + path, json=json) as resp:
                if resp.status == 429:
                    data = await resp.json(content_type=None)
                    await asyncio.sleep(min(float(data.get("retry_after", 1)), 30) + 0.1)
                    continue
                if resp.status >= 500 and attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status == 204:
                    return None
                data = await resp.json(content_type=None) if resp.content_length != 0 else None
                if resp.status >= 400:
                    raise self._error(resp.status, data or {})
                return data
        raise DiscordError(429, "Discord limite le débit : réessaie dans quelques minutes")

    @staticmethod
    def _error(status: int, data: dict) -> DiscordError:
        code = data.get("code", 0) if isinstance(data, dict) else 0
        messages = {
            401: "Jeton du bot invalide (DISCORD_TOKEN)",
            50013: "Le bot a pas la permission (mets-lui Administrateur, ou monte son rôle plus haut)",
            50001: "Le bot a pas accès à ce serveur",
            10004: "Serveur introuvable : le bot est pas dedans",
            10057: "Modèle discord.new introuvable",
            30005: "Limite de 250 rôles atteinte",
            30013: "Limite de 500 salons atteinte",
        }
        message = messages.get(code) or messages.get(status) or (data.get("message") if isinstance(data, dict) else "") or f"Erreur Discord {status}"
        if code == 50035 and isinstance(data, dict):
            message = f"Discord refuse une valeur : {data.get('errors', data.get('message'))}"
        return DiscordError(status, message, code)

    async def me(self) -> dict:
        return await self.request("GET", "/users/@me")

    async def guilds(self) -> list[dict]:
        return await self.request("GET", "/users/@me/guilds")

    async def template(self, code: str) -> dict:
        return await self.request("GET", f"/guilds/templates/{quote(code, safe='')}")


@dataclass
class GuildState:
    guild: dict
    roles: list[dict]
    channels: list[dict]
    bot_id: str
    bot_top: int  # position du rôle le plus haut du bot
    bot_perms: int
    bot_roles: list[str] = field(default_factory=list)
    owner: bool = False

    @property
    def id(self) -> str:
        return self.guild["id"]

    def can(self, perm: int) -> bool:
        return self.owner or bool(self.bot_perms & ADMIN) or bool(self.bot_perms & perm)

    def role_name(self, role_id: str, *, managed: bool = True) -> str | None:
        """Nom d'un rôle par son ID (None si inconnu ; si managed=False, None aussi pour les rôles de bots)."""
        if role_id == self.id:
            return EVERYONE
        role = next((r for r in self.roles if r["id"] == role_id), None)
        if role is None or (role.get("managed") and not managed):
            return None
        return role["name"]


async def load_state(api: DiscordAPI, guild_id: str) -> GuildState:
    guild_id = str(int(guild_id))
    me = await api.me()
    guild = await api.request("GET", f"/guilds/{guild_id}")
    roles = guild.get("roles") or await api.request("GET", f"/guilds/{guild_id}/roles")
    channels = await api.request("GET", f"/guilds/{guild_id}/channels")
    member = await api.request("GET", f"/guilds/{guild_id}/members/{me['id']}")
    mine = [r for r in roles if r["id"] in member.get("roles", []) or r["id"] == guild_id]
    perms = 0
    for r in mine:
        perms |= int(r["permissions"])
    top = max((r["position"] for r in mine), default=0)
    return GuildState(guild=guild, roles=roles, channels=channels, bot_id=me["id"], bot_top=top, bot_perms=perms,
                      bot_roles=list(member.get("roles", [])), owner=guild.get("owner_id") == me["id"])


# ---------------------------------------------------------------------------------------------------
# Plan : ce qui va changer
# ---------------------------------------------------------------------------------------------------
@dataclass
class Action:
    kind: str
    label: str
    payload: dict = field(default_factory=dict)
    target: str | None = None  # ID Discord de ce qui est modifié
    blocked: str | None = None  # raison si le bot peut pas le faire
    destructive: bool = False

    def public(self) -> dict:
        return {"kind": self.kind, "label": self.label, "blocked": self.blocked, "destructive": self.destructive}


@dataclass
class Plan:
    actions: list[Action]
    warnings: list[str]
    template: Template
    mirror: bool

    def public(self) -> dict:
        counts = {"create": 0, "update": 0, "delete": 0, "blocked": 0}
        for a in self.actions:
            if a.blocked:
                counts["blocked"] += 1
            elif a.kind.startswith("create"):
                counts["create"] += 1
            elif a.kind.startswith("delete"):
                counts["delete"] += 1
            else:
                counts["update"] += 1
        return {"actions": [a.public() for a in self.actions], "warnings": self.warnings, "counts": counts}


def _role_overwrites(channel: dict, state: GuildState) -> tuple[Overwrites, list[dict]]:
    """Permissions d'un salon existant : (par nom de rôle, autres entrées gardées telles quelles)."""
    named: Overwrites = {}
    other: list[dict] = []  # membres, rôles de bots, rôles inconnus : jamais touchés
    for ow in channel.get("permission_overwrites") or []:
        name = state.role_name(ow["id"], managed=False) if int(ow["type"]) == 0 else None
        if name is None:
            other.append({k: ow[k] for k in ("id", "type", "allow", "deny")})
        elif int(ow["allow"]) or int(ow["deny"]):
            named[name] = (int(ow["allow"]), int(ow["deny"]))
    return named, other


def _overwrites_change(desired: Overwrites | None, channel: dict | None, state: GuildState, mirror: bool) -> bool:
    if desired is None:
        return False
    if channel is None:
        return True
    current, _ = _role_overwrites(channel, state)
    target = dict(desired) if mirror else {**current, **desired}
    target = {k: v for k, v in target.items() if v != (0, 0)}
    return target != current


def _perm_names(value: int) -> str:
    names = int_to_perms(value)
    return ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")


def build_plan(t: Template, state: GuildState, *, mirror: bool = False) -> Plan:
    actions: list[Action] = []
    warnings: list[str] = []
    gid = state.id
    is_admin = state.owner or bool(state.bot_perms & ADMIN)
    no_roles = None if state.can(MANAGE_ROLES) else "le bot a pas la permission Gérer les rôles"
    no_channels = None if state.can(MANAGE_CHANNELS) else "le bot a pas la permission Gérer les salons"
    if not is_admin:
        warnings.append("Conseil : donne la permission Administrateur au bot le temps de construire le serveur "
                        "(sinon Discord refuse les permissions que le bot a pas lui-même).")

    def too_much(perms: int | None) -> str | None:
        if perms is None or is_admin:
            return None
        missing = perms & ~state.bot_perms
        return f"le bot peut pas donner des permissions qu'il a pas ({_perm_names(missing)})" if missing else None

    def above_bot(role: dict) -> str | None:
        return None if state.owner or role["position"] < state.bot_top else "ce rôle est au-dessus du rôle du bot"

    # Rôles utilisés dans les permissions : doivent exister quelque part
    template_roles = {r.name.lower() for r in t.roles}
    managed = {r["name"].lower() for r in state.roles if r.get("managed")}
    known = template_roles | managed | (set() if mirror else {r["name"].lower() for r in state.roles})
    unknown = sorted(n for n in referenced_roles(t) if n.lower() not in known)
    if unknown:
        hint = " (en mode miroir, ils doivent être dans « roles »)" if mirror else ""
        raise TemplateError(f"Rôle(s) utilisé(s) dans des permissions mais absent(s) du modèle{hint} : {', '.join(unknown)}")

    # Serveur
    if t.name and t.name != state.guild.get("name"):
        actions.append(Action("rename_guild", f"Renommer le serveur en « {t.name} »", {"name": t.name},
                              blocked=None if state.can(MANAGE_GUILD) else "le bot a pas la permission Gérer le serveur"))
    everyone = next((r for r in state.roles if r["id"] == gid), None)
    if t.everyone is not None and everyone and int(everyone["permissions"]) != t.everyone:
        actions.append(Action("update_role", "Modifier les permissions de @everyone", {"permissions": str(t.everyone)},
                              target=gid, blocked=no_roles or too_much(t.everyone)))

    # Rôles
    existing_roles = [r for r in state.roles if r["id"] != gid and not r.get("managed")]
    matched_roles: dict[str, dict] = {}
    for role in t.roles:
        found = next((r for r in existing_roles if r["name"].lower() == role.name.lower()), None)
        payload = {k: v for k, v in {
            "name": role.name,
            "permissions": str(role.permissions) if role.permissions is not None else None,
            "color": role.color, "hoist": role.hoist, "mentionable": role.mentionable,
        }.items() if v is not None}
        if found is None:
            actions.append(Action("create_role", f"Créer le rôle « {role.name} »", payload,
                                  blocked=no_roles or too_much(role.permissions)))
            continue
        matched_roles[role.name.lower()] = found
        changes = {k: v for k, v in payload.items() if (str(found.get(k)) if k == "permissions" else found.get(k)) != v}
        if changes:
            detail = ", ".join({"name": "nom", "permissions": "permissions", "color": "couleur", "hoist": "affichage à part",
                                "mentionable": "mentionnable"}[k] for k in changes)
            actions.append(Action("update_role", f"Modifier le rôle « {found['name']} » ({detail})", changes,
                                  target=found["id"], blocked=no_roles or above_bot(found) or too_much(role.permissions)))
    created_roles = any(a.kind == "create_role" for a in actions)
    wanted = [matched_roles[r.name.lower()]["id"] for r in t.roles if r.name.lower() in matched_roles]
    current = [r["id"] for r in sorted(existing_roles, key=lambda r: -r["position"]) if r["id"] in wanted]
    if len(t.roles) > 1 and (created_roles or current != wanted):
        actions.append(Action("reorder_roles", "Mettre les rôles dans l'ordre du modèle", blocked=no_roles))

    # Catégories
    cats = [c for c in state.channels if c["type"] == 4]
    matched_cats: dict[str, dict] = {}
    for cat in t.categories:
        found = next((c for c in cats if c["name"].lower() == cat.name.lower()
                      and c["id"] not in {m["id"] for m in matched_cats.values()}), None)
        if found is None:
            actions.append(Action("create_channel", f"Créer la catégorie « {cat.name} »",
                                  {"name": cat.name, "type": 4, "overwrites": cat.overwrites}, blocked=no_channels))
            continue
        matched_cats[cat.name.lower()] = found
        changes: dict = {}
        if found["name"] != cat.name:
            changes["name"] = cat.name
        if _overwrites_change(cat.overwrites, found, state, mirror):
            changes["overwrites"] = cat.overwrites
        if changes:
            actions.append(Action("update_channel", f"Modifier la catégorie « {found['name']} » ({_fr(changes)})",
                                  changes, target=found["id"], blocked=no_channels))

    # Salons : d'abord ceux déjà dans la bonne catégorie, ensuite ceux à déplacer
    others = [c for c in state.channels if c["type"] != 4]
    compatible = lambda c, tc: c["type"] == tc.type or {c["type"], tc.type} <= {0, 5}  # noqa: E731
    same_name = lambda c, tc: channel_key(c["name"], c["type"]) == channel_key(tc.name, tc.type)  # noqa: E731
    wanted_parent = {}
    for tc in t.all_channels():
        parent = matched_cats.get((tc.category or "").lower())
        wanted_parent[id(tc)] = parent["id"] if parent else None
    matches: dict[int, dict] = {}
    used: set[str] = set()
    for strict in (True, False):
        for tc in t.all_channels():
            if id(tc) in matches or (strict and tc.category and wanted_parent[id(tc)] is None):
                continue
            found = next((c for c in others if c["id"] not in used and compatible(c, tc) and same_name(c, tc)
                          and (not strict or (c.get("parent_id") or None) == wanted_parent[id(tc)])), None)
            if found:
                matches[id(tc)] = found
                used.add(found["id"])
    for tc in t.all_channels():
        found = matches.get(id(tc))
        where = f"« {tc.name} »" + (f" dans « {tc.category} »" if tc.category else "")
        kind_label = {0: "le salon", 2: "le vocal", 5: "le salon d'annonces", 13: "la scène", 15: "le forum"}[tc.type]
        fields = {k: v for k, v in {"topic": tc.topic, "nsfw": tc.nsfw, "rate_limit_per_user": tc.slowmode,
                                    "user_limit": tc.user_limit}.items() if v is not None}
        if found is None:
            actions.append(Action("create_channel", f"Créer {kind_label} {where}",
                                  {"name": tc.name, "type": tc.type, "category": tc.category, "overwrites": tc.overwrites, **fields},
                                  blocked=no_channels))
            continue
        changes = {}
        if tc.type in VOICE_LIKE and found["name"] != tc.name:
            changes["name"] = tc.name
        defaults = {"topic": "", "nsfw": False, "rate_limit_per_user": 0, "user_limit": 0}
        for key, value in fields.items():
            if (found.get(key) or defaults[key]) != (value or defaults[key]):
                changes[key] = value
        if (found.get("parent_id") or None) != wanted_parent[id(tc)] or (tc.category and wanted_parent[id(tc)] is None):
            changes["category"] = tc.category
        if _overwrites_change(tc.overwrites, found, state, mirror):
            changes["overwrites"] = tc.overwrites
        if tc.type != found["type"]:
            changes["type"] = tc.type
        if changes:
            actions.append(Action("update_channel", f"Modifier {kind_label} « {found['name']} » ({_fr(changes)})",
                                  changes, target=found["id"], blocked=no_channels))

    channel_changes = any(a.kind in ("create_channel", "update_channel") for a in actions)
    if (t.categories or t.all_channels()) and (channel_changes or channel_positions(t, state.channels)):
        actions.append(Action("reorder_channels", "Mettre les catégories et les salons dans l'ordre du modèle",
                              blocked=no_channels))

    # Mode miroir : supprimer ce qui est pas dans le modèle
    if mirror:
        keep_roles = {m["id"] for m in matched_roles.values()}
        for r in sorted(existing_roles, key=lambda r: -r["position"]):
            if r["id"] not in keep_roles:
                actions.append(Action("delete_role", f"Supprimer le rôle « {r['name']} »", target=r["id"],
                                      blocked=no_roles or above_bot(r), destructive=True))
        keep = used | {m["id"] for m in matched_cats.values()}
        for c in sorted(state.channels, key=lambda c: (c["type"] == 4, c.get("position", 0))):
            if c["id"] not in keep:
                what = "la catégorie" if c["type"] == 4 else "le salon"
                actions.append(Action("delete_channel", f"Supprimer {what} « {c['name']} »", target=c["id"],
                                      blocked=no_channels, destructive=True))
    return Plan(actions=actions, warnings=warnings, template=t, mirror=mirror)


def _fr(changes: dict) -> str:
    names = {"name": "nom", "topic": "sujet", "nsfw": "NSFW", "rate_limit_per_user": "mode lent", "user_limit": "limite",
             "category": "catégorie", "overwrites": "permissions", "type": "type"}
    return ", ".join(names.get(k, k) for k in changes)


# ---------------------------------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    owner_id: int
    guild_id: str
    steps: list[dict]
    status: str = "running"  # running, done, error
    error: str | None = None
    started: float = field(default_factory=time.time)

    def public(self) -> dict:
        done = sum(1 for s in self.steps if s["state"] not in ("pending", "running"))
        return {"id": self.id, "status": self.status, "error": self.error, "steps": self.steps,
                "done": done, "total": len(self.steps)}


JOBS: dict[str, Job] = {}


def new_job(owner_id: int, guild_id: str, plan: Plan, allow_delete: bool) -> Job:
    for jid in [j for j, job in JOBS.items() if time.time() - job.started > 3600]:
        JOBS.pop(jid, None)
    steps = [{"label": a.label, "state": "pending", "error": None} for a in plan.actions]
    for step, action in zip(steps, plan.actions):
        if action.blocked:
            step.update(state="skipped", error=action.blocked)
        elif action.destructive and not allow_delete:
            step.update(state="skipped", error="suppression pas confirmée")
    job = Job(id=secrets.token_hex(6), owner_id=owner_id, guild_id=str(guild_id), steps=steps)
    JOBS[job.id] = job
    return job


def _ow_payload(overwrites: Overwrites, current: dict | None, role_ids: dict[str, str], state: GuildState, mirror: bool) -> list[dict]:
    named, other = ({}, []) if current is None else _role_overwrites(current, state)
    target = dict(overwrites) if mirror else {**named, **overwrites}
    result = list(other)
    for name, (allow, deny) in target.items():
        rid = state.id if name == EVERYONE else role_ids.get(name.lower())
        if rid and (allow or deny):
            result.append({"id": rid, "type": 0, "allow": str(allow), "deny": str(deny)})
    return result


async def apply_plan(api: DiscordAPI, state: GuildState, plan: Plan, job: Job) -> None:
    ctx = {
        "roles": {r["name"].lower(): r["id"] for r in sorted(state.roles, key=lambda r: r["position"])
                  if r["id"] != state.id and not r.get("managed")},
        "cats": {c["name"].lower(): c["id"] for c in state.channels if c["type"] == 4},
        "channels": {c["id"]: c for c in state.channels},
    }
    try:
        for step, action in zip(job.steps, plan.actions):
            if step["state"] != "pending":
                continue
            step["state"] = "running"
            try:
                await _apply_one(api, state, plan, action, ctx)
                step["state"] = "ok"
            except DiscordError as e:
                step.update(state="error", error=str(e))
                if e.status == 401:
                    raise
        job.status = "done"
    except Exception as e:  # noqa: BLE001
        job.status, job.error = "error", str(e)
        for step in job.steps:
            if step["state"] in ("pending", "running"):
                step.update(state="skipped", error="arrêté")


async def _apply_one(api: DiscordAPI, state: GuildState, plan: Plan, a: Action, ctx: dict) -> None:
    gid, p, t = state.id, a.payload, plan.template
    role_ids, cat_ids, channels = ctx["roles"], ctx["cats"], ctx["channels"]
    if a.kind == "rename_guild":
        await api.request("PATCH", f"/guilds/{gid}", {"name": p["name"]})
    elif a.kind == "create_role":
        role = await api.request("POST", f"/guilds/{gid}/roles", p)
        role_ids[role["name"].lower()] = role["id"]
    elif a.kind == "update_role":
        role = await api.request("PATCH", f"/guilds/{gid}/roles/{a.target}", p)
        if role and a.target != gid:
            role_ids[role["name"].lower()] = role["id"]
    elif a.kind == "delete_role":
        await api.request("DELETE", f"/guilds/{gid}/roles/{a.target}")
    elif a.kind == "reorder_roles":
        roles = await api.request("GET", f"/guilds/{gid}/roles")
        top = max((r["position"] for r in roles if r["id"] in state.bot_roles), default=0)  # a monté si on a créé des rôles
        below = [r for r in sorted(roles, key=lambda r: -r["position"])
                 if r["id"] != gid and (state.owner or r["position"] < top)]
        wanted = [role_ids[r.name.lower()] for r in t.roles if r.name.lower() in role_ids]
        ordered = [rid for rid in wanted if any(r["id"] == rid for r in below)]
        ordered += [r["id"] for r in below if r["id"] not in ordered]
        if [r["id"] for r in below] != ordered:
            await api.request("PATCH", f"/guilds/{gid}/roles",
                              [{"id": rid, "position": len(ordered) - i} for i, rid in enumerate(ordered)])
    elif a.kind == "create_channel":
        body = {k: v for k, v in p.items() if k not in ("overwrites", "category")}
        if p.get("category"):
            body["parent_id"] = cat_ids.get(p["category"].lower())
        if p.get("overwrites") is not None:
            body["permission_overwrites"] = _ow_payload(p["overwrites"], None, role_ids, state, True)
        channel = await api.request("POST", f"/guilds/{gid}/channels", body)
        channels[channel["id"]] = channel
        if channel["type"] == 4:
            cat_ids[channel["name"].lower()] = channel["id"]
    elif a.kind == "update_channel":
        body = {k: v for k, v in p.items() if k not in ("overwrites", "category")}
        if "category" in p:
            body["parent_id"] = cat_ids.get(p["category"].lower()) if p["category"] else None
        if p.get("overwrites") is not None:
            body["permission_overwrites"] = _ow_payload(p["overwrites"], channels.get(a.target), role_ids, state, plan.mirror)
        channel = await api.request("PATCH", f"/channels/{a.target}", body)
        if channel:
            channels[channel["id"]] = channel
    elif a.kind == "delete_channel":
        await api.request("DELETE", f"/channels/{a.target}")
    elif a.kind == "reorder_channels":
        positions = channel_positions(t, await api.request("GET", f"/guilds/{gid}/channels"))
        if positions:
            await api.request("PATCH", f"/guilds/{gid}/channels", positions)


def channel_positions(t: Template, channels: list[dict]) -> list[dict]:
    """Nouvelles positions si l'ordre diffère du modèle (modèle d'abord, puis le reste dans son ordre actuel).
    Discord met toujours les vocaux sous les salons texte : on compare donc texte et vocal séparément."""
    changes: list[dict] = []

    def reorder(current: list[dict], wanted: list[dict], start: int = 0) -> None:
        ordered = wanted + [c for c in current if c not in wanted]
        if ordered != current:
            changes.extend({"id": c["id"], "position": start + i} for i, c in enumerate(ordered))

    by_pos = lambda c: (c.get("position", 0), int(c["id"]))  # noqa: E731
    cats = sorted([c for c in channels if c["type"] == 4], key=by_pos)
    wanted_cats = []
    for tc in t.categories:
        match = next((c for c in cats if c not in wanted_cats and c["name"].lower() == tc.name.lower()), None)
        if match:
            wanted_cats.append(match)
    reorder(cats, wanted_cats)

    cat_by_name = {c["name"].lower(): c["id"] for c in reversed(wanted_cats)}
    groups: dict[tuple[str | None, bool], list[TChannel]] = {}
    for tc in t.all_channels():
        parent = cat_by_name.get(tc.category.lower()) if tc.category else None
        if tc.category and parent is None:
            continue
        groups.setdefault((parent, tc.type in VOICE_LIKE), []).append(tc)
    for (parent, voice), tcs in groups.items():
        children = sorted([c for c in channels if c["type"] != 4 and (c.get("parent_id") or None) == parent
                           and (c["type"] in VOICE_LIKE) == voice], key=by_pos)
        wanted: list[dict] = []
        for tc in tcs:
            match = next((c for c in children if c not in wanted and channel_key(c["name"], c["type"]) == channel_key(tc.name, tc.type)), None)
            if match:
                wanted.append(match)
        reorder(children, wanted)
    return changes


# ---------------------------------------------------------------------------------------------------
# Export d'un serveur existant / import d'un modèle discord.new
# ---------------------------------------------------------------------------------------------------
def _ow_dict(overwrites: list[dict], role_name) -> dict:
    result = {}
    for ow in overwrites or []:
        if int(ow.get("type", 0)) != 0:
            continue
        name = role_name(ow["id"])
        allow, deny = int(ow.get("allow", 0)), int(ow.get("deny", 0))
        if name is None or not (allow or deny):
            continue
        entry = {}
        if allow:
            entry["allow"] = int_to_perms(allow)
        if deny:
            entry["deny"] = int_to_perms(deny)
        result[name] = entry
    return result


def snapshot_to_dict(name: str, roles: list[dict], channels: list[dict], everyone_id: Any) -> dict:
    everyone_id = str(everyone_id)
    by_id = {str(r["id"]): r for r in roles}

    def role_name(rid: Any) -> str | None:
        rid = str(rid)
        if rid == everyone_id:
            return EVERYONE
        r = by_id.get(rid)
        return r["name"] if r else None

    out: dict[str, Any] = {"name": name}
    everyone = by_id.get(everyone_id)
    if everyone:
        out["everyone"] = {"permissions": int_to_perms(int(everyone.get("permissions", 0)))}
    out["roles"] = []
    for r in sorted((r for r in roles if str(r["id"]) != everyone_id and not r.get("managed")), key=lambda r: -int(r.get("position", 0))):
        role: dict[str, Any] = {"name": r["name"]}
        if r.get("color"):
            role["color"] = f"#{int(r['color']):06x}"
        if r.get("hoist"):
            role["hoist"] = True
        if r.get("mentionable"):
            role["mentionable"] = True
        role["permissions"] = int_to_perms(int(r.get("permissions", 0)))
        out["roles"].append(role)

    def channel_dict(c: dict, parent_ow: dict | None) -> dict:
        d: dict[str, Any] = {"name": c["name"]}
        if c["type"] != 0:
            d["type"] = TYPE_NAMES.get(c["type"], "text")
        if c.get("topic"):
            d["topic"] = c["topic"]
        if c.get("nsfw"):
            d["nsfw"] = True
        if c.get("rate_limit_per_user"):
            d["slowmode"] = c["rate_limit_per_user"]
        if c.get("user_limit"):
            d["user_limit"] = c["user_limit"]
        ow = _ow_dict(c.get("permission_overwrites"), role_name)
        # Seulement ce qui diffère de la catégorie (un rôle de la catégorie absent du salon = {})
        extra = {k: v for k, v in ow.items() if (parent_ow or {}).get(k) != v}
        extra.update({k: {} for k in (parent_ow or {}) if k not in ow})
        if extra:
            d["overwrites"] = extra
        return d

    usable = [c for c in channels if c.get("type") in TYPE_NAMES]
    cats = sorted([c for c in usable if c["type"] == 4], key=lambda c: (int(c.get("position", 0)), str(c["id"])))

    def children(parent: Any) -> list[dict]:
        return sorted([c for c in usable if c["type"] != 4 and str(c.get("parent_id")) == str(parent)],
                      key=lambda c: (c["type"] in VOICE_LIKE, int(c.get("position", 0)), str(c["id"])))

    out["categories"] = []
    for cat in cats:
        ow = _ow_dict(cat.get("permission_overwrites"), role_name)
        entry: dict[str, Any] = {"name": cat["name"]}
        if ow:
            entry["overwrites"] = ow
        entry["channels"] = [channel_dict(c, ow) for c in children(cat["id"])]
        out["categories"].append(entry)
    out["channels"] = [channel_dict(c, None) for c in children(None)]
    if not out["channels"]:
        del out["channels"]
    return out


def to_yaml(data: dict) -> str:
    class Dumper(yaml.SafeDumper):
        pass

    def flow_list(dumper, value):
        flow = all(isinstance(v, str) for v in value) and len(value) <= 12
        return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flow)

    Dumper.add_representer(list, flow_list)
    text = yaml.dump(data, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120)
    return "# Modèle exporté par Bot Panel\n" + text


async def export_guild(api: DiscordAPI, guild_id: str) -> str:
    state = await load_state(api, guild_id)
    return to_yaml(snapshot_to_dict(state.guild["name"], state.roles, state.channels, state.id))


TEMPLATE_CODE = re.compile(r"(?:discord\.new/|discord(?:app)?\.com/template/)?([A-Za-z0-9]{2,32})/?$")


async def import_discord_template(api: DiscordAPI, code_or_url: str) -> str:
    match = TEMPLATE_CODE.search((code_or_url or "").strip())
    if not match:
        raise TemplateError("Code de modèle invalide (ex. https://discord.new/abcd1234)")
    data = await api.template(match.group(1))
    src = data.get("serialized_source_guild") or {}
    roles = [{**r, "position": i} for i, r in enumerate(src.get("roles") or [])]
    everyone = roles[0]["id"] if roles else 0
    return to_yaml(snapshot_to_dict(src.get("name") or data.get("name") or "Serveur", roles, src.get("channels") or [], everyone))


def invite_url(application_id: str) -> str:
    return f"https://discord.com/oauth2/authorize?client_id={application_id}&scope=bot&permissions={INVITE_PERMISSIONS}"


EXAMPLE = """# Modèle de serveur : modifie, puis « Voir le plan » pour voir ce qui va changer.
name: Mon serveur

everyone:
  permissions: [view_channel, send_messages, read_message_history, add_reactions, connect, speak, use_application_commands]

roles:                     # du plus haut au plus bas
  - name: Admin
    color: "#e0457b"
    hoist: true
    permissions: [administrator]
  - name: Modo
    color: "#9b59b6"
    hoist: true
    permissions: [manage_messages, kick_members, moderate_members, manage_nicknames]
  - name: Membre
    color: "#3498db"
  - name: Nouveau

categories:
  - name: Bienvenue
    overwrites:
      "@everyone": {deny: [send_messages]}
    channels:
      - {name: règles, topic: "Lis les règles avant de parler"}
      - name: annonces
      - name: arrivées

  - name: Discussion
    overwrites:
      Nouveau: {deny: [send_messages]}
    channels:
      - {name: général, topic: "On jase de tout"}
      - {name: médias, slowmode: 10}
      - {name: commandes-bots}
      - {name: Vocal, type: voice}
      - {name: Jeux, type: voice, user_limit: 5}

  - name: Staff
    private: true
    overwrites:
      Modo: {allow: [view_channel]}
    channels:
      - name: staff
      - name: logs
      - {name: Réunion, type: voice}
"""
