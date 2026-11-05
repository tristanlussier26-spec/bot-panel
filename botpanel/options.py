"""« Options du bot » : un bot peut décrire ses réglages, et Bot Panel affiche le formulaire.

Ça passe par des fichiers dans le dossier du bot (pas de réseau, pas de port) :

    .botpanel/schema.json   écrit par le bot : ses pages de réglages (voir FIELD_TYPES)
    .botpanel/state.json    écrit par le bot : ses serveurs (salons, rôles) et les valeurs actuelles
    .botpanel/inbox/*.json  écrit par le panel : une modification demandée
    .botpanel/outbox/*.json écrit par le bot : la réponse (ok ou l'erreur à afficher)

Le bot valide lui-même ce qu'il reçoit. Le panel, lui, se méfie de ce que le bot écrit (tailles, types).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from botpanel.files import BotFiles, FileError

DIR = ".botpanel"
FIELD_TYPES = {
    "bool", "int", "text", "textarea", "lines", "select",
    "channel", "category", "role", "channels", "roles",
    "role_map",      # un rôle pour chaque clé de « keys » (ex. grades -> rôles)
    "number_roles",  # liste « nombre -> rôle » (ex. niveau -> rôle de récompense)
}
MAX_SCHEMA = 512 * 1024
MAX_STATE = 8 * 1024 * 1024
STALE_AFTER = 90  # secondes sans nouvelles du bot


class OptionsError(Exception):
    pass


def _read_json(files: BotFiles, name: str, limit: int) -> Any:
    try:
        path = files.path(f"{DIR}/{name}")
    except FileError:
        return None
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > limit:
        raise OptionsError(f"{name} est trop gros")
    try:
        return json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # le bot est peut-être en train de l'écrire


def _s(value: Any, limit: int = 200) -> str:
    return str(value if value is not None else "")[:limit]


def _id(value: Any) -> str:
    text = _s(value, 30)
    return text if text.isdigit() else ""


def has_schema(files: BotFiles) -> bool:
    try:
        return files.path(f"{DIR}/schema.json").is_file()
    except FileError:
        return False


def clean_schema(raw: Any) -> dict:
    """Schéma propre (rien de ce que le bot écrit est utilisé tel quel)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("pages"), list):
        raise OptionsError("schema.json invalide : il faut une liste « pages »")
    pages = []
    for p in raw["pages"][:50]:
        if not isinstance(p, dict) or not isinstance(p.get("fields"), list):
            continue
        fields = []
        for f in p["fields"][:100]:
            if not isinstance(f, dict) or f.get("type") not in FIELD_TYPES or not _s(f.get("key")):
                continue
            field = {"key": _s(f["key"], 80), "label": _s(f.get("label") or f["key"]), "type": f["type"],
                     "help": _s(f.get("help"), 500)}
            for bound in ("min", "max"):
                if isinstance(f.get(bound), (int, float)) and not isinstance(f.get(bound), bool):
                    field[bound] = f[bound]
            if f["type"] == "select":
                field["choices"] = [[_s(c[0], 100), _s(c[1])] for c in (f.get("choices") or [])[:100]
                                    if isinstance(c, (list, tuple)) and len(c) == 2]
            if f["type"] == "role_map":
                field["keys"] = [[_s(k[0], 100), _s(k[1])] for k in (f.get("keys") or [])[:50]
                                 if isinstance(k, (list, tuple)) and len(k) == 2]
            fields.append(field)
        pages.append({"id": _s(p.get("id"), 80), "title": _s(p.get("title") or p.get("id"), 80),
                      "icon": _s(p.get("icon"), 8), "description": _s(p.get("description"), 500), "fields": fields})
    return {"title": _s(raw.get("title"), 80), "scope": "global" if raw.get("scope") == "global" else "guild", "pages": pages}


