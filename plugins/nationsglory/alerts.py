"""Logique pure des alertes NationsGlory (guerres, power, warzone) : testable sans Discord ni API.

Chaque fonction compare ce que l'API donne maintenant à ce que le bot a vu au tour d'avant
(gardé dans la base du plugin) et retourne les messages à publier. Au tout premier tour,
on prend note sans rien annoncer, pour pas inonder le salon avec l'historique.
"""
from __future__ import annotations

from dataclasses import dataclass

# Motifs de guerre (codes du jeu, voir la doc de l'API) -> texte lisible
WAR_REASONS = {
    "KILL_WILDERNESS": "kill en wilderness",
    "PROVOCATION": "provocation",
    "TERRITORIAL_EXPANSION": "expansion territoriale",
    "TERRITORIAL_DEFENSE": "défense territoriale",
    "TREASON": "trahison",
    "INTRUSION": "intrusion",
    "SCAM": "arnaque",
    "TPKILL": "TP kill",
    "COLONY_IMDEPENDENCE": "indépendance d'une colonie",  # orthographe du jeu
    "COLONY_REFUSAL": "refus de colonisation",
    "COLONY_PROTECTION": "protection d'une colonie",
    "COLONY_STEAL": "vol de colonie",
    "BUFFER_COUNTRY": "pays tampon",
    "UNDER_POWER": "sous le power",
    "REPRISAL": "représailles",
    "FOLLOW_WAR": "guerre de soutien",
    "MMR500": "MMR 500",
    "EMPIRE_RIVALITY": "rivalité d'empires",
    "WAR_REVENGE": "revanche",
    "BOMBING_ASSISTANCE": "aide au bombardement",
    "WARZONE_DOMINATION": "domination de la warzone",
    "PROTECTORAT": "protectorat",
}
STATUS_LABELS = {"negotiation": "En négociation", "in_progress": "En cours", "finished": "Terminée"}

# Niveaux de power du pays, du meilleur au pire
POWER_OK, POWER_LOW, POWER_UNDER = "ok", "bas", "sous"


def reason_label(code: str | None) -> str:
    if not code:
        return "sans motif publié"
    return WAR_REASONS.get(code, code.replace("_", " ").lower())


def bold(name: str | None) -> str:
    text = str(name or "?").replace("*", "\\*").replace("_", "\\_")
    return f"**{text}**"


# ---------------------------------------------------------------- guerres
@dataclass(frozen=True)
class WarEvent:
    kind: str  # declared | started | ended | assault
    war: dict
    important: bool = False  # mérite la mention du rôle d'alerte
    detail: str = ""


def war_snapshot(wars: list[dict]) -> dict[str, str]:
    """Ce qu'on garde d'un tour à l'autre : {war_id: statut}."""
    return {str(w["war_id"]): str(w.get("status")) for w in wars if w.get("war_id") is not None}


def war_events(previous: dict[str, str] | None, wars: list[dict], country_id: str) -> list[WarEvent]:
    """Nouvelles guerres, débuts et fins depuis le tour d'avant. Rien au premier tour."""
    if previous is None:
        return []
    events: list[WarEvent] = []
    for w in sorted(wars, key=lambda w: int(w.get("war_id") or 0)):
        wid, status = str(w.get("war_id")), w.get("status")
        before = previous.get(wid)
        we_defend = w.get("defender_id") == country_id
        if before is None:
            if status == "finished":
                continue  # finie avant qu'on la voie : rien à annoncer
            events.append(WarEvent("declared", w, important=we_defend))
        elif before != status:
            if status == "in_progress" and before == "negotiation":
                events.append(WarEvent("started", w, important=True))
            elif status == "finished":
                events.append(WarEvent("ended", w))
    return events


def assault_events(war: dict, seen: int | None, country_id: str) -> tuple[list[WarEvent], int]:
    """Nouveaux assauts d'une guerre en cours. Retourne (événements, nombre d'assauts vus)."""
    assaults = [a for a in war.get("assaults") or [] if isinstance(a, dict)]
    if seen is None:
        return [], len(assaults)
    events = []
    attacker = war.get("attacker_id") == country_id
    for a in assaults[seen:]:
        won = (a.get("winner") == "attacker") == attacker
        events.append(WarEvent("assault", war, detail="won" if won else "lost"))
    return events, len(assaults)


