"""Déclinaison magnétique (WMM_2025, offline, déterministe)."""

from __future__ import annotations

from aerobriefer.data.magnetic import decimal_year, declination_deg
from aerobriefer.domain.geo import Position


def test_decimal_year():
    assert abs(decimal_year("2026-08-07") - 2026.60) < 0.05
    assert abs(decimal_year("2026-01-01") - 2026.0) < 0.02


def test_declinaison_sw_france_2026():
    # SW France ~ +1.3° Est en 2026 (valeur WMM_2025). Déterministe.
    value = declination_deg(Position(45.2, -0.8), year=2026.6)
    assert 1.0 <= value <= 1.7


def test_declinaison_positive_vers_est():
    # Marseille est plus à l'est → déclinaison Est plus forte que SW.
    ouest = declination_deg(Position(45.2, -0.8), year=2026.6)
    est = declination_deg(Position(43.3, 5.4), year=2026.6)
    assert est > ouest
