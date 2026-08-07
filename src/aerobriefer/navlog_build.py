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
from .data import airports, airspace, frequencies, navaids
from .data.elevation import DEFAULT_MARGIN_FT, Elevation, sample_positions, zmin_ft
from .data.winds import WindsAloft
from .domain.geo import Corridor, Position
from .domain.models import Aerodrome, Runway
from .domain.navlog import LegComputation, NavLog, Wind, compute_navlog
from .domain.route import Leg, Route
from .domain.window import UtcDateTime

DEFAULT_CRUISE_ALTITUDE_FT = 3000.0

#: Lien robuste par OACI vers le catalogue SIA (carte VAC officielle du terrain).
SIA_VAC_SEARCH = "https://www.sia.aviation-civile.gouv.fr/catalogsearch/result/?q={icao}"
#: Visualisateur eAIP/VAC officiel (outil général).
SIA_VAIP = "https://www.sia.aviation-civile.gouv.fr/vaip"


_ARROWS = ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖")

#: Code Morse international (lettres + chiffres) — pour vérifier l'ident d'un VOR.
_MORSE = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
}

#: Plafond de radios listées par branche (au-delà, la case déborde).
_MAX_RADIOS = 8


def _morse(text: str) -> str:
    """Ident en Morse, lettre par lettre (ex. « CNA » → « -.-. -. .- »)."""
    return " ".join(_MORSE.get(c, "") for c in text.upper() if c in _MORSE)


@dataclass(frozen=True, slots=True)
class Radio:
    """Une radio dont le pilote a besoin sur la branche, ÉTIQUETÉE par sa source
    (terrain ou espace) : « LFBD TWR », « TMA BORDEAUX », etc."""

    label: str
    freq: str


@dataclass(frozen=True, slots=True)
class Vor:
    ident: str
    morse: str
    freq: str
    radial_deg: int
    distance_nm: int


@dataclass(frozen=True, slots=True)
class Diversion:
    """Terrain de déroutement le plus proche. La direction est RELATIVE à la route
    suivie (tourne à gauche/droite de X°, flèche où ↑ = droit devant), pas un cap
    absolu — c'est ce qu'on manœuvre. Plus la piste et la marge d'atterrissage."""

    icao: str
    distance_nm: int
    relative_deg: int
    side: str  # "G" (gauche) | "D" (droite)
    arrow: str
    runway: str | None  # QFU le plus long + longueur, ex. « 11/29 · 1090 m »
    landing_required_m: int | None
    margin_pct: float | None
    ok: bool | None


@dataclass(frozen=True, slots=True)
class LegAnnotation:
    """Ce que le navlog affiche EN PLUS du calcul pur, pour une branche :
    TOUTES les radios utiles (terrains + espaces traversés), le VOR le plus proche
    (avec Morse, en secondaire), et le déroutement — auto-remplis."""

    radios: list[Radio]
    vor: Vor | None
    diversion: Diversion | None


@dataclass(frozen=True, slots=True)
class VacLink:
    icao: str
    name: str
    url: str


def _vac_url(icao: str) -> str:
    return SIA_VAC_SEARCH.format(icao=icao)


def _is_vhf_comm(freq_mhz: str) -> bool:
    """Vraie fréquence VHF air (118–137 MHz) : écarte les scories de la source
    (ex. un « 29.372 » égaré dans OurAirports)."""
    try:
        return 118.0 <= float(freq_mhz) <= 137.0
    except ValueError:
        return False


def _relative(bearing_true_deg: float, track_true_deg: float) -> tuple[int, str, str]:
    """Position d'un cap PAR RAPPORT à la route suivie.

    Renvoie (écart |°| arrondi, côté 'G'/'D', flèche relative où ↑ = droit devant).
    L'écart est indépendant de la déclinaison (différence de deux caps vrais)."""
    rel = (bearing_true_deg - track_true_deg + 180.0) % 360.0 - 180.0  # (-180, 180]
    side = "G" if rel < 0 else "D"
    arrow = _ARROWS[round((rel % 360.0) / 45.0) % 8]  # 0 = ↑ devant, +90 = → droite
    return round(abs(rel)), side, arrow


def _airport_radios(icao: str) -> list[Radio]:
    """Radios VHF d'un terrain, étiquetées « OACI TYPE » (TWR/GND/APP/ATIS…)."""
    return [
        Radio(label=f"{icao} {freq.kind}", freq=freq.freq_mhz)
        for freq in frequencies.for_icao(icao)
        if _is_vhf_comm(freq.freq_mhz)
    ][:4]


