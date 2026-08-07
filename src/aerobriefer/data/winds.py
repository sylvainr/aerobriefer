"""Vent en altitude (« winds aloft ») via Open-Meteo, pour le log de navigation.

Open-Meteo publie le vent par NIVEAU DE PRESSION (1000/925/850/700 hPa) avec la
hauteur géopotentielle de chaque niveau : on interpole à l'altitude planifiée de
chaque branche. Gratuit, sans clé — c'est l'équivalent numérique exploitable du
WINTEM (qui, lui, n'est qu'une image).

Deux morceaux nettement séparés :
- `interpolate_wind` : PUR (aucune I/O). Prend le bloc horaire d'Open-Meteo, une
  altitude et un instant, rend un `Wind`. Interpolation VECTORIELLE (composantes)
  pour éviter le saut de direction autour de 360°. C'est là qu'est la logique,
  testable sans réseau.
- `WindsAloft` : la collecte (fetch + cache disque par point et par jour).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import httpx

from ..domain.geo import Position
from ..domain.navlog import Wind
from ..domain.window import UtcDateTime

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
LEVELS_HPA = (1000, 925, 850, 700)
_M_PER_FT = 0.3048
ENV_DIR = "AEROBRIEFER_WINDS_DIR"
_DEFAULT_DIR = Path(".cache/winds")


def _hour_key(when: UtcDateTime) -> str:
    """Clé d'échéance Open-Meteo (heure pleine UTC, sans offset)."""
    return f"{when:%Y-%m-%dT%H:00}"


def interpolate_wind(hourly: dict[str, Any], altitude_ft: float, when: UtcDateTime) -> Wind:
    """Vent interpolé à `altitude_ft` et à l'heure pleine la plus proche de `when`.

    `hourly` est le bloc `hourly` d'Open-Meteo (listes parallèles indexées par
    `time`). On interpole entre les deux niveaux de pression qui encadrent
    l'altitude (par hauteur géopotentielle) ; hors de la plage des niveaux, on
    prend le niveau extrême sans extrapoler.
    """
    times = hourly["time"]
    index = _nearest_hour_index(times, when)

    altitude_m = altitude_ft * _M_PER_FT
    samples = _levels_at(hourly, index)  # (height_m, from_deg, speed_kt), triés par hauteur
    if not samples:
        raise ValueError("aucun niveau de vent exploitable dans la réponse")

    if altitude_m <= samples[0][0]:
        return _wind(samples[0][1], samples[0][2])
    if altitude_m >= samples[-1][0]:
        return _wind(samples[-1][1], samples[-1][2])

    for (h0, d0, s0), (h1, d1, s1) in zip(samples, samples[1:], strict=False):
        if h0 <= altitude_m <= h1:
            frac = (altitude_m - h0) / (h1 - h0)
            fx = _lerp(_vx(d0, s0), _vx(d1, s1), frac)
            fy = _lerp(_vy(d0, s0), _vy(d1, s1), frac)
            speed = math.hypot(fx, fy)
            from_deg = math.degrees(math.atan2(fx, fy)) % 360.0
            return _wind(from_deg, speed)
    # Ne devrait pas arriver (encadrement garanti par les bornes ci-dessus).
    return _wind(samples[-1][1], samples[-1][2])


def _levels_at(hourly: dict[str, Any], index: int) -> list[tuple[float, float, float]]:
    samples: list[tuple[float, float, float]] = []
    for level in LEVELS_HPA:
        height = hourly.get(f"geopotential_height_{level}hPa")
        speed = hourly.get(f"wind_speed_{level}hPa")
        direction = hourly.get(f"wind_direction_{level}hPa")
        if not (height and speed and direction):
            continue
        h, s, d = height[index], speed[index], direction[index]
        if h is None or s is None or d is None:
            continue
        samples.append((float(h), float(d), float(s)))
    samples.sort(key=lambda triple: triple[0])
    return samples


def _nearest_hour_index(times: list[str], when: UtcDateTime) -> int:
    target = _hour_key(when)
    if target in times:
        return times.index(target)
    # À défaut d'échéance exacte, la plus proche par comparaison lexicographique
    # d'ISO (ordre chronologique) — suffisant, les pas sont horaires.
    return min(range(len(times)), key=lambda i: abs(_iso_minutes(times[i]) - _iso_minutes(target)))


def _iso_minutes(iso: str) -> int:
    """Minutes depuis une origine arbitraire, pour comparer deux ISO horaires."""
    date, _, clock = iso.partition("T")
    year, month, day = (int(p) for p in date.split("-"))
    hour = int(clock[:2]) if clock else 0
    return ((year * 12 + month) * 31 + day) * 24 * 60 + hour * 60


def _lerp(a: float, b: float, frac: float) -> float:
    return a + (b - a) * frac


