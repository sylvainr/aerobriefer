"""Espaces aériens français — téléchargés, mis en cache, filtrés par géométrie.

Comme les aérodromes (cf. `refdata`), c'est de la donnée de RÉFÉRENCE : on la
télécharge une fois depuis la source et on la met en cache localement, jamais
committée.

Source : `france.geojson` de **planeur-net/airspace** (GitHub) — dérivé de l'AIP
France, 1608 espaces, tous des polygones déjà tessellés (aucun arc à gérer), avec
classe + type + plancher/plafond structurés. Licence non déclarée par le dépôt →
usage personnel ; ne pas redistribuer le fichier brut (d'où le cache gitignoré).

Schéma d'une feature (vérifié) :
    properties: {name, class, type,
                 lowerCeiling: {value, unit, referenceDatum},
                 upperCeiling: {...}, frequency?: {value, name}}
    geometry:   Polygon (coordinates[0] = anneau [lon, lat])
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

import httpx

from ..domain.geo import Geometry, Position
from ..domain.models import Airspace, AltitudeLimit

SOURCE_URL = "https://raw.githubusercontent.com/planeur-net/airspace/master/france.geojson"

ENV_DIR = "AEROBRIEFER_AIRSPACE_DIR"
_DEFAULT_DIR = Path(".cache/airspace")
_FILENAME = "france.geojson"

_TIMEOUT = httpx.Timeout(120.0)
_UA = {"User-Agent": "aerobriefer/0.1 (reference data, personal use)"}


def _data_dir() -> Path:
    override = os.environ.get(ENV_DIR)
    return Path(override) if override else _DEFAULT_DIR


def _geojson_path() -> Path:
    directory = _data_dir()
    path = directory / _FILENAME
    if path.exists():
        return path
    if os.environ.get(ENV_DIR):
        raise FileNotFoundError(
            f"{ENV_DIR}={directory} : {_FILENAME} absent. En mode override, la "
            "fixture doit être pré-remplie (aucun téléchargement)."
        )
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=_TIMEOUT, headers=_UA, follow_redirects=True) as client:
        response = client.get(SOURCE_URL)
        response.raise_for_status()
        path.write_bytes(response.content)
    return path


@lru_cache(maxsize=1)
def _all_airspaces() -> tuple[Airspace, ...]:
    data = json.loads(_geojson_path().read_text(encoding="utf-8"))
    out: list[Airspace] = []
    for feature in data.get("features", []):
        airspace = _parse_feature(feature)
        if airspace is not None:
            out.append(airspace)
    return tuple(out)


def _parse_feature(feature: dict) -> Airspace | None:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "Polygon":
        return None  # MVP : on ne gère que les polygones simples (tous le sont)
    rings = geometry.get("coordinates") or []
    if not rings:
        return None
    try:
        polygon = tuple(Position(pt[1], pt[0]) for pt in rings[0])
    except (IndexError, TypeError, ValueError):
        return None

    props = feature.get("properties") or {}
    lower = _limit(props.get("lowerCeiling"))
    upper = _limit(props.get("upperCeiling"))
    if lower is None or upper is None:
        return None

    freq = props.get("frequency") or {}
    frequency = freq.get("value") if isinstance(freq, dict) else None

    return Airspace(
        name=str(props.get("name") or "?"),
        airspace_class=str(props.get("class") or "UNC"),
        airspace_type=str(props.get("type") or "?"),
        polygon=polygon,
        lower=lower,
        upper=upper,
        frequency=str(frequency) if frequency else None,
    )


def _limit(raw: dict | None) -> AltitudeLimit | None:
    if not isinstance(raw, dict):
        return None
    try:
        return AltitudeLimit(
            value=float(raw["value"]),
            unit=str(raw.get("unit") or "FT"),
            reference=str(raw.get("referenceDatum") or "MSL"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def intersecting(geometry: Geometry) -> list[Airspace]:
    """Espaces dont l'empreinte touche la géométrie du vol, triés par plancher."""
    hits = [a for a in _all_airspaces() if a.intersects(geometry)]
    hits.sort(key=lambda a: a.lower.feet_amsl)
    return hits
