"""Réglages par serveur Discord : types de champs, valeurs par défaut et validation.

Les pages viennent des plugins (plugin.json). Ordre de priorité : valeurs par défaut < valeurs enregistrées.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Field:
    key: str
    label: str
    type: str  # bool, int, text, textarea, channel, category, role, roles, channels, lines, select, role_map, level_roles
    default: Any = None
    help: str = ""
    min: int | None = None
    max: int | None = None
    choices: list[tuple[str, str]] = field(default_factory=list)
    keys: list[tuple[str, str]] = field(default_factory=list)  # role_map : [(clé, libellé), ...]


@dataclass
class Page:
    id: str
    title: str
    icon: str
    section: str  # où les valeurs sont gardées (une section par plugin)
    description: str
    fields: list[Field]


PAGES: list[Page] = []  # le bot de base a pas de réglages : ce sont les plugins qui ajoutent leurs pages


def defaults() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for page in PAGES:
        section = result.setdefault(page.section, {})
        for f in page.fields:
            section[f.key] = copy.deepcopy(f.default)
    return result


def merge(base: dict, override: dict) -> dict:
    """Fusionne section par section ; la valeur d'un champ (même une liste ou un dict) se remplace d'un bloc."""
    out = copy.deepcopy(base)
    for section, values in override.items():
        if isinstance(values, dict) and isinstance(out.get(section), dict):
            out[section].update(copy.deepcopy(values))
        else:
            out[section] = copy.deepcopy(values)
    return out


def build_config(saved: dict[str, dict]) -> dict:
    """Valeurs par défaut des pages < valeurs enregistrées."""
    return merge(defaults(), saved)


class ValidationError(ValueError):
    pass


def _as_id(value: Any) -> int:
    if value in (None, "", "0", 0):
        return 0
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValidationError("ID invalide") from None
    if number < 0:
        raise ValidationError("ID invalide")
    return number


def coerce(f: Field, value: Any) -> Any:
    """Convertit et vérifie une valeur reçue du panel."""
    t = f.type
    if t == "bool":
        return bool(value)
    if t == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"« {f.label} » doit être un nombre") from None
        if (f.min is not None and number < f.min) or (f.max is not None and number > f.max):
            raise ValidationError(f"« {f.label} » doit être entre {f.min} et {f.max}")
        return number
    if t in ("text", "textarea"):
        text = str(value or "")
        if len(text) > (200 if t == "text" else 2000):
            raise ValidationError(f"« {f.label} » est trop long")
        return text
    if t in ("channel", "category", "role"):
        return _as_id(value)
    if t in ("channels", "roles"):
        if not isinstance(value, list):
            raise ValidationError(f"« {f.label} » : liste attendue")
        return [i for i in dict.fromkeys(_as_id(v) for v in value) if i]
    if t == "lines":
        lines = value if isinstance(value, list) else str(value or "").splitlines()
        return [str(x).strip()[:100] for x in lines if str(x).strip()][:200]
    if t == "select":
        if value not in [c[0] for c in f.choices]:
            raise ValidationError(f"« {f.label} » : choix invalide")
        return value
    if t == "role_map":
        if not isinstance(value, dict):
            raise ValidationError(f"« {f.label} » : format invalide")
        return {k: _as_id(value.get(k, 0)) for k, _ in f.keys}
    if t == "level_roles":
        if not isinstance(value, dict):
            raise ValidationError("Récompenses : format invalide")
        out = {}
        for level, role in value.items():
            try:
                lvl = int(level)
            except ValueError:
                raise ValidationError("Récompenses : niveau invalide") from None
            if not 1 <= lvl <= 1000:
                raise ValidationError("Récompenses : niveau entre 1 et 1000")
            if role_id := _as_id(role):
                out[str(lvl)] = role_id
        return out
    raise ValidationError(f"Type inconnu : {t}")


def validate_page(page_id: str, values: dict) -> tuple[str, dict]:
    """Retourne (section, valeurs propres) pour une page de « Options du bot »."""
    page = next((p for p in PAGES if p.id == page_id), None)
    if page is None:
        raise ValidationError("Page inconnue")
    clean = {}
    for f in page.fields:
        if f.key in values:
            clean[f.key] = coerce(f, values[f.key])
    return page.section, clean
