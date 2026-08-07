"""Points de report VFR (points de report visuels), dérivés du jeu de données
communautaire `vrp_france` (GitHub s-celles/vrp_france).

Sert à deux choses : résoudre un point dans une route (« LFBD/N » → coordonnées)
et l'afficher dans le viewer. Donnée de RÉFÉRENCE : fetch + cache, jamais
committée. ⚠️ Le jeu est marqué « for simulation purpose only » et sa licence
n'est pas déclarée → usage personnel, À VÉRIFIER sur la carte VAC officielle ; on
ne redistribue pas le fichier brut.

Format source (CSV LittleNavHap) : colonnes VRP, "OACI/IDENT", ?, lat, lon, alt,
?, ?, description, région… On garde (OACI, IDENT, description, position).
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from ..domain.geo import Position

SOURCE_URL = (
    "https://raw.githubusercontent.com/s-celles/vrp_france/master/vrp_france_littlenavmap.csv"
)
ENV_DIR = "AEROBRIEFER_REPORTING_POINTS_DIR"
_DEFAULT_DIR = Path(".cache/reporting_points")
_FILENAME = "vrp_france.csv"

_TIMEOUT = httpx.Timeout(60.0)
_UA = {"User-Agent": "aerobriefer/0.1 (reference data, personal use)"}


@dataclass(frozen=True, slots=True)
class ReportingPoint:
    icao: str
    ident: str  # code du point, ex. « N », « SW », « SA »
    name: str  # description, ex. « Bordeaux Pont d'Aquitaine »
    position: Position


def _csv_path() -> Path:
    directory = Path(os.environ.get(ENV_DIR, _DEFAULT_DIR))
    path = directory / _FILENAME
    if path.exists():
        return path
    if os.environ.get(ENV_DIR):
        raise FileNotFoundError(
            f"{ENV_DIR}={directory} : {_FILENAME} absent (fixture à pré-remplir, aucun réseau)."
        )
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=_TIMEOUT, headers=_UA, follow_redirects=True) as client:
        response = client.get(SOURCE_URL)
        response.raise_for_status()
        path.write_bytes(response.content)
    return path


def parse_rows(text: str) -> list[ReportingPoint]:
    """Parse le CSV vrp_france en points (pur, testable sans réseau)."""
    out: list[ReportingPoint] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5 or "/" not in row[1]:
            continue
        icao, _, ident = row[1].partition("/")
        try:
            position = Position(float(row[3]), float(row[4]))
        except (ValueError, IndexError):
            continue
        name = row[8] if len(row) > 8 else ""
        out.append(ReportingPoint(icao=icao, ident=ident, name=name, position=position))
    return out


@lru_cache(maxsize=1)
def _load() -> dict[tuple[str, str], ReportingPoint]:
    points = parse_rows(_csv_path().read_text(encoding="utf-8"))
    return {(p.icao, p.ident): p for p in points}


def resolve(spec: str) -> ReportingPoint | None:
    """« LFBD/N » → le point de report, ou None si inconnu.

    Source privilégiée : OpenAIP (si clé configurée), bien plus complète. Sinon
    repli sur vrp_france. OpenAIP n'attache pas d'OACI au point : on requête autour
    de la position de l'aérodrome et on garde l'OACI demandé."""
    icao, _, ident = spec.partition("/")
    icao = icao.strip().upper()
    ident = ident.strip().upper()
    if _openaip_available():
        try:
            point = _openaip_resolve(icao, ident)
        except Exception:  # noqa: BLE001 - OpenAIP indispo (429, réseau) → repli vrp_france
            point = None
        if point is not None:
            return point
    return _load().get((icao, ident))


def for_icao(icao: str) -> list[ReportingPoint]:
    icao = icao.strip().upper()
    if _openaip_available():
        try:
            points = _openaip_for_icao(icao)
        except Exception:  # noqa: BLE001 - OpenAIP indispo → repli vrp_france
            points = []
        if points:
            return points
    return [p for (i, _), p in _load().items() if i == icao]


def near(position: Position, *, within_nm: float) -> list[ReportingPoint]:
    """Points de report dans un rayon, du plus proche au plus loin."""
    if _openaip_available():
        try:
            points = _openaip_near(position, within_nm)
        except Exception:  # noqa: BLE001 - OpenAIP indispo → repli vrp_france
            points = []
        if points:
            return points
    scored = [(p, position.distance_nm(p.position)) for p in _load().values()]
    close = [p for p, d in scored if d <= within_nm]
    close.sort(key=lambda p: position.distance_nm(p.position))
    return close


# --- Source OpenAIP (préférée quand une clé est configurée) -----------------


def _openaip_available() -> bool:
    from . import openaip

    return openaip.available()


def _openaip_resolve(icao: str, ident: str) -> ReportingPoint | None:
    from . import airports, openaip

    aerodrome = airports.lookup(icao)
    if aerodrome is None:
        return None
    for point in openaip.reporting_points_near(aerodrome.position):
        if point.ident == ident:
            return ReportingPoint(
                icao=icao, ident=point.ident, name=point.name, position=point.position
            )
    return None


def _openaip_for_icao(icao: str) -> list[ReportingPoint]:
    from . import airports, openaip

    aerodrome = airports.lookup(icao)
    if aerodrome is None:
        return []
    return [
        ReportingPoint(icao=icao, ident=p.ident, name=p.name, position=p.position)
        for p in openaip.reporting_points_near(aerodrome.position)
    ]


def _openaip_near(position: Position, within_nm: float) -> list[ReportingPoint]:
    from . import airports, openaip

    dist_m = int(within_nm * 1852)
    points = openaip.reporting_points_near(position, dist_m=dist_m)
    out: list[ReportingPoint] = []
    for point in points:
        nearby = airports.nearest(point.position, within_nm=12.0, limit=1)
        icao = nearby[0][0].icao if nearby else ""
        out.append(
            ReportingPoint(icao=icao, ident=point.ident, name=point.name, position=point.position)
        )
    out.sort(key=lambda p: position.distance_nm(p.position))
    return out
