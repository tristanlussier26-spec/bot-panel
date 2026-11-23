"""Logique pure de la synchro des grades NationsGlory -> rôles Discord (testable sans Discord)."""
from __future__ import annotations

from dataclasses import dataclass, field

RANKS = ("LEADER", "OFFICER", "MEMBER", "RECRUIT")  # du plus haut au plus bas
RANK_LABELS = {"LEADER": "Chef", "OFFICER": "Officier", "MEMBER": "Membre", "RECRUIT": "Recrue"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class SyncSettings:
    rank_roles: dict[str, int]
    country_role_id: int = 0
    outside_role_id: int = 0
    cumulative: bool = False

    @property
    def managed_roles(self) -> set[int]:
        return {r for r in (*self.rank_roles.values(), self.country_role_id, self.outside_role_id) if r}


@dataclass
class SyncPlan:
    add_roles: set[int] = field(default_factory=set)
    remove_roles: set[int] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not (self.add_roles or self.remove_roles)


def parse_settings(cfg: dict) -> tuple[SyncSettings, list[str]]:
    """Lit les réglages « ng » d'un serveur. Retourne (réglages, avertissements)."""
    warnings: list[str] = []
    rank_roles: dict[str, int] = {}
    for key, value in cfg.get("grades", {}).items():
        rank = str(key).strip().upper()
        if rank not in RANKS:
            raise ConfigError(f"grade « {key} » inconnu, utilise {', '.join(RANKS)}")
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"grade {rank} : l'ID du rôle doit être un nombre") from None
        if role_id:
            rank_roles[rank] = role_id
        else:
            warnings.append(f"aucun rôle Discord pour le grade {RANK_LABELS[rank]}")

    country_role_id = int(cfg.get("role_pays_id", 0))
    outside_role_id = int(cfg.get("role_hors_pays_id", 0))
    rank_ids = set(rank_roles.values())
    if country_role_id and country_role_id in rank_ids:
        raise ConfigError("le rôle « membres du pays » est déjà utilisé pour un grade")
    if outside_role_id and (outside_role_id in rank_ids or outside_role_id == country_role_id):
        raise ConfigError("le rôle « hors du pays » doit être différent des autres")

    settings = SyncSettings(rank_roles, country_role_id, outside_role_id, bool(cfg.get("roles_cumulatifs", False)))
    return settings, warnings


def plan_sync(member_role_ids: set[int], in_country: bool, rank: str | None, s: SyncSettings) -> SyncPlan:
    """Rôles à ajouter/retirer pour qu'un membre lié corresponde à son grade NationsGlory."""
    wanted: set[int] = set()
    if in_country:
        if s.country_role_id:
            wanted.add(s.country_role_id)
        if rank in RANKS:
            ranks = RANKS[RANKS.index(rank):] if s.cumulative else (rank,)
            wanted |= {s.rank_roles[r] for r in ranks if r in s.rank_roles}
    elif s.outside_role_id:
        wanted.add(s.outside_role_id)
    return SyncPlan(
        add_roles=wanted - member_role_ids,
        remove_roles=(s.managed_roles & member_role_ids) - wanted,
    )


def plan_unlink(member_role_ids: set[int], s: SyncSettings) -> SyncPlan:
    """Compte délié : on retire tous les rôles gérés par la synchro."""
    return SyncPlan(remove_roles=s.managed_roles & member_role_ids)


# ---- Options de texte (format du surnom, message d'arrivée) ----------------------------------
class _Keep(dict):
    """Laisse les variables inconnues telles quelles ({truc} reste {truc}) au lieu de planter."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fill(template: str, **values: str) -> str:
    try:
        return str(template).format_map(_Keep(values))
    except (ValueError, IndexError, AttributeError):  # accolade mal fermée, {0}, {a.b} : on garde le texte tel quel
        return str(template)


def format_nick(template: str, pseudo: str, grade: str | None, country: str) -> str:
    """Le surnom Discord d'un membre lié. Juste le pseudo s'il est hors du pays ou si ça dépasse 32 caractères."""
    if grade is None or not template or template.strip() == "{pseudo}":
        return pseudo
    nick = fill(template, pseudo=pseudo, grade=grade, pays=country).strip()
    if pseudo not in nick or not 1 <= len(nick) <= 32:
        return pseudo
    return nick
