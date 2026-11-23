"""Logique pure de la veille NationsGlory : rapport du jour, diplomatie, pays surveillés, forum, marché.

Comme alerts.py : chaque fonction compare l'état actuel à celui du tour d'avant et retourne quoi publier.
Rien au premier tour (on prend note), pour pas inonder les salons avec l'historique.
"""
from __future__ import annotations

from .alerts import POWER_OK, POWER_UNDER, WarEvent, bold, power_level, reason_label

SPARK = "▁▂▃▄▅▆▇█"

CEREALS = {
    "wheat": "Blé", "corn": "Maïs", "oats": "Avoine", "barley": "Orge", "rye": "Seigle", "soybean": "Soja",
    "sunflower": "Tournesol", "sorgho": "Sorgho", "fonio": "Fonio", "arcticoats": "Avoine arctique",
    "glazedspelt": "Épeautre glacé", "kamut": "Kamut",
}
SKILLS = {"adventurer": "Aventurier", "builder": "Bâtisseur", "engineer": "Ingénieur", "farmer": "Fermier",
          "lumberjack": "Bûcheron", "miner": "Mineur"}


def cereal_name(code: str | None) -> str:
    code = str(code or "")
    base = code.removeprefix("cereal_")
    legacy = {"ble": "Blé", "mais": "Maïs", "avoine": "Avoine", "orge": "Orge", "seigle": "Seigle", "soja": "Soja"}
    return CEREALS.get(base) or legacy.get(base) or base.replace("_", " ").capitalize() or "?"


def fmt(value, suffix: str = "") -> str:
    return f"{value:,}{suffix}".replace(",", " ") if isinstance(value, int) else "?"


def signed(delta: int | None, suffix: str = "") -> str:
    if not isinstance(delta, int) or delta == 0:
        return "="
    return f"{'+' if delta > 0 else '−'}{fmt(abs(delta))}{suffix}"


# ---------------------------------------------------------------- graphiques en texte
def sparkline(values: list[int | None]) -> str:
    """Petite courbe en caractères : ▁▂▃▅▇ (un trou pour une valeur inconnue)."""
    known = [v for v in values if isinstance(v, (int, float))]
    if not known:
        return ""
    low, high = min(known), max(known)
    span = (high - low) or 1
    out = []
    for v in values:
        if not isinstance(v, (int, float)):
            out.append(" ")
        else:
            out.append(SPARK[round((v - low) / span * (len(SPARK) - 1))] if high != low else SPARK[3])
    return "".join(out)


def compress(values: list, width: int = 40) -> list:
    """Garde au plus `width` points (la dernière valeur de chaque paquet), pour tenir dans Discord."""
    if len(values) <= width:
        return list(values)
    size = len(values) / width
    out = []
    for i in range(width):
        chunk = [v for v in values[int(i * size): int((i + 1) * size)] if v is not None]
        out.append(chunk[-1] if chunk else None)
    return out


def series_summary(points: list[dict]) -> dict | None:
    """Début, fin, min, max et variation d'une série de l'API."""
    values = [p.get("value") for p in points]
    known = [v for v in values if isinstance(v, int)]
    if not known:
        return None
    return {"first": known[0], "last": known[-1], "min": min(known), "max": max(known),
            "delta": known[-1] - known[0], "spark": sparkline(compress(values))}


# ---------------------------------------------------------------- diplomatie
def relations_snapshot(card: dict) -> dict:
    rel = card.get("relations") or {}
    return {
        "allies": sorted(i for i in rel.get("allies") or [] if isinstance(i, str)),
        "enemies": sorted(i for i in rel.get("enemies") or [] if isinstance(i, str)),
        "colonies": sorted(c if isinstance(c, str) else (c or {}).get("country_id") or ""
                           for c in card.get("colonies") or []),
        "colonized_by": card.get("colonized_by") if isinstance(card.get("colonized_by"), str)
        else (card.get("colonized_by") or {}).get("country_id") if isinstance(card.get("colonized_by"), dict) else None,
        "alliance": card.get("alliance_key"),
    }


