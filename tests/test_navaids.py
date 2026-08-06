"""Tests navaids (VOR) + fréquences, sur la fixture de référence.

La fixture porte les navaids et fréquences réels de la région (générés depuis
OurAirports). On vérifie la lecture, la recherche du VOR le plus proche et le
calcul de radiale, plus la lecture des fréquences d'un terrain.
"""

from __future__ import annotations

import math

import pytest

from aerobriefer.data import airports, frequencies, navaids
from aerobriefer.domain.geo import Position


def test_vor_le_plus_proche_de_bordeaux():
    lfbd = airports.require("LFBD").position
    found = navaids.nearest(lfbd, within_nm=40)
    assert found is not None
    navaid, distance = found
    assert navaid.kind in {"VOR", "VOR-DME", "VORTAC"}
    assert distance < 40


def test_radiale_est_un_relevement_depuis_le_vor():
    # VOR fictif à (45.0, 0.0) ; point plein est → radiale ~090, plein nord ~360/0.
    vor = navaids.Navaid("TST", "Test", "VOR", "113.0", Position(45.0, 0.0), None)
    east = Position(45.0, 0.5)
    radial, dist = navaids.radial_and_distance(vor, east)
    assert radial == pytest.approx(90.0, abs=1.0)
    assert dist > 0
    north = Position(45.5, 0.0)
    radial_n, _ = navaids.radial_and_distance(vor, north)
    assert radial_n == pytest.approx(0.0, abs=1.0) or radial_n == pytest.approx(360.0, abs=1.0)


def test_declinaison_decale_la_radiale():
    vor = navaids.Navaid("TST", "Test", "VOR", "113.0", Position(45.0, 0.0), None)
    east = Position(45.0, 0.5)
    plain, _ = navaids.radial_and_distance(vor, east, magnetic_variation_deg=0.0)
    shifted, _ = navaids.radial_and_distance(vor, east, magnetic_variation_deg=2.0)
    assert shifted == pytest.approx((plain - 2.0) % 360.0, abs=1e-6)


def test_frequences_d_un_terrain():
    # Au moins un terrain de la fixture porte des fréquences.
    with_freq = [a.icao for a in airports.all_aerodromes() if frequencies.for_icao(a.icao)]
    assert with_freq, "la fixture doit contenir des fréquences"
    freqs = frequencies.for_icao(with_freq[0])
    assert all(f.freq_mhz for f in freqs)


def test_bearing_coherent_avec_domaine():
    a = Position(45.0, 0.0)
    b = Position(46.0, 0.0)  # plein nord
    assert math.degrees(a.bearing_to(b)) % 360.0 == pytest.approx(0.0, abs=0.5)
