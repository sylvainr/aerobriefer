"""Client OpenAIP (api.core.openaip.net) : points de report VFR ET espaces aériens.

Source de RÉFÉRENCE, bien plus complète que vrp_france / planeur-net (couvre les
petits terrains — ex. LFCY — et les espaces sous licence claire). Nécessite une
clé API gratuite dans l'env `OPENAIP_KEY` (compte gratuit sur openaip.net).
Requête HTTP + cache disque (jamais committé).

⚠️ Données © contributeurs OpenAIP (licence ODbL) → usage personnel ; on ne
redistribue pas le brut et on VÉRIFIE toujours sur la carte VAC officielle.

Auth : header `x-openaip-api-key`. On interroge par proximité (`pos` + `dist` en
mètres). Les points de report ont un `name` court (« S », « N »…) ; les espaces
aériens ont des enums numériques (type/classe/unité) qu'on mappe côté appelant.
L'appartenance d'un point à un terrain n'est PAS un code OACI dans la donnée : on
requête AUTOUR d'une position connue (celle de l'aérodrome) et l'appelant attribue
l'OACI. Les fonctions `*_near` renvoient les features BRUTES (dict) mises en cache."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..domain.geo import Position

ENV_KEY = "OPENAIP_KEY"
ENV_DIR = "AEROBRIEFER_OPENAIP_DIR"
_BASE = "https://api.core.openaip.net/api"
_DEFAULT_DIR = Path(".cache/openaip")
_TIMEOUT = httpx.Timeout(30.0)


@dataclass(frozen=True, slots=True)
class OpenAipPoint:
    ident: str  # code court du point, ex. « S », « N », « SW »
    name: str  # libellé (souvent = ident)
    position: Position
    compulsory: bool  # point de report OBLIGATOIRE


def available() -> bool:
    """Vrai si une clé OpenAIP est configurée (sinon on retombe sur vrp_france)."""
    return bool(os.environ.get(ENV_KEY))


def _cache_dir() -> Path:
    return Path(os.environ.get(ENV_DIR, _DEFAULT_DIR))


def _fetch(resource: str, lat: float, lon: float, dist_m: int) -> list[dict]:
    key = os.environ[ENV_KEY]
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(
            f"{_BASE}/{resource}",
            params={"pos": f"{lat},{lon}", "dist": dist_m, "limit": 1000},
            headers={"x-openaip-api-key": key},
        )
        response.raise_for_status()
        data = response.json()
    items = data.get("items") if isinstance(data, dict) else data
    return list(items) if isinstance(items, list) else []


def _cached_near(resource: str, position: Position, dist_m: int) -> list[dict]:
    """Features brutes d'un endpoint OpenAIP dans un rayon (m), cache disque.

    Clé de cache = ressource + position arrondie (2 décimales) + rayon : une même
    zone n'interroge le service qu'une fois."""
    lat = round(position.lat, 2)
    lon = round(position.lon, 2)
    cache = _cache_dir()
    path = cache / f"{resource}_{lat}_{lon}_{dist_m}.json"
    if path.exists():
        return list(json.loads(path.read_text(encoding="utf-8")))
    items = _fetch(resource, position.lat, position.lon, dist_m)
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")
    return items


def reporting_points_near(position: Position, *, dist_m: int = 15000) -> list[OpenAipPoint]:
    """Points de report OpenAIP dans un rayon (mètres) autour d'une position."""
    items = _cached_near("reporting-points", position, dist_m)
    return [p for p in map(_to_point, items) if p is not None]


def airspaces_near(position: Position, *, dist_m: int) -> list[dict]:
    """Espaces aériens OpenAIP (features BRUTES) dans un rayon (mètres).

    Le mapping enums → modèle domaine est fait par l'appelant (`data.airspace`),
    au plus près du modèle `Airspace`."""
    return _cached_near("airspaces", position, dist_m)


def _to_point(item: dict) -> OpenAipPoint | None:
    try:
        lon, lat = item["geometry"]["coordinates"][:2]
        name = str(item["name"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if not name:
        return None
    return OpenAipPoint(
        ident=name.upper(),
        name=name,
        position=Position(float(lat), float(lon)),
        compulsory=bool(item.get("compulsory")),
    )
