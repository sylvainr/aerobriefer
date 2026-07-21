"""Tests du pipeline viewer (données + injection). Le rendu WebGL lui-même n'est
pas testable en unitaire — on vérifie la structure et l'injection déterministe."""

from __future__ import annotations

from datetime import UTC, timedelta

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Position
from aerobriefer.domain.models import Airspace, AltitudeLimit
from aerobriefer.domain.package import BriefingPackage
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.render.viewer import CLASS_COLORS, render_viewer, viewer_data

LFCY = Position(45.628101, -0.9725)
T0 = UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC)


def _package() -> BriefingPackage:
    ctx = BriefingContext.local(
        center=LFCY,
        radius_nm=20.0,
        window=TimeWindow(T0, T0 + timedelta(hours=3)),
        icao="LFCY",
    )
    poly = [Position(45.5, -1.1), Position(45.5, -0.8), Position(45.8, -0.8)]
    airspace = Airspace(
        name="TMA TEST",
        airspace_class="D",
        airspace_type="TMA",
        polygon=poly,
        lower=AltitudeLimit(0, "FT", "GND"),
        upper=AltitudeLimit(65, "FL", "STD"),
    )
    return BriefingPackage(context=ctx, airspaces=[airspace])


def test_viewer_data_contract():
    data = viewer_data(_package())
    assert data["center"]["lat"] == LFCY.lat
    assert data["flight"]["radius_nm"] == 20.0
    (a,) = data["airspaces"]
    assert a["type"] == "TMA" and a["class"] == "D"
    assert a["upper_ft"] == 6500  # FL65 → pieds
    assert a["upper_label"] == "FL65"
    assert a["color"] == CLASS_COLORS["D"]
    # polygone en [lon, lat] (convention GeoJSON)
    assert a["polygon"][0] == [-1.1, 45.5]


def test_render_viewer_injects_data_and_consumes_marker():
    html = render_viewer(_package())
    assert "/*__AEROBRIEFER_DATA__*/null" not in html, "le marqueur doit être remplacé"
    assert "TMA TEST" in html
    assert "<!doctype html" in html.lower() or "<!DOCTYPE html" in html


def test_render_viewer_handles_no_airspaces():
    ctx = BriefingContext.local(
        center=LFCY, radius_nm=20.0, window=TimeWindow(T0, T0 + timedelta(hours=3)), icao="LFCY"
    )
    html = render_viewer(BriefingPackage(context=ctx))
    assert "/*__AEROBRIEFER_DATA__*/null" not in html
