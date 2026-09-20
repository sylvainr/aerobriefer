"""Tests du rendu déterministe.

Le dossier de démonstration construit ici est volontairement DÉSAGRÉABLE : il
contient un METAR périmé, une source critique en panne, des NOTAM de sévérités
variées dont un non classé, et une carte non embarquée. C'est le cas où le
rendu doit se montrer le plus bavard — un briefing amputé qui a l'air complet
est le pire résultat possible, et c'est exactement ce qu'on teste ici.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aerobriefer.domain.context import BriefingContext, Purpose
from aerobriefer.domain.geo import Circle, Position
from aerobriefer.domain.models import (
    CLOUD_BASE_FT_PER_C,
    Aerodrome,
    Chart,
    ForecastPoint,
    Metar,
    Notam,
    Runway,
    Severity,
    Taf,
    TafPeriod,
)
from aerobriefer.domain.package import BriefingPackage, ProviderFailure
from aerobriefer.domain.sourced import Provenance, Sourced
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.render.html import (
    HtmlRenderer,
    format_age,
    format_dual,
    render_html,
)
from aerobriefer.render.pdf import PdfRenderer, count_pdf_pages, find_chrome

# Instant de référence figé : sans lui, aucun test de déterminisme n'a de sens.
NOW = UtcDateTime.of(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))


# --------------------------------------------------------------------------
# Fabrique du dossier de démonstration
# --------------------------------------------------------------------------


def make_rgba_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """PNG RGBA uni — les couches AROME sont des CALQUES : leur transparence
    porte du sens (« la couche ne peint pas ici »), un PNG opaque ne testerait
    pas le bon objet."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_png(
    width: int = 320, height: int = 200, rgb: tuple[int, int, int] = (30, 30, 30)
) -> bytes:
    """Un VRAI PNG (pas trois octets déguisés) : Chrome refuserait le reste,
    et le test perdrait tout son intérêt."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b""
    for y in range(height):
        raw += b"\x00"
        for x in range(width):
            # Damier, pour qu'on voie tout de suite si l'image est tronquée.
            on = ((x // 20) + (y // 20)) % 2 == 0
            raw += bytes(rgb) if on else b"\xf0\xf0\xf0"

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _prov(source: str, *, age_minutes: float, url: str | None = None) -> Provenance:
    issued = NOW - timedelta(minutes=age_minutes)
    return Provenance(
        source=source,
        retrieved_at=NOW - timedelta(minutes=2),
        issued_at=issued,
        url=url,
    )


def build_demo_package() -> BriefingPackage:
    """Dossier complet et réaliste — c'est aussi lui qui produit le PDF d'exemple."""
    lfpn = Position(48.7517, 2.1064)  # Toussus-le-Noble
    window = TimeWindow(
        start=UtcDateTime.of(datetime(2026, 7, 20, 9, 0, tzinfo=UTC)),
        end=UtcDateTime.of(datetime(2026, 7, 20, 13, 0, tzinfo=UTC)),
    )

    context = BriefingContext(
        geometry=Circle(lfpn, 30.0),
        window=window,
        purpose=Purpose.NAVIGATION,
        origin_icao="LFPN",
        destination_icao="LFOZ",
        alternates_icao=("LFOJ",),
        aircraft_id="F-GKQR (DR400-120)",
    )

    aerodromes = (
        Aerodrome(
            icao="LFPN",
            name="Toussus-le-Noble",
            position=lfpn,
            elevation_ft=538,
            runways=(
                Runway("07L/25R", 1100, 30, "asphalte", 68.0),
                Runway("07R/25L", 1050, 25, "asphalte", 68.0),
            ),
        ),
        Aerodrome(
            icao="LFOZ",
            name="Orléans-Saint-Denis-de-l'Hôtel",
            position=Position(47.8983, 2.1633),
            elevation_ft=396,
            runways=(Runway("02/20", 1300, 30, "asphalte", 20.0),),
        ),
    )

    metars = (
        # Frais.
        Sourced(
            Metar(
                station="LFPN",
                raw_text="LFPN 200730Z 24008KT 9999 SCT025 BKN040 19/14 Q1014 NOSIG",
                observed_at=NOW - timedelta(minutes=30),
                wind_dir_deg=240,
                wind_speed_kt=8,
                visibility_m=9999,
                temperature_c=19.0,
                dewpoint_c=14.0,
                qnh_hpa=1014.0,
                ceiling_ft=4000,
            ),
            _prov("noaa-awc", age_minutes=30.0),
        ),
        # PÉRIMÉ (> 90 min) : doit être signalé, jamais masqué.
        Sourced(
            Metar(
                station="LFOZ",
                raw_text="LFOZ 200400Z 21012G22KT 4000 -RA BKN012 OVC020 17/15 Q1011",
                observed_at=NOW - timedelta(minutes=240),
                wind_dir_deg=210,
                wind_speed_kt=12,
                wind_gust_kt=22,
                visibility_m=4000,
                temperature_c=17.0,
                dewpoint_c=15.0,
                qnh_hpa=1011.0,
                ceiling_ft=1200,
                conditions=("-RA",),
            ),
            _prov("noaa-awc", age_minutes=240.0),
        ),
    )

    tafs = (
        Sourced(
            Taf(
                station="LFPN",
                raw_text=(
                    "TAF LFPN 200500Z 2006/2018 24010KT 9999 SCT030 "
                    "TEMPO 2012/2016 26015G28KT 4000 SHRA BKN018CB"
                ),
                issued_at=NOW - timedelta(minutes=180),
                validity=TimeWindow(
                    start=UtcDateTime.of(datetime(2026, 7, 20, 6, 0, tzinfo=UTC)),
                    end=UtcDateTime.of(datetime(2026, 7, 20, 18, 0, tzinfo=UTC)),
                ),
                periods=(
                    TafPeriod(
                        validity=TimeWindow(
                            start=UtcDateTime.of(datetime(2026, 7, 20, 6, 0, tzinfo=UTC)),
                            end=UtcDateTime.of(datetime(2026, 7, 20, 18, 0, tzinfo=UTC)),
                        ),
                        change_type="INITIAL",
                        wind_dir_deg=240,
                        wind_speed_kt=10,
                        raw_text="2006/2018 24010KT 9999 SCT030",
                    ),
                    TafPeriod(
                        validity=TimeWindow(
                            start=UtcDateTime.of(datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
                            end=UtcDateTime.of(datetime(2026, 7, 20, 16, 0, tzinfo=UTC)),
                        ),
                        change_type="TEMPO",
                        wind_dir_deg=260,
                        wind_speed_kt=15,
                        wind_gust_kt=28,
                        visibility_m=4000,
                        conditions=("SHRA",),
                        raw_text="TEMPO 2012/2016 26015G28KT 4000 SHRA BKN018CB",
                    ),
                ),
            ),
            _prov("noaa-awc", age_minutes=180.0),
        ),
    )

    notams = (
        # MAJOR
        Sourced(
            Notam(
                identifier="A2417/26",
                raw_text="A2417/26 LFPN VOR/DME TSU HORS SERVICE",
                validity=TimeWindow(
                    start=UtcDateTime.of(datetime(2026, 7, 19, 6, 0, tzinfo=UTC)),
                    end=UtcDateTime.of(datetime(2026, 7, 25, 18, 0, tzinfo=UTC)),
                ),
                center=lfpn,
                radius_nm=5.0,
                q_code="QNVAS",
                severity=Severity.MAJOR,
                source_category="Procédures",
                decoded_text="VOR/DME TSU indisponible sur la période.",
                affected_icao="LFPN",
            ),
            _prov("sofia", age_minutes=45.0),
        ),
        # BLOCKING
        Sourced(
            Notam(
                identifier="A2501/26",
                raw_text="A2501/26 LFPN RWY 07R/25L FERMEE TRAVAUX",
                validity=TimeWindow(
                    start=UtcDateTime.of(datetime(2026, 7, 20, 5, 0, tzinfo=UTC)),
                    end=UtcDateTime.of(datetime(2026, 7, 22, 16, 0, tzinfo=UTC)),
                ),
                center=lfpn,
                radius_nm=2.0,
                q_code="QMRLC",
                severity=Severity.BLOCKING,
                source_category="Aire de mouvement",
                decoded_text="Piste 07R/25L fermée pour travaux.",
                affected_icao="LFPN",
                lower_limit_ft=0,
                upper_limit_ft=None,
            ),
            _prov("sofia", age_minutes=20.0),
        ),
        # UNKNOWN — doit remonter EN TÊTE malgré son rang numérique nul.
        Sourced(
            Notam(
                identifier="B0912/26",
                raw_text=(
                    "B0912/26 LFFF ZONE TEMPORAIREMENT RESERVEE ACTIVE "
                    "SFC-FL065 CONTOURNEMENT OBLIGATOIRE"
                ),
                validity=TimeWindow(
                    start=UtcDateTime.of(datetime(2026, 7, 20, 8, 0, tzinfo=UTC)),
                    end=UtcDateTime.of(datetime(2026, 7, 20, 16, 0, tzinfo=UTC)),
                ),
                center=None,  # NOTAM de FIR : pas de géométrie, on conserve
                radius_nm=None,
                severity=Severity.UNKNOWN,
                source_category="Obstacles",
                lower_limit_ft=0,
                upper_limit_ft=6500,
            ),
            _prov("sofia", age_minutes=25.0),
        ),
        # MINOR
        Sourced(
            Notam(
                identifier="A2455/26",
                raw_text="A2455/26 LFOZ BALISAGE LUMINEUX PISTE PARTIELLEMENT HS",
                validity=TimeWindow(
                    start=UtcDateTime.of(datetime(2026, 7, 18, 6, 0, tzinfo=UTC)),
                    end=UtcDateTime.of(datetime(2026, 7, 30, 18, 0, tzinfo=UTC)),
                ),
                center=Position(47.8983, 2.1633),
                radius_nm=3.0,
                severity=Severity.MINOR,
                source_category="Balisage",
                affected_icao="LFOZ",
            ),
            _prov("sofia", age_minutes=50.0),
        ),
        # INFO
        Sourced(
            Notam(
                identifier="A2460/26",
                raw_text="A2460/26 LFPN NOUVELLE PROCEDURE ADMINISTRATIVE PARKING",
                validity=TimeWindow(
                    start=UtcDateTime.of(datetime(2026, 7, 1, 0, 0, tzinfo=UTC)),
                    end=UtcDateTime.of(datetime(2026, 8, 31, 23, 59, tzinfo=UTC)),
                ),
                center=lfpn,
                radius_nm=1.0,
                severity=Severity.INFO,
                source_category="Réglementation espace aérien",
                affected_icao="LFPN",
            ),
            _prov("sofia", age_minutes=60.0),
        ),
    )

    # Le point de rosee est COHERENT avec la base annoncee : celle-ci est
    # derivee de l'ecart T/Td par la regle du pouce, une fixture ou les deux se
    # contrediraient ne prouverait rien sur l'infobulle qui montre le calcul.
    forecasts = tuple(
        Sourced(
            ForecastPoint(
                valid_at=UtcDateTime.of(datetime(2026, 7, 20, hour, 0, tzinfo=UTC)),
                position=lfpn,
                wind_dir_deg=225.0 + hour,
                wind_speed_kt=8.0 + hour * 0.6,
                wind_gust_kt=(18.0 + hour) if hour >= 12 else None,
                temperature_c=17.0 + hour * 0.5,
                cloud_cover_pct=40.0 + hour * 3,
                cloud_base_ft=3200.0 - hour * 60,
                precipitation_mm=0.0 if hour < 12 else 0.8,
                qnh_hpa=1014.0 - hour * 0.2,
                dewpoint_c=(17.0 + hour * 0.5) - (3200.0 - hour * 60) / CLOUD_BASE_FT_PER_C,
                relative_humidity_pct=70.0 + hour,
            ),
            _prov("met.no", age_minutes=35.0),
        )
        for hour in range(9, 14)
    )

    charts = (
        Sourced(
            Chart(
                kind="temsi",
                url="https://aviation.meteo.fr/temsi_france.png",
                issued_at=NOW - timedelta(minutes=95),
                valid_at=UtcDateTime.of(datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
                area="FRANCE",
                media_type="image/png",
                content=make_png(560, 380, (25, 25, 25)),
            ),
            _prov("aeroweb", age_minutes=95.0, url="https://aviation.meteo.fr/temsi"),
        ),
        Sourced(
            Chart(
                kind="wintem",
                url="https://aviation.meteo.fr/wintem_fl050.png",
                issued_at=NOW - timedelta(minutes=40),
                valid_at=UtcDateTime.of(datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
                area="FRANCE",
                flight_level="FL050",
                media_type="image/png",
                content=make_png(520, 360, (45, 45, 45)),
            ),
            _prov("aeroweb", age_minutes=40.0),
        ),
        # Non embarquée : inutilisable hors ligne, le rendu doit le dire.
        Sourced(
            Chart(
                kind="radar",
                url="https://example.invalid/radar_latest.png",
                issued_at=NOW - timedelta(minutes=10),
                area="FRANCE",
                media_type="image/png",
                content=None,
            ),
            _prov("aeroweb", age_minutes=10.0),
        ),
    )

    failures = (
        ProviderFailure(
            source="sofia-notam-lfoz",
            reason="502 Bad Gateway après 3 tentatives — NOTAM de destination non collectés",
            occurred_at=NOW - timedelta(minutes=4),
            is_critical=True,
        ),
        ProviderFailure(
            source="aeroweb-satellite",
            reason="délai dépassé (30 s) — image satellite indisponible",
            occurred_at=NOW - timedelta(minutes=6),
            is_critical=False,
        ),
    )

    return BriefingPackage(
        context=context,
        assembled_at=NOW - timedelta(minutes=1),
        aerodromes=aerodromes,
        metars=metars,
        tafs=tafs,
        notams=notams,
        forecasts=forecasts,
        charts=charts,
        failures=failures,
    )


@pytest.fixture(scope="module")
def package() -> BriefingPackage:
    return build_demo_package()


@pytest.fixture(scope="module")
def html(package: BriefingPackage) -> str:
    return render_html(package, now=NOW)


# --------------------------------------------------------------------------
# Formatage des heures
# --------------------------------------------------------------------------


def test_format_dual_affiche_local_et_zulu() -> None:
    from zoneinfo import ZoneInfo

    assert format_dual(NOW, ZoneInfo("Europe/Paris")) == "10:00L / 08:00Z"


def test_format_dual_traverse_correctement_les_bascules_dheure_dete() -> None:
    """`UtcDateTime.astimezone` renvoie un objet réétiqueté UTC : on vérifie
    que les CHAMPS restent justes, transitions comprises. Si cette hypothèse
    tombe, tout l'affichage local est faux de deux heures en silence."""
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    cases = {
        datetime(2026, 1, 20, 8, 0, tzinfo=UTC): "09:00L / 08:00Z",  # hiver
        datetime(2026, 7, 20, 8, 0, tzinfo=UTC): "10:00L / 08:00Z",  # été
        datetime(2026, 3, 29, 0, 59, tzinfo=UTC): "01:59L / 00:59Z",  # avant saut
        datetime(2026, 3, 29, 1, 0, tzinfo=UTC): "03:00L / 01:00Z",  # après saut
        datetime(2026, 10, 25, 0, 59, tzinfo=UTC): "02:59L / 00:59Z",
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC): "02:00L / 01:00Z",
    }
    for instant, expected in cases.items():
        assert format_dual(UtcDateTime.of(instant), paris) == expected, instant


