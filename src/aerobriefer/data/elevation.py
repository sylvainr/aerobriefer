"""Élévation du terrain (Open-Meteo, données SRTM), pour la Zmin du navlog.

Zmin = altitude de sécurité d'une branche = relief le plus haut du couloir + une
marge (1000 ft en plaine). On échantillonne des points le long de la branche, on
prend l'élévation max, on ajoute la marge et on arrondit au-dessus.

⚠️ TERRAIN seulement : l'API ne connaît pas les obstacles ponctuels (antennes,
pylônes). La Zmin calculée est un plancher indicatif — les obstacles NOTAM/VAC
restent à vérifier.

Partie PURE (échantillonnage, marge, arrondi) séparée de la collecte (fetch+cache).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import httpx

from ..domain.geo import Position

ENDPOINT = "https://api.open-meteo.com/v1/elevation"
ENV_DIR = "AEROBRIEFER_ELEVATION_DIR"
_DEFAULT_DIR = Path(".cache/elevation")
_M_TO_FT = 3.280839895
DEFAULT_MARGIN_FT = 1000.0


def sample_positions(start: Position, end: Position, *, spacing_nm: float = 5.0) -> list[Position]:
    """Points le long de la branche (extrémités comprises), ~un tous les `spacing_nm`."""
    distance = start.distance_nm(end)
    steps = max(2, int(distance / spacing_nm) + 1)
    return [
        Position(
            start.lat + (end.lat - start.lat) * i / steps,
            start.lon + (end.lon - start.lon) * i / steps,
        )
        for i in range(steps + 1)
    ]


def zmin_ft(max_terrain_m: float, *, margin_ft: float = DEFAULT_MARGIN_FT) -> float:
    """Altitude de sécurité : relief max (m) → pieds + marge, arrondi aux 100 ft sup."""
    raw = max_terrain_m * _M_TO_FT + margin_ft
    return math.ceil(raw / 100.0) * 100.0


class Elevation:
    """Élévation du terrain, batchée et mise en cache par point.

    Une requête regroupe tous les points demandés (l'API accepte des listes) ; le
    cache disque (`AEROBRIEFER_ELEVATION_DIR`) évite de les redemander."""

    def __init__(
        self, *, cache_dir: Path | None = None, client: httpx.Client | None = None
    ) -> None:
        self._dir = cache_dir or Path(os.environ.get(ENV_DIR, _DEFAULT_DIR))
        self._client = client
        self._path = self._dir / "elevation.json"
        self._cache: dict[str, float] = {}
        if self._path.exists():
            self._cache = json.loads(self._path.read_text(encoding="utf-8"))

    def elevation_m(self, points: list[Position]) -> list[float]:
        missing = [p for p in points if _key(p) not in self._cache]
        if missing:
            self._fetch(missing)
        return [self._cache.get(_key(p), 0.0) for p in points]

    def max_terrain_m(self, points: list[Position]) -> float:
        elevations = self.elevation_m(points)
        return max(elevations) if elevations else 0.0

    def _fetch(self, points: list[Position]) -> None:
        params = {
            "latitude": ",".join(f"{p.lat:.4f}" for p in points),
            "longitude": ",".join(f"{p.lon:.4f}" for p in points),
        }
        client = self._client
        if client is not None:
            response = client.get(ENDPOINT, params=params, timeout=15.0)
        else:
            with httpx.Client(timeout=15.0) as owned:
                response = owned.get(ENDPOINT, params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        elevations = payload.get("elevation") or []
        for point, elevation in zip(points, elevations, strict=False):
            if elevation is not None:
                self._cache[_key(point)] = float(elevation)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache), encoding="utf-8")


def _key(point: Position) -> str:
    return f"{point.lat:.3f},{point.lon:.3f}"
