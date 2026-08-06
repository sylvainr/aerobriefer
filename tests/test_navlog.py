"""Tests du calcul de navlog : triangle des vitesses + cumul temporel.

Le triangle est de la trigonométrie pure : on le teste sur des cas dont on
connaît la réponse à la main (sans vent, plein face, plein arrière, travers
pur). Le cumul (heures de passage, totaux) est vérifié sur une route simple.
"""

from __future__ import annotations

import math
from datetime import UTC, timedelta

import pytest

from aerobriefer.domain.geo import Position
from aerobriefer.domain.navlog import Wind, _wind_triangle, compute_navlog
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.domain.window import UtcDateTime


def _north_route() -> Route:
    """Deux branches plein nord (route vraie = 0°), ~60 NM chacune."""
    return Route(
        [
            Waypoint("A", Position(45.0, 0.0), altitude_ft=2000.0),
            Waypoint("B", Position(46.0, 0.0), altitude_ft=3000.0),
            Waypoint("C", Position(47.0, 0.0), altitude_ft=3000.0),
        ]
    )


def test_sans_vent_cap_egal_route_et_vitesse_sol_egale_tas():
    wca, gs, head = _wind_triangle(true_track_deg=90.0, tas_kt=120.0, wind=Wind(0.0, 0.0))
    assert wca == pytest.approx(0.0)
    assert gs == pytest.approx(120.0)
    assert head == pytest.approx(0.0)


def test_plein_vent_de_face_reduit_la_vitesse_sol():
    # Route au nord, vent DU nord (from 0) = plein face.
    wca, gs, head = _wind_triangle(true_track_deg=0.0, tas_kt=120.0, wind=Wind(0.0, 30.0))
    assert wca == pytest.approx(0.0)
    assert head == pytest.approx(30.0)  # composante de face positive
    assert gs == pytest.approx(90.0)


def test_plein_vent_arriere_augmente_la_vitesse_sol():
    # Route au nord, vent DU sud (from 180) = plein arrière.
    wca, gs, head = _wind_triangle(true_track_deg=0.0, tas_kt=120.0, wind=Wind(180.0, 30.0))
    assert wca == pytest.approx(0.0)
    assert head == pytest.approx(-30.0)  # arrière = face négative
    assert gs == pytest.approx(150.0)


def test_travers_pur_de_droite_corrige_a_droite():
    # Route au nord, vent DE l'est (from 90) = travers pur de la droite.
    wca, gs, head = _wind_triangle(true_track_deg=0.0, tas_kt=100.0, wind=Wind(90.0, 20.0))
    assert wca == pytest.approx(math.degrees(math.asin(0.2)), abs=1e-6)
    assert wca > 0  # correction vers la droite, face au vent
    assert head == pytest.approx(0.0, abs=1e-9)
    assert gs == pytest.approx(100.0 * math.cos(math.asin(0.2)), abs=1e-6)


def test_vent_de_face_superieur_a_la_tas_leve():
    with pytest.raises(ValueError, match="impraticable"):
        _wind_triangle(true_track_deg=0.0, tas_kt=20.0, wind=Wind(0.0, 25.0))


def test_travers_superieur_a_la_tas_leve():
    with pytest.raises(ValueError, match="supérieur à la TAS"):
        _wind_triangle(true_track_deg=0.0, tas_kt=20.0, wind=Wind(90.0, 25.0))


def test_cumul_heures_de_passage_et_totaux_sans_vent():
    dep = UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC)
    log = compute_navlog(
        _north_route(),
        departure_time=dep,
        tas_kt=120.0,
        winds=[Wind(0.0, 0.0), Wind(0.0, 0.0)],
        fuel_flow_lph=30.0,
    )
    assert len(log.legs) == 2
    # ~60 NM à 120 kt = 30 min par branche.
    assert log.legs[0].time == pytest.approx(timedelta(minutes=30), abs=timedelta(seconds=30))
    assert log.legs[0].eta == dep + log.legs[0].time
    assert log.legs[1].eta == log.legs[0].eta + log.legs[1].time
    assert log.eta_arrival == log.legs[1].eta
    assert log.total_time == log.legs[0].time + log.legs[1].time
    # 30 L/h × ~1 h = ~30 L.
    assert log.total_fuel_l == pytest.approx(30.0, abs=1.0)


def test_altitude_de_branche_est_la_moyenne_des_extremites():
    log = compute_navlog(
        _north_route(),
        departure_time=UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC),
        tas_kt=120.0,
        winds=[Wind(0.0, 0.0), Wind(0.0, 0.0)],
    )
    assert log.legs[0].altitude_ft == pytest.approx(2500.0)  # (2000 + 3000) / 2
    assert log.legs[1].altitude_ft == pytest.approx(3000.0)
    assert log.total_fuel_l is None  # pas de débit fourni


def test_declinaison_magnetique_decale_les_caps():
    log = compute_navlog(
        _north_route(),
        departure_time=UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC),
        tas_kt=120.0,
        winds=[Wind(0.0, 0.0), Wind(0.0, 0.0)],
        magnetic_variation_deg=2.0,  # 2° Est
    )
    # Route vraie 0° → route magnétique 358°.
    assert log.legs[0].magnetic_track_deg == pytest.approx(358.0)
    assert log.legs[0].magnetic_heading_deg == pytest.approx(358.0)


def test_un_vent_par_branche_obligatoire():
    with pytest.raises(ValueError, match="un par branche"):
        compute_navlog(
            _north_route(),
            departure_time=UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC),
            tas_kt=120.0,
            winds=[Wind(0.0, 0.0)],  # une seule pour deux branches
        )