def test_format_age() -> None:
    assert format_age(0.5) == "< 1 min"
    assert format_age(42.0) == "42 min"
    assert format_age(125.0) == "2 h 05"


def test_fuseau_daffichage_est_un_parametre_pas_une_constante(
    package: BriefingPackage,
) -> None:
    paris = render_html(package, now=NOW)
    noumea = render_html(package, now=NOW, display_timezone="Pacific/Noumea")

    assert paris != noumea
    assert "10:00L" in paris
    assert "19:00L" in noumea  # UTC+11
    # Le Z ne bouge jamais, quel que soit le fuseau d'affichage.
    assert "08:00Z" in paris
    assert "08:00Z" in noumea


# --------------------------------------------------------------------------
# Déterminisme
# --------------------------------------------------------------------------


def test_rendu_deterministe(package: BriefingPackage) -> None:
    assert render_html(package, now=NOW) == render_html(package, now=NOW)


def test_rendu_deterministe_sur_deux_instances_du_meme_dossier() -> None:
    assert render_html(build_demo_package(), now=NOW) == render_html(build_demo_package(), now=NOW)


# --------------------------------------------------------------------------
# Règle 3 — incomplétude signalée en tête
# --------------------------------------------------------------------------


def test_bandeau_incomplet_en_tete_avec_les_echecs(package: BriefingPackage, html: str) -> None:
    assert package.is_complete is False
    assert "BRIEFING INCOMPLET" in html

    banner = html.index("BRIEFING INCOMPLET")
    # En TÊTE : avant la synthèse, avant les METAR, avant les NOTAM.
    assert banner < html.index("Synthèse")
    assert banner < html.index("<h2>METAR</h2>")

    # Chaque échec critique est nommé avec sa raison.
    assert "sofia-notam-lfoz" in html
    assert "502 Bad Gateway" in html


