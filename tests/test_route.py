"""Tests de la route de navigation (points tournants + altitudes)."""

from __future__ import annotations

import pytest

from aerobriefer.domain.geo import Corridor, Position
from aerobriefer.domain.route import Route, Waypoint


def _wp(name, lat, lon, alt=None):
    return Waypoint(name=name, position=Position(lat, lon), altitude_ft=alt)


def test_route_requires_two_points():
    with pytest.raises(ValueError):
        Route([_wp("A", 45.0, 0.0)])


def test_legs_distance_and_track():
    # LFCY (Royan) → LFBN (Niort), ~48 NM cap ~30°
    lfcy = _wp("LFCY", 45.628, -0.9725)
    lfbn = _wp("LFBN", 46.313, -0.394, 2500)
    route = Route([lfcy, lfbn])
    (leg,) = route.legs()
    assert 45 < leg.distance_nm < 50
    assert 20 < leg.true_track_deg < 45  # vers le nord-est
    assert route.total_distance_nm() == pytest.approx(leg.distance_nm)


def test_multi_leg_total_distance():
    route = Route([_wp("A", 45.0, 0.0), _wp("B", 45.5, 0.0), _wp("C", 46.0, 0.0)])
    legs = route.legs()
    assert len(legs) == 2
    assert route.total_distance_nm() == pytest.approx(sum(leg.distance_nm for leg in legs))
    # plein nord
    assert all(abs(leg.true_track_deg) < 1 or abs(leg.true_track_deg - 360) < 1 for leg in legs)


def test_corridor_geometry_from_route():
    route = Route([_wp("A", 45.0, -1.0), _wp("B", 46.0, -1.0)])
    corridor = route.corridor(10.0)
    assert isinstance(corridor, Corridor)
    # un point à ~5 NM de l'axe est dans le couloir de demi-largeur 10
    assert corridor.contains(Position(45.5, -0.9))


def test_parse_route_cli():
    from aerobriefer.cli import parse_route

    route = parse_route("LFBN@2500", default_first="LFCY")
    assert [w.name for w in route.waypoints] == ["LFCY", "LFBN"]
    assert route.waypoints[1].altitude_ft == 2500.0


def test_parse_route_with_coordinates():
    from aerobriefer.cli import parse_route

    route = parse_route("LFCY,46.0,-0.5@3000,LFBN")
    names = [w.name for w in route.waypoints]
    assert names[0] == "LFCY" and names[-1] == "LFBN"
    # le point coordonnées est bien reconstitué
    mid = route.waypoints[1]
    assert abs(mid.position.lat - 46.0) < 0.01 and abs(mid.position.lon + 0.5) < 0.01
    assert mid.altitude_ft == 3000.0
