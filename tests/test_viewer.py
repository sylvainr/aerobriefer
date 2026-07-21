"""Tests du pipeline viewer.

Depuis l'enrichissement « région », `viewer_data` puise les aérodromes et espaces
dans la donnée de RÉFÉRENCE (fixture, via conftest), pas dans le package : le
viewer montre toute la région, pas seulement la zone filtrée du briefing. Le
rendu WebGL lui-même n'est pas testable en unitaire — on teste structure et
injection déterministe.
"""

from __future__ import annotations

from aerobriefer.assemble import assemble_briefing
from aerobriefer.cli import build_context
from aerobriefer.domain.geo import Position
from aerobriefer.render.viewer import CLASS_COLORS, render_viewer, viewer_data


def _package(icao: str = "LFCY", radius_nm: float = 20.0):
    ctx = build_context(icao, date="2026-07-21", heure="10:00", duree_h=3, rayon_nm=radius_nm)
    return assemble_briefing(ctx, [])


def test_viewer_shows_whole_region_not_just_flight():
    """Le viewer montre TOUS les aérodromes et espaces alentour, pas juste LFCY."""
    data = viewer_data(_package())
    aeros = data["flight"]["aerodromes"]
    icaos = {a["icao"] for a in aeros}
    assert "LFCY" in icaos
    assert len(aeros) > 3, "la région doit contenir plusieurs terrains, pas que le départ"
    lfcy = next(a for a in aeros if a["icao"] == "LFCY")
    assert lfcy["is_flight_aerodrome"] is True
    assert lfcy["runways"], "LFCY doit porter ses pistes (dont l'herbe SIA)"
    assert data["airspaces"], "des espaces doivent entourer LFCY"


def test_viewer_data_airspace_fields():
    data = viewer_data(_package())
    a = data["airspaces"][0]
    for key in ("name", "class", "type", "color", "lower_ft", "upper_ft", "polygon"):
        assert key in a
    assert a["color"] == CLASS_COLORS.get(a["class"], "#888888")
    lon, lat = a["polygon"][0]
    assert -10 < lon < 10 and 40 < lat < 52


def test_render_viewer_consumes_marker_and_is_html():
    html = render_viewer(_package())
    assert "/*__AEROBRIEFER_DATA__*/null" not in html, "le marqueur doit être remplacé"
    assert "<!doctype html" in html.lower() or "<!DOCTYPE html" in html
    assert "LFCY" in html


def test_render_viewer_handles_empty_region():
    """Loin de tout (océan) : aucune donnée, mais rendu propre sans marqueur."""
    from datetime import UTC, timedelta

    from aerobriefer.domain.context import BriefingContext
    from aerobriefer.domain.window import TimeWindow, UtcDateTime

    t0 = UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC)
    ctx = BriefingContext.local(
        center=Position(30.0, -40.0),  # Atlantique
        radius_nm=20.0,
        window=TimeWindow(t0, t0 + timedelta(hours=3)),
        icao=None,
    )
    html = render_viewer(assemble_briefing(ctx, []))
    assert "/*__AEROBRIEFER_DATA__*/null" not in html


def test_viewer_data_ground_and_base_layers():
    data = viewer_data(_package())
    ground = data["ground"]
    assert ground["half_extent_m"] > 0
    bbox = ground["bbox"]
    assert bbox["minLon"] < data["center"]["lon"] < bbox["maxLon"]
    assert bbox["minLat"] < data["center"]["lat"] < bbox["maxLat"]
    for layer in ground["layers"].values():
        assert "label" in layer
        assert "{" not in layer["url"], "l'URL doit être prête, bbox rempli"
        assert layer["url"].startswith("https://")


def test_viewer_data_route_when_navigation():
    ctx = build_context(
        "LFCY", date="2026-07-21", heure="10:00", duree_h=3, rayon_nm=20, route="LFBN@2500"
    )
    data = viewer_data(assemble_briefing(ctx, []))
    route = data["route"]
    assert route is not None
    assert [w["name"] for w in route["waypoints"]] == ["LFCY", "LFBN"]
    assert route["waypoints"][1]["altitude_ft"] == 2500.0
    assert route["total_distance_nm"] > 0


def test_viewer_data_no_route_for_local():
    assert viewer_data(_package())["route"] is None