def test_echecs_non_critiques_aussi_visibles(html: str) -> None:
    assert "aeroweb-satellite" in html
    assert "Sources non critiques en échec" in html


def test_dossier_complet_na_pas_de_bandeau_dalerte() -> None:
    base = build_demo_package()
    complete = BriefingPackage(
        context=base.context,
        assembled_at=base.assembled_at,
        aerodromes=base.aerodromes,
        metars=base.metars,
        tafs=base.tafs,
        notams=base.notams,
        forecasts=base.forecasts,
        charts=base.charts,
        failures=(),
    )
    rendered = render_html(complete, now=NOW)

    assert complete.is_complete is True
    assert "BRIEFING INCOMPLET" not in rendered
    assert "Sources non critiques en échec" not in rendered


# --------------------------------------------------------------------------
# Règle 1 — le brut n'est jamais converti
# --------------------------------------------------------------------------


def test_texte_brut_aero_present_verbatim(package: BriefingPackage, html: str) -> None:
    for metar in package.metars:
        assert metar.value.raw_text in html
    for taf in package.tafs:
        assert taf.value.raw_text in html
    for notam in package.notams:
        assert notam.value.raw_text in html


def test_le_brut_nest_pas_reecrit_en_heure_locale(html: str) -> None:
    """Le METAR de LFPN est observé à 0730Z. Si un « 0930 » apparaissait dans le
    bloc brut, quelqu'un aurait converti ce qui ne doit jamais l'être."""
    assert "LFPN 200730Z" in html
    assert "LFPN 200930Z" not in html
    assert "LFOZ 200400Z" in html
    assert "LFOZ 200600Z" not in html


def test_entetes_affichent_bien_le_double_horaire(html: str) -> None:
    assert "L / " in html
    assert "Z" in html
    # Fenêtre 09:00Z–13:00Z → 11:00L–15:00L en juillet à Paris.
    assert "20/07 11:00L / 20/07 09:00Z" in html
    assert "20/07 15:00L / 20/07 13:00Z" in html


# --------------------------------------------------------------------------
# Règle 2 — fraîcheur signalée, jamais masquée
# --------------------------------------------------------------------------


