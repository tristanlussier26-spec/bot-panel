"""Logique pure des entreprises NationsGlory : celles du pays, totaux, rangs, activité (sans Discord ni API).

L'API nomme seulement le propriétaire d'une entreprise (pas ses employés) : les entreprises « du pays »
sont celles dont le propriétaire est membre du pays, plus celles ajoutées à la main dans les options.
"""
from __future__ import annotations

from .veille import cereal_name, fmt, sparkline

TYPES = {
    "farm": ("🌾", "Ferme"), "electric": ("⚡", "Électricité"), "petrol": ("🛢️", "Pétrole"), "repair": ("🔧", "Réparation"),
    "build": ("🏗️", "Construction"), "terraform": ("⛰️", "Terraformation"), "trader": ("📈", "Trader"),
    "engineer": ("⚙️", "Ingénierie"), "casino": ("🎰", "Casino"), "realestate": ("🏠", "Immobilier"), "bet": ("🎲", "Paris"),
    "pvp": ("⚔️", "PvP"), "journalist": ("📰", "Journal"), "loan": ("🏦", "Prêts"), "lawyer": ("⚖️", "Avocats"),
}
PARCEL_STATUS = {"free": "libres", "to_rent": "à louer", "to_sell": "à vendre", "rented": "louées"}


def type_label(kind: str | None) -> str:
    icon, label = TYPES.get(str(kind or ""), ("🏢", str(kind or "?").capitalize()))
    return f"{icon} {label}"


def country_enterprises(rows: list[dict], members: dict[str, dict], extra_names: list[str] | None = None) -> list[dict]:
    """Entreprises dont le propriétaire est membre du pays, plus celles nommées dans les options."""
    extra = {n.strip().lower() for n in extra_names or [] if n.strip()}
    out = [r for r in rows
           if str(r.get("owner") or "").lower() in members or str(r.get("name") or "").lower() in extra]
    return sorted(out, key=lambda r: (-(r.get("turnover") or 0), str(r.get("name")).lower()))


def totals(rows: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r.get("type") or "?"] = by_type.get(r.get("type") or "?", 0) + 1
    return {
        "count": len(rows),
        "turnover": sum(r.get("turnover") or 0 for r in rows),
        "bank": sum(r.get("bank_balance") or 0 for r in rows),
        "employees": sum(r.get("members_count") or 0 for r in rows),
        "contracts": sum(r.get("contracts_done") or 0 for r in rows),
        "open": sum(1 for r in rows if r.get("is_open")),
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
    }


def rank(rows: list[dict], name: str, field: str = "turnover", kind: str | None = None) -> tuple[int | None, int]:
    """(rang, sur combien) d'une entreprise sur le serveur, par champ, tous types ou un seul type."""
    pool = [r for r in rows if (kind is None or r.get("type") == kind) and isinstance(r.get(field), int)]
    pool.sort(key=lambda r: -r[field])
    pos = next((i + 1 for i, r in enumerate(pool) if r.get("name") == name), None)
    return pos, len(pool)


