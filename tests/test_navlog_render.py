"""Tests du RENDU HTML du log de navigation.

Le calcul est testé dans `test_navlog.py` ; ici on ne vérifie que ce que le
pilote lit sur la feuille.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from aerobriefer.domain.geo import Position
from aerobriefer.domain.navlog import Wind, compute_navlog
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.domain.window import UtcDateTime
from aerobriefer.render.navlog import render_navlog_html

NOW = UtcDateTime.of(datetime(2026, 8, 30, 8, 0, tzinfo=UTC))


def _html() -> str:
    route = Route(
        [
            Waypoint("LFCY/N", Position(45.68, -1.02), altitude_ft=2500.0),
            Waypoint("Tvs-LFDN-Rochefort", Position(45.82924, -0.85744), altitude_ft=2500.0),
            Waypoint("LFBN", Position(46.3135, -0.3945), altitude_ft=1200.0),
        ]
    )
    navlog = compute_navlog(
        route,
        departure_time=NOW,
        tas_kt=110.0,
        winds=[Wind(240.0, 15.0), Wind(240.0, 15.0)],
        fuel_flow_lph=32.0,
    )
    return render_navlog_html(
        navlog,
        origin="LFCY",
        destination="LFBN",
        aircraft_name="Robin DR400/160 (F-GGJY)",
        tas_kt=110.0,
        fuel_flow_lph=32.0,
        now=NOW,
    )


def _branch_cells(html: str) -> list[str]:
    """Contenu texte de chaque cellule de la colonne « Branche »."""
    cells = re.findall(r'<td class="branche">(.*?)</td>', html, flags=re.S)
    return [" ".join(re.sub(r"<[^>]+>", " ", c).split()) for c in cells]


def test_la_colonne_branche_ne_montre_que_la_destination() -> None:
    """Le départ d'une branche est l'arrivée de la précédente : le répéter
    doublait l'information sur chaque ligne."""
    cells = _branch_cells(_html())
    assert len(cells) == 2, "une cellule par branche"
    assert cells[0].startswith("→ Tvs-LFDN-Rochefort")
    assert cells[1].startswith("→ LFBN")
    # Le point de DÉPART de la branche n'apparaît plus dans sa propre cellule.
    assert "LFCY/N" not in cells[0]
    assert "Tvs-LFDN-Rochefort" not in cells[1]


def test_chaque_cellule_de_branche_porte_une_seule_fleche() -> None:
    for cell in _branch_cells(_html()):
        assert cell.count("→") == 1, f"une flèche et une seule : {cell!r}"


def test_la_destination_finale_reste_lisible_en_derniere_branche() -> None:
    """Le cas qui motive la demande : savoir d'un coup d'œil que la dernière
    branche mène à LFBN."""
    assert _branch_cells(_html())[-1].startswith("→ LFBN")
