from datetime import UTC, timedelta

import pytest

from aerobriefer.assemble import assemble_briefing
from aerobriefer.cli import build_context
from aerobriefer.data import airports
from aerobriefer.domain.geo import Position
from aerobriefer.domain.models import Metar, Notam, Severity
from aerobriefer.domain.sourced import Provenance, Sourced
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers.base import ProviderError

T0 = UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC)
WINDOW = TimeWindow(T0, T0 + timedelta(hours=3))
LFCY = Position(45.628101, -0.9725)


def _ctx(radius_nm=20.0):
    from aerobriefer.domain.context import BriefingContext

    return BriefingContext.local(center=LFCY, radius_nm=radius_nm, window=WINDOW, icao="LFCY")


def _sourced(value, source="test"):
    return Sourced(value, Provenance(source=source, retrieved_at=T0, issued_at=T0))


class FakeProvider:
    def __init__(self, name, items=(), error=None, is_critical=False):
        self.name = name
        self.is_critical = is_critical
        self._items = items
        self._error = error

    def fetch(self, context):
        if self._error:
            raise self._error
        return self._items


# --- base aérodromes ---------------------------------------------------------


def test_lfcy_is_in_the_database():
    lfcy = airports.require("LFCY")
    assert "Royan" in lfcy.name
    assert abs(lfcy.position.lat - 45.628) < 0.01
    assert lfcy.position.lon < 0  # longitude ouest : piège classique de signe


def test_lookup_is_case_insensitive():
    assert airports.lookup("lfcy") is not None


def test_unknown_icao_raises():
    with pytest.raises(KeyError):
        airports.require("ZZZZ")


def test_nearest_is_sorted_and_bounded():
    lfcy = airports.require("LFCY")
    found = airports.nearest(lfcy.position, within_nm=30, limit=5)
    distances = [d for _, d in found]
    assert distances == sorted(distances)
    assert all(d <= 30 for d in distances)


# --- conversion horaire ------------------------------------------------------


def test_local_hour_is_converted_to_utc():
    """10h00 locale en juillet (CEST, UTC+2) doit donner 08:00Z."""
    context = build_context("LFCY", date="2026-07-21", heure="10:00", duree_h=3, rayon_nm=20)
    assert context.window.start.hour == 8
    assert context.window.start.tzinfo is UTC
    assert context.window.duration_hours == 3.0


# --- agrégation --------------------------------------------------------------


def test_failing_provider_becomes_a_visible_failure():
    """Un provider en échec ne doit jamais disparaître silencieusement."""
    providers = [FakeProvider("cassé", error=ProviderError("cassé", "HTTP 503"), is_critical=True)]
    package = assemble_briefing(_ctx(), providers, parallel=False)
    assert len(package.failures) == 1
    assert package.failures[0].source == "cassé"
    assert not package.is_complete


def test_unexpected_exception_is_also_captured():
    """Même une exception non prévue ne doit pas tuer le briefing."""
    providers = [FakeProvider("bugué", error=ValueError("boum"))]
    package = assemble_briefing(_ctx(), providers, parallel=False)
    assert len(package.failures) == 1
    assert "boum" in package.failures[0].reason


def test_one_provider_failing_does_not_block_the_others():
    metar = _sourced(Metar(station="LFBD", raw_text="METAR LFBD...", observed_at=T0))
    providers = [
        FakeProvider("ko", error=ProviderError("ko", "timeout")),
        FakeProvider("ok", items=[metar]),
    ]
    package = assemble_briefing(_ctx(), providers, parallel=False)
    assert len(package.metars) == 1
    assert len(package.failures) == 1


def test_notams_are_refiltered_locally():
    """On ne délègue pas un critère de sécurité à la source : re-filtrage local."""
    inside = Notam(
        identifier="A1/26", raw_text="proche", validity=WINDOW, center=LFCY, radius_nm=2.0
    )
    far = Notam(
        identifier="A2/26",
        raw_text="Toulouse",
        validity=WINDOW,
        center=Position(43.6, 1.4),
        radius_nm=2.0,
    )
    providers = [FakeProvider("sofia", items=[_sourced(inside), _sourced(far)])]
    package = assemble_briefing(_ctx(), providers, parallel=False)
    kept = [s.value.identifier for s in package.notams]
    assert kept == ["A1/26"]


def test_notam_outside_time_window_is_dropped():
    yesterday = TimeWindow(T0 - timedelta(days=3), T0 - timedelta(days=2))
    stale = Notam(
        identifier="A3/26", raw_text="périmé", validity=yesterday, center=LFCY, radius_nm=1.0
    )
    providers = [FakeProvider("sofia", items=[_sourced(stale)])]
    assert len(assemble_briefing(_ctx(), providers, parallel=False).notams) == 0


def test_notam_without_geometry_survives_refiltering():
    wide = Notam(identifier="A4/26", raw_text="FIR entier", validity=WINDOW)
    providers = [FakeProvider("sofia", items=[_sourced(wide)])]
    assert len(assemble_briefing(_ctx(), providers, parallel=False).notams) == 1


