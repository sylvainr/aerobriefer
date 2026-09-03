"""Index des suppléments à l'AIP — parsing et résolution, sans réseau.

Ce qu'on éprouve ici est ce qui serait DANGEREUX en cas d'erreur : une
référence mal normalisée pointerait vers le mauvais supplément, ou vers rien.
Le parsing est testé sur un fragment de la page réelle du SIA plutôt que sur du
HTML inventé — c'est le contrat observé qui compte.
"""

from __future__ import annotations

from aerobriefer.data.supaip import (
    FALLBACK_URL,
    LIST_URL,
    SupAipEntry,
    SupAipIndex,
    normalize_reference,
    parse_list,
)

# Fragment repris tel quel de la liste SUP AIP MÉTROPOLE (2026-08-31).
PAGE = """
<tr>
  <td class="td_icone_liste_document"><img src="pdf_icon.png"/></td>
  <td class="td_lien_liste_document">
    <a target="_blank" class="lien_sup_aip" href="https://www.sia.aviation-civile.gouv.fr/documents/download/f/d/15066172/">
      <b>162/2026</b> <span>Cr&eacute;ation de 2 Zones R&eacute;glement&eacute;es Temporaires (ZRT)
      dans la r&eacute;gion de COGNAC <i class="fas fa-external-link-alt"></i></span>
    </a>
  </td>
</tr>
<tr>
  <td class="td_lien_liste_document">
    <a target="_blank" class="lien_sup_aip" href="https://www.sia.aviation-civile.gouv.fr/documents/download/f/d/14634691/">
      <b>065/2025</b> <span>Cr&eacute;ation de deux ZRT
      <i class="fas fa-external-link-alt"></i></span>
    </a>
  </td>
</tr>
"""


def test_lecture_de_la_liste_reelle() -> None:
    entries = parse_list(PAGE)
    assert set(entries) == {"065/2025", "162/2026"}
    zrt = entries["162/2026"]
    assert zrt.url.endswith("/15066172/")
    assert "COGNAC" in zrt.title
    assert "<" not in zrt.title, "le titre doit être du texte, pas du HTML"
    assert "&eacute;" not in zrt.title, "les entités HTML doivent être décodées"


def test_lidentifiant_du_document_est_repris_tel_quel() -> None:
    """Il est OPAQUE : rien dans « 162/2026 » ne permet de le recalculer. Le
    reconstruire, c'est risquer le mauvais supplément dans un dossier de vol."""
    assert parse_list(PAGE)["162/2026"].url == (
        "https://www.sia.aviation-civile.gouv.fr/documents/download/f/d/15066172/"
    )


def test_normalisation_des_deux_ecritures_dannee() -> None:
    """Les NOTAM écrivent « 162/26 », le SIA « 162/2026 »."""
    assert normalize_reference("162/26") == "162/2026"
    assert normalize_reference("162/2026") == "162/2026"


def test_les_zeros_de_tete_sont_conserves() -> None:
    """« 65/2025 » ne se retrouverait pas dans la liste, qui écrit « 065/2025 »."""
    assert normalize_reference("065/25") == "065/2025"
    assert normalize_reference("065/25") != "65/2025"


def test_une_reference_incomprehensible_ne_produit_rien() -> None:
    for mauvais in ("", "AIP SUP", "abc/26", "162-26", "162/2"):
        assert normalize_reference(mauvais) == ""


def test_resolution_depuis_lecriture_dun_notam() -> None:
    index = SupAipIndex(entries=parse_list(PAGE))
    entry = index.lookup("162/26")
    assert entry is not None and entry.url.endswith("/15066172/")


def test_une_reference_absente_ne_resout_pas() -> None:
    """Un supplément d'outre-mer ou trop ancien n'est pas dans cette liste : on
    doit le savoir, pas recevoir un lien approximatif."""
    index = SupAipIndex(entries=parse_list(PAGE))
    assert index.lookup("999/26") is None


def test_le_repli_est_la_liste_officielle_pas_une_url_fabriquee() -> None:
    assert FALLBACK_URL == LIST_URL
    assert FALLBACK_URL.startswith("https://www.sia.aviation-civile.gouv.fr/")
    assert not FALLBACK_URL.endswith(".pdf")


