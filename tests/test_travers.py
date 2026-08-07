"""Travers (point abeam) : projection orthogonale + intégration route/navplan."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aerobriefer.cli import parse_route
from aerobriefer.domain.geo import Position, project_onto_segment
from aerobriefer.navplan import NavPlan


def test_projection_dans_le_segment():
    a, b = Position(45.0, -1.0), Position(45.0, 0.0)  # ouest → est, lat 45
    foot, t = project_onto_segment(Position(45.2, -0.5), a, b)
    assert 0.49 < t < 0.51
    assert abs(foot.lat - 45.0) < 1e-6
    assert abs(foot.lon - (-0.5)) < 1e-3


def test_projection_hors_segment():
    a, b = Position(45.0, -1.0), Position(45.0, 0.0)
    _, t = project_onto_segment(Position(45.2, 0.5), a, b)
    assert t > 1.0


def test_route_travers_valide():
    route = parse_route("A:45.0/-1.0,B:45.0/0.0,~R:45.2/-0.5")
    assert [w.name for w in route.waypoints] == ["A", "B", "T/R"]
    travers = route.waypoints[-1]
    assert abs(travers.position.lat - 45.0) < 1e-6
    assert abs(travers.position.lon - (-0.5)) < 1e-3


def test_route_travers_hors_segment_rejete():
    with pytest.raises(ValueError, match="hors du segment"):
        parse_route("A:45.0/-1.0,B:45.0/0.0,~R:45.2/0.5")


def test_route_travers_exige_deux_points():
    with pytest.raises(ValueError, match="deux points précédents"):
        parse_route("~R:45.0/-0.5")


def test_navplan_travers_premier_point_rejete():
    with pytest.raises(ValidationError, match="premier point"):
        NavPlan.model_validate(
            {
                "date": "2026-08-07",
                "duree_h": 1.0,
                "depart": "LFCY",
                "points": [{"nom": "LFBD", "travers": True}],
            }
        )


def test_navplan_travers_ok_apres_deux_points():
    plan = NavPlan.model_validate(
        {
            "date": "2026-08-07",
            "duree_h": 1.0,
            "depart": "LFCY",
            "points": [{"nom": "LFDK"}, {"nom": "LFBD", "travers": True}],
        }
    )
    # encodé avec le préfixe « ~ » pour parse_route
    assert "~LFBD" in plan.route_spec
