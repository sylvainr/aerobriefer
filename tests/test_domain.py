from datetime import UTC, datetime, timedelta, timezone

from aerobriefer.domain.context import BriefingContext, Purpose
from aerobriefer.domain.geo import Circle, Corridor, Position
from aerobriefer.domain.models import Notam, Severity
from aerobriefer.domain.package import BriefingPackage, ProviderFailure
from aerobriefer.domain.sourced import Provenance, Sourced
from aerobriefer.domain.window import TimeWindow, UtcDateTime

LFPN = Position(48.7519, 2.1064)  # Toussus-le-Noble
LFOB = Position(49.4544, 2.1128)  # Beauvais-Tillé
LFPO = Position(48.7233, 2.3794)  # Orly, ~11 NM à l'est de LFPN

T0 = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
FLIGHT = TimeWindow(T0, T0 + timedelta(hours=2))


def prov(source="test", issued=T0):
    return Provenance(source=source, retrieved_at=T0, issued_at=issued)


# --- géométrie ---------------------------------------------------------------


def test_distance_lfpn_lfpo_is_about_11nm():
    assert 10.5 < LFPN.distance_nm(LFPO) < 11.5


def test_circle_contains_respects_notam_radius():
    circle = Circle(LFPN, radius_nm=10.0)
    # LFPO est à ~11 NM : hors du cercle en tant que point...
    assert not circle.contains(LFPO)
    # ...mais un NOTAM de 5 NM de rayon centré là déborde dans notre zone.
    assert circle.contains(LFPO, radius_nm=5.0)


def test_corridor_accepts_point_near_route_rejects_point_far():
    corridor = Corridor([LFPN, LFOB], half_width_nm=10.0)
    midpoint = Position(49.10, 2.11)
    assert corridor.contains(midpoint)
    assert not corridor.contains(Position(49.10, 4.50))


def test_corridor_does_not_extend_beyond_its_endpoints():
    """Le piège du cross-track nu : un point aligné mais très en amont doit
    sortir du couloir, sinon un NOTAM lointain remonterait dans le briefing."""
    corridor = Corridor([LFPN, LFOB], half_width_nm=10.0)
    far_north = Position(52.0, 2.11)  # dans le prolongement, ~150 NM au-delà
    assert not corridor.contains(far_north)


def test_corridor_bounding_circle_covers_all_points():
    corridor = Corridor([LFPN, LFOB], half_width_nm=10.0)
    bounding = corridor.bounding_circle()
    assert bounding.contains(LFPN)
    assert bounding.contains(LFOB)


# --- fenêtres ----------------------------------------------------------------


def test_window_overlap_is_inclusive_at_boundary():
    just_before = TimeWindow(T0 - timedelta(hours=1), T0)
    assert FLIGHT.overlaps(just_before)


def test_window_rejects_naive_datetime():
    try:
        TimeWindow(datetime(2026, 7, 20, 10, 0), T0)
    except ValueError:
        return
    raise AssertionError("un datetime naïf aurait dû être refusé")


# --- UtcDateTime : l'invariant porté par le type ------------------------------


def test_utcdatetime_rejects_naive_construction():
    try:
        UtcDateTime(2026, 7, 20, 10, 0)
    except ValueError:
        return
    raise AssertionError("construction naïve aurait dû être refusée")


def test_utcdatetime_of_rejects_naive():
    try:
        UtcDateTime.of(datetime(2026, 7, 20, 10, 0))
    except ValueError:
        return
    raise AssertionError("of() aurait dû refuser un naïf")


def test_utcdatetime_normalises_other_offsets():
    """12h00 en UTC+2 doit devenir 10h00 UTC, pas rester tel quel."""
    paris = timezone(timedelta(hours=2))
    normalised = UtcDateTime.of(datetime(2026, 7, 20, 12, 0, tzinfo=paris))
    assert normalised.hour == 10
    assert normalised.tzinfo is UTC
    assert normalised == T0


def test_utcdatetime_survives_arithmetic():
    """Sans réencapsulation, `instant + timedelta` retomberait en datetime nu
    et l'invariant fuirait dès le premier calcul."""
    later = UtcDateTime.of(T0) + timedelta(hours=3)
    assert isinstance(later, UtcDateTime)
    earlier = UtcDateTime.of(T0) - timedelta(hours=3)
    assert isinstance(earlier, UtcDateTime)


def test_subtracting_two_instants_gives_timedelta():
    delta = UtcDateTime.of(T0 + timedelta(hours=2)) - UtcDateTime.of(T0)
    assert delta == timedelta(hours=2)