def _vx(from_deg: float, speed: float) -> float:
    return speed * math.sin(math.radians(from_deg))


def _vy(from_deg: float, speed: float) -> float:
    return speed * math.cos(math.radians(from_deg))


def _wind(from_deg: float, speed_kt: float) -> Wind:
    return Wind(from_deg=round(from_deg % 360.0, 1), speed_kt=round(speed_kt, 1))


class WindsAloft:
    """Collecte du vent en altitude, avec cache disque par point et par jour.

    Le cache évite de refrapper Open-Meteo pour chaque branche d'une même nav (les
    points d'une route partagent souvent la même case de 0,25°). Répertoire de
    cache surchageable par la variable d'environnement `AEROBRIEFER_WINDS_DIR`
    (les tests y pointent une fixture, sans réseau).
    """

    def __init__(
        self, *, cache_dir: Path | None = None, client: httpx.Client | None = None
    ) -> None:
        self._dir = cache_dir or Path(os.environ.get(ENV_DIR, _DEFAULT_DIR))
        self._client = client
        self._memory: dict[str, dict[str, Any]] = {}

    def wind_at(self, position: Position, altitude_ft: float, when: UtcDateTime) -> Wind:
        hourly = self._hourly(position, when)
        return interpolate_wind(hourly, altitude_ft, when)

    def _hourly(self, position: Position, when: UtcDateTime) -> dict[str, Any]:
        key = f"{position.lat:.2f}_{position.lon:.2f}_{when:%Y-%m-%d}"
        if key in self._memory:
            return self._memory[key]
        path = self._dir / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = self._fetch(position, when)
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise ValueError("réponse Open-Meteo sans bloc 'hourly'")
        self._memory[key] = hourly
        return hourly

    def _fetch(self, position: Position, when: UtcDateTime) -> dict[str, Any]:
        fields = [
            f"{metric}_{level}hPa"
            for level in LEVELS_HPA
            for metric in ("wind_speed", "wind_direction", "geopotential_height")
        ]
        params = {
            "latitude": f"{position.lat:.4f}",
            "longitude": f"{position.lon:.4f}",
            "hourly": ",".join(fields),
            "wind_speed_unit": "kn",
            "start_date": f"{when:%Y-%m-%d}",
            "end_date": f"{when:%Y-%m-%d}",
            "timezone": "UTC",
        }
        client = self._client
        if client is not None:
            response = client.get(ENDPOINT, params=params, timeout=15.0)
        else:
            with httpx.Client(timeout=15.0) as owned:
                response = owned.get(ENDPOINT, params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload


class SurfaceWinds:
    """Vent de SURFACE (10 m) prévu à un point, via Open-Meteo — pour la piste
    favorable et le vent traversier/face au terrain de déroutement.

    Même mécanique de cache que `WindsAloft`, requête distincte (champs 10 m)."""

    def __init__(
        self, *, cache_dir: Path | None = None, client: httpx.Client | None = None
    ) -> None:
        self._dir = cache_dir or Path(os.environ.get(ENV_DIR, _DEFAULT_DIR))
        self._client = client
        self._memory: dict[str, dict[str, Any]] = {}

    def wind_at(self, position: Position, when: UtcDateTime) -> Wind | None:
        """Vent de surface à l'heure pleine la plus proche, ou None si indisponible."""
        hourly = self._hourly(position, when)
        times = hourly.get("time") or []
        if not times:
            return None
        index = _nearest_hour_index(times, when)
        speeds = hourly.get("wind_speed_10m") or []
        directions = hourly.get("wind_direction_10m") or []
        if index >= len(speeds) or speeds[index] is None or directions[index] is None:
            return None
        return Wind(
            from_deg=round(float(directions[index]) % 360.0, 1),
            speed_kt=round(float(speeds[index]), 1),
        )

    def _hourly(self, position: Position, when: UtcDateTime) -> dict[str, Any]:
        key = f"sfc_{position.lat:.2f}_{position.lon:.2f}_{when:%Y-%m-%d}"
        if key in self._memory:
            return self._memory[key]
        path = self._dir / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = self._fetch(position, when)
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise ValueError("réponse Open-Meteo sans bloc 'hourly'")
        self._memory[key] = hourly
        return hourly

    def _fetch(self, position: Position, when: UtcDateTime) -> dict[str, Any]:
        params = {
            "latitude": f"{position.lat:.4f}",
            "longitude": f"{position.lon:.4f}",
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kn",
            "start_date": f"{when:%Y-%m-%d}",
            "end_date": f"{when:%Y-%m-%d}",
            "timezone": "UTC",
        }
        client = self._client
        if client is not None:
            response = client.get(ENDPOINT, params=params, timeout=15.0)
        else:
            with httpx.Client(timeout=15.0) as owned:
                response = owned.get(ENDPOINT, params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload
