"""Tests du provider AROME maille fine — sans réseau.

Ce qui est éprouvé ici est précisément ce qui échouerait EN SILENCE : demander
un run que le visualiseur n'expose pas, ou une échéance antérieure au run.
Dans les deux cas le WMS répond 200 avec une image vide — aucune exception, une
carte blanche au dossier. D'où des tests sur la table de runs et le calcul des
échéances plutôt que sur le transport.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Circle, Position
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers.arome import LAYERS, echeances, select_run
from aerobriefer.render.html import CHART_LABELS, CHART_ORDER, RELEASE_CADENCE


def _utc(day: int, hour: int, minute: int = 0) -> UtcDateTime:
    return UtcDateTime.of(datetime(2026, 8, day, hour, minute, tzinfo=UTC))


def _context(start_hour: int, duration_h: float = 2.0, day: int = 30) -> BriefingContext:
    start = _utc(day, start_hour)
    return BriefingContext(
        geometry=Circle(Position(46.0, -0.7), 40.0),
        window=TimeWindow(start, UtcDateTime.of(start + timedelta(hours=duration_h))),
    )


# ---------------------------------------------------------------------------
# Table des runs — reprise du client, PAS de la cadence théorique d'AROME
# ---------------------------------------------------------------------------


def test_les_bascules_de_run_suivent_la_table_du_visualiseur() -> None:
    assert select_run(_utc(30, 0, 0)) == _utc(29, 18)  # veille 18Z
    assert select_run(_utc(30, 5, 44)) == _utc(29, 18)
    assert select_run(_utc(30, 5, 45)) == _utc(30, 3)  # bascule
    assert select_run(_utc(30, 11, 4)) == _utc(30, 3)
    assert select_run(_utc(30, 11, 5)) == _utc(30, 6)
    assert select_run(_utc(30, 15, 44)) == _utc(30, 6)
    assert select_run(_utc(30, 15, 45)) == _utc(30, 12)
    assert select_run(_utc(30, 22, 59)) == _utc(30, 12)
    assert select_run(_utc(30, 23, 0)) == _utc(30, 18)


def test_le_run_de_minuit_remonte_bien_a_la_veille() -> None:
    """Le piège de la table : avant 05:45 on est sur le run de la VEILLE. Une
    erreur de jour ici demanderait un run inexistant — image vide, sans erreur."""
    run = select_run(_utc(1, 2, 30))
    assert (run.day, run.hour) == (31, 18)
    assert run.month == 7, "le 1er août à 02:30Z, le run est celui du 31 JUILLET"


def test_le_run_n_est_jamais_dans_le_futur() -> None:
    for hour in range(24):
        for minute in (0, 30, 59):
            now = _utc(30, hour, minute)
            assert select_run(now) <= now, f"run futur à {hour:02d}:{minute:02d}Z"


# ---------------------------------------------------------------------------
# Échéances
# ---------------------------------------------------------------------------


def test_les_echeances_encadrent_la_fenetre_de_vol() -> None:
    """Une échéance avant et une après : voir si ça se lève ou si ça se ferme."""
    stamps = echeances(_context(8), run=_utc(30, 3))
    assert stamps == [_utc(30, 7), _utc(30, 8), _utc(30, 9), _utc(30, 10), _utc(30, 11)]


def test_aucune_echeance_anterieure_au_run() -> None:
    """AROME ne rétro-prévoit pas : demander avant le run rend une image vide."""
    stamps = echeances(_context(8), run=_utc(30, 9))
    assert stamps == [_utc(30, 9), _utc(30, 10), _utc(30, 11)]
    assert all(s >= _utc(30, 9) for s in stamps)


def test_un_run_posterieur_au_vol_ne_donne_aucune_echeance() -> None:
    """Cas limite qui doit remonter en échec, pas en dossier vide."""
    assert echeances(_context(8), run=_utc(30, 18)) == []


def test_les_echeances_sont_horaires_rondes_et_croissantes() -> None:
    stamps = echeances(_context(9, duration_h=4.0), run=_utc(30, 3))
    assert all(s.minute == 0 and s.second == 0 for s in stamps)
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


# ---------------------------------------------------------------------------
# Intégration au rendu : une couche sans étiquette sortirait en « AROME_VISI »
# ---------------------------------------------------------------------------


def test_chaque_couche_a_son_libelle_sa_place_et_sa_cadence() -> None:
    for layer in LAYERS:
        assert layer.kind in CHART_LABELS, f"libellé manquant : {layer.kind}"
        assert layer.kind in CHART_ORDER, f"place non définie dans l'ordre : {layer.kind}"
        assert layer.kind in RELEASE_CADENCE, f"cadence manquante : {layer.kind}"


def test_arome_se_lit_apres_la_temsi_et_avant_le_radar() -> None:
    """Le zonage expertisé d'abord, le champ de modèle ensuite : AROME n'est pas
    relu par un prévisionniste."""
    ordre = list(CHART_ORDER)
    for layer in LAYERS:
        assert ordre.index("temsi") < ordre.index(layer.kind)
        assert ordre.index(layer.kind) < ordre.index("radar")


def test_les_champs_wms_sont_ceux_du_catalogue_observe() -> None:
    """Garde-fou contre une faute de frappe : un champ inconnu répond 200 avec
    une image vide, pas une erreur."""
    assert {layer.champ for layer in LAYERS} == {
        "gafor.visi_metropole",
        "gafor.plafond_metropole",
        "model.nebul_bas",
    }
