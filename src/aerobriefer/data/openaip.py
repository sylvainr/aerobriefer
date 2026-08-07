"""Points de report VFR via OpenAIP (api.core.openaip.net).

Source de RÉFÉRENCE, bien plus complète que `vrp_france` (couvre les petits
terrains, ex. LFCY). Nécessite une clé API gratuite dans l'env `OPENAIP_KEY`
(compte gratuit sur openaip.net). Requête HTTP + cache disque (jamais committé).

⚠️ Données © contributeurs OpenAIP (licence ODbL) → usage personnel ; on ne
redistribue pas le brut et on VÉRIFIE toujours sur la carte VAC officielle.

Auth : header `x-openaip-api-key`. Les points de report ont un `name` court
(« S », « N », « SW »…), une géométrie GeoJSON (coordonnées [lon, lat]) et un
drapeau `compulsory` (point obligatoire). L'appartenance à un terrain n'est PAS
un code OACI dans la donnée : on requête AUTOUR d'une position connue (celle de
l'aérodrome) et c'est l'appelant qui attribue l'OACI.
"""

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


def _fetch_near(lat: float, lon: float, dist_m: int) -> list[dict]:
    key = os.environ[ENV_KEY]
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(
            f"{_BASE}/reporting-points",
            params={"pos": f"{lat},{lon}", "dist": dist_m, "limit": 1000},
            headers={"x-openaip-api-key": key},
        )
        response.raise_for_status()
        data = response.json()
    items = data.get("items") if isinstance(data, dict) else data
    return list(items) if isinstance(items, list) else []


def reporting_points_near(position: Position, *, dist_m: int = 15000) -> list[OpenAipPoint]:
    """Points de report OpenAIP dans un rayon (mètres) autour d'une position.

    Mis en cache sur disque (clé = position arrondie + rayon) : une position donnée
    n'interroge le service qu'une fois."""
    lat = round(position.lat, 2)
    lon = round(position.lon, 2)
    cache = _cache_dir()
    path = cache / f"near_{lat}_{lon}_{dist_m}.json"
    if path.exists():
        items = json.loads(path.read_text(encoding="utf-8"))
    else:
        items = _fetch_near(position.lat, position.lon, dist_m)
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items), encoding="utf-8")
    return [p for p in map(_to_point, items) if p is not None]


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
