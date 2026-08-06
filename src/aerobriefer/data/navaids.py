"""Radionavigation (VOR/DME), dérivée d'OurAirports via `refdata`.

Sert au log de navigation : pour chaque point, on veut le VOR le plus proche et la
RADIALE à afficher (relèvement DEPUIS le VOR VERS le point, en magnétique) plus la
distance. C'est de la donnée de référence, lue par lookup, pas un provider.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from functools import lru_cache

from ..domain.geo import Position
from . import refdata

#: Familles qui portent une radiale exploitable (le DME seul ne donne qu'une distance).
_RADIAL_KINDS = ("VOR", "VOR-DME", "VORTAC")


@dataclass(frozen=True, slots=True)
class Navaid:
    ident: str
    name: str
    kind: str
    freq_mhz: str
    position: Position
    elevation_ft: int | None


@lru_cache(maxsize=1)
def _load() -> list[Navaid]:
    out: list[Navaid] = []
    with refdata.navaids_csv().open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                position = Position(float(row["lat"]), float(row["lon"]))
            except (ValueError, KeyError):
                continue
            elev = row.get("elev_ft") or ""
            out.append(
                Navaid(
                    ident=row["ident"],
                    name=row["name"],
                    kind=row["type"],
                    freq_mhz=row.get("freq_mhz", ""),
                    position=position,
                    elevation_ft=int(float(elev)) if elev else None,
                )
            )
    return out


def nearest(
    position: Position, *, within_nm: float = 80.0, kinds: tuple[str, ...] = _RADIAL_KINDS
) -> tuple[Navaid, float] | None:
    """VOR le plus proche (portant une radiale) et sa distance, ou None si aucun."""
    best: tuple[Navaid, float] | None = None
    for navaid in _load():
        if navaid.kind not in kinds:
            continue
        distance = position.distance_nm(navaid.position)
        if distance <= within_nm and (best is None or distance < best[1]):
            best = (navaid, distance)
    return best


def radial_and_distance(
    navaid: Navaid, point: Position, *, magnetic_variation_deg: float = 0.0
) -> tuple[float, float]:
    """Radiale (relèvement magnétique DEPUIS le VOR VERS `point`) et distance NM.

    La radiale d'un VOR est un cap magnétique : on retranche la déclinaison
    (positive vers l'Est) du relèvement vrai.
    """
    true_bearing = math.degrees(navaid.position.bearing_to(point)) % 360.0
    radial = (true_bearing - magnetic_variation_deg) % 360.0
    return radial, navaid.position.distance_nm(point)
