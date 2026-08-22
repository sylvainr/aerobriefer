"""Test de l'export GPX (pur, sans réseau)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from aerobriefer.domain.geo import Position
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.gpx import route_to_gpx

_NS = {"g": "http://www.topografix.com/GPX/1/1"}


def _route() -> Route:
    return Route(
        waypoints=(
            Waypoint(name="LFCY", position=Position(lat=45.68, lon=-1.02), altitude_ft=None),
            Waypoint(
                name="Tvs-LFDN & <Rochefort>",
                position=Position(lat=45.82924, lon=-0.85744),
                altitude_ft=2500,
            ),
            Waypoint(
                name="LFBN", position=Position(lat=46.313477, lon=-0.394529), altitude_ft=1200
            ),
        )
    )


def test_gpx_est_un_xml_valide_avec_un_rtept_par_point() -> None:
    xml = route_to_gpx(_route(), title="LFCY → LFBN")
    root = ET.fromstring(xml)  # lève si XML invalide
    rtepts = root.findall("g:rte/g:rtept", _NS)
    assert len(rtepts) == 3
    # Coordonnées présentes et exactes sur le premier point.
    assert rtepts[0].get("lat") == "45.680000"
    assert rtepts[0].get("lon") == "-1.020000"
    names = [pt.find("g:name", _NS).text for pt in rtepts]
    assert names[0] == "LFCY" and names[-1] == "LFBN"


def test_gpx_convertit_altitude_en_metres_et_echappe_le_xml() -> None:
    xml = route_to_gpx(_route(), title="LFCY → LFBN")
    root = ET.fromstring(xml)
    rtepts = root.findall("g:rte/g:rtept", _NS)
    # 2500 ft → 762.0 m ; 1200 ft → 365.8 m ; 1er point sans altitude → pas de <ele>.
    assert rtepts[0].find("g:ele", _NS) is None
    assert rtepts[1].find("g:ele", _NS).text == "762.0"
    assert rtepts[2].find("g:ele", _NS).text == "365.8"
    # Le nom contenant & et < > est bien échappé (le parse XML l'aurait sinon cassé).
    assert rtepts[1].find("g:name", _NS).text == "Tvs-LFDN & <Rochefort>"
