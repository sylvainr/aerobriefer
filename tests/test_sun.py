"""Tests des éphémérides solaires (lever/coucher, nuit aéronautique).

On teste des invariants sûrs (lever < coucher, nuit après coucher) et un ordre de
grandeur connu (Bordeaux en été), sans exiger la seconde près.
"""

from __future__ import annotations

from datetime import date, timedelta

from aerobriefer.domain.sun import AERONAUTICAL_NIGHT_OFFSET, sun_times


def test_lever_avant_coucher():
    s = sun_times(date(2026, 8, 7), 44.83, -0.70)  # Bordeaux
    assert s.sunrise is not None and s.sunset is not None
    assert s.sunrise < s.sunset


def test_coucher_bordeaux_ete_ordre_de_grandeur():
    # 07/08/2026 à Bordeaux : coucher vers ~19:2x UTC (≈ 21:2x locale CEST).
    s = sun_times(date(2026, 8, 7), 44.83, -0.70)
    assert s.sunset is not None
    assert 18 <= s.sunset.hour <= 20


def test_nuit_aeronautique_est_coucher_plus_30():
    s = sun_times(date(2026, 8, 7), 44.83, -0.70)
    assert s.aeronautical_night_start is not None and s.sunset is not None
    assert s.aeronautical_night_start - s.sunset == AERONAUTICAL_NIGHT_OFFSET


def test_jour_plus_long_plus_au_nord_en_ete():
    south = sun_times(date(2026, 6, 21), 43.0, 2.0)
    north = sun_times(date(2026, 6, 21), 50.0, 2.0)
    assert south.sunset is not None and north.sunset is not None
    # Plus au nord en été : coucher plus tard.
    assert north.sunset > south.sunset


def test_fin_de_nuit_est_lever_moins_30():
    s = sun_times(date(2026, 1, 15), 44.83, -0.70)
    assert s.aeronautical_night_end is not None and s.sunrise is not None
    assert s.sunrise - s.aeronautical_night_end == timedelta(minutes=30)