def test_parse_accepts_zulu_and_rejects_offsetless():
    assert UtcDateTime.parse("2026-07-20T10:00:00Z") == T0
    assert UtcDateTime.parse("2026-07-20T12:00:00+02:00") == T0
    try:
        UtcDateTime.parse("2026-07-20T10:00:00")
    except ValueError:
        return
    raise AssertionError("un ISO sans offset aurait dû être refusé")


def test_now_is_utc_typed():
    assert isinstance(UtcDateTime.now(), UtcDateTime)
    assert UtcDateTime.now().tzinfo is UTC


# --- filtrage NOTAM ----------------------------------------------------------


def make_notam(ident, center, radius, validity=FLIGHT, severity=Severity.INFO):
    return Notam(
        identifier=ident,
        raw_text=f"{ident} test",
        validity=validity,
        center=center,
        radius_nm=radius,
        severity=severity,
    )


def test_notam_outside_geometry_is_excluded():
    circle = Circle(LFPN, radius_nm=10.0)
    far = make_notam("A1/26", Position(43.6, 1.4), 2.0)  # Toulouse
    assert not far.concerns(circle, FLIGHT)


def test_notam_outside_window_is_excluded():
    circle = Circle(LFPN, radius_nm=20.0)
    yesterday = TimeWindow(T0 - timedelta(days=2), T0 - timedelta(days=1))
    stale = make_notam("A2/26", LFPN, 1.0, validity=yesterday)
    assert not stale.concerns(circle, FLIGHT)


def test_notam_without_geometry_is_kept():
    """Politique explicite : sans géométrie déclarée, on conserve."""
    circle = Circle(LFPN, radius_nm=10.0)
    fir_wide = Notam(identifier="A3/26", raw_text="FIR", validity=FLIGHT)
    assert fir_wide.concerns(circle, FLIGHT)


# --- dossier -----------------------------------------------------------------


def test_unknown_severity_sorts_first():
    notams = [
        Sourced(make_notam("A/26", LFPN, 1.0, severity=Severity.INFO), prov()),
        Sourced(make_notam("B/26", LFPN, 1.0, severity=Severity.BLOCKING), prov()),
        Sourced(make_notam("C/26", LFPN, 1.0, severity=Severity.UNKNOWN), prov()),
    ]
    pkg = BriefingPackage(context=_ctx(), notams=notams)
    order = [s.value.identifier for s in pkg.notams_by_severity()]
    assert order == ["C/26", "B/26", "A/26"]


def test_critical_failure_marks_package_incomplete():
    pkg = BriefingPackage(
        context=_ctx(),
        failures=[ProviderFailure("sofia", "HTTP 503", T0, is_critical=True)],
    )
    assert not pkg.is_complete


def test_package_without_failures_is_complete():
    assert BriefingPackage(context=_ctx()).is_complete


def test_age_uses_issued_at_not_retrieval():
    """Un TAF émis à 05h et téléchargé à l'instant a six heures d'âge."""
    issued = T0 - timedelta(hours=6)
    item = Sourced("TAF...", Provenance("test", retrieved_at=T0, issued_at=issued))
    assert 359 < item.age_minutes(now=T0) < 361
    assert item.is_stale(90.0, now=T0)


def _ctx():
    return BriefingContext.local(center=LFPN, radius_nm=20.0, window=FLIGHT, icao="LFPN")


def test_stations_of_interest_dedupes_and_orders():
    ctx = BriefingContext(
        geometry=Circle(LFPN, 20.0),
        window=FLIGHT,
        purpose=Purpose.NAVIGATION,
        origin_icao="lfpn",
        destination_icao="LFOB",
        alternates_icao=("LFPN", "LFPO"),
    )
    assert ctx.stations_of_interest == ("LFPN", "LFOB", "LFPO")


def test_astimezone_leaves_the_utc_type_honestly():
    """Régression : `astimezone` rendait un objet aux champs justes mais
    réétiqueté UTC par __new__ — faux de 2 h à la première soustraction."""
    from zoneinfo import ZoneInfo

    instant = UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC)
    paris = instant.astimezone(ZoneInfo("Europe/Paris"))

    assert paris.hour == 10, "10h00 locale en juillet"
    assert not isinstance(paris, UtcDateTime), "on quitte le domaine : type nu attendu"
    assert paris.utcoffset() == timedelta(hours=2), "le tzinfo doit dire la vérité"
    assert paris == instant, "et rester le MÊME instant"


