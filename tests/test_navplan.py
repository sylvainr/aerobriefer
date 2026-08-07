"""Tests du parsing STRICT des fichiers de navigation (Pydantic).

Le fichier valide se charge ; au moindre écart (champ inconnu, type, borne,
format, coordonnées dépareillées, aucun point) le chargement CASSE.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aerobriefer.navplan import NavPlan, load_navplan

_VALID = {
    "titre": "LFCY → LFBD",
    "date": "2026-08-07",
    "heure_locale": "15:00",
    "duree_h": 3.0,
    "fuseau": "Europe/Paris",
    "aeronef": "DR400",
    "declinaison_deg": 1.0,
    "demi_couloir_nm": 10.0,
    "depart": "LFCY",
    "points": [
        {"nom": "MARENNES", "lat": 45.82, "lon": -1.10, "altitude_ft": 2000},
        {"nom": "LFBD", "altitude_ft": 2500},
    ],
}


def _plan(**overrides: object) -> NavPlan:
    return NavPlan.model_validate({**_VALID, **overrides})


def test_navigation_valide_et_route_reconstruite():
    plan = _plan()
    assert plan.title == "LFCY → LFBD"
    assert plan.depart == "LFCY"
    assert plan.route_spec == "MARENNES:45.82/-1.1@2000,LFBD@2500"
    assert plan.heure == "15:00" and plan.zone == "Europe/Paris"


def test_champ_inconnu_rejete():
    with pytest.raises(ValidationError):
        _plan(vitesse_secrete=42)  # champ non prévu → extra=forbid


def test_date_mal_formee_rejetee():
    with pytest.raises(ValidationError):
        _plan(date="07/08/2026")


def test_heure_mal_formee_rejetee():
    with pytest.raises(ValidationError):
        _plan(heure_locale="15h00")


def test_depart_non_oaci_rejete():
    with pytest.raises(ValidationError):
        _plan(depart="Royan")


def test_duree_hors_bornes_rejetee():
    with pytest.raises(ValidationError):
        _plan(duree_h=0.0)
    with pytest.raises(ValidationError):
        _plan(duree_h=99.0)


def test_aucun_point_rejete():
    with pytest.raises(ValidationError):
        _plan(points=[])


def test_coordonnees_depareillees_rejetees():
    with pytest.raises(ValidationError):
        _plan(points=[{"nom": "X", "lat": 45.0}])  # lat sans lon


def test_type_faux_rejete():
    with pytest.raises(ValidationError):
        _plan(duree_h="trois heures")


def test_load_navplan_depuis_fichier(tmp_path):
    path = tmp_path / "nav.json"
    path.write_text(json.dumps(_VALID), encoding="utf-8")
    plan = load_navplan(path)
    assert plan.depart == "LFCY"
