"""Assemblage du log de navigation : route + avion + vent en altitude → `NavLog`.

Fait le pont entre le calcul PUR (`domain.navlog.compute_navlog`) et les sources
concrètes : la TAS/conso viennent du modèle avion, le vent de chaque branche est
échantillonné en altitude (Open-Meteo) au milieu de la branche.

Le vent en altitude évolue lentement à l'échelle d'un vol VFR : on l'échantillonne
à l'heure de départ pour toutes les branches (raffinable plus tard en visant l'ETA
de chaque branche). La déclinaison magnétique est une ENTRÉE (à lire sur la carte
VAC/OACI) : on ne l'invente pas.
"""

from __future__ import annotations

from .aircraft.model import Aircraft
from .data.winds import WindsAloft
from .domain.geo import Position
from .domain.navlog import NavLog, Wind, compute_navlog
from .domain.route import Leg, Route
from .domain.window import UtcDateTime

DEFAULT_CRUISE_ALTITUDE_FT = 3000.0


def _leg_midpoint(leg: Leg) -> Position:
    """Milieu géographique de la branche (moyenne lat/lon, suffisant à l'échelle
    d'une branche VFR) pour y échantillonner le vent."""
    start, end = leg.start.position, leg.end.position
    return Position((start.lat + end.lat) / 2.0, (start.lon + end.lon) / 2.0)


def _leg_altitude_ft(leg: Leg, default_ft: float) -> float:
    known = [a for a in (leg.start.altitude_ft, leg.end.altitude_ft) if a is not None]
    return sum(known) / len(known) if known else default_ft


def build_navlog(
    route: Route,
    aircraft: Aircraft,
    *,
    departure_time: UtcDateTime,
    winds: WindsAloft | None = None,
    magnetic_variation_deg: float = 0.0,
    default_altitude_ft: float = DEFAULT_CRUISE_ALTITUDE_FT,
) -> NavLog:
    """Log de navigation complet pour `route` volée par `aircraft`.

    `winds` absent → on instancie un `WindsAloft` par défaut (fetch+cache
    Open-Meteo). Chaque branche est échantillonnée à son altitude planifiée
    (moyenne des extrémités, ou `default_altitude_ft` à défaut).
    """
    source = winds if winds is not None else WindsAloft()
    leg_winds: list[Wind] = []
    for leg in route.legs():
        altitude = _leg_altitude_ft(leg, default_altitude_ft)
        leg_winds.append(source.wind_at(_leg_midpoint(leg), altitude, departure_time))

    return compute_navlog(
        route,
        departure_time=departure_time,
        tas_kt=aircraft.cruise_tas_kt,
        winds=leg_winds,
        fuel_flow_lph=aircraft.cruise_fuel_lph or None,
        magnetic_variation_deg=magnetic_variation_deg,
    )