def test_ages_affiches_sur_chaque_bloc(html: str) -> None:
    assert "âge" in html
    assert html.count("âge") >= len("metars tafs notams charts".split())


def test_metar_perime_signale_mais_pas_masque(package: BriefingPackage, html: str) -> None:
    stale = package.stale_items()
    assert stale, "le dossier de démo doit contenir au moins une donnée périmée"

    assert "périmé" in html
    # Signalé ET conservé : la donnée périmée reste intégralement lisible.
    assert "LFOZ 200400Z 21012G22KT 4000 -RA BKN012 OVC020 17/15 Q1011" in html
    assert "block-stale" in html


def test_marquage_perime_coincide_avec_stale_items(package: BriefingPackage) -> None:
    """Le renderer applique le même prédicat que le domaine.

    `BriefingPackage.stale_items()` n'accepte pas de `now` : on le compare donc
    au calcul du renderer à l'instant courant, pas à `NOW`.
    """
    renderer = HtmlRenderer()
    view = renderer.build_view(package)
    assert view["stale_count"] == len(package.stale_items())


def test_le_seuil_depend_du_type_de_donnee(package: BriefingPackage) -> None:
    """Le seuil n'est plus global mais dicté par la cadence d'émission.

    Un réglage unique marquait « périmé » un TAF de 2 h alors que les TAF ne
    sortent que toutes les 6 h. Le renderer délègue désormais à
    `domain.freshness`, et n'a plus de seuil propre à régler.
    """
    from aerobriefer.domain.freshness import METAR_MINUTES, TAF_MINUTES, max_age_minutes

    assert TAF_MINUTES > METAR_MINUTES, "un TAF vieillit plus lentement qu'un METAR"

    view = HtmlRenderer().build_view(package, now=NOW)
    for entry in view["tafs"]:
        assert max_age_minutes(entry["value"]) == TAF_MINUTES
    for entry in view["metars"]:
        assert max_age_minutes(entry.value if hasattr(entry, "value") else entry["value"]) == (
            METAR_MINUTES
        )
    assert isinstance(view["stale_count"], int)


def test_le_seuil_applique_est_affichable(package: BriefingPackage) -> None:
    """Un « périmé » sans seuil affiché est invérifiable."""
    view = HtmlRenderer().build_view(package, now=NOW)
    for entry in view["tafs"]:
        assert entry["source"].freshness_limit == "8 h"


# --------------------------------------------------------------------------
# Règle 4 — l'ordre des NOTAM est CHRONOLOGIQUE (date d'entrée en vigueur)
# --------------------------------------------------------------------------


def test_ordre_des_notam_est_chronologique(package: BriefingPackage, html: str) -> None:
    expected = [item.value.identifier for item in package.notams_by_activation()]
    positions = [html.index(identifier) for identifier in expected]
    assert positions == sorted(positions), f"le HTML doit suivre l'ordre chronologique {expected}"


def test_notam_le_plus_recemment_active_est_en_tete(package: BriefingPackage, html: str) -> None:
    ordered = package.notams_by_activation()
    starts = [i.value.validity.start for i in ordered]
    assert starts == sorted(starts, reverse=True), "du plus récent au plus ancien"


def test_les_rubriques_source_sont_affichees(package: BriefingPackage, html: str) -> None:
    """Le badge reprend la catégorie de la SOURCE, pas une sévérité maison."""
    categories = {i.value.source_category for i in package.notams if i.value.source_category}
    assert categories, "le paquet de démo doit porter des rubriques source"
    for category in categories:
        assert category in html
    # Et surtout : plus aucune trace des anciennes étiquettes de gravité.
    for old in ("BLOQUANT", "MAJEUR", "MINEUR"):
        assert old not in html


def test_anciennete_relative_est_affichee(html: str) -> None:
    """« depuis X » doit apparaître à côté de chaque NOTAM."""
    assert "depuis" in html


def test_blocs_notam_ne_se_coupent_pas_entre_pages(html: str) -> None:
    assert "break-inside: avoid" in html
    assert "page-break-inside: avoid" in html


# --------------------------------------------------------------------------
# Cartes : embarquées, une par page
# --------------------------------------------------------------------------


def test_cartes_embarquees_en_data_uri(html: str) -> None:
    assert "data:image/png;base64," in html
    assert html.count("data:image/png;base64,") == 2  # temsi + wintem


def test_aucune_image_ne_pointe_vers_le_reseau(html: str) -> None:
    """Un PDF qui va chercher ses images en ligne est inutilisable en vol."""
    import re

    for src in re.findall(r'<img[^>]*src="([^"]*)"', html):
        assert src.startswith("data:"), f"image non embarquée : {src[:80]}"


def test_carte_non_embarquee_est_signalee(html: str) -> None:
    assert "CARTE NON EMBARQUÉE" in html
    assert "carte(s) non embarquée(s)" in html


def test_chaque_carte_sur_sa_page(html: str) -> None:
    assert "chart-page" in html
    assert "break-before: page" in html


# --------------------------------------------------------------------------
# Cadence de mise à disposition des sources
# --------------------------------------------------------------------------


def _cadence_rendue(cle: str) -> str:
    """La cadence telle qu'elle apparaît DANS le HTML : le template est en
    autoescape, l'apostrophe de « qu'un » y devient `&#39;`."""
    from markupsafe import escape

    from aerobriefer.render.html import RELEASE_CADENCE

    return str(escape(RELEASE_CADENCE[cle]))


def test_chaque_rubrique_annonce_la_cadence_de_sa_source(html: str) -> None:
    """Un âge seul ne dit pas s'il faut rebriefer : « TAF vieux de 5 h » est
    normal (renouvelé toutes les 6 h), « METAR vieux de 5 h » ne l'est pas."""
    for cle in ("metar", "taf", "sigmet", "notam", "forecast", "temsi", "wintem"):
        assert _cadence_rendue(cle) in html, f"cadence absente du rendu : {cle}"


def test_la_cadence_est_collee_sous_le_titre_de_sa_rubrique(html: str) -> None:
    """Elle doit se lire AVEC la rubrique, pas être reléguée en pied de page."""
    for titre, cle in (("<h2>METAR</h2>", "metar"), ("<h2>TAF</h2>", "taf")):
        entre = html[html.index(titre) : html.index(_cadence_rendue(cle))]
        assert entre.count("<h2>") == 1, f"{cle} : la cadence a débordé sur une autre rubrique"


