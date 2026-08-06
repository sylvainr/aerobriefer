"""Tests des helpers PURS d'élévation (échantillonnage, marge, arrondi Zmin).

Le fetch réseau n'est pas testé ici ; on vérifie la logique qui transforme un
relief en altitude de sécurité, et l'échantillonnage d'une branche.
"""

from __future__ import annotations

import pytest

from aerobriefer.data.elevation import sample_positions, zmin_ft
from aerobriefer.domain.geo import Position


def test_zmin_ajoute_1000ft_et_arrondit_aux_100():
    # 300 m ≈ 984 ft ; +1000 = 1984 → arrondi 2000.
    assert zmin_ft(300.0) == 2000.0
    # 0 m → +1000 → 1000.
    assert zmin_ft(0.0) == 1000.0


def test_zmin_marge_personnalisable():
    assert zmin_ft(0.0, margin_ft=2000.0) == 2000.0


def test_echantillonnage_inclut_les_extremites():
    start, end = Position(45.0, 0.0), Position(46.0, 0.0)  # ~60 NM
    points = sample_positions(start, end, spacing_nm=5.0)
    assert points[0].lat == pytest.approx(45.0)
    assert points[-1].lat == pytest.approx(46.0)
    assert len(points) >= 12  # ~60 NM / 5 NM


def test_echantillonnage_branche_courte_au_moins_trois_points():
    points = sample_positions(Position(45.0, 0.0), Position(45.05, 0.0), spacing_nm=5.0)
    assert len(points) >= 3
