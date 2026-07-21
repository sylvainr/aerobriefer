"""Go/no-go longueur de piste : croise un avion, une piste et des conditions.

C'est le déblocage promis : pour un décollage ou un atterrissage donné, comparer
la distance REQUISE (calculée par le modèle avion aux conditions du moment) à la
longueur DISPONIBLE de la piste, et rendre un verdict lisible.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import Runway
from .model import Aircraft, Conditions, DistanceResult, Surface


@dataclass(frozen=True, slots=True)
class RunwayAssessment:
    operation: str  # "décollage" | "atterrissage"
    runway_ident: str
    available_m: int
    required_m: int
    """Distance pour franchir 15 m, corrigée des conditions, × facteur de sécurité."""
    margin_m: int
    margin_pct: float
    ok: bool
    result: DistanceResult
    safety_factor: float
    notes: tuple[str, ...]


def surface_of(runway: Runway) -> Surface:
    """Surface du domaine → surface du modèle avion (paved/grass)."""
    return "grass" if runway.is_paved is False else "paved"


def assess_runway(
    aircraft: Aircraft,
    runway: Runway,
    conditions: Conditions,
    *,
    operation: str = "takeoff",
    safety_factor: float = 1.0,
) -> RunwayAssessment:
    """Verdict pour une piste.

    `safety_factor` majore la distance requise (le pilote applique SA marge — la
    réglementation ou le club imposent souvent un coefficient ; par défaut 1.0,
    la distance certifiée brute). La surface prise en compte est celle de la
    PISTE, pas celle passée dans `conditions` — on ne calcule pas une perf herbe
    sur une piste revêtue.
    """
    surface = surface_of(runway)
    effective = Conditions(
        pressure_altitude_ft=conditions.pressure_altitude_ft,
        temperature_c=conditions.temperature_c,
        mass_kg=conditions.mass_kg,
        headwind_kt=conditions.headwind_kt,
        surface=surface,
        slope_pct=conditions.slope_pct,
    )
    result: DistanceResult = (
        aircraft.takeoff(effective) if operation == "takeoff" else aircraft.landing(effective)
    )
    required = round(result.over_15m_m * safety_factor)
    available = runway.length_m
    margin = available - required
    label = "décollage" if operation == "takeoff" else "atterrissage"

    notes = list(result.notes)
    if safety_factor != 1.0:
        notes.append(f"facteur de sécurité ×{safety_factor:.2f}")

    return RunwayAssessment(
        operation=label,
        runway_ident=runway.ident,
        available_m=available,
        required_m=required,
        margin_m=margin,
        margin_pct=round(100.0 * margin / available, 1) if available else 0.0,
        ok=required <= available,
        result=result,
        safety_factor=safety_factor,
        notes=tuple(notes),
    )
