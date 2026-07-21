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

    def corridor(self, half_width_nm: float) -> Corridor:
        """Couloir le long de la route, pour le filtrage NOTAM/météo/espaces."""
        return Corridor([w.position for w in self.waypoints], half_width_nm)

    def bounding_circle(self) -> Circle:
        return self.corridor(0.1).bounding_circle()
