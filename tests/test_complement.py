"""Tests de l'index du Complément aux cartes aéronautiques.

Le parsing est testé sur du TEXTE, pas sur un PDF : `parse_pages` est pure et
prend les pages déjà extraites. On peut donc éprouver les cas tordus rencontrés
sur le document réel (zone sans nom, suffixe collé au plafond, zone étalée sur
deux pages) sans fabriquer de PDF ni dépendre de `pypdf`.
"""

from __future__ import annotations

from pathlib import Path

from aerobriefer.data.complement import (
    ComplementIndex,
    DocumentInfo,
    build_index,
    index_path_for,
    load_index,
    normalize_zone_id,
    parse_document_info,
    parse_pages,
    write_index,
)

# Mise en page réelle du complément : les colonnes se retrouvent fusionnées sur
# une seule ligne, l'identifiant collé à la valeur du plafond.
PAGE_ZONES = (
    "Plafond / Upper limit 4000ft AMSLLF R 49 L1 COGNAC "
    "Plancher / Lower limit 3000ft AMSL\n"
    "Horaires d'activation / Hours H24\n"
    "Plafond / Upper limit 1500ft ASFCLF R 49 S SAINTES Plancher / Lower limit SFC\n"
    "Plafond / Upper limit 800ft ASFCLF R 4 A Plancher / Lower limit SFC\n"
    "Plafond / Upper limit 3300ft AMSLLF P 1 BLAYAIS-BRAUD ET SAINT LOUIS "
    "Plancher / Lower limit SFC\n"
    "Conditions de pénétration La désactivation de la LF R 49 E2 est annoncée par le SIV\n"
)

COUVERTURE = (
    "Complément aux cartes \naéronautiques \n"
    "Informations aéronautiques en vigueur : \n16 APR 2026 \n"
    "Prochaine édition : automne 2026\n1ère édition  \n2026\n"
)


def test_identifiant_normalise_toutes_les_ecritures() -> None:
    """Le complément imprime « LF R 49 L1 », les NOTAM écrivent « LF-R49L1 »."""
    for ecriture in ("LF R 49 L1", "LF-R49L1", "LFR49L1", "lf-r49 l1"):
        assert normalize_zone_id(ecriture) == "LFR49L1"


def test_parse_extrait_zone_limites_et_page_pdf() -> None:
    zones = parse_pages(["", "", PAGE_ZONES])  # la zone est en page PDF 3
    z = zones["LFR49L1"]
    assert z.label == "LF R 49 L1 COGNAC"
    assert z.name == "COGNAC"
    assert z.upper_limit == "4000ft AMSL"
    assert z.lower_limit == "3000ft AMSL"
    assert z.pages == [3], "la page stockée est celle du FICHIER, base 1"


def test_zone_sans_nom_garde_son_suffixe() -> None:
    """« LF R 4 A » : le A est le suffixe, pas un nom. Le confondre créerait
    une zone « LF R 4 » qui n'existe pas."""
    zones = parse_pages([PAGE_ZONES])
    assert "LFR4A" in zones
    assert "LFR4" not in zones
    assert zones["LFR4A"].name == ""


def test_la_prose_ne_cree_pas_de_zone() -> None:
    """Une zone citée dans un paragraphe n'est pas une fiche : seule la fiche
    porte le plafond et le plancher."""
    zones = parse_pages([PAGE_ZONES])
    assert "LFR49E2" not in zones


def test_une_zone_sur_deux_pages_liste_les_deux() -> None:
    zones = parse_pages([PAGE_ZONES, PAGE_ZONES])
    assert zones["LFR49L1"].pages == [1, 2]


def test_couverture_donne_edition_et_date_dentree_en_vigueur() -> None:
    edition, iso, brut = parse_document_info(COUVERTURE)
    assert edition == "1ère édition 2026"
    assert iso == "2026-04-16"
    assert brut == "16 APR 2026"


def test_couverture_illisible_ne_ment_pas() -> None:
    """Mieux vaut un index qui ignore son édition qu'un index qui l'invente."""
    assert parse_document_info("page de garde sans rien") == ("", "", "")


def test_nom_du_fichier_compagnon() -> None:
    p = index_path_for(Path("/ref/complement-cartes-2026-1.pdf"))
    assert p.name == "complement-cartes-2026-1.pdf.index.json"
    assert p.parent == Path("/ref"), "le compagnon vit À CÔTÉ de son PDF"


def test_aller_retour_json_et_resolution(tmp_path: Path) -> None:
    index = ComplementIndex(
        document=DocumentInfo(filename="c.pdf", page_count=2),
        zones=parse_pages([PAGE_ZONES]),
    )
    path = tmp_path / "c.pdf.index.json"
    write_index(index, path)
    relu = load_index(path)

    assert relu.zones == index.zones
    zone = relu.lookup("LF-R49L1")
    assert zone is not None and zone.pages == [1]
    assert relu.lookup("LF-R999") is None


def test_index_ecrit_est_deterministe(tmp_path: Path) -> None:
    """Deux constructions du même document doivent donner le même octet : un
    diff entre deux éditions ne doit montrer que ce qui a changé."""
    index = ComplementIndex(
        document=DocumentInfo(filename="c.pdf"), zones=parse_pages([PAGE_ZONES])
    )
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_index(index, a)
    write_index(load_index(a), b)
    assert a.read_text() == b.read_text()


def test_build_index_est_bien_expose() -> None:
    """Garde-fou d'import : `build_index` tire `pypdf` en import LOCAL, pour que
    la chaîne de rendu n'en dépende jamais."""
    assert callable(build_index)
