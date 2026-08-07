"""Tests du contrat de données du bilan carburant (pur, sans réseau).

Le JS de la page recompose le carburant requis (total minutes → litres → kg) à
partir de ces données : temps de vol total issu du navlog, conso/densité/capacité
du modèle de l'avion.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from aerobriefer.aircraft.examples.dr400 import DR400_160
from aerobriefer.domain.geo import Position
from aerobriefer.domain.navlog import Wind, compute_navlog
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.domain.window import UtcDateTime
from aerobriefer.render.fuelbalance import fuelbalance_data


def _navlog():
    """Deux branches plein nord (route vraie 0°), ~60 NM chacune, sans vent."""
    route = Route(
        [
            Waypoint("LFAA", Position(45.0, 0.0), altitude_ft=2000.0),
            Waypoint("LFBB", Position(46.0, 0.0), altitude_ft=3000.0),
            Waypoint("LFCC", Position(47.0, 0.0), altitude_ft=3000.0),
        ]
    )
    return compute_navlog(
        route,
        departure_time=UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC),
        tas_kt=120.0,
        winds=[Wind(0.0, 0.0), Wind(0.0, 0.0)],
        fuel_flow_lph=30.0,
    )


def test_expose_conso_densite_et_capacite_de_l_avion():
    aircraft = DR400_160()
    data = fuelbalance_data(_navlog(), aircraft)
    assert data["fuel_flow_lph"] == aircraft.cruise_fuel_lph
    assert data["density_kg_per_l"] == aircraft.weight_balance.fuels[0].density_kg_per_l
    expected_capacity = sum(t.capacity_l for t in aircraft.weight_balance.fuels)
    assert data["capacity_l"] == pytest.approx(expected_capacity)
    assert data["capacity_l"] > 0.0


def test_temps_de_vol_total_depuis_le_navlog():
    data = fuelbalance_data(_navlog(), DR400_160())
    # 2 branches de ~60 NM à 120 kt = ~30 min chacune → ~60 min au total.
    assert data["flight_min"] == pytest.approx(60, abs=2)


def test_nom_avion():
    data = fuelbalance_data(_navlog(), DR400_160())
    assert "F-ZZZZ" in data["aircraft"]
