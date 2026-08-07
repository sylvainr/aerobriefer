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
    # VOR le plus proche : ident + Morse + radiale.
    assert note.vor is not None
    assert note.vor.morse and set(note.vor.morse) <= {".", "-", " "}
    # Radios ÉTIQUETÉES (au moins les fréquences de LFBD, terrain d'arrivée).
    assert note.radios
    assert any("LFBD" in r.label for r in note.radios)
    assert all(r.freq for r in note.radios)
    # Déroutement RELATIF à la route : cap signé pour la flèche + terrain nommé + pistes.
    div = note.diversion
    assert div is not None
    assert div.name  # nom du terrain (pas juste le code)
    assert div.side in {"G", "D"}
    assert 0 <= div.relative_deg <= 180
    assert -180 <= div.relative_signed_deg <= 180
    assert div.runways  # au moins une piste listée
    assert any(rw.favoured for rw in div.runways)  # une piste favorable désignée


def test_deroutement_relatif_a_la_route():
    from aerobriefer.navlog_build import _relative

    # Route au 161°, déroutement au cap 145° → 16° à GAUCHE, presque devant.
    angle, side, arrow = _relative(145.0, 161.0)
    assert angle == 16
    assert side == "G"
    # Route au 161°, déroutement au 200° → 39° à DROITE.
    angle2, side2, _ = _relative(200.0, 161.0)
    assert angle2 == 39
    assert side2 == "D"


def test_liste_vac_contient_depart_et_arrivee():
    _, vac = annotate_navlog(_navlog(), DR400_160())
    icaos = {v.icao for v in vac}
    assert {"LFCY", "LFBD"} <= icaos
    for link in vac:
        assert link.url.startswith("https://www.sia.aviation-civile.gouv.fr/")
        assert link.icao in link.url
