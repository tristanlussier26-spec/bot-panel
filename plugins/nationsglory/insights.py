"""Logique pure des commandes de consultation : classements, membres en ligne, activité (sans Discord ni API)."""
from __future__ import annotations

from datetime import date

# Critère de /classement -> (champ de la liste des pays, libellé, suffixe)
RANKINGS = {
    "power": ("power", "Power", ""),
    "score": ("score", "Score", ""),
    "membres": ("members_count", "Membres", ""),
    "claims": ("claims", "Claims", ""),
    "mmr": ("mmr", "MMR", ""),
    "banque": ("bank_balance", "Banque", " $"),
}
MISSING_DAYS = 90  # l'API publie la dernière connexion seulement pour les 90 derniers jours


def fmt(value) -> str:
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else "?"


def ranking(countries: list[dict], criterion: str) -> list[dict]:
    """Pays avec au moins un membre, triés par le critère (les pays vides ont parfois un power artificiel)."""
    field = RANKINGS[criterion][0]
    rows = [c for c in countries if (c.get("members_count") or 0) > 0 and isinstance(c.get(field), int)]
    return sorted(rows, key=lambda c: (-c[field], str(c.get("name") or "").lower()))


def ranking_lines(rows: list[dict], criterion: str, country_id: str | None, top: int = 10) -> tuple[list[str], str | None]:
    """(lignes du top, ligne de notre pays s'il est plus loin)."""
    field, _, suffix = RANKINGS[criterion]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    def line(pos: int, c: dict) -> str:
        name = f"**{c.get('name')}**"
        if c.get("country_id") == country_id:
            name = f"__{name}__"
        return f"{medals.get(pos, f'`{pos}.`')} {name} · {fmt(c[field])}{suffix}"

    lines = [line(i + 1, c) for i, c in enumerate(rows[:top])]
    ours = next(((i + 1, c) for i, c in enumerate(rows) if c.get("country_id") == country_id), None)
    extra = line(*ours) if ours and ours[0] > top else None
    return lines, extra


def online_members(members: dict[str, dict], online: set[str]) -> list[dict]:
    """Membres du pays connectés en ce moment, du plus haut grade au plus bas."""
    order = {"LEADER": 0, "OFFICER": 1, "MEMBER": 2, "RECRUIT": 3}
    rows = [m for key, m in members.items() if key in online]
    return sorted(rows, key=lambda m: (order.get(m.get("role"), 9), m["username"].lower()))


def days_since(day: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(day)).days
    except (TypeError, ValueError):
        return None


def inactivity(members: dict[str, dict], logins: dict[str, list], today: date, days: int) -> dict:
    """Classe les membres selon leur dernière connexion connue.

    `logins` : {pseudo en minuscules: [jour | "none" | "private", horodatage de la vérification]}.
    Retourne {"inactifs": [(membre, jours)], "absents": [...], "prives": [...], "inconnus": [...], "verifies": n}.
    """
    result: dict = {"inactifs": [], "absents": [], "prives": [], "inconnus": [], "verifies": 0}
    for key, m in members.items():
        entry = logins.get(key)
        if not entry:
            result["inconnus"].append(m)
            continue
        result["verifies"] += 1
        value = entry[0]
        if value == "private":
            result["prives"].append(m)
        elif value == "none":
            result["absents"].append(m)  # aucune connexion depuis plus de 90 jours
        else:
            n = days_since(value, today)
            if n is not None and n >= days:
                result["inactifs"].append((m, n))
    result["inactifs"].sort(key=lambda x: (-x[1], x[0]["username"].lower()))
    return result


def next_to_check(members: dict[str, dict], logins: dict[str, list], now: float, refresh: float) -> str | None:
    """Le membre dont la dernière connexion est la plus vieille à vérifier (jamais vérifié d'abord)."""
    best, best_ts = None, None
    for key, m in members.items():
        entry = logins.get(key)
        ts = entry[1] if entry else -1.0
        if entry and now - ts < refresh:
            continue
        if best_ts is None or ts < best_ts:
            best, best_ts = m["username"], ts
    return best