def diplomacy_lines(previous: dict | None, now: dict, names: dict[str, str], alliances: dict[str, str]) -> list[str]:
    """Nouveaux alliés et ennemis, fins d'alliance, colonies, colonisation, changement d'alliance."""
    if previous is None:
        return []

    def n(cid: str | None) -> str:
        return bold(names.get(cid or "") or "un pays disparu")

    lines = []
    for cid in sorted(set(now["allies"]) - set(previous.get("allies") or [])):
        lines.append(f"🤝 {n(cid)} devient notre allié.")
    for cid in sorted(set(previous.get("allies") or []) - set(now["allies"])):
        lines.append(f"💔 {n(cid)} est plus notre allié.")
    for cid in sorted(set(now["enemies"]) - set(previous.get("enemies") or [])):
        lines.append(f"⚔️ {n(cid)} devient notre ennemi.")
    for cid in sorted(set(previous.get("enemies") or []) - set(now["enemies"])):
        lines.append(f"🕊️ {n(cid)} est plus notre ennemi.")
    for cid in sorted(set(now["colonies"]) - set(previous.get("colonies") or []) - {""}):
        lines.append(f"🏴 {n(cid)} devient notre colonie.")
    for cid in sorted(set(previous.get("colonies") or []) - set(now["colonies"]) - {""}):
        lines.append(f"🏳️ {n(cid)} est plus notre colonie.")
    if now["colonized_by"] != previous.get("colonized_by"):
        if now["colonized_by"]:
            lines.append(f"⛓️ On est maintenant une colonie de {n(now['colonized_by'])}!")
        else:
            lines.append("🗽 On est plus une colonie : indépendance!")
    if now["alliance"] != previous.get("alliance"):
        if now["alliance"]:
            lines.append(f"🛡️ On rejoint l'alliance {bold(alliances.get(now['alliance']) or now['alliance'])}.")
        else:
            lines.append(f"🚪 On quitte l'alliance {bold(alliances.get(previous.get('alliance')) or '?')}.")
    return lines


# ---------------------------------------------------------------- pays surveillés
def resolve_watchlist(entries: list[str], countries: list[dict], own_id: str | None) -> tuple[dict[str, str], list[str]]:
    """{country_id: nom} des pays surveillés trouvés, et les noms introuvables."""
    by_name = {str(c.get("name") or "").lower(): c for c in countries}
    found, missing = {}, []
    for entry in entries:
        row = by_name.get(entry.strip().lower())
        if row and row.get("country_id") and row["country_id"] != own_id:
            found[row["country_id"]] = row.get("name") or entry
        elif not row:
            missing.append(entry.strip())
    return found, missing


def watch_power_lines(previous: dict[str, str] | None, countries: list[dict], watched: dict[str, str]) -> tuple[list[str], dict]:
    """Pays surveillés qui passent sous le power (ou qui en sortent). Retourne (lignes, nouvel état)."""
    state, lines = {}, []
    rows = {c.get("country_id"): c for c in countries}
    for cid, name in watched.items():
        row = rows.get(cid)
        if row is None:
            if previous is not None and previous.get(cid) not in (None, "gone"):
                lines.append(f"💀 {bold(name)} a disparu (pays dissous?).")
            state[cid] = "gone"
            continue
        level = power_level(row, 0) or POWER_OK
        state[cid] = level
        before = (previous or {}).get(cid)
        if previous is None or before is None or before == level:
            continue
        if level == POWER_UNDER:
            lines.append(f"🎯 {bold(row.get('name'))} (surveillé) est **sous le power** "
                         f"(power {row.get('power')} · claims {row.get('claims')}) : attaquable!")
        elif before == POWER_UNDER:
            lines.append(f"🛡️ {bold(row.get('name'))} (surveillé) est plus sous le power.")
    return lines, state


def neutral_war_line(event: WarEvent) -> str:
    """Comme alerts.war_line, mais pour une guerre entre deux autres pays."""
    w = event.war
    att, dfn = bold(w.get("attacker_name")), bold(w.get("defender_name"))
    if event.kind == "declared":
        return f"👀 {att} déclare la guerre à {dfn} ({reason_label(w.get('reason'))})."
    if event.kind == "started":
        return f"👀 La guerre {att} contre {dfn} commence."
    if event.kind == "ended":
        winner = w.get("winner")
        if winner in ("attacker", "defender"):
            return f"👀 Guerre {att} contre {dfn} finie : victoire de {att if winner == 'attacker' else dfn}."
        return f"👀 Guerre {att} contre {dfn} finie sans vainqueur."
    return ""


