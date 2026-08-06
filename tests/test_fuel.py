"""Tests du bilan carburant réglementaire (pur)."""

from __future__ import annotations

import pytest

from aerobriefer.domain.fuel import fuel_plan


def test_somme_des_postes_et_marge():
    # 30 L/h : roulage 10 min = 5 L, réserve 30 min = 15 L.
    plan = fuel_plan(trip_l=18.0, fuel_flow_lph=30.0, capacity_l=110.0)
    assert plan.taxi_l == pytest.approx(5.0)
    assert plan.reserve_l == pytest.approx(15.0)
    assert plan.trip_l == pytest.approx(18.0)
    assert plan.total_l == pytest.approx(5.0 + 18.0 + 0.0 + 15.0)
    assert plan.margin_l == pytest.approx(110.0 - 38.0)
    assert plan.sufficient is True


def test_degagement_ajoute_du_carburant():
    sans = fuel_plan(trip_l=18.0, fuel_flow_lph=30.0, capacity_l=110.0)
    avec = fuel_plan(trip_l=18.0, fuel_flow_lph=30.0, capacity_l=110.0, alternate_min=20.0)
    assert avec.alternate_l == pytest.approx(10.0)  # 20 min à 30 L/h
    assert avec.total_l == pytest.approx(sans.total_l + 10.0)


def test_insuffisant_si_trajet_depasse_la_capacite():
    plan = fuel_plan(trip_l=100.0, fuel_flow_lph=30.0, capacity_l=110.0)
    assert plan.margin_l < 0
    assert plan.sufficient is False


def test_endurance_capacite_sur_debit():
    plan = fuel_plan(trip_l=10.0, fuel_flow_lph=30.0, capacity_l=90.0)
    assert plan.endurance_min == pytest.approx(180.0)  # 90 / 30 h = 3 h
