"""Déclinaison magnétique (variation) via le World Magnetic Model 2025, OFFLINE.

Déterministe, aucune IA, aucun réseau : le modèle WMM_2025 (coefficients +
harmoniques sphériques) est fourni par `pygeomag`, valable 2025-2030. Sert à
convertir routes/caps vrais → magnétiques dans le log de navigation, au lieu de
faire saisir la déclinaison à la main (elle se déduit de la position + la date).

Convention : positif vers l'EST (comme partout dans le domaine).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pygeomag import GeoMag  # type: ignore[import-untyped]

from ..domain.geo import Position

_COF = "WMM_2025.COF"  # 2025-2030 ; le modèle par défaut de pygeomag est plus ancien


@lru_cache(maxsize=1)
def _model() -> GeoMag:
    import pygeomag

    cof = Path(pygeomag.__file__).parent / "wmm" / _COF
    if not cof.exists():
        raise FileNotFoundError(f"coefficients WMM introuvables : {cof}")
    return GeoMag(coefficients_file=str(cof))


def decimal_year(date_iso: str) -> float:
    """« AAAA-MM-JJ » → année décimale (ex. 2026-08-07 ≈ 2026.60).

    Précision au mois : la déclinaison varie lentement (~0,1°/an), le jour exact
    est sans effet. Volontairement sans `datetime` (banni : TID251)."""
    year, month, day = (int(part) for part in date_iso.split("-"))
    day_of_year = (month - 1) * 30.4 + day
    return year + day_of_year / 365.25


def declination_deg(position: Position, *, year: float) -> float:
    """Déclinaison magnétique (° Est positif) à une position et une année décimale."""
    result = _model().calculate(glat=position.lat, glon=position.lon, alt=0, time=year)
    return round(float(result.d), 1)