def war_line(event: WarEvent, country_id: str) -> str:
    w = event.war
    att, dfn = bold(w.get("attacker_name")), bold(w.get("defender_name"))
    we_attack = w.get("attacker_id") == country_id
    if event.kind == "declared":
        if we_attack:
            return f"⚔️ {att} déclare la guerre à {dfn} ({reason_label(w.get('reason'))})."
        return f"🚨 {att} nous déclare la guerre! Motif : {reason_label(w.get('reason'))}."
    if event.kind == "started":
        return f"🔥 La guerre {att} contre {dfn} commence!"
    if event.kind == "ended":
        winner = w.get("winner")
        if winner in ("attacker", "defender"):
            won = (winner == "attacker") == we_attack
            result = "🏆 Victoire" if won else "💀 Défaite"
            return f"{result} : la guerre {att} contre {dfn} est terminée."
        return f"🕊️ La guerre {att} contre {dfn} est terminée sans vainqueur (paix)."
    if event.kind == "assault":
        enemy = dfn if we_attack else att
        if event.detail == "won":
            return f"✅ Assaut gagné contre {enemy}!"
        return f"❌ Assaut perdu contre {enemy}."
    return ""


# ---------------------------------------------------------------- power
def power_level(card: dict, margin: int) -> str | None:
    """ok, bas (moins de `margin` de power au-dessus des claims) ou sous (attaquable : power < claims)."""
    power, claims = card.get("power"), card.get("claims")
    if not isinstance(power, int) or not isinstance(claims, int):
        return None
    if power < claims:
        return POWER_UNDER
    if power - claims < max(0, margin):
        return POWER_LOW
    return POWER_OK


def power_alert(previous: str | None, level: str | None, card: dict, name: str) -> tuple[str | None, bool]:
    """Message quand le niveau de power change. Retourne (message ou None, mention du rôle?)."""
    if previous is None or level is None or level == previous:
        return None, False
    power, claims = card.get("power"), card.get("claims")
    numbers = f"(power {power} · claims {claims})"
    order = (POWER_OK, POWER_LOW, POWER_UNDER)
    if level == POWER_UNDER:
        return f"🚨 {bold(name)} est **sous le power** {numbers} : n'importe qui peut nous déclarer la guerre!", True
    if level == POWER_LOW and order.index(previous) < order.index(level):
        return f"⚠️ Le power de {bold(name)} se rapproche des claims {numbers}. Connectez-vous!", False
    if level == POWER_OK:
        return f"✅ Le power de {bold(name)} est revenu en sécurité {numbers}.", False
    return f"⚠️ {bold(name)} est plus sous le power, mais reste juste {numbers}.", False


def attackable_enemies(enemy_ids: list[str], countries: list[dict]) -> dict[str, dict]:
    """Ennemis dont le power est plus bas que les claims : {country_id: ligne de la liste des pays}."""
    wanted = set(enemy_ids)
    result = {}
    for c in countries:
        cid = c.get("country_id")
        power, claims = c.get("power"), c.get("claims")
        if cid in wanted and isinstance(power, int) and isinstance(claims, int) and power < claims:
            result[cid] = c
    return result


def enemy_lines(previous: list[str] | None, now: dict[str, dict]) -> list[str]:
    """Annonce les ennemis qui viennent de passer sous le power. Rien au premier tour."""
    if previous is None:
        return []
    return [
        f"🎯 Notre ennemi {bold(c.get('name'))} est **sous le power** (power {c.get('power')} · claims {c.get('claims')}) : on peut l'attaquer!"
        for cid, c in now.items()
        if cid not in previous
    ]


# ---------------------------------------------------------------- warzone
def warzone_summary(rows: list[dict], country_id: str, top: int = 10) -> tuple[str | None, list[str], str | None]:
    """(jour clos, lignes du top, ligne de notre pays s'il est hors du top)."""
    if not rows:
        return None, [], None
    rows = sorted(rows, key=lambda r: int(r.get("rank") or 10**6))
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    def line(r: dict) -> str:
        rank = r.get("rank")
        name = bold(r.get("country_name"))
        if r.get("country_id") == country_id:
            name = f"__{name}__"
        total = f"{r.get('total', 0):,}".replace(",", " ")
        return f"{medals.get(rank, f'`{rank}.`')} {name} · {total} pts"

    lines = [line(r) for r in rows[:top]]
    ours = next((r for r in rows if r.get("country_id") == country_id), None)
    extra = line(ours) if ours and int(ours.get("rank") or 0) > top else None
    return str(rows[0].get("period_end") or ""), lines, extra