def owners_index(rows: list[dict]) -> dict[str, list[dict]]:
    """{propriétaire en minuscules: [ses entreprises]}."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("owner"):
            out.setdefault(str(r["owner"]).lower(), []).append(r)
    return out


def enterprise_events(previous: dict[str, dict] | None, rows: list[dict]) -> list[str]:
    """Nouvelles entreprises du pays, celles qui disparaissent et les changements de propriétaire. Rien au 1er tour."""
    now = {r["name"]: {"owner": r.get("owner"), "type": r.get("type")} for r in rows if r.get("name")}
    if previous is None:
        return []
    lines = []
    for name in sorted(now.keys() - previous.keys(), key=str.lower):
        r = now[name]
        lines.append(f"🏢 **{r['owner'] or '?'}** a fondé l'entreprise **{name}** ({type_label(r['type'])})!")
    for name in sorted(previous.keys() - now.keys(), key=str.lower):
        lines.append(f"📉 L'entreprise **{name}** ({type_label(previous[name].get('type'))}) est plus au pays (fermée, renommée ou vendue).")
    for name in sorted(now.keys() & previous.keys(), key=str.lower):
        before, after = previous[name].get("owner"), now[name]["owner"]
        if before and after and before.lower() != after.lower():
            lines.append(f"🔁 **{name}** change de propriétaire : {before} → **{after}**.")
    return lines


def snapshot(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: {"owner": r.get("owner"), "type": r.get("type")} for r in rows if r.get("name")}


# ---------------------------------------------------------------- activité (opérations, énergie, parcelles)
def last_days(history: dict | None, days: int = 14) -> list[tuple[str, int]]:
    items = sorted((k, v) for k, v in (history or {}).items() if isinstance(v, (int, float)))
    return items[-days:]


def operations_fields(ops: list[dict]) -> list[tuple[str, str]]:
    fields = []
    for op in ops:
        kind = op.get("kind")
        data = op.get(kind) if kind else None
        if not isinstance(data, dict):
            continue
        if kind == "farm":
            top = sorted((data.get("top") or {}).items(), key=lambda x: -x[1])[:5]
            daily: dict[str, int] = {}
            for per_day in (data.get("history") or {}).values():
                for day, qty in (per_day or {}).items():
                    daily[day] = daily.get(day, 0) + (qty or 0)
            days = last_days(daily)
            lines = [f"Récolté aujourd'hui : **{fmt(data.get('today_collected'))}** · total {fmt(data.get('total_collected'))}"]
            if days:
                lines.append(f"`{sparkline([v for _, v in days])}` {len(days)} derniers jours")
            if top:
                lines.append("Top : " + ", ".join(f"{cereal_name(c)} {fmt(q)}" for c, q in top))
            fields.append(("🌾 Récoltes", "\n".join(lines)))
        elif kind == "trader":
            days = last_days(data.get("history"))
            lines = [f"Rendement du jour : **{(data.get('today_bp') or 0) / 100:+.2f} %**",
                     f"Revenus générés : {fmt(data.get('total_generated'))} $"]
            if data.get("total_investment") is not None:
                lines.append(f"Investi : {fmt(data.get('total_investment'))} $ par {data.get('investors_count') or 0} investisseur(s)")
            if days:
                lines.append(f"`{sparkline([v for _, v in days])}` rendement des {len(days)} derniers jours")
            fields.append(("📈 Trading", "\n".join(lines)))
        elif kind == "bet":
            bets = [b for b in data.get("bets") or [] if isinstance(b, dict)]
            open_bets = [b for b in bets if b.get("status") == "open"]
            lines = [f"{len(open_bets)} pari(s) ouvert(s) sur {len(bets)}"]
            for b in open_bets[:3]:
                lines.append(f"• {b.get('title')} : {b.get('option1')} / {b.get('option2')} ({b.get('bettors_count', 0)} parieurs)")
            fields.append(("🎲 Paris", "\n".join(lines)))
    return fields


def energy_fields(rows: list[dict]) -> list[tuple[str, str]]:
    fields = []
    for e in rows:
        label = "⚡ Électricité" if e.get("kind") == "electricity" else "🛢️ Pétrole"
        gen, sold = last_days(e.get("history_generated"), 7), last_days(e.get("history_collected"), 7)
        lines = [f"Prix de base : {fmt(e.get('base_price'))} $ · production totale {fmt(e.get('total_generated'))}"]
        if gen:
            lines.append(f"`{sparkline([v for _, v in gen])}` produit sur 7 jours : {fmt(sum(v for _, v in gen))}")
        if sold:
            lines.append(f"Vendu sur 7 jours : {fmt(sum(v for _, v in sold))}")
        fields.append((label, "\n".join(lines)))
    return fields


def parcels_field(rows: list[dict]) -> tuple[str, str] | None:
    if not rows:
        return None
    counts: dict[str, int] = {}
    for p in rows:
        counts[p.get("status") or "?"] = counts.get(p.get("status") or "?", 0) + 1
    lines = [" · ".join(f"{n} {PARCEL_STATUS.get(s, s)}" for s, n in counts.items())]
    for p in [p for p in rows if p.get("status") in ("to_rent", "to_sell")][:4]:
        unit = "/jour" if p.get("price_unit") == "per_day" else ""
        lines.append(f"• {p.get('parcel')} : {PARCEL_STATUS.get(p.get('status'))} {fmt(p.get('price'))} ${unit}")
    return (f"🏠 Parcelles ({len(rows)})", "\n".join(lines))
