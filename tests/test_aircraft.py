"""Tests du framework avion et de l'exemple DR400.

Les valeurs de référence viennent du manuel de vol DR400/160 (F-GGJY). On vérifie
d'abord que le modèle REPRODUIT le manuel aux points de grille (sinon tout le
reste est faux), puis l'interpolation, le clampage hors table, les corrections,
et le go/no-go piste.
"""

from __future__ import annotations

import pytest

from aerobriefer.aircraft.examples.dr400 import DR400_160
from aerobriefer.aircraft.model import (
    Conditions,
    isa_offset_c,
    isa_temperature_c,
    kmh_to_kt,
)
from aerobriefer.aircraft.runway import assess_runway
from aerobriefer.domain.models import Runway


@pytest.fixture
def dr400() -> DR400_160:
    return DR400_160()


def _isa(alt_ft: float) -> Conditions:
    """Conditions à l'ISA pour une altitude, masse max, béton, vent nul."""
    return Conditions(alt_ft, isa_temperature_c(alt_ft), 1050.0, surface="paved")


# --- helpers d'unités --------------------------------------------------------


def test_isa_temperature_and_offset():
    assert isa_temperature_c(0) == 15.0
    assert isa_temperature_c(4000) == 7.0
    assert isa_offset_c(0, 25.0) == 10.0  # 25 °C au sol = ISA+10


def test_kmh_to_kt():
    assert abs(kmh_to_kt(308.0) - 166.3) < 0.5


def test_speeds_converted_from_manual(dr400: DR400_160):
    assert round(dr400.speeds.vne_kt) == 166
    assert round(dr400.speeds.vfe_kt) == 92


# --- reproduction du manuel aux points de grille ----------------------------


def test_takeoff_grid_points_match_manual(dr400: DR400_160):
    """Cases exactes du manuel p.5.2 : (roulement, distance 15 m)."""
    # 1050 kg, 0 ft, ISA, béton → 330 / 620
    r = dr400.takeoff(Conditions(0, 15.0, 1050.0, surface="paved"))
    assert (r.ground_roll_m, r.over_15m_m) == (330, 620)
    # herbe → 435 / 745
    r = dr400.takeoff(Conditions(0, 15.0, 1050.0, surface="grass"))
    assert (r.ground_roll_m, r.over_15m_m) == (435, 745)
    # 850 kg, 8000 ft, ISA+20, béton → 400 / 790
    r = dr400.takeoff(Conditions(8000, isa_temperature_c(8000) + 20, 850.0, surface="paved"))
    assert (r.ground_roll_m, r.over_15m_m) == (400, 790)


def test_landing_grid_points_match_manual(dr400: DR400_160):
    """Cases exactes du manuel p.5.5."""
    # 1045 kg, 0 ft, ISA, béton (freinage modéré) → 250 / 545
    r = dr400.landing(Conditions(0, 15.0, 1045.0, surface="paved"))
    assert (r.ground_roll_m, r.over_15m_m) == (250, 545)
    # herbe (sans frein) → 375 / 670
    r = dr400.landing(Conditions(0, 15.0, 1045.0, surface="grass"))
    assert (r.ground_roll_m, r.over_15m_m) == (375, 670)


# --- interpolation -----------------------------------------------------------


def test_mass_interpolation_is_linear(dr400: DR400_160):
    """950 kg est à mi-chemin de 850 (395) et 1050 (620) → ≈ 508."""
    r = dr400.takeoff(Conditions(0, 15.0, 950.0, surface="paved"))
    assert r.over_15m_m == 508


def test_altitude_interpolation(dr400: DR400_160):
    """2000 ft entre 0 (620) et 4000 (840) à 1050 kg ISA → ≈ 730."""
    r = dr400.takeoff(Conditions(2000, isa_temperature_c(2000), 1050.0, surface="paved"))
    assert 725 <= r.over_15m_m <= 735


# --- clampage hors table (jamais d'extrapolation) ---------------------------


def test_above_table_is_clamped_and_flagged(dr400: DR400_160):
    r = dr400.takeoff(_isa(10000))  # au-dessus de 8000 ft
    # clampé à la valeur 8000 ft (1165), pas extrapolé au-delà
    assert r.over_15m_m == 1165
    assert any("HORS TABLE" in n for n in r.notes)


def test_within_table_is_not_flagged(dr400: DR400_160):
    r = dr400.takeoff(_isa(0))
    assert not any("HORS TABLE" in n for n in r.notes)


# --- corrections -------------------------------------------------------------


def test_headwind_shortens_distance(dr400: DR400_160):
    """Vent de face 20 kt → ×0.66 (manuel)."""
    calm = dr400.takeoff(Conditions(0, 15.0, 1050.0, surface="paved"))
    windy = dr400.takeoff(Conditions(0, 15.0, 1050.0, headwind_kt=20, surface="paved"))
    assert windy.over_15m_m == round(calm.over_15m_m * 0.66)
    assert any("vent" in n for n in windy.notes)


def test_tailwind_is_not_rewarded(dr400: DR400_160):
    """Un vent arrière ne raccourcit rien (marge de sécurité)."""
    calm = dr400.takeoff(Conditions(0, 15.0, 1050.0, surface="paved"))
    tail = dr400.takeoff(Conditions(0, 15.0, 1050.0, headwind_kt=-10, surface="paved"))
    assert tail.over_15m_m == calm.over_15m_m


# --- go/no-go piste ----------------------------------------------------------


def test_runway_assessment_go(dr400: DR400_160):
    """LFCY revêtue 1255 m, décollage 1050 kg à 25 °C : GO large."""
    rwy = Runway(ident="10/28", length_m=1255, surface="ASP", true_bearing_deg=101.0)
    c = Conditions(72, 25.0, 1050.0)
    a = assess_runway(dr400, rwy, c, operation="takeoff")
    assert a.ok
    assert a.required_m < a.available_m
    assert a.margin_m == a.available_m - a.required_m


def test_runway_assessment_nogo_on_short_strip(dr400: DR400_160):
    """Une piste courte doit sortir NO-GO."""
    short = Runway(ident="09/27", length_m=400, surface="herbe", true_bearing_deg=90.0)
    c = Conditions(0, 30.0, 1050.0)
    a = assess_runway(dr400, short, c, operation="takeoff")
    assert not a.ok
    assert a.margin_m < 0


def test_assessment_uses_runway_surface_not_conditions(dr400: DR400_160):
    """La perf herbe s'applique sur une piste herbe même si les conditions
    disent 'paved' — on ne calcule pas une perf béton sur de l'herbe."""
    grass = Runway(ident="10/28", length_m=1000, surface="herbe", true_bearing_deg=101.0)
    paved = Runway(ident="10/28", length_m=1000, surface="ASP", true_bearing_deg=101.0)
    c = Conditions(72, 25.0, 1050.0, surface="paved")
    on_grass = assess_runway(dr400, grass, c, operation="takeoff")
    on_paved = assess_runway(dr400, paved, c, operation="takeoff")
    assert on_grass.required_m > on_paved.required_m


def test_safety_factor_increases_required(dr400: DR400_160):
    rwy = Runway(ident="10/28", length_m=1255, surface="ASP", true_bearing_deg=101.0)
    c = Conditions(72, 25.0, 1050.0)
    raw = assess_runway(dr400, rwy, c, operation="takeoff", safety_factor=1.0)
    safe = assess_runway(dr400, rwy, c, operation="takeoff", safety_factor=1.3)
    assert safe.required_m > raw.required_m
    assert any("sécurité" in n for n in safe.notes)
