"""Route de navigation : une suite de points tournants, chacun avec une altitude.

À la SOFIA : ADEP, points intermédiaires, ADEST. Un point peut être un aérodrome
(résolu par code OACI), un point VFR publié, ou des coordonnées. On lui associe
une altitude planifiée pour pouvoir visualiser le profil vertical et voir quels
espaces la trajectoire traverse, et à quelle hauteur.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .geo import Circle, Corridor, Position


@dataclass(frozen=True, slots=True)
class Waypoint:
    name: str
    position: Position
    altitude_ft: float | None = None
    """Altitude PLANIFIÉE au point (ft AMSL). Absente = non spécifiée."""


@dataclass(frozen=True, slots=True)
class Leg:
    """Une branche entre deux points tournants."""

    start: Waypoint
    end: Waypoint
    distance_nm: float
    true_track_deg: float
    """Route vraie (pas magnétique — la déclinaison est un raffinement ultérieur)."""


@dataclass(frozen=True, slots=True)
class Route:
    waypoints: Sequence[Waypoint] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if len(self.waypoints) < 2:
            raise ValueError("une route exige au moins deux points")

    def legs(self) -> list[Leg]:
        out: list[Leg] = []
        for a, b in zip(self.waypoints, self.waypoints[1:], strict=False):
            out.append(
                Leg(
                    start=a,
                    end=b,
                    distance_nm=a.position.distance_nm(b.position),
                    true_track_deg=math.degrees(a.position.bearing_to(b.position)) % 360.0,
                )
            )
        return out

    def total_distance_nm(self) -> float:
        return sum(leg.distance_nm for leg in self.legs())

    def along_track_nm(self, point: Position) -> float:
        """Distance le long de la route (départ = 0) de la projection orthogonale
        de `point` sur la polyligne.

        Sert à ORDONNER des stations « dans le sens du vol » : projeter chaque
        station sur l'axe de nav (le segment le plus proche) donne son rang, du
        départ vers l'arrivée. On retient le segment dont la projection est la
        plus proche du point, et on cumule la distance parcourue jusque-là.
        """
        best_along = 0.0
        best_perp_m = math.inf
        cumulative_nm = 0.0
        for start, end in zip(self.waypoints, self.waypoints[1:], strict=False):
            seg_nm = start.position.distance_nm(end.position)
            if seg_nm == 0.0:
                continue
            fraction, perp_m = _project_fraction(start.position, end.position, point)
            clamped = min(1.0, max(0.0, fraction))
            if perp_m < best_perp_m:
                best_perp_m = perp_m
                best_along = cumulative_nm + clamped * seg_nm
            cumulative_nm += seg_nm
        return best_along

    def corridor(self, half_width_nm: float) -> Corridor:
        """Couloir le long de la route, pour le filtrage NOTAM/météo/espaces."""
        return Corridor([w.position for w in self.waypoints], half_width_nm)

    def bounding_circle(self) -> Circle:
        return self.corridor(0.1).bounding_circle()


_M_PER_DEG = 111_320.0


def _project_fraction(start: Position, end: Position, point: Position) -> tuple[float, float]:
    """Projette `point` sur le segment start→end en géométrie plane locale.

    Renvoie (fraction, distance perpendiculaire en mètres). `fraction` est la
    position le long du segment (0 au départ, 1 à l'arrivée), non bornée pour
    distinguer les points au-delà des extrémités. Projection équirectangulaire
    autour de `start` : sans distorsion sensible à l'échelle d'une branche VFR.
    """
    cos_lat = math.cos(math.radians(start.lat))
    ex = (end.lon - start.lon) * _M_PER_DEG * cos_lat
    ey = (end.lat - start.lat) * _M_PER_DEG
    px = (point.lon - start.lon) * _M_PER_DEG * cos_lat
    py = (point.lat - start.lat) * _M_PER_DEG
    seg_sq = ex * ex + ey * ey
    if seg_sq == 0.0:
        return 0.0, math.hypot(px, py)
    fraction = (px * ex + py * ey) / seg_sq
    perp_m = math.hypot(px - fraction * ex, py - fraction * ey)
    return fraction, perp_m
