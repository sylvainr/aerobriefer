"""Géométries de briefing : où on vole, et comment on teste qu'une donnée nous concerne.

Tout est en milles nautiques. Les calculs sont en sphérique (rayon terrestre moyen) :
l'erreur face à l'ellipsoïde est de l'ordre de 0.3 %, très en dessous de la précision
des rayons NOTAM eux-mêmes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

EARTH_RADIUS_NM = 3440.065


@dataclass(frozen=True, slots=True)
class Position:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"latitude hors bornes : {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"longitude hors bornes : {self.lon}")

    def distance_nm(self, other: Position) -> float:
        """Distance orthodromique."""
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp = p2 - p1
        dl = math.radians(other.lon - self.lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))

    def bearing_to(self, other: Position) -> float:
        """Route initiale vraie, en radians."""
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dl = math.radians(other.lon - self.lon)
        y = math.sin(dl) * math.cos(p2)
        x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return math.atan2(y, x)


class Geometry(Protocol):
    """Une zone d'intérêt.

    Le seul contrat exigé d'un provider : savoir si un point, entouré d'une
    incertitude éventuelle, tombe dans la zone. `radius_nm` permet de tester un
    NOTAM circulaire sans le réduire à son centre.
    """

    def contains(self, point: Position, radius_nm: float = 0.0) -> bool: ...

    def bounding_circle(self) -> Circle:
        """Cercle englobant — sert aux providers dont l'API ne sait interroger
        qu'un rayon autour d'un point. On sur-collecte, puis on refiltre avec
        `contains`."""
        ...


@dataclass(frozen=True, slots=True)
class Circle:
    center: Position
    radius_nm: float

    def __post_init__(self) -> None:
        if self.radius_nm <= 0:
            raise ValueError(f"rayon doit être positif : {self.radius_nm}")

    def contains(self, point: Position, radius_nm: float = 0.0) -> bool:
        return self.center.distance_nm(point) <= self.radius_nm + radius_nm

    def bounding_circle(self) -> Circle:
        return self


@dataclass(frozen=True, slots=True)
class Corridor:
    """Couloir de largeur constante le long d'une suite de branches.

    `half_width_nm` est la marge de part et d'autre de la route — c'est la
    convention des briefings de route (une « largeur de 10 NM » usuelle
    correspond à half_width_nm=10, soit 20 NM de couloir total).
    """

    points: Sequence[Position]
    half_width_nm: float

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("un couloir exige au moins deux points")
        if self.half_width_nm <= 0:
            raise ValueError(f"demi-largeur doit être positive : {self.half_width_nm}")

    def contains(self, point: Position, radius_nm: float = 0.0) -> bool:
        margin = self.half_width_nm + radius_nm
        return any(
            _distance_to_segment_nm(point, a, b) <= margin
            for a, b in zip(self.points, self.points[1:], strict=False)
        )

    def bounding_circle(self) -> Circle:
        """Centré sur le milieu de la route, rayonné pour couvrir tous les points.

        Volontairement grossier : il ne sert qu'à cadrer la collecte, le filtrage
        fin restant à la charge de `contains`.
        """
        lat = sum(p.lat for p in self.points) / len(self.points)
        lon = sum(p.lon for p in self.points) / len(self.points)
        center = Position(lat, lon)
        reach = max(center.distance_nm(p) for p in self.points)
        return Circle(center, reach + self.half_width_nm)


def _distance_to_segment_nm(p: Position, a: Position, b: Position) -> float:
    """Distance d'un point à un segment orthodromique [a, b].

    Hors des extrémités, la distance pertinente est celle au point extrême et non
    la distance transversale — sans quoi un NOTAM situé loin dans le prolongement
    de la route serait retenu à tort.
    """
    leg_nm = a.distance_nm(b)
    if leg_nm == 0.0:
        return a.distance_nm(p)

    d13 = a.distance_nm(p) / EARTH_RADIUS_NM
    theta13 = a.bearing_to(p)
    theta12 = a.bearing_to(b)

    cross_track = math.asin(math.sin(d13) * math.sin(theta13 - theta12))
    # cos(cross_track) ne s'annule que pour un point au pôle du grand cercle,
    # à 90° de la route : bien au-delà de tout couloir réaliste.
    ratio = min(1.0, max(-1.0, math.cos(d13) / math.cos(cross_track)))
    along_track_nm = math.acos(ratio) * EARTH_RADIUS_NM

    if along_track_nm < 0:
        return a.distance_nm(p)
    if along_track_nm > leg_nm:
        return b.distance_nm(p)
    return abs(cross_track) * EARTH_RADIUS_NM
