"""Tests du calcul masse & centrage (pur) et du contrat de données de la page.

Le calcul de bras et le test d'enveloppe sont la logique sûre ; le JS de la page
les reproduit, donc ils doivent être justes ici. On vérifie sur des chargements
dont on connaît la réponse à la main, plus l'appartenance à l'enveloppe.
"""

from __future__ import annotations

import pytest

from aerobriefer.aircraft.examples.dr400 import DR400_160
from aerobriefer.aircraft.model import FuelTank, Station, WeightBalance
from aerobriefer.render.massbalance import massbalance_data


def _simple_wb() -> WeightBalance:
    # Enveloppe rectangulaire simple : bras 0.20–0.35 m, masse 400–1000 kg.
    return WeightBalance(
        empty_mass_kg=600.0,
        empty_arm_m=0.30,
        stations=(Station(name="Avant", arm_m=0.40, max_kg=200.0),),
        fuel=FuelTank(arm_m=1.0, capacity_l=100.0, density_kg_per_l=0.72),
        envelope=((0.20, 400.0), (0.20, 1000.0), (0.35, 1000.0), (0.35, 400.0)),
        max_mass_kg=1000.0,
    )


def test_masse_et_bras_a_vide_sans_chargement():
    wb = _simple_wb()
    st = wb.state({}, fuel_l=0.0)
    assert st.mass_kg == pytest.approx(600.0)
    assert st.arm_m == pytest.approx(0.30)
    assert st.within_envelope is True


def test_moment_additionne_postes_et_carburant():
    wb = _simple_wb()
    # 600 kg @0.30 + 100 kg @0.40 + (50 L × 0.72 = 36 kg) @1.0
    st = wb.state({"Avant": 100.0}, fuel_l=50.0)
    mass = 600.0 + 100.0 + 36.0
    moment = 600.0 * 0.30 + 100.0 * 0.40 + 36.0 * 1.0
    assert st.mass_kg == pytest.approx(mass)
    assert st.arm_m == pytest.approx(moment / mass)


def test_hors_enveloppe_si_bras_trop_arriere():
    wb = _simple_wb()
    # Beaucoup de carburant très en arrière (bras 1.0) → CG au-delà de 0.35 m.
    st = wb.state({}, fuel_l=100.0)
    assert st.arm_m > 0.35
    assert st.within_envelope is False


def test_contrat_de_donnees_de_la_page_dr400():
    data = massbalance_data(DR400_160())
    assert data["is_placeholder"] is True  # DR400 d'exemple = valeurs fictives
    assert data["max_mass_kg"] == 1050.0
    assert {s["name"] for s in data["stations"]} == {"Sièges avant", "Sièges arrière", "Bagages"}
    assert data["fuel"]["capacity_l"] == 110.0
    assert len(data["envelope"]) >= 3
    assert all(len(point) == 2 for point in data["envelope"])