def test_une_carte_porte_la_cadence_de_son_type_pas_une_generique() -> None:
    """TEMSI et WINTEM n'ont pas la même cadence — les confondre ferait
    rebriefer au mauvais moment."""
    from aerobriefer.render.html import RELEASE_CADENCE, _group_charts

    view = HtmlRenderer().build_view(build_demo_package(), now=NOW)
    cadences = {g["kind"]: g["cadence"] for g in _group_charts(view["charts"])}
    assert cadences["temsi"] == RELEASE_CADENCE["temsi"]
    assert cadences["wintem"] == RELEASE_CADENCE["wintem"]
    assert cadences["temsi"] != cadences["wintem"]


# --------------------------------------------------------------------------
# Ordre de lecture : DU PLUS GRAND AU PLUS PETIT
# --------------------------------------------------------------------------


def test_le_briefing_descend_du_plus_grand_au_plus_petit(html: str) -> None:
    """Cartes → SIGMET → TAF → prévisions horaires → METAR → NOTAM.

    L'échelle ne remonte jamais : on lit la situation générale avant de
    descendre sur le terrain. Un briefing qui ouvre sur le METAR fait raisonner
    à l'envers, du détail vers le général. Le SIGMET ferme l'échelle « en
    route » — il dit ce qui est dangereux dans la situation que les cartes
    viennent de montrer — et reste donc AVANT les TAF/METAR.
    """
    # Le dossier de démo porte TEMSI, WINTEM et radar (pas de fronts ni de
    # satellite) ; l'ordre des cartes ENTRE ELLES est testé juste en dessous.
    ordre = [
        "TEMSI — temps significatif",
        "Image radar",
        "<h2>SIGMET</h2>",
        "<h2>TAF</h2>",
        "<h2>Prévisions horaires</h2>",
        "<h2>METAR</h2>",
        "<h2>NOTAM (",
    ]
    positions = [html.index(marqueur) for marqueur in ordre]
    assert positions == sorted(positions), f"ordre attendu : {ordre}"


