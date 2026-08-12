"""Étude de dégagement / déroutement : découverte des terrains + performances.

Tout est hors-ligne : la fixture refdata contient LFCY et ses voisins, et l'avion
d'exemple F-ZZZZ (DR400/160) porte des tables de performance complètes.
"""

from datetime import UTC, timedelta

from aerobriefer.aircraft.model import Conditions
from aerobriefer.aircraft.registry import resolve
from aerobriefer.aircraft.runway import assess_runway
from aerobriefer.diversion import (
    _STD_QNH_HPA,
    _pressure_altitude_ft,
    _verdict,
    build_diversion_study,
)
from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Position
from aerobriefer.domain.models import Metar, Runway
from aerobriefer.domain.package import BriefingPackage
from aerobriefer.domain.sourced import Provenance, Sourced
from aerobriefer.domain.window import TimeWindow, UtcDateTime

T0 = UtcDateTime(2026, 8, 12, 7, 0, tzinfo=UTC)
WINDOW = TimeWindow(T0, T0 + timedelta(hours=3))
LFCY = Position(45.628101, -0.9725)
AC = resolve("F-ZZZZ")  # DR400/160 d'exemple, tables de perf complètes


def _ctx() -> BriefingContext:
    return BriefingContext.local(center=LFCY, radius_nm=20.0, window=WINDOW, icao="LFCY")


def _sourced(value: Metar) -> Sourced[Metar]:
    return Sourced(value, Provenance(source="test", retrieved_at=T0, issued_at=T0))


def _landing(length_m: int):
    rwy = Runway(ident="09/27", length_m=length_m, surface="asphalte", true_bearing_deg=90.0)
    cond = Conditions(pressure_altitude_ft=0.0, temperature_c=15.0, mass_kg=AC.max_landing_mass_kg)
    return assess_runway(AC, rwy, cond, operation="landing")


# --- fonctions pures --------------------------------------------------------


def test_pressure_altitude_follows_qnh():
    assert _pressure_altitude_ft(100, None) == 100
    assert _pressure_altitude_ft(100, _STD_QNH_HPA) == 100
    # QNH au-dessus du standard → altitude-pression PLUS BASSE.
    high = _pressure_altitude_ft(100, 1018.0)
    assert high < 100
    assert round(high) == round(100 + (_STD_QNH_HPA - 1018.0) * 27.0)


def test_verdict_bands():
    assert _verdict(None) == "INCONNU"
    assert _verdict(_landing(300)) == "INSUFFISANT"  # bien trop court
    assert _verdict(_landing(3000)) == "OK"  # large
    # Une longueur à +10 % du requis tombe dans la bande « serré » (marge < 20 %).
    required = _landing(5000).required_m
    tight = _landing(round(required * 1.1))
    assert tight.ok
    assert 0 < tight.margin_pct < 20
    assert _verdict(tight) == "SERRÉ"


# --- découverte des terrains ------------------------------------------------


def test_study_discovers_sorted_neighbours():
    study = build_diversion_study(
        BriefingPackage(context=_ctx()), AC, radius_nm=30, limit=10, variation_deg=1.2
    )
    icaos = [f.icao for f in study.fields]
    assert icaos, "des terrains de dégagement sont trouvés autour de LFCY"
    assert "LFCY" not in icaos  # le terrain de référence est exclu
    dists = [f.distance_nm for f in study.fields]
    assert dists == sorted(dists)  # triés par distance croissante
    assert all(f.distance_nm <= 30 for f in study.fields)
    assert "LFDK" in icaos  # voisin connu (~9 NM au sud)
    # Cap magnétique = cap vrai − déclinaison.
    f0 = study.fields[0]
    assert abs(((f0.bearing_true_deg - 1.2) % 360) - f0.bearing_mag_deg) < 1e-6


def test_fields_have_perf_and_fall_back_to_isa_without_metar():
    study = build_diversion_study(BriefingPackage(context=_ctx()), AC, radius_nm=30, limit=8)
    assert study.fields
    assert all(f.temp_is_isa for f in study.fields)  # aucun METAR → température ISA
    with_runway = [f for f in study.fields if f.longest_runway_m]
    assert with_runway
    assert all(f.best_landing is not None for f in with_runway)
    assert all(f.verdict in {"OK", "SERRÉ", "INSUFFISANT", "INCONNU"} for f in study.fields)


def test_masses_default_to_aircraft_maxima():
    study = build_diversion_study(BriefingPackage(context=_ctx()), AC, radius_nm=25, limit=5)
    assert study.mass_takeoff_kg == AC.max_takeoff_mass_kg
    assert study.mass_landing_kg == AC.max_landing_mass_kg


# --- météo empruntée au voisin ----------------------------------------------


def test_borrows_nearest_metar_when_field_has_none():
    metar = _sourced(
        Metar(
            station="LFBG",
            raw_text="LFBG 120630Z 09010KT CAVOK 20/12 Q1018 NOSIG",
            observed_at=T0,
            wind_dir_deg=90,
            wind_speed_kt=10,
            temperature_c=20.0,
            qnh_hpa=1018.0,
            flight_category="VFR",
        )
    )
    study = build_diversion_study(
        BriefingPackage(context=_ctx(), metars=(metar,)), AC, radius_nm=35, limit=15
    )
    borrowers = [f for f in study.fields if f.metar_is_proxy and f.metar_station == "LFBG"]
    assert borrowers, "des terrains sans obs. propre empruntent le METAR de LFBG"
    borrowed = borrowers[0]
    assert borrowed.metar_distance_nm is not None and borrowed.metar_distance_nm > 0
    assert borrowed.temp_is_isa is False  # une obs. (empruntée) fournit la température
    assert borrowed.qnh_hpa == 1018.0
    assert borrowed.flight_category == "VFR"


# --- rendu ------------------------------------------------------------------


def test_render_diversion_html_smoke():
    from aerobriefer.render.diversion import render_diversion_html

    study = build_diversion_study(BriefingPackage(context=_ctx()), AC, radius_nm=30, limit=6)
    html = render_diversion_html(study, now=T0)
    assert "BRIEFING DÉGAGEMENT" in html
    assert "Synthèse" in html
    assert study.fields[0].icao in html
    assert "Carte VAC" in html
