"""Test du parsing OpenAIP (pur, sans réseau)."""

from __future__ import annotations

from aerobriefer.data.openaip import _to_point


def test_to_point_geojson_lon_lat():
    p = _to_point({"name": "S", "compulsory": True, "geometry": {"coordinates": [-0.99, 45.57]}})
    assert p is not None
    assert p.ident == "S"
    assert p.compulsory is True
    assert round(p.position.lat, 2) == 45.57
    assert round(p.position.lon, 2) == -0.99


def test_to_point_ignore_malforme():
    assert _to_point({"name": "X"}) is None
    assert _to_point({"geometry": {"coordinates": [1, 2]}}) is None
