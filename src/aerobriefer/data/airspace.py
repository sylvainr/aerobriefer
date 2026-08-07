"""Espaces aériens français — téléchargés, mis en cache, filtrés par géométrie.

Comme les aérodromes (cf. `refdata`), c'est de la donnée de RÉFÉRENCE : on la
télécharge une fois depuis la source et on la met en cache localement, jamais
committée.

Deux sources, dans l'ordre de préférence :

1. **OpenAIP** (si une clé `OPENAIP_KEY` est configurée) — licence ODbL claire,
   complet et à jour, interrogé par proximité. Les champs sont des ENUMS
   numériques (type/classe/unité/référence) qu'on mappe ici vers le modèle
   domaine (mapping vérifié empiriquement sur la région, cf. `_OA_*`).
2. **planeur-net/airspace** `france.geojson` (repli) — dérivé de l'AIP France,
   polygones tessellés, licence non déclarée → usage personnel, ne pas
   redistribuer le brut (cache gitignoré).

Schéma planeur-net (repli) :
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
    """Espaces dont l'empreinte touche la géométrie du vol, triés par plancher.

    Source privilégiée : OpenAIP (si clé configurée), sinon repli planeur-net."""
    if _openaip_available():
        hits = _openaip_intersecting(geometry)
        if hits:
            return hits
    hits = [a for a in _all_airspaces() if a.intersects(geometry)]
    hits.sort(key=lambda a: a.lower.feet_amsl)
    return hits


# --- Source OpenAIP (préférée quand une clé est configurée) -----------------
#
# Enums OpenAIP → modèle domaine, VÉRIFIÉS sur la région (SW France) :
#   type    1=R 2=D 3=P 4=CTR 7=TMA 10=FIR 21=GSEC 26=CTA 33=SIV …
#   icaoClass 0..6 = A..G ; 8 = non classé (R/D/P/SIV/planeur) → « UNC »
#   unit    1=FT 6=FL ; referenceDatum 0=GND 1=MSL 2=STD

_OA_TYPE = {
    0: "OTHER",
    1: "R",
    2: "D",
    3: "P",
    4: "CTR",
    5: "TMZ",
    6: "RMZ",
    7: "TMA",
    8: "TRA",
    9: "TSA",
    10: "FIR",
    11: "UIR",
    12: "ADIZ",
    13: "ATZ",
    14: "MATZ",
    15: "AWY",
    16: "MTR",
    17: "ALERT",
    18: "WARNING",
    19: "PROTECTED",
    20: "HTZ",
    21: "GSEC",
    22: "TRP",
    23: "TIZ",
    24: "TIA",
    25: "MTA",
    26: "CTA",
    27: "ACC",
    28: "SPORT",
    29: "LAOR",
    30: "MRT",
    31: "TFR",
    32: "VFR",
    33: "SIV",
    34: "LTA",
    35: "UTA",
}
_OA_CLASS = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G"}
_OA_UNIT = {1: "FT", 6: "FL"}
_OA_REF = {0: "GND", 1: "MSL", 2: "STD"}


def _enum(mapping: dict[int, str], raw: object, default: str) -> str:
    """Enum numérique OpenAIP → libellé, robuste (valeur absente/non entière)."""
    return mapping.get(raw, default) if isinstance(raw, int) else default


def _openaip_available() -> bool:
    from . import openaip

    return openaip.available()


def _openaip_intersecting(geometry: Geometry) -> list[Airspace]:
    from . import openaip

    circle = geometry.bounding_circle()
    dist_m = int(circle.radius_nm * 1852)
    features = openaip.airspaces_near(circle.center, dist_m=dist_m)
    spaces = [s for s in map(_from_openaip, features) if s is not None and s.intersects(geometry)]
    spaces.sort(key=lambda a: a.lower.feet_amsl)
    return spaces


def _from_openaip(feature: dict) -> Airspace | None:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "Polygon":
        return None
    rings = geometry.get("coordinates") or []
    if not rings:
        return None
    try:
        polygon = tuple(Position(pt[1], pt[0]) for pt in rings[0])
    except (IndexError, TypeError, ValueError):
        return None

    lower = _oa_limit(feature.get("lowerLimit"))
    upper = _oa_limit(feature.get("upperLimit"))
    if lower is None or upper is None:
        return None

    return Airspace(
        name=str(feature.get("name") or "?"),
        airspace_class=_enum(_OA_CLASS, feature.get("icaoClass"), "UNC"),
        airspace_type=_enum(_OA_TYPE, feature.get("type"), "?"),
        polygon=polygon,
        lower=lower,
        upper=upper,
        frequency=_oa_frequency(feature.get("frequencies")),
    )


def _oa_frequency(freqs: object) -> str | None:
    """Fréquence de contrôle de l'espace : la primaire, sinon la première."""
    if not isinstance(freqs, list) or not freqs:
        return None
    primary = next((f for f in freqs if isinstance(f, dict) and f.get("primary")), None)
    chosen = primary or freqs[0]
    value = chosen.get("value") if isinstance(chosen, dict) else None
    return str(value) if value else None


def _oa_limit(raw: dict | None) -> AltitudeLimit | None:
    if not isinstance(raw, dict):
        return None
    try:
        return AltitudeLimit(
            value=float(raw["value"]),
            unit=_enum(_OA_UNIT, raw.get("unit"), "FT"),
            reference=_enum(_OA_REF, raw.get("referenceDatum"), "MSL"),
        )
    except (KeyError, TypeError, ValueError):
        return None