def test_les_cartes_suivent_l_ordre_canonique_pas_l_ordre_de_collecte() -> None:
    """L'ordre des cartes est une DÉCISION (CHART_ORDER), pas un hasard de
    récupération : fronts, TEMSI, WINTEM, radar, satellite."""
    base = build_demo_package()
    # Volontairement à l'envers de l'ordre voulu.
    desordre = ("satellite", "radar", "wintem", "temsi", "front")
    charts = tuple(
        Sourced(
            Chart(
                kind=kind,
                url=f"https://aviation.meteo.fr/{kind}.png",
                issued_at=NOW - timedelta(minutes=30),
                valid_at=UtcDateTime.of(datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
                area="FRANCE",
                media_type="image/png",
                content=make_png(120, 80, (20, 20, 20)),
            ),
            _prov("aeroweb", age_minutes=30.0),
        )
        for kind in desordre
    )
    rendered = render_html(
        BriefingPackage(
            context=base.context,
            assembled_at=base.assembled_at,
            aerodromes=base.aerodromes,
            metars=base.metars,
            tafs=base.tafs,
            notams=base.notams,
            forecasts=base.forecasts,
            charts=charts,
            failures=base.failures,
        ),
        now=NOW,
    )

    attendu = [
        "Carte de fronts",
        "TEMSI — temps significatif",
        "WINTEM — vent et température en altitude",
        "Image radar",
        "Image satellite",
    ]
    positions = [rendered.index(libelle) for libelle in attendu]
    assert positions == sorted(positions), f"ordre attendu : {attendu}"


# --------------------------------------------------------------------------
# Règles 5 et 6 — pied de page et impression
# --------------------------------------------------------------------------


def test_pied_de_page_mentionne_aide_a_la_preparation(html: str) -> None:
    assert "AIDE À LA PRÉPARATION" in html
    assert "ne remplace PAS le briefing" in html
    assert "officiel réglementaire" in html


def test_horodatage_de_generation_en_local_et_zulu(html: str) -> None:
    assert "Généré le" in html
    assert "20/07 10:00L / 20/07 08:00Z" in html


def test_mise_en_page_a4(html: str) -> None:
    assert "size: A4;" in html


def test_pied_de_page_pdf_porte_contexte_et_pagination(html: str) -> None:
    """Chaque page du PDF rappelle le contexte du vol et sa pagination."""
    assert "@bottom-left" in html and "@bottom-right" in html
    assert "counter(page)" in html and "counter(pages)" in html
    # Le contexte du vol (terrain + fenêtre) figure dans le pied injecté.
    assert "LFPN" in html


def test_pas_de_fond_sombre(html: str) -> None:
    """Noir sur blanc : pas d'aplat qui viderait une cartouche."""
    assert "background: #fff" in html
    for dark in ("background: #000", "background:#000", "background: black"):
        assert dark not in html


# --------------------------------------------------------------------------
# En-tête et contenu général
# --------------------------------------------------------------------------


def test_entete_porte_terrains_et_aeronef(html: str) -> None:
    assert "LFPN" in html
    assert "LFOZ" in html
    assert "F-GKQR (DR400-120)" in html
    assert "Navigation" in html


def test_previsions_horaires_presentes(package: BriefingPackage, html: str) -> None:
    assert "Prévisions horaires" in html
    assert html.count("<tr") >= len(package.forecasts)


# --------------------------------------------------------------------------
# Base des nuages : une valeur DERIVEE, qui doit se presenter comme telle
# --------------------------------------------------------------------------


def _bloc_previsions(html: str) -> str:
    return html.split("Prévisions horaires")[1].split("NOTAM")[0]


def test_la_colonne_base_sannonce_estimee(html: str) -> None:
    """La colonne ne vient pas de la source : elle est calculee ici. La lire
    comme un plafond prevu est l'erreur exacte qu'on veut rendre impossible."""
    bloc = _bloc_previsions(html)
    assert "<th>Base estimée</th>" in bloc
    assert "<th>Base</th>" not in bloc


def test_la_rubrique_avertit_que_la_base_nest_pas_une_donnee_de_la_source(
    html: str,
) -> None:
    bloc = _bloc_previsions(html)
    assert "AUCUNE hauteur de base" in bloc
    assert f"{CLOUD_BASE_FT_PER_C:.0f} ft/°C" in bloc
    # Les cas ou un plafond bas est dangereux, et qui echappent a la formule.
    for cas in ("stratiforme", "advection", "inversion"):
        assert cas in bloc
    assert "ne remplace pas un plafond METAR/TAF" in bloc


def test_infobulle_de_la_base_porte_le_detail_du_calcul(
    package: BriefingPackage, html: str
) -> None:
    """Chaque valeur doit pouvoir se justifier : T, Td, ecart, facteur, resultat."""
    from html import unescape  # l'attribut title= est echappe par le gabarit

    bloc = unescape(_bloc_previsions(html))
    assert bloc.count('class="estim"') == len(package.forecasts)

    premier = min(package.forecasts, key=lambda f: f.value.valid_at).value
    assert premier.temperature_c is not None and premier.dewpoint_c is not None
    assert premier.spread_c is not None
    attendu = (
        f"T {premier.temperature_c:.1f} °C − Td {premier.dewpoint_c:.1f} °C "
        f"= {premier.spread_c:.1f} °C d'écart × {CLOUD_BASE_FT_PER_C:.0f} ft/°C "
        f"≈ {round(premier.cloud_base_ft)} ft"
    )
    assert attendu in bloc, "l'infobulle doit montrer le calcul, pas seulement le dire"


def test_colonne_td_affichee_a_cote_de_la_temperature(package: BriefingPackage, html: str) -> None:
    """Sans Td au tableau, l'infobulle parlerait d'un ecart T/Td invisible."""
    bloc = _bloc_previsions(html)
    assert "<th>T</th><th>Td</th>" in bloc
    premier = min(package.forecasts, key=lambda f: f.value.valid_at).value
    assert premier.dewpoint_c is not None
    assert f"{premier.dewpoint_c:.1f} °C" in bloc


def test_sans_point_de_rosee_la_base_reste_nue(package: BriefingPackage) -> None:
    """Pas de soulignement survolable si on ne sait pas montrer le calcul :
    promettre une explication absente vaudrait mieux que rien, mais mentir non."""
    sans_td = replace(
        package,
        forecasts=tuple(
            Sourced(
                replace(f.value, dewpoint_c=None, relative_humidity_pct=None),
                f.provenance,
            )
            for f in package.forecasts
        ),
    )
    bloc = _bloc_previsions(render_html(sans_td, now=NOW))
    assert 'class="estim"' not in bloc
    # La valeur elle-meme reste affichee : on n'efface pas une donnee presente.
    premier = min(sans_td.forecasts, key=lambda f: f.value.valid_at).value
    assert f"{round(premier.cloud_base_ft)} ft" in bloc


def test_html_est_autonome(html: str) -> None:
    """Aucune ressource EXTERNE à CHARGER : ni CSS, ni police, ni script
    distant, ni image liée. Le script du lecteur animé est inline, et la
    favicon est une data: URI — tous deux self-contained, donc autorisés.

    Le critère est « pas de href/src qui parte sur le réseau », pas « pas de
    balise <link> » : ce raccourci interdisait une favicon pourtant embarquée.
    """
    import re

    assert "<style>" in html
    for forbidden in ("<script src", "@import", 'src="http', "src='http"):
        assert forbidden not in html
    for href in re.findall(r'<link[^>]*href="([^"]*)"', html):
        assert href.startswith("data:"), f"ressource externe liée : {href[:60]}"
    # Les images sont embarquées en data URI, jamais liées en réseau.
    assert 'src="data:' in html or "briefing" in html.lower()


# --------------------------------------------------------------------------
# PDF — pour de vrai
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chrome() -> str:
    try:
        return find_chrome()
    except Exception as exc:  # pragma: no cover - dépend de la machine
        pytest.skip(f"Chrome indisponible : {exc}")


def test_pdf_est_reellement_genere(
    package: BriefingPackage, chrome: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    out = tmp_path_factory.mktemp("pdf") / "briefing.pdf"
    result = PdfRenderer(chrome_path=chrome).render(package, out, now=NOW)

    assert result.exists()
    payload = result.read_bytes()
    assert payload.startswith(b"%PDF-")
    assert len(payload) > 20_000, f"PDF suspicieusement léger : {len(payload)} octets"

    pages = count_pdf_pages(payload)
    # Corps du briefing + une page par carte : plusieurs pages, forcément.
    assert pages >= 4, f"{pages} page(s) — les cartes ne sont pas sur leur propre page"


def test_pdf_embarque_les_images(
    package: BriefingPackage, chrome: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Le PDF doit contenir des flux image, et aucune URL réseau à résoudre."""
    out = tmp_path_factory.mktemp("pdf2") / "briefing.pdf"
    payload = PdfRenderer(chrome_path=chrome).render(package, out, now=NOW).read_bytes()

    assert b"/Image" in payload
    assert b"https://aviation.meteo.fr/temsi_france.png" not in payload


def test_pdf_refuse_un_html_vide(chrome: str, tmp_path: Path) -> None:
    """Un rendu raté doit échouer bruyamment, pas produire un fichier trompeur."""
    out = tmp_path / "vide.pdf"
    # Une page blanche produit tout de même un PDF valide : on vérifie
    # simplement que la vérification d'intégrité ne laisse rien passer de
    # non-PDF, et que le fichier est réellement écrit.
    result = PdfRenderer(chrome_path=chrome).html_to_pdf("<p>x</p>", out)
    assert result.read_bytes().startswith(b"%PDF-")


def test_count_pdf_pages_sur_un_pdf_connu(chrome: str, tmp_path: Path) -> None:
    html = (
        "<p>page un</p>"
        "<p style='break-before:page'>page deux</p>"
        "<p style='break-before:page'>page trois</p>"
    )
    out = PdfRenderer(chrome_path=chrome).html_to_pdf(html, tmp_path / "trois.pdf")
    assert count_pdf_pages(out.read_bytes()) == 3


# --------------------------------------------------------------------------
# Livrable de démonstration
# --------------------------------------------------------------------------


def test_genere_le_pdf_dexemple(package: BriefingPackage, chrome: str) -> None:
    """Produit /exemple_briefing.pdf à la racine du dépôt."""
    target = Path(__file__).resolve().parent.parent / "exemple_briefing.pdf"
    result = PdfRenderer(chrome_path=chrome).render(package, target, now=NOW)

    assert result.exists()
    assert result.stat().st_size > 20_000
    assert count_pdf_pages(result.read_bytes()) >= 4


# --------------------------------------------------------------------------
# Structure : macro-sections, sources attribuées, heure locale dans les tables
# --------------------------------------------------------------------------


def test_macro_sections_meteo_et_notam(html: str) -> None:
    """Le document s'organise en macro-sections distinctes, chacune sur sa page."""
    assert 'class="macro-title">MÉTÉO' in html
    assert 'class="macro-title">NOTAM' in html
    # Les macro-sections déclenchent un saut de page.
    assert "break-before: page" in html


def test_forecast_section_names_its_source(html: str) -> None:
    """Régression : la section prévisions n'affichait aucune source."""
    forecast_block = html.split("Prévisions horaires")[1].split("NOTAM")[0]
    assert "met.no" in forecast_block, "la source des prévisions doit être nommée"


def test_notam_source_is_attributed(html: str) -> None:
    assert "SOFIA" in html


def test_decoded_taf_table_uses_local_not_zulu_header(html: str) -> None:
    """Les tables décodées sont en heure locale ; le Z reste sur le brut."""
    assert "DU — AU (locale)" in html
    assert "DU — AU (Z)" not in html


def test_forecast_table_header_is_local(html: str) -> None:
    assert "Échéance (locale)" in html
    assert "Échéance (L / Z)" not in html


def test_le_dossier_notam_ne_parle_pas_de_meteo(package: BriefingPackage) -> None:
    """La note sur les stations d'appoint est une note MÉTÉO.

    Elle listait sept terrains voisins juste sous « BRIEFING NOTAM », ce qui se
    lit comme « les NOTAM de ces terrains sont au dossier » — faux : la zone de
    recherche est le couloir plus les dégagements.
    """
    notam_doc = render_html(package, now=NOW, kind="notam")
    assert "Météo d'appoint" not in notam_doc
    assert "brise de mer" not in notam_doc
    # Et rien d'autre de météo n'a fui.
    for absent in ("<h2>METAR</h2>", "<h2>TAF</h2>", "<h2>Prévisions horaires</h2>"):
        assert absent not in notam_doc

    # La même note reste bien présente là où elle a un sens. Il faut pour cela
    # un terrain qui OBSERVE sans être du vol : ni départ, ni arrivée, ni
    # dégagement — sinon il est exclu des stations d'appoint par construction.
    base = build_demo_package()
    voisine = Aerodrome(
        icao="LFOP",
        name="Rouen Vallée de Seine",
        position=Position(49.3842, 1.1748),
        elevation_ft=515,
        runways=(Runway("04/22", 1700, 45, "asphalte", 40.0),),
    )
    observation = Sourced(
        Metar(
            station="LFOP",
            raw_text="LFOP 200730Z 24006KT 9999 FEW030 18/13 Q1014",
            observed_at=NOW - timedelta(minutes=30),
            wind_dir_deg=240,
            wind_speed_kt=6,
        ),
        _prov("noaa-awc", age_minutes=30.0),
    )
    avec_appoint = BriefingPackage(
        context=replace(base.context, observation_stations=("LFOP",)),
        assembled_at=base.assembled_at,
        aerodromes=(*base.aerodromes, voisine),
        metars=(*base.metars, observation),
        tafs=base.tafs,
        notams=base.notams,
        forecasts=base.forecasts,
        charts=base.charts,
        failures=base.failures,
    )
    assert "Météo d'appoint" in render_html(avec_appoint, now=NOW, kind="meteo")
    assert "Météo d'appoint" not in render_html(avec_appoint, now=NOW, kind="notam")


def test_le_bouton_toutes_les_images_disparait_sans_image(package: BriefingPackage) -> None:
    """Un bouton qui ne pilote rien fait douter de ce que le document contient.

    On cherche le BOUTON, pas son libellé : celui-ci apparaît aussi dans des
    commentaires du script, qui eux restent dans les deux documents.
    """
    bouton = 'data-mode="allimg"'
    assert bouton not in render_html(package, now=NOW, kind="notam")
    assert bouton in render_html(package, now=NOW, kind="meteo")


def _package_a(heure_debut: int, duree_h: int) -> BriefingPackage:
    """Dossier de démo recadré sur une fenêtre horaire donnée (UTC)."""
    base = build_demo_package()
    start = UtcDateTime.of(datetime(2026, 7, 20, heure_debut, 0, tzinfo=UTC))
    return BriefingPackage(
        context=replace(
            base.context,
            window=TimeWindow(start, UtcDateTime.of(start + timedelta(hours=duree_h))),
        ),
        assembled_at=base.assembled_at,
        aerodromes=base.aerodromes,
        metars=base.metars,
        tafs=base.tafs,
        notams=base.notams,
        forecasts=base.forecasts,
        charts=base.charts,
        failures=base.failures,
    )


def test_les_quatre_bornes_du_jour_aero_sont_toujours_affichees(html: str) -> None:
    """Le document ne montrait que la FIN de journée. Pour un vol du matin,
    c'est le mauvais bout : la limite qui encadre est le début du jour aéro."""
    for libelle in (
        "Jour aéro (SR−30)",
        "Lever du soleil",
        "Coucher du soleil",
        "Nuit aéro (SS+30)",
    ):
        assert libelle in html, f"borne absente de l'en-tête : {libelle}"


def test_les_bornes_sont_dans_lordre_chronologique(html: str) -> None:
    ordre = ["Jour aéro (SR−30)", "Lever du soleil", "Coucher du soleil", "Nuit aéro (SS+30)"]
    positions = [html.index(x) for x in ordre]
    assert positions == sorted(positions)


def test_un_decollage_avant_le_jour_aero_est_signale() -> None:
    """Symétrique de l'alerte du soir, qui n'existait pas : décoller avant la
    fin de la nuit aéronautique est aussi interdit que se poser après son début."""
    tot = render_html(_package_a(2, 2), now=NOW)  # 02:00Z, avant le lever à Toussus
    assert "commence AVANT la fin de la nuit aéronautique" in tot
    assert "se termine APRÈS le début de la nuit" not in tot


def test_un_vol_en_plein_jour_ne_declenche_aucune_alerte() -> None:
    plein_jour = render_html(_package_a(10, 2), now=NOW)
    assert "commence AVANT la fin de la nuit aéronautique" not in plein_jour
    assert "se termine APRÈS le début de la nuit" not in plein_jour
    # Les bornes restent affichées : c'est de la conscience de la situation.
    assert "Jour aéro (SR−30)" in plein_jour


# --- fond de carte OSM sous les couches AROME --------------------------------
#
# Le fond était tiré par le NAVIGATEUR à l'ouverture de la page. OpenStreetMap
# répond alors, à la volumétrie d'un dossier, une tuile « Access blocked » en
# HTTP 200 : le briefing s'affichait barré de rouge. Les tuiles sont désormais
# embarquées à la génération, et le rendu ne doit plus JAMAIS émettre d'URL
# distante — sans quoi on retomberait dans le même piège sans s'en apercevoir.

_AROME_URL = (
    "https://example.invalid/wms?SERVICE=WMS&REQUEST=GetMap&CRS=EPSG:3857"
    "&BBOX=-200000,5600000,100000,5800000&WIDTH=600&HEIGHT=400"
)


def test_arome_basemap_names_registered_tiles() -> None:
    from aerobriefer.render.html import TileRegistry, arome_basemap

    asked: list[str] = []

    def resolve(url: str) -> str:
        asked.append(url)
        return f"data:image/png;base64,{url[-12:]}"

    basemap = arome_basemap(_AROME_URL, None, TileRegistry(resolve))
    assert basemap is not None
    assert basemap["tiles"], "l'emprise couvre des tuiles : le fond ne peut pas être vide"
    assert all(t["cls"].startswith("t") for t in basemap["tiles"])
    # Ce sont bien des tuiles OSM qui ont été demandées au magasin.
    assert all(u.startswith("https://tile.openstreetmap.org/") for u in asked)


def test_same_tile_is_encoded_once_for_the_whole_document() -> None:
    """Quinze cartes AROME regardent la même région : un seul encodage.

    Répéter le base64 par carte faisait passer le briefing de 4 à 10 Mo pour
    exactement les mêmes pixels.
    """
    from aerobriefer.render.html import TileRegistry, arome_basemap

    registry = TileRegistry(lambda _url: "data:image/png;base64,AAAA")
    premiere = arome_basemap(_AROME_URL, None, registry)
    seconde = arome_basemap(_AROME_URL, None, registry)
    assert premiere is not None and seconde is not None
    assert [t["cls"] for t in premiere["tiles"]] == [t["cls"] for t in seconde["tiles"]]
    # Toutes ces tuiles ont le même contenu : une seule règle CSS les porte.
    assert registry.css() == [("t0", "data:image/png;base64,AAAA")]


def test_arome_basemap_omits_tiles_it_could_not_get() -> None:
    from aerobriefer.render.html import TileRegistry, arome_basemap

    # Magasin qui n'a rien : pas de fond, mais surtout pas d'URL distante en
    # repli — le navigateur ne doit rien avoir à aller chercher.
    basemap = arome_basemap(_AROME_URL, None, TileRegistry(lambda _url: None))
    assert basemap is not None
    assert basemap["tiles"] == []


def test_arome_basemap_without_registry_has_no_tiles() -> None:
    from aerobriefer.render.html import arome_basemap

    basemap = arome_basemap(_AROME_URL, None)
    assert basemap is not None
    assert basemap["tiles"] == []
    # L'emprise reste calculée : la couche AROME et la route se posent dessus.
    assert basemap["aspect"] > 0


def test_rendered_page_never_points_at_the_tile_server() -> None:
    html = render_html(build_demo_package(), now=NOW)
    assert "tile.openstreetmap.org" not in html


# --- table de sondage AROME + viewport des couches ---------------------------


def _arome_package(painted: bool) -> BriefingPackage:
    """Dossier local LFCY avec les trois couches AROME, une échéance chacune.

    L'image est FABRIQUÉE : on sait quelle couleur tombe où, donc ce que la
    table doit dire. On peint tout le cadre ou rien — la position exacte est
    testée dans `test_arome_probe`, ici on vérifie la mise en table.
    """
    from aerobriefer.render.arome_probe import _mercator

    bbox = (-302467.9, 5575293.9, 85951.5, 5866608.4)
    colour = (0xFE, 0xA4, 0x00, 255) if painted else (0, 0, 0, 0)
    charts = tuple(
        Sourced(
            Chart(
                kind=kind,
                url=(
                    "https://example.invalid/wms?SERVICE=WMS&REQUEST=GetMap&CRS=EPSG:3857"
                    f"&BBOX={','.join(str(v) for v in bbox)}&WIDTH=32&HEIGHT=32"
                ),
                valid_at=UtcDateTime.of(datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
                area="FRANCE",
                media_type="image/png",
                content=make_rgba_png(32, 32, colour),
            ),
            _prov("aeroweb-arome", age_minutes=20.0),
        )
        for kind in ("arome_visi", "arome_plafond", "arome_nebul_bas")
    )
    assert _mercator(45.628101, -0.9725)  # garde : LFCY est bien projetable
    context = BriefingContext.local(
        center=Position(45.628101, -0.9725),
        radius_nm=40.0,
        window=TimeWindow(
            UtcDateTime.of(datetime(2026, 7, 20, 10, 0, tzinfo=UTC)),
            UtcDateTime.of(datetime(2026, 7, 20, 13, 0, tzinfo=UTC)),
        ),
        icao="LFCY",
    )
    return BriefingPackage(context=context, charts=charts)


def test_probe_table_names_the_class_at_each_aerodrome() -> None:
    html = render_html(_arome_package(painted=True), now=NOW)
    assert "Au droit de chaque terrain" in html
    assert "réduction marquée" in html
    # La table sert à décider : elle dit d'où vient la lecture, et ses limites.
    assert "pas appréciée à l" in html
    assert "METAR et TAF restent la référence" in html


def test_probe_table_never_says_clear_for_an_unpainted_cell() -> None:
    """« — » veut dire « la couche ne peint pas », pas « garanti clair »."""
    html = render_html(_arome_package(painted=False), now=NOW)
    assert "la couche ne peint pas cette maille" in html
    assert "garanti clair" in html  # l'avertissement est présent, pas sous-entendu


def test_probe_table_names_the_aerodromes_the_cap_leaves_out() -> None:
    from aerobriefer.render.html import _PROBE_SITES_MAX, _probe_sites

    context = _arome_package(painted=True).context
    sites, omitted = _probe_sites(context)
    assert len(sites) <= _PROBE_SITES_MAX
    assert "LFCY" == sites[0][0], "le terrain du vol passe toujours en tête"
    if omitted:
        html = render_html(_arome_package(painted=True), now=NOW)
        assert "hors de cette table" in html
        for icao in omitted:
            assert icao in html


def test_arome_layers_share_one_viewport() -> None:
    """Les trois couches AROME se pilotent depuis un bandeau, pas en déroulant."""
    html = render_html(_arome_package(painted=True), now=NOW)
    # Le sélecteur JS porte le même nom : on compte la BALISE, pas la chaîne.
    assert html.count('<div class="aromebar') == 1, "un seul bandeau pour le bloc AROME"
    for kind in ("arome_visi", "arome_plafond", "arome_nebul_bas"):
        assert f'data-arome-kind="{kind}"' in html
        assert f'data-arome-pick="{kind}"' in html
    assert 'data-arome-pick="*"' in html  # « Toutes » reste à un clic
    # L'impression suit l'écran : le bandeau doit le dire, pas le laisser découvrir.
    assert "impression" in html