def war_detail_lines(war: dict, names: dict[str, str]) -> list[tuple[str, str]]:
    """Champs (titre, texte) de la fiche d'une guerre : assauts, missiles, accords."""
    fields: list[tuple[str, str]] = []
    assaults = [a for a in war.get("assaults") or [] if isinstance(a, dict)]
    if assaults:
        att = sum(1 for a in assaults if a.get("winner") == "attacker")
        fields.append(("Assauts", f"🗡️ {war.get('attacker_name')} : {att} · 🛡️ {war.get('defender_name')} : {len(assaults) - att}"))
    missiles = war.get("missiles") or {}
    parts = []
    for side, label in (("attacker", war.get("attacker_name")), ("defender", war.get("defender_name"))):
        fired = missiles.get(side) or {}
        if fired:
            parts.append(f"**{label}** : " + ", ".join(f"{n}× {k}" for k, n in sorted(fired.items(), key=lambda x: -x[1])))
    if parts:
        fields.append(("Missiles", "\n".join(parts)))
    agreements = [a for a in war.get("agreements_signed") or [] if isinstance(a, dict)]
    if agreements:
        fields.append(("Accords", "\n".join(
            f"{a.get('type')} signé le {a.get('signed_on')} ({a.get('duration_days')} j, {a.get('status')})" for a in agreements)))
    return fields


# ---------------------------------------------------------------- fiche des membres (balayage des profils)
RANK_ORDER = {"LEADER": 0, "OFFICER": 1, "MEMBER": 2, "RECRUIT": 3}


def profile_entry(profile: dict | None, server: str, fallback_login: str | None, now: float) -> list:
    """Ce qu'on garde d'un membre : [dernière connexion | "none" | "private", vérifié à, temps de jeu ici (min),
    points de succès, grade boutique, première connexion, temps de jeu total (min)]."""
    if profile and profile.get("private"):
        return ["private", now]
    if profile:
        here = next((s for s in profile.get("servers") or [] if isinstance(s, dict) and s.get("server") == server), {})
        login = here.get("last_login_on") or fallback_login or "none"
        return [login, now, here.get("playtime_minutes"), profile.get("achievement_points"), here.get("grade"),
                profile.get("first_join_on"), profile.get("playtime_total_minutes")]
    return [fallback_login or "none", now]


def entry_field(entry: list | None, index: int):
    return entry[index] if entry and len(entry) > index else None


def member_rows(members: dict[str, dict], logins: dict[str, list], online: set[str],
                owners: dict[str, list[dict]], today: date) -> list[dict]:
    rows = []
    for key, m in members.items():
        entry = logins.get(key)
        login = entry_field(entry, 0)
        days = days_since(login, today) if login not in (None, "none", "private") else None
        rows.append({
            "key": key, "name": m["username"], "role": m.get("role"), "online": key in online,
            "login": login, "days": days, "playtime": entry_field(entry, 2), "achievements": entry_field(entry, 3),
            "grade": entry_field(entry, 4), "first_join": entry_field(entry, 5),
            "enterprises": owners.get(key, []),
        })
    return rows


def sort_members(rows: list[dict], how: str) -> list[dict]:
    if how == "activite":  # connectés d'abord, puis les plus récents
        return sorted(rows, key=lambda r: (not r["online"], r["days"] if r["days"] is not None else 10**4, r["name"].lower()))
    if how == "temps":
        return sorted(rows, key=lambda r: (-(r["playtime"] or 0), r["name"].lower()))
    if how == "succes":
        return sorted(rows, key=lambda r: (-(r["achievements"] or 0), r["name"].lower()))
    if how == "anciennete":
        return sorted(rows, key=lambda r: (r["first_join"] or "9999", r["name"].lower()))
    return sorted(rows, key=lambda r: (RANK_ORDER.get(r["role"], 9), not r["online"], r["name"].lower()))


def hours(minutes) -> str:
    return f"{minutes // 60:,} h".replace(",", " ") if isinstance(minutes, int) else "?"


def member_line(r: dict) -> str:
    dot = "🟢" if r["online"] else ("⚫" if r["days"] is None or r["days"] < 7 else "😴")
    bits = [f"{dot} **{r['name']}**"]
    if r["online"]:
        bits.append("en jeu")
    elif r["login"] == "private":
        bits.append("profil privé")
    elif r["login"] == "none":
        bits.append("+90 j")
    elif r["days"] is not None:
        bits.append("aujourd'hui" if r["days"] == 0 else f"il y a {r['days']} j")
    if isinstance(r["playtime"], int):
        bits.append(hours(r["playtime"]))
    if r["enterprises"]:
        bits.append("🏢" * min(3, len(r["enterprises"])))
    return " · ".join(bits)
