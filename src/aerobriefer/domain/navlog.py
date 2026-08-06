"""Log de navigation : triangle des vitesses + timing, branche par branche.

Pur, sans aucune I/O. À partir d'une `Route` (points tournants + altitudes), d'un
`Wind` par branche, d'une vitesse-air vraie (TAS) et d'une heure de départ, on
calcule pour chaque branche le cap à tenir (vrai ET magnétique), la dérive, la
vitesse-sol, le temps de branche, l'heure de passage cumulée et le carburant.

Le vent et les performances de l'avion sont des ENTRÉES : leur collecte (vent en
altitude Open-Meteo, TAS/conso du modèle avion) vit ailleurs. Ce module ne fait
que la trigonométrie du vol et le cumul temporel — c'est là qu'est la logique à
tester, et elle doit l'être sans réseau.

Conventions : tous les caps/routes en degrés vrais (0 = Nord, sens horaire), sauf
mention « magnétique ». La déclinaison est comptée POSITIVE vers l'Est : cap
magnétique = cap vrai − déclinaison_est.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta  # noqa: TID251 - timedelta est une durée, pas un instant

from .route import Leg, Route
from .window import UtcDateTime


@dataclass(frozen=True, slots=True)
class Wind:
    """Vent d'une branche : direction D'OÙ IL VIENT (vrai) et force."""

    from_deg: float
    speed_kt: float

    def __post_init__(self) -> None:
        if self.speed_kt < 0:
            raise ValueError(f"force de vent négative : {self.speed_kt}")


@dataclass(frozen=True, slots=True)
class LegComputation:
    """Résultat du triangle des vitesses pour une branche."""

    leg: Leg
    altitude_ft: float | None
    wind: Wind
    tas_kt: float

    true_track_deg: float
    magnetic_track_deg: float
    true_heading_deg: float
    magnetic_heading_deg: float
    wind_correction_deg: float
    """Angle de correction de dérive à appliquer (cap = route + correction).
    Positif = correction vers la DROITE (le vent pousse vers la gauche)."""

    ground_speed_kt: float
    headwind_kt: float
    """Composante longitudinale : positive = vent de face, négative = arrière."""

    distance_nm: float
    time: timedelta
    eta: UtcDateTime
    """Heure de passage au point d'ARRIVÉE de la branche."""

    fuel_l: float | None


@dataclass(frozen=True, slots=True)
class NavLog:
    departure_time: UtcDateTime
    legs: list[LegComputation]

    @property
    def total_distance_nm(self) -> float:
        return sum(leg.distance_nm for leg in self.legs)

    @property
    def total_time(self) -> timedelta:
        total = timedelta()
        for leg in self.legs:
            total += leg.time
        return total

    @property
    def total_fuel_l(self) -> float | None:
        fuels = [leg.fuel_l for leg in self.legs]
        if any(f is None for f in fuels):
            return None
        return sum(f for f in fuels if f is not None)

    @property
    def eta_arrival(self) -> UtcDateTime:
        return self.legs[-1].eta if self.legs else self.departure_time


def _leg_altitude_ft(leg: Leg) -> float | None:
    """Altitude représentative de la branche pour l'échantillonnage du vent.

    Moyenne des altitudes des deux extrémités quand elles sont connues ; sinon
    celle qui l'est ; sinon `None` (le vent devra être fourni autrement)."""
    start, end = leg.start.altitude_ft, leg.end.altitude_ft
    known = [a for a in (start, end) if a is not None]
    if not known:
        return None
    return sum(known) / len(known)


def _wind_triangle(true_track_deg: float, tas_kt: float, wind: Wind) -> tuple[float, float, float]:
    """Résout le triangle des vitesses.

    Renvoie (correction_de_dérive_deg, vitesse_sol_kt, composante_de_face_kt).
    Lève `ValueError` si le vent de face atteint ou dépasse la TAS (la branche ne
    peut pas être parcourue) — un navlog qui afficherait un temps fini serait
    trompeur.
    """
    if tas_kt <= 0:
        raise ValueError(f"TAS non exploitable : {tas_kt}")
    # Écart entre la provenance du vent et la route. Composante traversière
    # positive = vent venant de la droite → on corrige vers la droite (WCA > 0).
    beta = math.radians(wind.from_deg - true_track_deg)
    crosswind = wind.speed_kt * math.sin(beta)
    headwind = wind.speed_kt * math.cos(beta)

    ratio = crosswind / tas_kt
    if not -1.0 <= ratio <= 1.0:
        raise ValueError("vent traversier supérieur à la TAS : branche impraticable")
    wca = math.asin(ratio)
    ground_speed = tas_kt * math.cos(wca) - headwind
    if ground_speed <= 0.0:
        raise ValueError("vent de face ≥ TAS : branche impraticable")
    return math.degrees(wca), ground_speed, headwind


def compute_navlog(
    route: Route,
    *,
    departure_time: UtcDateTime,
    tas_kt: float,
    winds: list[Wind],
    fuel_flow_lph: float | None = None,
    magnetic_variation_deg: float = 0.0,
) -> NavLog:
    """Calcule le log de navigation branche par branche.

    `winds` porte un vent par branche, dans l'ordre des branches de la route (donc
    `len(winds) == len(route.legs())`). `tas_kt` est la vitesse-air vraie de
    croisière (constante pour l'instant ; une variation avec l'altitude viendra
    du modèle avion). `magnetic_variation_deg` est la déclinaison, positive vers
    l'Est. `fuel_flow_lph` absent → carburant non calculé.
    """
    legs = route.legs()
    if len(winds) != len(legs):
        raise ValueError(
            f"{len(winds)} vents pour {len(legs)} branches : il en faut un par branche"
        )

    results: list[LegComputation] = []
    clock = departure_time
    for leg, wind in zip(legs, winds, strict=True):
        wca_deg, ground_speed_kt, headwind_kt = _wind_triangle(leg.true_track_deg, tas_kt, wind)
        true_heading = (leg.true_track_deg + wca_deg) % 360.0
        time = timedelta(hours=leg.distance_nm / ground_speed_kt)
        clock = UtcDateTime.of(clock + time, "heure de passage")
        results.append(
            LegComputation(
                leg=leg,
                altitude_ft=_leg_altitude_ft(leg),
                wind=wind,
                tas_kt=tas_kt,
                true_track_deg=leg.true_track_deg,
                magnetic_track_deg=(leg.true_track_deg - magnetic_variation_deg) % 360.0,
                true_heading_deg=true_heading,
                magnetic_heading_deg=(true_heading - magnetic_variation_deg) % 360.0,
                wind_correction_deg=wca_deg,
                ground_speed_kt=ground_speed_kt,
                headwind_kt=headwind_kt,
                distance_nm=leg.distance_nm,
                time=time,
                eta=clock,
                fuel_l=None
                if fuel_flow_lph is None
                else fuel_flow_lph * time.total_seconds() / 3600.0,
            )
        )
    return NavLog(departure_time=departure_time, legs=results)
