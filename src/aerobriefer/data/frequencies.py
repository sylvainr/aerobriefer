"""Fréquences radio des aérodromes, dérivées d'OurAirports via `refdata`.

Pour le log de navigation : les radios pertinentes (TWR, APP, ATIS, AFIS, A/A…)
d'un terrain, lues par lookup OACI.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache

from . import refdata

#: Ordre d'intérêt pour le vol : tour/sol/appro/info/auto-info d'abord.
_PRIORITY = {"TWR": 0, "GND": 1, "APP": 2, "ATIS": 3, "AFIS": 4, "A/A": 5, "UNICOM": 6}


@dataclass(frozen=True, slots=True)
class Frequency:
    icao: str
    kind: str
    description: str
    freq_mhz: str


@lru_cache(maxsize=1)
def _by_icao() -> dict[str, tuple[Frequency, ...]]:
    index: dict[str, list[Frequency]] = {}
    with refdata.frequencies_csv().open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            index.setdefault(row["icao"], []).append(
                Frequency(
                    icao=row["icao"],
                    kind=row.get("type", ""),
                    description=row.get("description", ""),
                    freq_mhz=row.get("freq_mhz", ""),
                )
            )
    return {
        icao: tuple(sorted(freqs, key=lambda f: _PRIORITY.get(f.kind, 9)))
        for icao, freqs in index.items()
    }


def for_icao(icao: str) -> tuple[Frequency, ...]:
    return _by_icao().get(icao.strip().upper(), ())
