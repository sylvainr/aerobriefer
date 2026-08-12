"""Étude de déroutement / dégagement : les terrains où l'on POURRAIT se poser.

Étape 1 du process, formalisée : autour des terrains du vol (départ, points
tournants, destination), on cherche les aérodromes voisins, et pour CHACUN on
répond à la seule question qui compte en cas de pépin — « puis-je m'y poser (et
en repartir) avec CET avion, dans CES conditions ? ». La réponse croise :

- la distance et le cap (magnétique) depuis le terrain de référence le plus proche ;
- la ou les pistes (longueur, revêtement) ;
- la performance de l'avion aux conditions du terrain (altitude-pression déduite
  du QNH, température, vent dans le sens favorable) via le moteur `aircraft` ;
- la météo observée (METAR du terrain, ou du voisin le plus proche à défaut).

Aucune donnée n'est inventée : si un terrain n'a pas de longueur de piste
connue, le verdict est « inconnu », pas « ok ». Les distances sont celles du
franchissement des 15 m (obstacle VFR), corrigées, éventuellement majorées d'un
facteur de sécurité.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .aircraft.model import AircraftSpec, Conditions, isa_temperature_c
from .aircraft.runway import RunwayAssessment, assess_runway
from .data import airports
from .domain.models import Aerodrome, WindComponents
from .domain.package import BriefingPackage
from .domain.window import UtcDateTime, utcnow
from .navlog_build import vac_url

# Altitude-pression : ~27 ft par hPa d'écart au standard, près du sol.
_HPA_TO_FT = 27.0
_STD_QNH_HPA = 1013.25

# Seuils de verdict d'ATTERRISSAGE (marge = (dispo − requis) / dispo).
_MARGIN_COMFORT_PCT = 20.0  # au-dessus : confortable


@dataclass(frozen=True, slots=True)
class RunwayPerf:
    """Performance de l'avion sur UNE piste d'un terrain candidat."""

    ident: str
    length_m: int
    surface: str | None
    is_paved: bool | None
    headwind_kt: float  # composante de face retenue (sens favorable de la piste)
    landing: RunwayAssessment | None  # None si longueur de piste inconnue
    takeoff: RunwayAssessment | None


@dataclass(frozen=True, slots=True)
class DiversionField:
    """Un terrain de dégagement étudié."""

    icao: str
    name: str
    distance_nm: float
    bearing_true_deg: float
    bearing_mag_deg: float
    from_icao: str  # terrain de référence le plus proche

    elevation_ft: int
    longest_runway_m: int | None
    pressure_altitude_ft: float
    temperature_c: float
    temp_is_isa: bool  # température supposée (ISA) faute de METAR
    qnh_hpa: float | None

    wind_dir_deg: int | None
    wind_speed_kt: int | None
    wind_gust_kt: int | None
    wind: WindComponents | None  # piste favorable au vent (crosswind, etc.)
    crosswind_over_demo: bool

    runways: tuple[RunwayPerf, ...]
    best_landing: RunwayAssessment | None
    best_takeoff: RunwayAssessment | None
    verdict: str  # OK | SERRÉ | INSUFFISANT | INCONNU

    # Météo (source réelle, avec provenance)
    metar_station: str | None
    metar_distance_nm: float | None
    metar_is_proxy: bool  # emprunté à un terrain voisin
    metar_age_min: float | None
    flight_category: str | None
    metar_raw: str | None
    taf_station: str | None
    taf_is_proxy: bool
    taf_raw: str | None

    vac_url: str


@dataclass(frozen=True, slots=True)
class DiversionStudy:
    """Le dossier de dégagement complet, prêt à rendre."""

    reference_icaos: tuple[str, ...]
    reference_names: tuple[str, ...]
    aircraft_name: str
    registration: str
    radius_nm: float
    mass_takeoff_kg: float
    mass_landing_kg: float
    safety_factor: float
    variation_deg: float
    demonstrated_crosswind_kt: float | None
    fields: tuple[DiversionField, ...]
    notes: tuple[str, ...]


def _pressure_altitude_ft(elevation_ft: int, qnh_hpa: float | None) -> float:
    if qnh_hpa is None:
        return float(elevation_ft)
    return elevation_ft + (_STD_QNH_HPA - qnh_hpa) * _HPA_TO_FT


def _nearest_station(target: Aerodrome, sourced_items: Sequence[Any]) -> tuple[Any, float] | None:
    """Item météo (Sourced) dont la station est la plus proche du terrain.

    Renvoie (item, distance_nm) ou None. Sert de repli quand le terrain n'a pas
    d'observation propre : on emprunte celle du voisin, en le disant.
    """
    best: tuple[Any, float] | None = None
    for item in sourced_items:
        station = airports.lookup(item.value.station)
        if station is None:
            continue
        dist = target.position.distance_nm(station.position)
        if best is None or dist < best[1]:
            best = (item, dist)
    return best


def _best(assessments: list[RunwayAssessment | None]) -> RunwayAssessment | None:
    """Meilleure piste : parmi les OK, la marge la plus grande ; sinon la moins mauvaise."""
    pool = [a for a in assessments if a is not None]
    if not pool:
        return None
    oks = [a for a in pool if a.ok]
    return max(oks or pool, key=lambda a: a.margin_pct)


def _verdict(best_landing: RunwayAssessment | None) -> str:
    if best_landing is None:
        return "INCONNU"
    if not best_landing.ok:
        return "INSUFFISANT"
    if best_landing.margin_pct < _MARGIN_COMFORT_PCT:
        return "SERRÉ"
    return "OK"


def _study_field(
    aerodrome: Aerodrome,
    distance_nm: float,
    from_icao: str,
    *,
    aircraft: AircraftSpec,
    package: BriefingPackage,
    variation_deg: float,
    mass_takeoff_kg: float,
    mass_landing_kg: float,
    safety_factor: float,
    now: UtcDateTime,
) -> DiversionField:
    origin = airports.require(from_icao)
    bearing_true = math.degrees(origin.position.bearing_to(aerodrome.position)) % 360.0
    bearing_mag = (bearing_true - variation_deg) % 360.0

    # Météo : observation propre du terrain, sinon la plus proche (marquée « proxy »).
    own = package.metar_for(aerodrome.icao)
    metar_is_proxy = own is None
    metar_pair = (own, 0.0) if own is not None else _nearest_station(aerodrome, package.metars)
    metar = metar_pair[0] if metar_pair else None
    metar_dist = metar_pair[1] if metar_pair else None

    own_taf = next((t for t in package.tafs if t.value.station.upper() == aerodrome.icao), None)
    taf_is_proxy = own_taf is None
    taf_pair = (own_taf, 0.0) if own_taf is not None else _nearest_station(aerodrome, package.tafs)
    taf = taf_pair[0] if taf_pair else None

    wind_dir = metar.value.wind_dir_deg if metar else None
    wind_speed = metar.value.wind_speed_kt if metar else None
    qnh = metar.value.qnh_hpa if metar else None
    obs_temp = metar.value.temperature_c if metar else None

    pa_ft = _pressure_altitude_ft(aerodrome.elevation_ft, qnh)
    temp_is_isa = obs_temp is None
    temp_c = isa_temperature_c(pa_ft) if obs_temp is None else obs_temp

    favoured = (
        aerodrome.favoured_wind_components(wind_dir, wind_speed)
        if wind_dir is not None and wind_speed is not None
        else None
    )
    demo = aircraft.demonstrated_crosswind_kt
    crosswind_over_demo = bool(favoured and demo and favoured.crosswind_kt > demo)

    runways: list[RunwayPerf] = []
    for rwy in aerodrome.runways:
        if not rwy.length_m or rwy.length_m <= 0:
            runways.append(
                RunwayPerf(rwy.ident, rwy.length_m or 0, rwy.surface, rwy.is_paved, 0.0, None, None)
            )
            continue
        # Vent dans le sens FAVORABLE de la piste : on retient la composante de face.
        headwind = 0.0
        if wind_dir is not None and wind_speed is not None:
            comp = rwy.wind_components(wind_dir, wind_speed)
            if comp is not None:
                headwind = abs(comp.headwind_kt)
        base = Conditions(
            pressure_altitude_ft=pa_ft,
            temperature_c=temp_c,
            mass_kg=mass_landing_kg,
            headwind_kt=headwind,
            slope_pct=0.0,
        )
        landing = assess_runway(
            aircraft, rwy, base, operation="landing", safety_factor=safety_factor
        )
        takeoff = assess_runway(
            aircraft,
            rwy,
            Conditions(
                pressure_altitude_ft=pa_ft,
                temperature_c=temp_c,
                mass_kg=mass_takeoff_kg,
                headwind_kt=headwind,
                slope_pct=0.0,
            ),
            operation="takeoff",
            safety_factor=safety_factor,
        )
        runways.append(
            RunwayPerf(
                rwy.ident, rwy.length_m, rwy.surface, rwy.is_paved, headwind, landing, takeoff
            )
        )

    best_landing = _best([r.landing for r in runways])
    best_takeoff = _best([r.takeoff for r in runways])

    return DiversionField(
        icao=aerodrome.icao,
        name=aerodrome.name,
        distance_nm=distance_nm,
        bearing_true_deg=bearing_true,
        bearing_mag_deg=bearing_mag,
        from_icao=from_icao,
        elevation_ft=aerodrome.elevation_ft,
        longest_runway_m=aerodrome.longest_runway_m,
        pressure_altitude_ft=pa_ft,
        temperature_c=temp_c,
        temp_is_isa=temp_is_isa,
        qnh_hpa=qnh,
        wind_dir_deg=wind_dir,
        wind_speed_kt=wind_speed,
        wind_gust_kt=metar.value.wind_gust_kt if metar else None,
        wind=favoured,
        crosswind_over_demo=crosswind_over_demo,
        runways=tuple(runways),
        best_landing=best_landing,
        best_takeoff=best_takeoff,
        verdict=_verdict(best_landing),
        metar_station=metar.value.station if metar else None,
        metar_distance_nm=metar_dist,
        metar_is_proxy=metar_is_proxy,
        metar_age_min=metar.age_minutes(now) if metar else None,
        flight_category=metar.value.flight_category if metar else None,
        metar_raw=metar.value.raw_text if metar else None,
        taf_station=taf.value.station if taf else None,
        taf_is_proxy=taf_is_proxy,
        taf_raw=taf.value.raw_text if taf else None,
        vac_url=vac_url(aerodrome.icao),
    )


def build_diversion_study(
    package: BriefingPackage,
    aircraft: AircraftSpec,
    *,
    radius_nm: float = 30.0,
    limit: int = 12,
    safety_factor: float = 1.0,
    variation_deg: float = 0.0,
    mass_takeoff_kg: float | None = None,
    mass_landing_kg: float | None = None,
    now: UtcDateTime | None = None,
) -> DiversionStudy:
    """Construit l'étude de dégagement autour des terrains du vol.

    Les terrains de référence sont ceux du vol (`flight_aerodromes`). Autour de
    chacun, on prend les aérodromes dans `radius_nm`, on les dédoublonne (distance
    au plus proche des terrains de référence), on garde les `limit` plus proches,
    et on étudie chacun. Masses par défaut : les MAXIMA (hypothèse conservatrice).
    """
    now = now or utcnow()
    mtow = mass_takeoff_kg if mass_takeoff_kg is not None else aircraft.max_takeoff_mass_kg
    mldw = mass_landing_kg if mass_landing_kg is not None else aircraft.max_landing_mass_kg

    references = [
        a for a in map(airports.lookup, package.context.flight_aerodromes) if a is not None
    ]
    flight_set = {a.icao for a in references}

    # Candidats : union des voisins de chaque terrain de référence, dédoublonnée
    # par ICAO en gardant la plus courte distance (et son terrain de référence).
    best_by_icao: dict[str, tuple[Aerodrome, float, str]] = {}
    for ref in references:
        for aero, dist in airports.nearest(ref.position, within_nm=radius_nm, limit=limit * 4):
            if aero.icao in flight_set:
                continue
            current = best_by_icao.get(aero.icao)
            if current is None or dist < current[1]:
                best_by_icao[aero.icao] = (aero, dist, ref.icao)

    ordered = sorted(best_by_icao.values(), key=lambda t: t[1])[:limit]
    fields = tuple(
        _study_field(
            aero,
            dist,
            from_icao,
            aircraft=aircraft,
            package=package,
            variation_deg=variation_deg,
            mass_takeoff_kg=mtow,
            mass_landing_kg=mldw,
            safety_factor=safety_factor,
            now=now,
        )
        for aero, dist, from_icao in ordered
    )

    notes = (
        f"Masses au maximum ({mtow:.0f} kg décollage / {mldw:.0f} kg atterrissage) — "
        "hypothèse conservatrice, à ajuster à votre chargement réel.",
        "Distances = franchissement des 15 m (obstacle VFR), corrigées "
        "altitude-pression / température / vent"
        + (f", majorées ×{safety_factor:.2f}." if safety_factor != 1.0 else "."),
        "Vent pris dans le sens le PLUS FAVORABLE de chaque piste.",
        "Météo « voisin » = observation empruntée au terrain doté d'un METAR le "
        "plus proche, faute d'observation sur le terrain même.",
        "Aide à la décision — le commandant de bord reste seul juge, carte VAC à l'appui.",
    )

    return DiversionStudy(
        reference_icaos=tuple(a.icao for a in references),
        reference_names=tuple(a.name for a in references),
        aircraft_name=aircraft.name,
        registration=aircraft.registration,
        radius_nm=radius_nm,
        mass_takeoff_kg=mtow,
        mass_landing_kg=mldw,
        safety_factor=safety_factor,
        variation_deg=variation_deg,
        demonstrated_crosswind_kt=aircraft.demonstrated_crosswind_kt,
        fields=fields,
        notes=notes,
    )
