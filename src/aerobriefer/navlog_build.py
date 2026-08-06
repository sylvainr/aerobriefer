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

import math
from dataclasses import dataclass

from .aircraft.model import Aircraft, Conditions, isa_temperature_c
from .aircraft.runway import assess_runway
from .data import airports, frequencies, navaids
from .data.elevation import DEFAULT_MARGIN_FT, Elevation, sample_positions, zmin_ft
from .data.winds import WindsAloft
from .domain.geo import Position
from .domain.models import Aerodrome, Runway
from .domain.navlog import NavLog, Wind, compute_navlog
from .domain.route import Leg, Route
from .domain.window import UtcDateTime

DEFAULT_CRUISE_ALTITUDE_FT = 3000.0

#: Lien robuste par OACI vers le catalogue SIA (carte VAC officielle du terrain).
SIA_VAC_SEARCH = "https://www.sia.aviation-civile.gouv.fr/catalogsearch/result/?q={icao}"
#: Visualisateur eAIP/VAC officiel (outil général).
SIA_VAIP = "https://www.sia.aviation-civile.gouv.fr/vaip"


@dataclass(frozen=True, slots=True)
class LegAnnotation:
    """Ce que le navlog affiche EN PLUS du calcul pur, pour le point d'ARRIVÉE de
    la branche : radiale VOR, radios, déroutement — auto-remplis depuis la donnée
    de référence, pour que le pilote n'ait rien à chercher."""

    vor: str | None
    radios: str | None
    diversion: str | None


@dataclass(frozen=True, slots=True)
class VacLink:
    icao: str
    name: str
    url: str


def _vac_url(icao: str) -> str:
    return SIA_VAC_SEARCH.format(icao=icao)


def _magnetic(true_deg: float, magnetic_variation_deg: float) -> float:
    return (true_deg - magnetic_variation_deg) % 360.0


def _is_vhf_comm(freq_mhz: str) -> bool:
    """Vraie fréquence VHF air (118–137 MHz) : écarte les scories de la source
    (ex. un « 29.372 » égaré dans OurAirports)."""
    try:
        return 118.0 <= float(freq_mhz) <= 137.0
    except ValueError:
        return False


def _radios_for(icao: str, *, limit: int = 3) -> str | None:
    freqs = [f for f in frequencies.for_icao(icao) if _is_vhf_comm(f.freq_mhz)][:limit]
    if not freqs:
        return None
    return " · ".join(f"{f.kind} {f.freq_mhz}" for f in freqs)


def _vor_for(point: Position, magnetic_variation_deg: float) -> str | None:
    found = navaids.nearest(point)
    if found is None:
        return None
    navaid, _ = found
    radial, distance = navaids.radial_and_distance(
        navaid, point, magnetic_variation_deg=magnetic_variation_deg
    )
    freq = f" {navaid.freq_mhz}" if navaid.freq_mhz else ""
    return f"{navaid.ident}{freq} R{radial:03.0f}°/{distance:.0f} NM"


def _diversion_for(
    point: Position, exclude: str | None, magnetic_variation_deg: float
) -> str | None:
    for aerodrome, distance in airports.nearest(point, within_nm=45.0, limit=3):
        if exclude and aerodrome.icao == exclude:
            continue
        bearing = _magnetic(
            math.degrees(point.bearing_to(aerodrome.position)) % 360.0, magnetic_variation_deg
        )
        return f"{aerodrome.icao} {distance:.0f} NM / {bearing:03.0f}°"
    return None


