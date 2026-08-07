"""Placement du marqueur « briefing d'arrivée » : ≥ 15 min avant l'atterrissage."""

from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

from aerobriefer.domain.geo import Position
from aerobriefer.domain.navlog import Wind, compute_navlog
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.domain.window import UtcDateTime
from aerobriefer.render.navlog import _rows

_TZ = ZoneInfo("Europe/Paris")


def _navlog(distances_nm: list[float], tas_kt: float = 60.0):
    # 60 kt → 1 NM = 1 min ; 1° de latitude ≈ 60 NM. Les distances valent donc
    # directement des minutes, et chaque branche monte plein nord.
    lats = [45.0]
    for d in distances_nm:
        lats.append(lats[-1] + d / 60.0)
    pts = [Waypoint(f"P{i}", Position(lat, 0.0), altitude_ft=2000.0) for i, lat in enumerate(lats)]
    return compute_navlog(
        Route(pts),
        departure_time=UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC),
        tas_kt=tas_kt,
        winds=[Wind(0.0, 0.0)] * len(distances_nm),
        fuel_flow_lph=30.0,
    )


def _brief_index(rows):
    return next(i for i, r in enumerate(rows) if r.get("brief_before"))


def test_derniere_leg_longue_marqueur_avant_derniere():
    # 2 branches de ~20 min : le briefing va avant la DERNIÈRE (≥ 15 min restants).
    rows = _rows(_navlog([20.0, 20.0]), _TZ, None, None, "kt")
    assert _brief_index(rows) == 1
    assert rows[1]["brief_remaining_min"] >= 15


def test_derniere_leg_courte_reporte_avant():
    # Dernière branche 5 min (< 15) : reportée à la branche d'avant.
    rows = _rows(_navlog([20.0, 20.0, 5.0]), _TZ, None, None, "kt")
    assert _brief_index(rows) == 1  # pas la dernière (index 2)
    assert rows[1]["brief_remaining_min"] >= 15


def test_vol_court_marqueur_au_debut():
    # Vol total < 15 min : briefing dès le départ.
    rows = _rows(_navlog([6.0, 5.0]), _TZ, None, None, "kt")
    assert _brief_index(rows) == 0
