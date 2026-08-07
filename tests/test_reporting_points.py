"""Tests des points de report VFR : parsing pur + résolution sur la fixture."""

from __future__ import annotations

from aerobriefer.data import reporting_points
from aerobriefer.data.reporting_points import parse_rows

_CSV = (
    "VRP,LFBD/N,,44.95,-0.70,1000,,,Bordeaux Nord,LF,25,\n"
    "VRP,LFBD/NW,,44.98,-0.85,1000,,,Bordeaux Nord-Ouest,LF,25,\n"
    "GARBAGE,pas de slash,,,,\n"
)


def test_parse_rows_extrait_oaci_ident_position():
    points = parse_rows(_CSV)
    assert len(points) == 2  # la ligne sans « / » est ignorée
    by_ident = {p.ident: p for p in points}
    assert by_ident["N"].icao == "LFBD"
    assert by_ident["N"].name == "Bordeaux Nord"
    assert abs(by_ident["N"].position.lat - 44.95) < 0.01


def test_resolve_point_de_report_de_la_fixture():
    # La fixture régionale contient les points de LFBD (dont N = November).
    point = reporting_points.resolve("LFBD/N")
    assert point is not None
    assert point.icao == "LFBD" and point.ident == "N"


def test_resolve_inconnu_rend_none():
    assert reporting_points.resolve("LFCY/ZZ") is None


def test_for_icao_liste_les_points():
    pts = reporting_points.for_icao("LFBD")
    assert {p.ident for p in pts} >= {"N", "NW", "S"}