def annotate_navlog(
    navlog: NavLog, *, magnetic_variation_deg: float = 0.0
) -> tuple[list[LegAnnotation], list[VacLink]]:
    """Enrichit chaque branche (VOR/radios/déroutement du point d'arrivée) et
    collecte les liens VAC de tous les terrains de la route."""
    annotations: list[LegAnnotation] = []
    vac: dict[str, VacLink] = {}

    def note_field(name: str) -> None:
        aerodrome = airports.lookup(name)
        if aerodrome is not None and aerodrome.icao not in vac:
            vac[aerodrome.icao] = VacLink(aerodrome.icao, aerodrome.name, _vac_url(aerodrome.icao))

    # Départ (début de la 1re branche) dans la liste VAC.
    if navlog.legs:
        note_field(navlog.legs[0].leg.start.name)

    for leg in navlog.legs:
        end = leg.leg.end
        aerodrome = airports.lookup(end.name)
        icao = aerodrome.icao if aerodrome is not None else None
        annotations.append(
            LegAnnotation(
                vor=_vor_for(end.position, magnetic_variation_deg),
                radios=_radios_for(icao) if icao else None,
                diversion=_diversion_for(end.position, icao, magnetic_variation_deg),
            )
        )
        note_field(end.name)

    return annotations, list(vac.values())


@dataclass(frozen=True, slots=True)
class PerfAssessment:
    """Verdict perf piste pour le dossier, aux conditions CONSERVATRICES (masse
    maxi, ISA+15, sans vent) : si ça passe là, ça passe le jour du vol."""

    label: str
    runway: str
    conditions: str
    required_m: int
    available_m: int
    margin_pct: float
    ok: bool


def _longest_runway(icao: str) -> tuple[Aerodrome | None, Runway | None]:
    aerodrome = airports.lookup(icao)
    if aerodrome is None or not aerodrome.runways:
        return None, None
    runways = [r for r in aerodrome.runways if r.length_m]
    if not runways:
        return aerodrome, None
    return aerodrome, max(runways, key=lambda r: r.length_m)


def _assess(
    icao: str, aircraft: Aircraft, *, operation: str, mass_kg: float
) -> PerfAssessment | None:
    aerodrome, runway = _longest_runway(icao)
    if aerodrome is None or runway is None:
        return None
    elevation = float(aerodrome.elevation_ft)
    conditions = Conditions(
        pressure_altitude_ft=elevation,
        temperature_c=isa_temperature_c(elevation) + 15.0,  # ISA+15, jour chaud conservateur
        mass_kg=mass_kg,
        headwind_kt=0.0,
    )
    verdict = assess_runway(aircraft, runway, conditions, operation=operation)
    label = "Décollage" if operation == "takeoff" else "Atterrissage"
    surface = f", {runway.surface}" if runway.surface else ""
    return PerfAssessment(
        label=f"{label} {icao}",
        runway=f"piste {runway.ident} ({runway.length_m} m{surface})",
        conditions=f"{mass_kg:.0f} kg · {elevation:.0f} ft · ISA+15 · sans vent",
        required_m=verdict.required_m,
        available_m=verdict.available_m,
        margin_pct=verdict.margin_pct,
        ok=verdict.ok,
    )


def runway_performance(
    aircraft: Aircraft, origin_icao: str, dest_icao: str
) -> list[PerfAssessment]:
    """Perfs décollage (départ) et atterrissage (arrivée), aux conditions
    conservatrices. Terrains sans piste connue : ignorés."""
    out: list[PerfAssessment] = []
    takeoff = _assess(
        origin_icao, aircraft, operation="takeoff", mass_kg=aircraft.max_takeoff_mass_kg
    )
    if takeoff is not None:
        out.append(takeoff)
    landing = _assess(
        dest_icao, aircraft, operation="landing", mass_kg=aircraft.max_landing_mass_kg
    )
    if landing is not None:
        out.append(landing)
    return out


def safety_altitudes(
    navlog: NavLog, *, elevation: Elevation | None = None, margin_ft: float = DEFAULT_MARGIN_FT
) -> list[float]:
    """Zmin par branche : relief le plus haut du couloir + marge, arrondi.

    Une seule requête d'élévation pour TOUTES les branches (batch). TERRAIN seul —
    les obstacles ponctuels ne sont pas couverts (à vérifier NOTAM/VAC)."""
    source = elevation if elevation is not None else Elevation()
    per_leg_points = [
        sample_positions(leg.leg.start.position, leg.leg.end.position) for leg in navlog.legs
    ]
    source.elevation_m([point for points in per_leg_points for point in points])  # pré-charge
    return [zmin_ft(source.max_terrain_m(points), margin_ft=margin_ft) for points in per_leg_points]


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