def test_aller_retour_json() -> None:
    index = SupAipIndex(entries=parse_list(PAGE))
    relu = SupAipIndex.model_validate_json(index.model_dump_json())
    assert relu.entries == index.entries
    assert isinstance(relu.entries["162/2026"], SupAipEntry)


def test_le_rendu_ne_touche_jamais_au_reseau(monkeypatch, tmp_path) -> None:
    """Règle du module de rendu : aucun appel réseau. Elle avait été enfreinte —
    `HtmlRenderer.__init__` chargeait l'index avec le réseau autorisé, et ça ne
    se voyait que parce que le CLI avait rempli le cache juste avant.
    """
    import httpx

    from aerobriefer.data import supaip
    from aerobriefer.render.html import HtmlRenderer

    def interdit(*args: object, **kwargs: object) -> None:
        raise AssertionError("le rendu a tenté un appel réseau")

    monkeypatch.setattr(httpx.Client, "get", interdit)
    monkeypatch.setattr(supaip, "index_path", lambda: tmp_path / "absent.json")

    renderer = HtmlRenderer()  # sans cache, sans index fourni : doit rester muet
    assert renderer._supaip.entries == {}
    assert renderer._aip_sup_link("162/26")["url"] == supaip.FALLBACK_URL


def test_lindex_fourni_par_lappelant_est_utilise_tel_quel(monkeypatch) -> None:
    """Le CLI charge l'index UNE fois puis le passe au rendu : deux chargements
    pourraient donner deux états différents dans le même dossier."""
    import httpx

    from aerobriefer.data.supaip import SupAipEntry, SupAipIndex
    from aerobriefer.render.html import HtmlRenderer

    def interdit(*args: object, **kwargs: object) -> None:
        raise AssertionError("le rendu a tenté un appel réseau")

    monkeypatch.setattr(httpx.Client, "get", interdit)
    index = SupAipIndex(
        entries={"162/2026": SupAipEntry(reference="162/2026", url="https://x/1/", title="ZRT")}
    )
    renderer = HtmlRenderer(supaip_index=index)
    assert renderer._aip_sup_link("162/26")["url"] == "https://x/1/"


def test_un_cache_perime_vaut_mieux_quun_index_vide(monkeypatch, tmp_path) -> None:
    """Défaut constaté : un index vieux de deux jours (TTL : un) était JETÉ, et
    le briefing affichait « référence non résolue » sur tous ses renvois.

    Les SUP AIP suivent le cycle AIRAC (28 jours) et l'identifiant d'un document
    publié ne bouge pas : un cache périmé reste très majoritairement juste.
    """
    import time

    import httpx

    from aerobriefer.data import supaip

    path = tmp_path / "supaip.json"
    path.write_text(SupAipIndex(entries=parse_list(PAGE)).model_dump_json(), encoding="utf-8")
    vieux = time.time() - 3 * 86400
    import os

    os.utime(path, (vieux, vieux))
    monkeypatch.setattr(supaip, "index_path", lambda: path)

    def interdit(*args: object, **kwargs: object) -> None:
        raise AssertionError("appel réseau interdit ici")

    monkeypatch.setattr(httpx.Client, "get", interdit)

    index = supaip.load_index(allow_network=False)
    assert index.lookup("162/26") is not None, "le cache périmé doit servir de repli"


def test_un_echec_reseau_ne_detruit_pas_le_cache(monkeypatch, tmp_path) -> None:
    """Le SIA indisponible ne doit pas priver le dossier de ses liens."""
    import httpx

    from aerobriefer.data import supaip

    path = tmp_path / "supaip.json"
    path.write_text(SupAipIndex(entries=parse_list(PAGE)).model_dump_json(), encoding="utf-8")
    import os
    import time

    vieux = time.time() - 3 * 86400
    os.utime(path, (vieux, vieux))
    monkeypatch.setattr(supaip, "index_path", lambda: path)
    monkeypatch.setattr(
        httpx.Client, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("nope"))
    )

    index = supaip.load_index(allow_network=True)
    assert index.lookup("065/25") is not None
    # Et le fichier n'a pas été écrasé par du vide.
    assert path.read_text(encoding="utf-8").strip() != ""