def test_items_are_dispatched_by_type():
    metar = _sourced(Metar(station="LFBD", raw_text="M", observed_at=T0))
    notam = _sourced(
        Notam(identifier="A5/26", raw_text="N", validity=WINDOW, center=LFCY, radius_nm=1.0)
    )
    providers = [FakeProvider("mixte", items=[metar, notam])]
    package = assemble_briefing(_ctx(), providers, parallel=False)
    assert len(package.metars) == 1 and len(package.notams) == 1


def test_aerodromes_are_resolved_from_context():
    package = assemble_briefing(_ctx(), [], parallel=False)
    assert [a.icao for a in package.aerodromes] == ["LFCY"]


def test_severity_ordering_survives_assembly():
    notams = [
        _sourced(Notam("A/26", "info", WINDOW, LFCY, 1.0, severity=Severity.INFO)),
        _sourced(Notam("B/26", "bloquant", WINDOW, LFCY, 1.0, severity=Severity.BLOCKING)),
        _sourced(Notam("C/26", "inconnu", WINDOW, LFCY, 1.0, severity=Severity.UNKNOWN)),
    ]
    package = assemble_briefing(_ctx(), [FakeProvider("sofia", items=notams)], parallel=False)
    assert [s.value.identifier for s in package.notams_by_severity()] == ["C/26", "B/26", "A/26"]


# --- pertinence des cartes ---------------------------------------------------


def _chart(kind, valid_at):
    from aerobriefer.domain.models import Chart

    return _sourced(Chart(kind=kind, url=f"http://x/{kind}", valid_at=valid_at, content=b"PNG"))


def test_chart_inside_window_is_reported_as_covering():
    from aerobriefer.domain.package import BriefingPackage

    pkg = BriefingPackage(context=_ctx(), charts=[_chart("front", T0 + timedelta(hours=1))])
    assert len(pkg.charts_covering_window()) == 1
    assert pkg.charts_outside_window() == ()
    assert pkg.missing_chart_kinds() == ()


def test_yesterday_temsi_is_flagged_not_hidden():
    """Cas réel LFCY : le seul TEMSI disponible date de la veille."""
    from aerobriefer.domain.package import BriefingPackage

    stale_temsi = _chart("temsi", T0 - timedelta(hours=20))
    pkg = BriefingPackage(context=_ctx(), charts=[stale_temsi])
    assert len(pkg.charts) == 1, "la carte doit être conservée"
    assert pkg.charts_covering_window() == ()
    assert len(pkg.charts_outside_window()) == 1, "et signalée comme hors fenêtre"
    assert pkg.missing_chart_kinds() == ("temsi",)


def test_chart_without_validity_counts_as_outside():
    """Sans échéance connue, on ne peut pas affirmer qu'elle couvre le vol."""
    from aerobriefer.domain.package import BriefingPackage

    pkg = BriefingPackage(context=_ctx(), charts=[_chart("radar", None)])
    assert len(pkg.charts_outside_window()) == 1


def test_notam_aerodromes_are_resolved_for_naming():
    """Le dossier doit porter les noms des terrains cités par les NOTAM,
    sinon le rendu ne peut afficher que « LFDK » là où on attend Soulac."""
    notam = _sourced(
        Notam(
            identifier="R1552/26",
            raw_text="ZRT",
            validity=WINDOW,
            center=LFCY,
            radius_nm=5.0,
            affected_icao="LFDK",
        )
    )
    package = assemble_briefing(_ctx(), [FakeProvider("sofia", items=[notam])], parallel=False)
    resolved = {a.icao: a.name for a in package.aerodromes}
    assert "LFDK" in resolved, "le terrain du NOTAM doit être dans le dossier"
    assert "Soulac" in resolved["LFDK"]


def test_metar_station_is_resolved_even_if_not_a_flight_aerodrome():
    metar = _sourced(Metar(station="LFBD", raw_text="METAR LFBD...", observed_at=T0))
    package = assemble_briefing(_ctx(), [FakeProvider("noaa", items=[metar])], parallel=False)
    assert "LFBD" in {a.icao for a in package.aerodromes}


def test_lfcy_has_both_runways_from_sia():
    """Le SIA (contrairement à OurAirports) a les DEUX pistes de LFCY : la 10/28
    revêtue ET la bande herbe 10R/28L (1000 m). Elle remplace OurAirports."""
    lfcy = airports.require("LFCY")
    idents = {r.ident for r in lfcy.runways}
    assert "10/28" in idents, "la piste revêtue"
    assert "10R/28L" in idents, "la bande herbe (que le SIA fournit, pas OurAirports)"
    grass = next(r for r in lfcy.runways if r.ident == "10R/28L")
    assert grass.length_m == 1000
    assert grass.is_paved is False
    # Parallèle à la revêtue : même cap, donc même traversier.
    paved = next(r for r in lfcy.runways if r.ident == "10/28")
    assert grass.true_bearing_deg == paved.true_bearing_deg
