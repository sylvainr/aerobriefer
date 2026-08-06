"""Tests de l'interpolation du vent en altitude (pur, sans réseau).

On fabrique un bloc `hourly` type Open-Meteo et on vérifie l'interpolation par
hauteur géopotentielle, le clamp aux extrêmes, et le passage vectoriel qui évite
le saut de direction autour de 360°.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from aerobriefer.data.winds import interpolate_wind
from aerobriefer.domain.window import UtcDateTime

WHEN = UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC)


def _hourly(**levels: tuple[float, float, float]) -> dict[str, list]:
    """Construit un bloc horaire à une échéance. `levels` : hPa -> (hauteur_m, dir, force)."""
    block: dict[str, list] = {"time": ["2026-08-07T13:00"]}
    for level, (height, direction, speed) in levels.items():
        block[f"geopotential_height_{level}hPa"] = [height]
        block[f"wind_direction_{level}hPa"] = [direction]
        block[f"wind_speed_{level}hPa"] = [speed]
    return block


def test_interpolation_lineaire_entre_deux_niveaux():
    # 925 hPa à 500 m (10 kt), 850 hPa à 1500 m (30 kt), même direction.
    hourly = _hourly(**{"925": (500.0, 90.0, 10.0), "850": (1500.0, 90.0, 30.0)})
    # 1000 m ≈ 3281 ft → à mi-hauteur → 20 kt.
    wind = interpolate_wind(hourly, altitude_ft=1000.0 / 0.3048, when=WHEN)
    assert wind.from_deg == pytest.approx(90.0, abs=0.1)
    assert wind.speed_kt == pytest.approx(20.0, abs=0.2)


def test_clamp_sous_le_plus_bas_niveau():
    hourly = _hourly(**{"925": (500.0, 200.0, 12.0), "850": (1500.0, 210.0, 18.0)})
    wind = interpolate_wind(hourly, altitude_ft=0.0, when=WHEN)  # sous 500 m
    assert wind.from_deg == pytest.approx(200.0, abs=0.1)
    assert wind.speed_kt == pytest.approx(12.0, abs=0.1)


def test_clamp_au_dessus_du_plus_haut_niveau():
    hourly = _hourly(**{"925": (500.0, 200.0, 12.0), "850": (1500.0, 210.0, 18.0)})
    wind = interpolate_wind(hourly, altitude_ft=20000.0, when=WHEN)  # au-dessus de 1500 m
    assert wind.from_deg == pytest.approx(210.0, abs=0.1)
    assert wind.speed_kt == pytest.approx(18.0, abs=0.1)


def test_passage_vectoriel_autour_de_360():
    # 350° et 010° encadrent 000°, pas 180° : l'interpolation vectorielle doit
    # donner ~000°, jamais ~180° (ce que ferait une moyenne scalaire naïve).
    hourly = _hourly(**{"925": (500.0, 350.0, 20.0), "850": (1500.0, 10.0, 20.0)})
    wind = interpolate_wind(hourly, altitude_ft=1000.0 / 0.3048, when=WHEN)
    assert wind.from_deg == pytest.approx(0.0, abs=0.5) or wind.from_deg == pytest.approx(
        360.0, abs=0.5
    )
    assert wind.speed_kt == pytest.approx(19.7, abs=0.5)