def clean_state(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"updated": 0, "scopes": []}
    scopes = []
    for sc in (raw.get("scopes") or [])[:200]:
        if not isinstance(sc, dict):
            continue
        sid = _s(sc.get("id"), 30)
        if not (sid.isdigit() or sid == "global"):
            continue
        scopes.append({
            "id": sid, "name": _s(sc.get("name"), 100),
            "icon": _s(sc.get("icon"), 300) if _s(sc.get("icon"), 300).startswith("https://cdn.discordapp.com/") else None,
            "channels": [{"id": _id(c.get("id")), "name": _s(c.get("name"), 100), "category": _s(c.get("category"), 100),
                          "voice": bool(c.get("voice"))}
                         for c in (sc.get("channels") or [])[:1000] if isinstance(c, dict) and _id(c.get("id"))],
            "categories": [{"id": _id(c.get("id")), "name": _s(c.get("name"), 100)}
                           for c in (sc.get("categories") or [])[:500] if isinstance(c, dict) and _id(c.get("id"))],
            "roles": [{"id": _id(r.get("id")), "name": _s(r.get("name"), 100),
                       "color": _s(r.get("color"), 7) if _s(r.get("color"), 7).startswith("#") else None,
                       "manageable": r.get("manageable", True) is not False}
                      for r in (sc.get("roles") or [])[:500] if isinstance(r, dict) and _id(r.get("id"))],
            "values": sc.get("values") if isinstance(sc.get("values"), dict) else {},
            "info": [[_s(i[0], 60), _s(i[1], 200)] for i in (sc.get("info") or [])[:12]
                     if isinstance(i, (list, tuple)) and len(i) == 2],
            # Comptes Discord qui gèrent ce serveur (pour les bots partagés)
            "managers": [_id(m) for m in (sc.get("managers") or [])[:5000] if _id(m)],
        })
    updated = raw.get("updated")
    return {"updated": updated if isinstance(updated, (int, float)) else 0, "scopes": scopes,
            "app_id": _id(raw.get("app_id")) or None}


def invite_url(app_id: str | None) -> str | None:
    if not app_id:
        return None
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot+applications.commands&permissions=8"


def load(files: BotFiles, *, only_for: str | None = None) -> dict:
    """Schéma + serveurs. only_for : ID Discord d'un compte qui voit seulement les serveurs qu'il gère."""
    raw = _read_json(files, "schema.json", MAX_SCHEMA)
    if raw is None:
        return {"available": False}
    schema = clean_schema(raw)
    state = clean_state(_read_json(files, "state.json", MAX_STATE))
    scopes = state["scopes"]
    if only_for is not None:
        scopes = [sc for sc in scopes if only_for in sc["managers"]]
    for sc in scopes:
        sc.pop("managers", None)  # les IDs des autres restent dans le bot
    return {"available": True, **schema, "scopes": scopes, "updated": state["updated"],
            "stale": time.time() - state["updated"] > STALE_AFTER, "invite": invite_url(state["app_id"])}


def managed_scopes(files: BotFiles, user_id: str) -> tuple[list[dict], str | None]:
    """Serveurs d'un bot partagé que ce compte gère, et le lien pour inviter le bot."""
    state = clean_state(_read_json(files, "state.json", MAX_STATE))
    mine = [{"id": sc["id"], "name": sc["name"], "icon": sc["icon"]} for sc in state["scopes"] if user_id in sc["managers"]]
    return mine, invite_url(state["app_id"])


async def send(files: BotFiles, scope: str, page: str, values: dict, user: dict, *, timeout: float = 10) -> dict:
    """Dépose la demande pour le bot et attend sa réponse."""
    if not (scope.isdigit() or scope == "global") or len(scope) > 30:
        raise OptionsError("Serveur invalide")
    if not isinstance(values, dict):
        raise OptionsError("Valeurs invalides")
    rid = secrets.token_hex(8)
    body = json.dumps({"id": rid, "scope": scope, "page": str(page)[:80], "values": values,
                       "user": {"id": str(user["id"]), "name": user["name"]}}, ensure_ascii=False)
    if len(body) > 256 * 1024:
        raise OptionsError("Trop de données")
    files.write_text(f"{DIR}/inbox/{rid}.tmp", body)
    files.rename(f"{DIR}/inbox/{rid}.tmp", f"{DIR}/inbox/{rid}.json")  # le bot voit jamais un fichier à moitié écrit
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        answer = _read_json(files, f"outbox/{rid}.json", 64 * 1024)
        if isinstance(answer, dict):
            for name in (f"{DIR}/outbox/{rid}.json", f"{DIR}/inbox/{rid}.json"):
                try:
                    files.delete(name)
                except (FileError, OSError):
                    pass
            return {"ok": bool(answer.get("ok")), "error": _s(answer.get("error"), 500) or None}
    try:
        files.delete(f"{DIR}/inbox/{rid}.json")
    except (FileError, OSError):
        pass
    raise OptionsError("Le bot a pas répondu : est-ce qu'il roule ? (regarde sa console)")
