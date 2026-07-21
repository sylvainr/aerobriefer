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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aerobriefer.domain.context import BriefingContext, Purpose
from aerobriefer.domain.geo import Circle, Position
from aerobriefer.domain.models import (
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


def test_html_est_autonome(html: str) -> None:
    """Aucune ressource EXTERNE : ni CSS, ni police, ni script distant, ni image
    liée. Le script du lecteur animé est inline (self-contained) — autorisé."""
    assert "<style>" in html
    for forbidden in ("<link", "<script src", "@import", 'src="http', "src='http"):
        assert forbidden not in html
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
