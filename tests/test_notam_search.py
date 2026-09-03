"""Traçabilité du briefing NOTAM : zone de recherche, dégagements, SUP AIP.

Trois exigences distinctes, toutes vérifiables sans réseau :

1. Un dégagement DÉCLARÉ entre dans la zone de recherche — sans quoi ses NOTAM
   manquent silencieusement au dossier.
2. La zone réellement interrogée est écrite noir sur blanc : « 28 NOTAM » ne
   veut rien dire si on ignore sur quelle surface ils ont été cherchés.
3. Un NOTAM qui renvoie à un SUP AIP est signalé — son texte ne contient pas
   l'information, il l'annonce.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Circle, Corridor, Position, Union
from aerobriefer.domain.route import Route, Waypoint
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.navplan import NavPlan
from aerobriefer.render.html import _search_zones, aip_sup_references

# ---------------------------------------------------------------------------
# 1. Géométrie : l'union fait entrer le dégagement sans élargir le couloir
# ---------------------------------------------------------------------------

LFCY = Position(45.62, -0.97)
LFBN = Position(46.31, -0.39)
LFFK = Position(46.44, -0.79)  # Fontenay-le-Comte, NETTEMENT à l'écart de la route


def _corridor() -> Corridor:
    return Corridor([LFCY, LFBN], half_width_nm=10.0)


def test_un_degagement_ecarte_est_hors_du_couloir() -> None:
    """Prémisse de toute la mécanique : un dégagement utile n'est pas sous la
    route, sinon il ne dégagerait de rien."""
    assert _corridor().contains(LFFK) is False


def test_lunion_le_fait_entrer_sans_elargir_le_couloir() -> None:
    zone = Union((_corridor(), Circle(LFFK, 10.0)))
    assert zone.contains(LFFK) is True
    # Un point à l'écart des DEUX reste dehors : on n'a pas ouvert les vannes.
    ailleurs = Position(44.0, 2.0)
    assert zone.contains(ailleurs) is False


def test_le_cercle_englobant_couvre_toutes_les_parties() -> None:
    zone = Union((_corridor(), Circle(LFFK, 10.0)))
    englobant = zone.bounding_circle()
    for point in (LFCY, LFBN, LFFK):
        assert englobant.contains(point), f"{point} hors du cercle de collecte"


def test_une_union_vide_est_refusee() -> None:
    import pytest

    with pytest.raises(ValueError, match="au moins une zone"):
        Union(())


# ---------------------------------------------------------------------------
# 2. Les paramètres de recherche sont écrits
# ---------------------------------------------------------------------------


def _context(alternates: tuple[str, ...], geometry: object) -> BriefingContext:
    window = TimeWindow(
        UtcDateTime.of(datetime(2026, 8, 30, 8, 0, tzinfo=UTC)),
        UtcDateTime.of(datetime(2026, 8, 30, 10, 0, tzinfo=UTC)),
    )
    return BriefingContext(
        geometry=geometry,  # type: ignore[arg-type]
        window=window,
        origin_icao="LFCY",
        destination_icao="LFBN",
        alternates_icao=alternates,
    )


def test_la_zone_decrite_nomme_le_couloir_et_chaque_degagement() -> None:
    zone = Union((_corridor(), Circle(LFFK, 10.0)))
    labels = _search_zones(_context(("LFFK",), zone))
    assert labels == [
        "Couloir de ±10 NM de part et d'autre de la route (2 points)",
        "Cercle de 10 NM autour de LFFK",
    ]


def test_un_brief_local_nomme_son_terrain_pas_un_degagement() -> None:
    labels = _search_zones(_context((), Circle(LFCY, 20.0)))
    assert labels == ["Cercle de 20 NM autour de LFCY"]


def test_on_ne_nomme_pas_les_cercles_si_le_compte_ne_correspond_pas() -> None:
    """Mieux vaut un libellé imprécis qu'un appariement FAUX : un cercle
    étiqueté du mauvais terrain tromperait sur ce qui a été cherché."""
    zone = Union((_corridor(), Circle(LFFK, 10.0)))
    labels = _search_zones(_context(("LFFK", "LFDN"), zone))
    assert "LFFK" not in labels[1]


# ---------------------------------------------------------------------------
# 3. Dégagements déclarés dans le plan de nav
# ---------------------------------------------------------------------------


def _plan(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "date": "2026-08-30",
        "duree_h": 2.0,
        "depart": "LFCY",
        "points": [{"nom": "LFBN"}],
    }
    base.update(extra)
    return base


def test_le_plan_accepte_des_degagements_et_leur_rayon() -> None:
    plan = NavPlan.model_validate(_plan(degagements=["LFFK"], degagement_rayon_nm=12.0))
    assert plan.degagements == ["LFFK"]
    assert plan.degagement_rayon_nm == 12.0


def test_sans_degagement_le_plan_reste_valide() -> None:
    """Rétrocompatibilité : les navs déjà écrites ne portent pas le champ."""
    assert NavPlan.model_validate(_plan()).degagements == []


def test_un_degagement_mal_ecrit_casse_le_chargement() -> None:
    import pytest

    with pytest.raises(ValueError, match="OACI"):
        NavPlan.model_validate(_plan(degagements=["Fontenay"]))


def test_un_degagement_en_double_casse_le_chargement() -> None:
    import pytest

    with pytest.raises(ValueError, match="doublon"):
        NavPlan.model_validate(_plan(degagements=["LFFK", "LFFK"]))


# ---------------------------------------------------------------------------
# 4. SUP AIP
# ---------------------------------------------------------------------------


def test_les_deux_ecritures_du_sup_aip_sont_reconnues() -> None:
    """Le même dossier porte « SUP AIP 162/26 » (français) et « AIP SUP 162/26 »
    (OACI), parfois avec AIRAC intercalé."""
    assert aip_sup_references("NOTAM TRIGGER - SUP AIP 162/26") == ["162/26"]
    assert aip_sup_references("TRIGGER NOTAM - AIP SUP 162/26") == ["162/26"]
    assert aip_sup_references("TRIGGER NOTAM - AIRAC AIP SUP 207/25") == ["207/25"]
    assert aip_sup_references("SUP AIP AIRAC 207/25") == ["207/25"]


def test_les_references_sont_dedoublonnees_et_triees() -> None:
    """Un NOTAM bilingue cite la même référence deux fois : une seule doit sortir."""
    refs = aip_sup_references(
        "NOTAM TRIGGER - SUP AIP 188/25 ... CE SUP AIP EST DISPONIBLE",
        "TRIGGER NOTAM - AIP SUP 188/25 ... THIS AIP SUP IS AVBL",
    )
    assert refs == ["188/25"]
    assert aip_sup_references("AIP SUP 055/26", "SUP AIP 053/26") == ["053/26", "055/26"]


def test_un_notam_ordinaire_ne_declenche_rien() -> None:
    assert aip_sup_references("RWY 10/28 CLSD DUE WIP") == []
    assert aip_sup_references(None) == []
    # « SUP AIP » sans référence exploitable ne doit pas fabriquer de renvoi.
    assert aip_sup_references("THIS AIP SUP IS AVBL AT WWW.SIA.AVIATION-CIVILE.GOUV.FR") == []


def test_la_route_reste_intacte() -> None:
    """Garde-fou : ajouter des dégagements ne doit pas toucher aux points."""
    route = Route([Waypoint("LFCY", LFCY), Waypoint("LFBN", LFBN)])
    assert [w.name for w in route.waypoints] == ["LFCY", "LFBN"]


# ---------------------------------------------------------------------------
# 5. Liens vers les suppléments à l'AIP
# ---------------------------------------------------------------------------


def test_un_supaip_resolu_est_lie_vers_son_document() -> None:
    from aerobriefer.data.supaip import SupAipEntry, SupAipIndex
    from aerobriefer.render.html import HtmlRenderer

    renderer = HtmlRenderer()
    renderer._supaip = SupAipIndex(
        entries={
            "162/2026": SupAipEntry(
                reference="162/2026", url="https://sia.example/doc/15066172/", title="ZRT COGNAC"
            )
        }
    )
    link = renderer._aip_sup_link("162/26")
    assert link["url"] == "https://sia.example/doc/15066172/"
    assert link["title"] == "ZRT COGNAC"
    assert link["resolved"] == "1"


def test_un_supaip_inconnu_renvoie_la_liste_jamais_une_url_inventee() -> None:
    from aerobriefer.data.supaip import FALLBACK_URL, SupAipIndex
    from aerobriefer.render.html import HtmlRenderer

    renderer = HtmlRenderer()
    renderer._supaip = SupAipIndex()
    link = renderer._aip_sup_link("999/26")
    assert link["url"] == FALLBACK_URL
    assert link["resolved"] == ""
    assert not link["url"].endswith(".pdf"), "aucune URL de PDF ne doit être fabriquée"


def test_la_copie_locale_prime_sur_le_lien_du_sia() -> None:
    """Un dossier emporté en vol doit s'ouvrir sans réseau."""
    from aerobriefer.data.supaip import SupAipEntry, SupAipIndex
    from aerobriefer.render.html import HtmlRenderer

    renderer = HtmlRenderer(supaip_local={"162/2026": "sup_aip/sup_aip_162-2026.pdf"})
    renderer._supaip = SupAipIndex(
        entries={
            "162/2026": SupAipEntry(
                reference="162/2026", url="https://sia.example/doc/1/", title=""
            )
        }
    )
    link = renderer._aip_sup_link("162/26")
    assert link["url"] == "sup_aip/sup_aip_162-2026.pdf"
    assert link["local"] == "1"


def test_un_vol_local_nomme_ses_degagements_pas_son_terrain() -> None:
    """Sur un vol LOCAL la géométrie du vol est elle-même un cercle : compter
    les cercles depuis le début décalait tout d'un rang et étiquetait les
    dégagements du nom du terrain de départ. On apparie par la FIN, les cercles
    des dégagements étant toujours ajoutés en dernier."""
    zone = Union((Circle(LFCY, 20.0), Circle(LFFK, 10.0), Circle(LFBN, 10.0)))
    labels = _search_zones(_context(("LFFK", "LFBN"), zone))
    assert labels == [
        "Cercle de 20 NM autour de LFCY",
        "Cercle de 10 NM autour de LFFK",
        "Cercle de 10 NM autour de LFBN",
    ]
