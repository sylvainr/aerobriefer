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


def test_airspace_mapping_openaip():
    from aerobriefer.data.airspace import _from_openaip

    feat = {
        "name": "CTR BORDEAUX",
        "type": 4,  # CTR
        "icaoClass": 3,  # D
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-0.7, 44.8], [-0.6, 44.8], [-0.6, 44.9]]],
        },
        "lowerLimit": {"value": 0, "unit": 1, "referenceDatum": 0},  # SFC
        "upperLimit": {"value": 2000, "unit": 1, "referenceDatum": 1},  # 2000 ft MSL
        "frequencies": [
            {"value": "118.300", "primary": True, "name": "BORDEAUX TWR"},
            {"value": "121.500", "primary": False, "name": "EMERGENCY"},
        ],
    }
    a = _from_openaip(feat)
    assert a is not None
    assert a.airspace_type == "CTR"
    assert a.airspace_class == "D"
    assert a.lower.label == "SFC"
    assert a.upper.label == "2000 ft AMSL"
    assert a.frequency == "118.300"  # la primaire


def test_airspace_mapping_flight_level():
    from aerobriefer.data.airspace import _from_openaip

    feat = {
        "name": "CTA BORDEAUX",
        "type": 26,
        "icaoClass": 8,  # non classé → UNC
        "geometry": {"type": "Polygon", "coordinates": [[[0, 45], [1, 45], [1, 46]]]},
        "lowerLimit": {"value": 145, "unit": 6, "referenceDatum": 2},  # FL145
        "upperLimit": {"value": 195, "unit": 6, "referenceDatum": 2},  # FL195
    }
    a = _from_openaip(feat)
    assert a is not None
    assert a.airspace_class == "UNC"
    assert a.lower.label == "FL145"
    assert a.frequency is None
