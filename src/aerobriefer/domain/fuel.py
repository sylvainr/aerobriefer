"""Bilan carburant réglementaire VFR — pur, sans I/O.

Ce que le pilote doit emporter, décomposé : roulage + trajet + dégagement +
réserve finale. La réserve finale VFR jour est de 30 minutes (règle par défaut,
paramétrable). On compare le total requis à la capacité utile pour donner une
marge et une endurance.

Le carburant de TRAJET vient du navlog ; le reste se dérive du débit de croisière.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Réserve finale VFR jour (minutes).
DEFAULT_RESERVE_MIN = 30.0
#: Roulage/démarrage par défaut (minutes).
DEFAULT_TAXI_MIN = 10.0


@dataclass(frozen=True, slots=True)
class FuelPlan:
    taxi_l: float
    trip_l: float
    alternate_l: float
    reserve_l: float
    total_l: float
    """Carburant minimum à emporter (somme des postes)."""
    capacity_l: float
    margin_l: float
    """Capacité − total : marge au-dessus du minimum (négatif = INSUFFISANT)."""
    endurance_min: float
    """Autonomie totale à pleins (capacité / débit)."""

    @property
    def sufficient(self) -> bool:
        return self.margin_l >= 0.0


def fuel_plan(
    *,
    trip_l: float,
    fuel_flow_lph: float,
    capacity_l: float,
    taxi_min: float = DEFAULT_TAXI_MIN,
    alternate_min: float = 0.0,
    reserve_min: float = DEFAULT_RESERVE_MIN,
) -> FuelPlan:
    """Bilan carburant. `alternate_min` = temps estimé vers le dégagement (0 = sans)."""

    def litres(minutes: float) -> float:
        return fuel_flow_lph * minutes / 60.0

    taxi = litres(taxi_min)
    alternate = litres(alternate_min)
    reserve = litres(reserve_min)
    total = taxi + trip_l + alternate + reserve
    endurance = (capacity_l / fuel_flow_lph * 60.0) if fuel_flow_lph > 0 else 0.0
    return FuelPlan(
        taxi_l=taxi,
        trip_l=trip_l,
        alternate_l=alternate,
        reserve_l=reserve,
        total_l=total,
        capacity_l=capacity_l,
        margin_l=capacity_l - total,
        endurance_min=endurance,
    )