def test_astimezone_survives_dst_transitions():
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    winter = UtcDateTime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(paris)
    summer = UtcDateTime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(paris)
    assert winter.hour == 13 and winter.utcoffset() == timedelta(hours=1)
    assert summer.hour == 14 and summer.utcoffset() == timedelta(hours=2)


# --- péremption par cadence de production ------------------------------------


def test_taf_is_not_stale_after_two_hours():
    """Régression : un seuil unique à 90 min marquait périmé un TAF de 2 h,
    alors que les TAF ne sont émis que toutes les 6 h — c'était le plus frais
    publié."""
    from aerobriefer.domain.freshness import max_age_minutes
    from aerobriefer.domain.models import Taf

    taf = Taf(
        station="LFBH",
        raw_text="TAF LFBH ...",
        issued_at=T0,
        validity=TimeWindow(T0, T0 + timedelta(hours=24)),
    )
    item = Sourced(taf, Provenance("noaa", retrieved_at=T0, issued_at=T0))
    two_hours_later = T0 + timedelta(hours=2)
    assert not item.is_stale(max_age_minutes(taf), now=two_hours_later)
    assert item.is_stale(max_age_minutes(taf), now=T0 + timedelta(hours=9))


def test_metar_stays_strict():
    from aerobriefer.domain.freshness import max_age_minutes
    from aerobriefer.domain.models import Metar

    metar = Metar(station="LFBD", raw_text="METAR ...", observed_at=T0)
    item = Sourced(metar, Provenance("noaa", retrieved_at=T0, issued_at=T0))
    assert item.is_stale(max_age_minutes(metar), now=T0 + timedelta(hours=2))


def test_radar_is_stricter_than_temsi():
    """Le radar sort tous les quarts d'heure, le TEMSI toutes les 3 h."""
    from aerobriefer.domain.freshness import max_age_minutes
    from aerobriefer.domain.models import Chart

    radar = Chart(kind="radar", url="x")
    temsi = Chart(kind="temsi", url="x")
    assert max_age_minutes(radar) < max_age_minutes(temsi)


# --- vent traversier ---------------------------------------------------------


def test_crosswind_pure_from_right():
    """Vent perpendiculaire à droite : traversier = force du vent, face = 0."""
    from aerobriefer.domain.models import Runway

    rwy = Runway(ident="09/27", length_m=1000, true_bearing_deg=90.0)  # piste vers l'est
    wc = rwy.wind_components(180.0, 20.0)  # vent du sud (à droite de l'axe est)
    assert wc is not None
    assert abs(wc.crosswind_kt - 20.0) < 0.01
    assert abs(wc.headwind_kt) < 0.01
    assert wc.from_right is True
    assert wc.arrow == "←"  # vient de droite → pousse vers la gauche


def test_crosswind_pure_headwind_has_no_cross():
    from aerobriefer.domain.models import Runway

    rwy = Runway(ident="18/36", length_m=1000, true_bearing_deg=180.0)
    wc = rwy.wind_components(180.0, 15.0)  # vent pile dans l'axe
    assert abs(wc.crosswind_kt) < 0.01
    assert abs(wc.headwind_kt - 15.0) < 0.01
    assert wc.is_tailwind is False


def test_tailwind_is_detected():
    from aerobriefer.domain.models import Runway

    rwy = Runway(ident="18/36", length_m=1000, true_bearing_deg=180.0)
    wc = rwy.wind_components(0.0, 12.0)  # vent du nord, piste vers le sud → arrière
    assert wc.is_tailwind is True
    assert wc.headwind_kt < 0


def test_unknown_bearing_yields_no_components():
    from aerobriefer.domain.models import Runway

    rwy = Runway(ident="??", length_m=1000, true_bearing_deg=None)
    assert rwy.wind_components(90.0, 10.0) is None


def test_favoured_runway_picks_into_wind_qfu():
    """Sur une 09/27, un vent d'est doit favoriser le QFU 09, pas 27."""
    from aerobriefer.domain.geo import Position
    from aerobriefer.domain.models import Aerodrome, Runway

    ad = Aerodrome(
        icao="TEST",
        name="Test",
        position=Position(48.0, 2.0),
        elevation_ft=0,
        runways=(Runway(ident="09/27", length_m=1500, true_bearing_deg=90.0),),
    )
    wc = ad.favoured_wind_components(90.0, 12.0)  # vent d'est pile dans l'axe 09
    assert wc.runway_ident == "09"
    assert wc.headwind_kt > 0
