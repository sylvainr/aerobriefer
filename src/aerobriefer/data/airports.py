"""Base aérodromes, dérivée d'OurAirports (domaine public).

Sous-ensemble Europe de l'Ouest, embarqué dans le paquet : le briefing doit
pouvoir se préparer sans réseau, et 115 Ko ne justifient pas une dépendance
externe.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from ..domain.geo import Position
from ..domain.models import Aerodrome, Runway

_CSV = Path(__file__).with_name("airports_eu.csv")
_RUNWAYS_CSV = Path(__file__).with_name("runways_eu.csv")
_RUNWAYS_SUPPLEMENT_CSV = Path(__file__).with_name("runways_supplement.csv")


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def _read_runway_rows(path: Path) -> Iterator[tuple[str, Runway]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            length_m = _to_float(row.get("length_m", ""))
            if length_m is None:
                continue
            heading = _to_float(row.get("le_heading", ""))
            if heading is None:
                heading = _heading_from_ident(row.get("le_ident", ""))
            ident = f"{row.get('le_ident', '')}/{row.get('he_ident', '')}".strip("/")
            yield (
                row["icao"],
                Runway(
                    ident=ident or "?",
                    length_m=int(length_m),
                    width_m=int(w) if (w := _to_float(row.get("width_m", ""))) else None,
                    surface=row.get("surface") or None,
                    true_bearing_deg=heading,
                ),
            )


@lru_cache(maxsize=1)
def _runways_by_icao() -> dict[str, tuple[Runway, ...]]:
    """Pistes indexées par OACI.

    Deux sources FUSIONNÉES : OurAirports (base publique, souvent incomplète — elle
    rate régulièrement les bandes herbe des petits terrains français) PLUS un
    fichier de complément maintenu à la main (`runways_supplement.csv`) pour les
    pistes qu'un pilote connaît et que la base ignore (ex. la bande herbe 10R/28L
    de LFCY, absente d'OurAirports). Le complément AJOUTE, il n'écrase pas.

    Le cap vrai retenu est celui de la QFU basse, avec repli sur l'orientation
    déduite du numéro de piste. Sert au vent traversier et aux longueurs de piste.
    """
    index: dict[str, list[Runway]] = {}
    for path in (_RUNWAYS_CSV, _RUNWAYS_SUPPLEMENT_CSV):
        for icao, runway in _read_runway_rows(path):
            index.setdefault(icao, []).append(runway)
    return {icao: tuple(rwys) for icao, rwys in index.items()}


def _heading_from_ident(ident: str) -> float | None:
    """« 07 » → 70°, « 27L » → 270°. Repli quand le cap vrai manque."""
    digits = "".join(c for c in ident if c.isdigit())
    if not digits:
        return None
    try:
        return (int(digits[:2]) % 36) * 10.0
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _load() -> dict[str, Aerodrome]:
    runways = _runways_by_icao()
    index: dict[str, Aerodrome] = {}
    with _CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                position = Position(float(row["lat"]), float(row["lon"]))
            except (ValueError, KeyError):
                continue  # ligne inexploitable : on saute plutôt que de propager
            index[row["icao"]] = Aerodrome(
                icao=row["icao"],
                name=row["name"],
                position=position,
                elevation_ft=int(float(row["elev_ft"])) if row["elev_ft"] else 0,
                runways=runways.get(row["icao"], ()),
            )
    return index


def lookup(icao: str) -> Aerodrome | None:
    return _load().get(icao.strip().upper())


def require(icao: str) -> Aerodrome:
    found = lookup(icao)
    if found is None:
        raise KeyError(f"aérodrome inconnu de la base : {icao}")
    return found


def nearest(
    position: Position, *, within_nm: float, limit: int = 10
) -> list[tuple[Aerodrome, float]]:
    """Aérodromes les plus proches, du plus près au plus loin, avec la distance.

    Sert à deux choses : proposer des dégagements, et trouver une station
    d'observation quand le terrain de départ n'en a pas — cas courant sur les
    petits terrains, où le METAR le plus proche est à 20 ou 30 NM.
    """
    scored = (
        (aerodrome, position.distance_nm(aerodrome.position)) for aerodrome in _load().values()
    )
    close = [pair for pair in scored if pair[1] <= within_nm]
    close.sort(key=lambda pair: pair[1])
    return close[:limit]


def all_aerodromes() -> Iterator[Aerodrome]:
    return iter(_load().values())