# ---------------------------------------------------------------- forum
def new_threads(previous: list[int] | None, threads: list[dict]) -> list[dict]:
    """Sujets jamais vus, du plus vieux au plus récent. Rien au premier tour."""
    if previous is None:
        return []
    seen = set(previous)
    fresh = [t for t in threads if isinstance(t.get("id"), int) and t["id"] not in seen]
    return sorted(fresh, key=lambda t: t["id"])


# ---------------------------------------------------------------- marché
def market_moves(reference: dict[str, int] | None, rows: list[dict], threshold_pct: int) -> tuple[list[str], dict[str, int]]:
    """Céréales dont le prix a bougé d'au moins `threshold_pct` % depuis la dernière alerte.
    Retourne (lignes, nouveaux prix de référence)."""
    ref = dict(reference or {})
    lines = []
    for row in sorted(rows, key=lambda r: str(r.get("cereal"))):
        code, price = row.get("cereal"), row.get("price")
        if not isinstance(price, int) or not code:
            continue
        before = ref.get(code)
        if reference is None or before is None or before <= 0:
            ref[code] = price
            continue
        change = (price - before) * 100 / before
        if abs(change) >= max(1, threshold_pct):
            arrow = "📈" if change > 0 else "📉"
            lines.append(f"{arrow} {bold(cereal_name(code))} : {fmt(before)} $ → {fmt(price)} $ ({change:+.0f} %)")
            ref[code] = price
    return lines, ref


# ---------------------------------------------------------------- rapport du jour
REPORT_STATS = (("members_count", "👥 Membres", ""), ("power", "⚡ Power", ""), ("claims", "🗺️ Claims", ""),
                ("bank_balance", "💰 Banque", " $"), ("score", "🏆 Score", ""), ("mmr", "🏅 MMR", ""))


def rank_of(countries: list[dict], country_id: str, field: str) -> int | None:
    rows = [c for c in countries if (c.get("members_count") or 0) > 0 and isinstance(c.get(field), int)]
    rows.sort(key=lambda c: -c[field])
    return next((i + 1 for i, c in enumerate(rows) if c.get("country_id") == country_id), None)


def report_snapshot(card: dict, members: dict[str, dict], countries: list[dict], country_id: str) -> dict:
    snap = {key: card.get(key) for key, _, _ in REPORT_STATS}
    if not isinstance(snap.get("members_count"), int):
        snap["members_count"] = len(members)
    snap["members"] = sorted(m["username"] for m in members.values())
    snap["ranks"] = {f: rank_of(countries, country_id, f) for f in ("power", "score", "members_count")}
    return snap


def report_fields(previous: dict | None, now: dict) -> list[tuple[str, str]]:
    """Champs (titre, texte) du rapport : stats avec variation, rangs, arrivées et départs."""
    fields = []
    stats = []
    for key, label, suffix in REPORT_STATS:
        value = now.get(key)
        if not isinstance(value, int):
            continue
        delta = value - previous[key] if previous and isinstance(previous.get(key), int) else None
        stats.append(f"{label} : **{fmt(value, suffix)}**" + (f" ({signed(delta, suffix)})" if delta is not None else ""))
    if stats:
        fields.append(("Le pays", "\n".join(stats)))
    ranks = []
    for field, label in (("power", "power"), ("score", "score"), ("members_count", "membres")):
        rank = (now.get("ranks") or {}).get(field)
        if rank is None:
            continue
        before = ((previous or {}).get("ranks") or {}).get(field)
        move = ""
        if isinstance(before, int) and before != rank:
            move = f" ({'⬆️' if rank < before else '⬇️'} {abs(before - rank)})"
        ranks.append(f"{rank}e en {label}{move}")
    if ranks:
        fields.append(("Classements", "\n".join(ranks)))
    if previous and isinstance(previous.get("members"), list):
        before, after = set(previous["members"]), set(now.get("members") or [])
        joined, left = sorted(after - before, key=str.lower), sorted(before - after, key=str.lower)
        moves = []
        if joined:
            moves.append(f"👋 Arrivées ({len(joined)}) : " + ", ".join(joined[:20]) + ("…" if len(joined) > 20 else ""))
        if left:
            moves.append(f"🚪 Départs ({len(left)}) : " + ", ".join(left[:20]) + ("…" if len(left) > 20 else ""))
        fields.append(("Mouvements", "\n".join(moves) or "Aucune arrivée ni départ."))
    return fields
