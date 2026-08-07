"""Tests de l'auto-remplissage du navlog (radiales VOR, radios, déroutement, VAC).

On construit un NavLog à la main (calcul pur, sans réseau) sur une route de la
fixture, puis on l'annote depuis la donnée de référence (navaids/fréquences).
"""

from __future__ import annotations

from aerobriefer.aircraft.examples.dr400 import DR400_160
from aerobriefer.data import airports
from aerobriefer.domain.navlog import Wind, compute_navlog
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.domain.window import UtcDateTime
from aerobriefer.navlog_build import annotate_navlog


def _navlog():
    from datetime import UTC

    lfcy = airports.require("LFCY")
    lfbd = airports.require("LFBD")
    route = Route(
        [
            Waypoint(lfcy.icao, lfcy.position, altitude_ft=2500.0),
            Waypoint(lfbd.icao, lfbd.position, altitude_ft=2500.0),
        ]
    )
    return compute_navlog(
        route,
        departure_time=UtcDateTime(2026, 8, 7, 13, 0, tzinfo=UTC),
        tas_kt=130.0,
        winds=[Wind(0.0, 0.0)],
    )


def test_annotations_remplissent_vor_radios_deroutement():
    annotations, vac = annotate_navlog(_navlog(), DR400_160(), magnetic_variation_deg=1.0)
    assert len(annotations) == 1
    note = annotations[0]
    # LFBD est une grande plateforme : VOR proche + radios + un déroutement.
    assert note.vor and "R" in note.vor and "NM" in note.vor
    assert note.radios  # LFBD a des fréquences dans la fixture
    # Déroutement enrichi : terrain, cap+flèche, et marge d'atterrissage.
    div = note.diversion
    assert div is not None
    assert div.icao and div.distance_nm >= 0
    assert div.arrow in {"↑", "↗", "→", "↘", "↓", "↙", "←", "↖"}
    assert 0 <= div.bearing_deg <= 360


def test_liste_vac_contient_depart_et_arrivee():
    _, vac = annotate_navlog(_navlog(), DR400_160())
    icaos = {v.icao for v in vac}
    assert {"LFCY", "LFBD"} <= icaos
    for link in vac:
        assert link.url.startswith("https://www.sia.aviation-civile.gouv.fr/")
        assert link.icao in link.url