def _airspace_radios(leg: LegComputation) -> list[Radio]:
    """Radios des espaces (TMA/CTR/SIV…) que la branche traverse à son altitude."""
    corridor = Corridor([leg.leg.start.position, leg.leg.end.position], 3.0)
    altitude = leg.altitude_ft
    out: list[Radio] = []
    for space in airspace.intersecting(corridor):
        if not space.frequency or not _is_vhf_comm(space.frequency):
            continue
        if altitude is not None and not (
            space.lower.feet_amsl - 500.0 <= altitude <= space.upper.feet_amsl + 500.0
        ):
            continue
        out.append(Radio(label=space.name, freq=space.frequency))
    return out


def _leg_radios(
    leg: LegComputation, *, departure_icao: str | None, arrival_icao: str | None
) -> list[Radio]:
    """TOUTES les radios utiles de la branche, dédoublonnées : terrain de départ
    (1re branche), espaces traversés, terrain d'arrivée."""
    radios: list[Radio] = []
    seen: set[tuple[str, str]] = set()

    def add(items: list[Radio]) -> None:
        for radio in items:
            key = (radio.label, radio.freq)
            if key not in seen:
                seen.add(key)
                radios.append(radio)

    if departure_icao:
        add(_airport_radios(departure_icao))
    add(_airspace_radios(leg))
    if arrival_icao:
        add(_airport_radios(arrival_icao))
    return radios[:_MAX_RADIOS]


def _vor_for(point: Position, magnetic_variation_deg: float) -> Vor | None:
    found = navaids.nearest(point)
    if found is None:
        return None
    navaid, _ = found
    radial, distance = navaids.radial_and_distance(
        navaid, point, magnetic_variation_deg=magnetic_variation_deg
    )
    return Vor(
        ident=navaid.ident,
        morse=_morse(navaid.ident),
        freq=navaid.freq_mhz,
        radial_deg=round(radial),
        distance_nm=round(distance),
    )


def _diversion_for(
    point: Position, exclude: str | None, aircraft: Aircraft, track_true_deg: float
) -> Diversion | None:
    """Déroutement le plus proche, exprimé RELATIVEMENT à la route suivie (tourner
    à G/D de X°), avec sa piste la plus longue et la marge d'atterrissage DR400."""
    for aerodrome, distance in airports.nearest(point, within_nm=45.0, limit=3):
        if exclude and aerodrome.icao == exclude:
            continue
        bearing_true = math.degrees(point.bearing_to(aerodrome.position)) % 360.0
        angle, side, arrow = _relative(bearing_true, track_true_deg)
        _, runway = _longest_runway(aerodrome.icao)
        landing = _assess(
            aerodrome.icao, aircraft, operation="landing", mass_kg=aircraft.max_landing_mass_kg
        )
        return Diversion(
            icao=aerodrome.icao,
            distance_nm=round(distance),
            relative_deg=angle,
            side=side,
            arrow=arrow,
            runway=f"{runway.ident} · {runway.length_m} m" if runway is not None else None,
            landing_required_m=landing.required_m if landing is not None else None,
            margin_pct=landing.margin_pct if landing is not None else None,
            ok=landing.ok if landing is not None else None,
        )
    return None


def annotate_navlog(
    navlog: NavLog, aircraft: Aircraft, *, magnetic_variation_deg: float = 0.0
) -> tuple[list[LegAnnotation], list[VacLink]]:
    """Enrichit chaque branche (radios, VOR, déroutement) et collecte les liens
    VAC de tous les terrains de la route."""
    annotations: list[LegAnnotation] = []
    vac: dict[str, VacLink] = {}

    def note_field(name: str) -> None:
        aerodrome = airports.lookup(name)
        if aerodrome is not None and aerodrome.icao not in vac:
            vac[aerodrome.icao] = VacLink(aerodrome.icao, aerodrome.name, _vac_url(aerodrome.icao))

    if navlog.legs:
        note_field(navlog.legs[0].leg.start.name)

    for index, leg in enumerate(navlog.legs):
        start_ad = airports.lookup(leg.leg.start.name)
        end_ad = airports.lookup(leg.leg.end.name)
        annotations.append(
            LegAnnotation(
                radios=_leg_radios(
                    leg,
                    departure_icao=start_ad.icao if (index == 0 and start_ad) else None,
                    arrival_icao=end_ad.icao if end_ad else None,
                ),
                vor=_vor_for(leg.leg.end.position, magnetic_variation_deg),
                diversion=_diversion_for(
                    leg.leg.end.position,
                    end_ad.icao if end_ad else None,
                    aircraft,
                    leg.true_track_deg,
                ),
            )
        )
        note_field(leg.leg.end.name)

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
