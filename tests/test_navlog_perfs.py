"""Test des perfs piste intégrées au navlog (décollage départ / atterrissage arrivée)."""

from __future__ import annotations

from aerobriefer.aircraft.examples.dr400 import DR400_160
from aerobriefer.navlog_build import runway_performance


def test_perfs_depart_et_arrivee():
    perfs = runway_performance(DR400_160(), "LFCY", "LFBD")
    # LFCY et LFBD ont des pistes connues dans la fixture → deux verdicts.
    assert len(perfs) == 2
    takeoff, landing = perfs
    assert takeoff.label.startswith("Décollage LFCY")
    assert landing.label.startswith("Atterrissage LFBD")
    for p in perfs:
        assert p.available_m > 0
        assert p.required_m > 0
        assert isinstance(p.ok, bool)
        # Bordeaux (grande piste) : l'atterrissage DR400 passe largement.
    assert landing.ok is True


def test_terrain_sans_piste_est_ignore():
    # Un code inconnu → pas de piste → pas de verdict (pas d'exception).
    perfs = runway_performance(DR400_160(), "LFCY", "ZZZZ")
    assert all("ZZZZ" not in p.label for p in perfs)
