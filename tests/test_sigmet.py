"""Tests du provider SIGMET (aviationweather.gov / isigmet).

L'essentiel tourne hors ligne sur une capture RÉELLE figée, réduite à quelques
SIGMET : un qui couvre la France (donc la zone LFCY) et un qui n'y touche pas
(FIR de Bakou), pour exercer le filtrage géographique. La fabrique de clients
`cache.make_client` est interceptée comme dans test_noaa, via httpx.MockTransport.

Un seul test tape la vraie API : il est marqué `network` ET conditionné par
AEROBRIEFER_NETWORK_TESTS=1, comme le test réseau de SOFIA — on ne martèle pas un
service public à chaque exécution de la suite.
"""

from __future__ import annotations

import os
from datetime import UTC, timedelta

import httpx
import pytest

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Position
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers import sigmet
from aerobriefer.providers.base import ProviderError
from aerobriefer.providers.sigmet import SigmetProvider

# --------------------------------------------------------------------------
# Fixtures figées — captures réduites de aviationweather.gov/api/data/isigmet
# --------------------------------------------------------------------------

# Le vol réel : LFCY (Royan-Médis), rayon 20 NM, demain 08:00Z → 11:00Z.
LFCY = Position(45.628101, -0.9725)

# Bornes de validité communes aux SIGMET de test : 06:00Z → 12:00Z, donc à cheval
# sur la fenêtre de vol. Ainsi le filtrage temporel passe pour TOUS, et seule la
# géographie départage — c'est bien elle qu'on veut éprouver.
_VALID_FROM = 1784613600  # 2026-07-21T06:00:00Z
_VALID_TO = 1784635200  # 2026-07-21T12:00:00Z

# SIGMET orage couvrant l'ouest de la France : son polygone enveloppe LFCY.
SIGMET_FRANCE = {
    "icaoId": "LFBB",
    "firId": "LFBB",
    "firName": "LFBB BORDEAUX",
    "receiptTime": "2026-07-21T05:40:12.500Z",
    "validTimeFrom": _VALID_FROM,
    "validTimeTo": _VALID_TO,
    "seriesId": "3",
    "hazard": "TS",
    "qualifier": "EMBD",
    "base": None,
    "top": 38000,
    "geom": "AREA",
    "coords": [
        {"lon": -3.0, "lat": 49.0},
        {"lon": 3.0, "lat": 49.0},
        {"lon": 3.0, "lat": 43.0},
        {"lon": -3.0, "lat": 43.0},
    ],
    "rawSigmet": "LFBB SIGMET 3 VALID 210600/211200 LFBB-\nLFBB BORDEAUX FIR EMBD TS "
    "OBS TOP FL380 STNR NC",
}

# SIGMET orage sur le FIR de Bakou : même fenêtre temporelle, mais à 3000 km — il
# ne doit PAS ressortir pour un vol autour de Royan (filtrage géographique).
SIGMET_BAKU = {
    "icaoId": "UBBB",
    "firId": "UBBB",
    "firName": "UBBA BAKU",
    "receiptTime": "2026-07-21T05:20:40.968Z",
    "validTimeFrom": _VALID_FROM,
    "validTimeTo": _VALID_TO,
    "seriesId": "2",
    "hazard": "TS",
    "qualifier": "EMBD",
    "base": None,
    "top": 34000,
    "geom": "AREA",
    "coords": [
        {"lon": 46.4, "lat": 41.57},
        {"lon": 47.5, "lat": 41.57},
        {"lon": 47.5, "lat": 39.9},
        {"lon": 46.4, "lat": 39.9},
    ],
    "rawSigmet": "UBBA SIGMET 2 VALID 210600/211200 UBBB-\nUBBA BAKU FIR EMBD TS TOP FL340",
}

# SIGMET sans polygone exploitable : la politique du domaine est de le conserver.
SIGMET_NO_POLYGON = {
    "icaoId": "KKCI",
    "firId": "KZWY",
    "receiptTime": "2026-07-21T05:00:00.000Z",
    "validTimeFrom": _VALID_FROM,
    "validTimeTo": _VALID_TO,
    "seriesId": "DELTA 1",
    "hazard": "TC",
    "qualifier": None,
    "top": 45000,
    "geom": "AREA",
    "coords": None,
    "rawSigmet": "WSNT01 SIGMET SANS GEOMETRIE EXPLOITABLE",
}


def _window() -> TimeWindow:
    """La fenêtre du vol réel : 2026-07-21 de 08:00Z à 11:00Z."""
    return TimeWindow(
        start=UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC),
        end=UtcDateTime(2026, 7, 21, 11, 0, tzinfo=UTC),
    )


def _context(radius_nm: float = 20.0) -> BriefingContext:
    return BriefingContext.local(center=LFCY, radius_nm=radius_nm, window=_window(), icao="LFCY")


def _client_raising(boom):
    """Fabrique un client dont chaque requête lève, pour tester les pannes réseau."""

    def handler(request: httpx.Request) -> httpx.Response:
        return boom(str(request.url))

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _stub_transport(monkeypatch, *, records, status=200, content=None):
    """Intercepte la FABRIQUE de clients (`cache.make_client`), pas `httpx.get`.

    Le provider passe par `cache.make_client()` pour bénéficier du cache de
    développement ; stubber `httpx.get` contournerait ce chemin.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url).split("?")[0]
        captured["params"] = dict(request.url.params)
        captured["headers"] = request.headers
        return httpx.Response(
            status_code=status,
            json=records if content is None else None,
            content=content,
            request=request,
        )

    def fake_make_client(**kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sigmet.cache, "make_client", fake_make_client)
    return captured


# --------------------------------------------------------------------------
# Conformité au contrat Provider
# --------------------------------------------------------------------------


def test_nom_categorie_criticite():
    assert SigmetProvider.name == "noaa-awc"
    assert SigmetProvider.category == "sigmet"
    assert SigmetProvider.is_critical is False


def test_requete_isigmet_json(monkeypatch):
    captured = _stub_transport(monkeypatch, records=[SIGMET_FRANCE])
    SigmetProvider().fetch(_context())

    assert captured["url"].endswith("/isigmet")
    assert captured["params"]["format"] == "json"
    assert captured["timeout"] == 10.0
    assert "aerobriefer" in captured["headers"]["User-Agent"]


# --------------------------------------------------------------------------
# Décodage des champs
# --------------------------------------------------------------------------


def test_decode_les_champs_du_sigmet(monkeypatch):
    _stub_transport(monkeypatch, records=[SIGMET_FRANCE])
    (sourced,) = SigmetProvider().fetch(_context())
    s = sourced.value

    assert s.hazard == "TS"
    assert s.qualifier == "EMBD"
    assert s.fir == "LFBB"
    assert s.raw_text == SIGMET_FRANCE["rawSigmet"]  # brut TOUJOURS conservé
    assert s.lower_ft is None  # base = null
    assert s.upper_ft == 38000  # top en pieds
    assert "LFBB" in s.identifier and "TS" in s.identifier and "3" in s.identifier
    assert s.validity.start == UtcDateTime(2026, 7, 21, 6, 0, tzinfo=UTC)
    assert s.validity.end == UtcDateTime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_polygone_respecte_l_ordre_lon_lat_de_la_source(monkeypatch):
    """La source donne {lon, lat} ; le domaine attend Position(lat, lon).

    Inverser les deux placerait le SIGMET à l'autre bout du monde — c'est le
    piège que ce test verrouille."""
    _stub_transport(monkeypatch, records=[SIGMET_FRANCE])
    (sourced,) = SigmetProvider().fetch(_context())
    polygon = sourced.value.polygon

    assert len(polygon) == 4
    assert polygon[0] == Position(49.0, -3.0)  # lat=49, lon=-3, pas l'inverse
    assert all(isinstance(v, Position) for v in polygon)


def test_provenance_portee_par_chaque_sigmet(monkeypatch):
    _stub_transport(monkeypatch, records=[SIGMET_FRANCE])
    (sourced,) = SigmetProvider().fetch(_context())

    assert sourced.provenance.source == "noaa-awc"
    # issued_at vient de la source (receiptTime), pas de notre horloge.
    assert sourced.provenance.issued_at == UtcDateTime(2026, 7, 21, 5, 40, 12, 500000, tzinfo=UTC)
    assert sourced.provenance.url is not None


def test_creation_time_prime_sur_receipt_time(monkeypatch):
    record = dict(SIGMET_FRANCE, creationTime="2026-07-21T05:30:00.000Z")
    _stub_transport(monkeypatch, records=[record])
    (sourced,) = SigmetProvider().fetch(_context())

    assert sourced.provenance.issued_at == UtcDateTime(2026, 7, 21, 5, 30, tzinfo=UTC)


# --------------------------------------------------------------------------
# Filtrage géographique — le cœur du provider
# --------------------------------------------------------------------------


def test_filtrage_geographique_garde_la_france_ecarte_bakou(monkeypatch):
    """Deux SIGMET valides sur la même fenêtre : seule la géographie départage."""
    _stub_transport(monkeypatch, records=[SIGMET_FRANCE, SIGMET_BAKU])
    results = SigmetProvider().fetch(_context())

    assert [s.value.fir for s in results] == ["LFBB"]  # Bakou écarté, France gardée


def test_sigmet_sans_polygone_est_conserve(monkeypatch):
    """Sans zone connue, la politique du domaine est de conserver."""
    _stub_transport(monkeypatch, records=[SIGMET_NO_POLYGON])
    (sourced,) = SigmetProvider().fetch(_context())

    assert sourced.value.polygon == ()
    assert sourced.value.raw_text == SIGMET_NO_POLYGON["rawSigmet"]


def test_enregistrement_vide_est_ecarte(monkeypatch):
    """L'endpoint renvoie parfois des `{}` : ni brut ni polygone, donc pas un
    SIGMET. On les écarte plutôt que d'exhiber un aléa fantôme (cas réel)."""
    _stub_transport(monkeypatch, records=[{}, {}, SIGMET_FRANCE])
    results = SigmetProvider().fetch(_context())

    assert [s.value.fir for s in results] == ["LFBB"]  # les coquilles vides tombent


def test_sigmet_hors_fenetre_temporelle_est_ecarte(monkeypatch):
    """Un SIGMET couvrant la France mais expiré avant le vol ne ressort pas."""
    passe = dict(
        SIGMET_FRANCE,
        validTimeFrom=1784538000,  # 2026-07-20T09:00:00Z
        validTimeTo=1784559600,  # 2026-07-20T15:00:00Z
    )
    _stub_transport(monkeypatch, records=[passe])
    assert list(SigmetProvider().fetch(_context())) == []


def test_beau_temps_aucun_sigmet_touche_la_zone(monkeypatch):
    """Cas cible LFCY par anticyclone : les SIGMET existent ailleurs, pas ici.

    Une liste vide est alors un résultat parfaitement valide, pas une panne."""
    _stub_transport(monkeypatch, records=[SIGMET_BAKU])
    assert list(SigmetProvider().fetch(_context())) == []


# --------------------------------------------------------------------------
# Réponse vide = pas de SIGMET actif (pas une erreur)
# --------------------------------------------------------------------------


def test_204_rend_liste_vide(monkeypatch):
    _stub_transport(monkeypatch, records=None, status=204, content=b"")
    assert list(SigmetProvider().fetch(_context())) == []


def test_corps_vide_rend_liste_vide(monkeypatch):
    _stub_transport(monkeypatch, records=None, status=200, content=b"")
    assert list(SigmetProvider().fetch(_context())) == []


def test_liste_json_vide_rend_liste_vide(monkeypatch):
    _stub_transport(monkeypatch, records=[])
    assert list(SigmetProvider().fetch(_context())) == []


# --------------------------------------------------------------------------
# Échecs : lever, jamais une liste vide silencieuse
# --------------------------------------------------------------------------


def test_erreur_reseau_leve(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("pas de réseau")

    monkeypatch.setattr(sigmet.cache, "make_client", _client_raising(boom))
    with pytest.raises(ProviderError) as excinfo:
        SigmetProvider().fetch(_context())
    assert excinfo.value.source == "noaa-awc"


def test_timeout_leve(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ReadTimeout("trop lent")

    monkeypatch.setattr(sigmet.cache, "make_client", _client_raising(boom))
    with pytest.raises(ProviderError):
        SigmetProvider().fetch(_context())


@pytest.mark.parametrize("status", [400, 403, 500, 503])
def test_statut_http_anormal_leve(monkeypatch, status):
    _stub_transport(monkeypatch, records=None, status=status, content=b"boom")
    with pytest.raises(ProviderError):
        SigmetProvider().fetch(_context())


def test_json_illisible_leve(monkeypatch):
    _stub_transport(monkeypatch, records=None, status=200, content=b"<html>oups</html>")
    with pytest.raises(ProviderError):
        SigmetProvider().fetch(_context())


def test_format_inattendu_leve(monkeypatch):
    _stub_transport(monkeypatch, records={"pas": "une liste"})
    with pytest.raises(ProviderError):
        SigmetProvider().fetch(_context())


# --------------------------------------------------------------------------
# Robustesse : le brut ne se perd jamais, les valeurs aberrantes ne plantent pas
# --------------------------------------------------------------------------


def test_coordonnees_aberrantes_ignorees_sans_perdre_le_sigmet(monkeypatch):
    """Un sommet hors bornes est ignoré, le SIGMET remonte quand même."""
    record = dict(
        SIGMET_FRANCE,
        coords=[
            {"lon": -3.0, "lat": 49.0},
            {"lon": 999.0, "lat": 49.0},  # longitude aberrante : ignorée
            {"lon": 3.0, "lat": 43.0},
            {"lon": -3.0, "lat": 43.0},
        ],
    )
    _stub_transport(monkeypatch, records=[record])
    (sourced,) = SigmetProvider().fetch(_context())

    assert len(sourced.value.polygon) == 3  # le sommet fou est tombé
    assert sourced.value.raw_text == SIGMET_FRANCE["rawSigmet"]


def test_sans_bornes_de_validite_reste_visible(monkeypatch):
    """Sans epochs exploitables, on retient large plutôt que de disparaître.

    Le repli de validité part de `retrieved_at` (= maintenant) : on teste donc
    avec une fenêtre centrée sur l'instant courant, pour ne pas coupler le test
    à l'horloge murale."""
    from datetime import timedelta

    from aerobriefer.domain.window import utcnow

    now = utcnow()
    around_now = TimeWindow(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
    context = BriefingContext.local(center=LFCY, radius_nm=20.0, window=around_now, icao="LFCY")

    record = dict(SIGMET_FRANCE)
    record.pop("validTimeFrom")
    record.pop("validTimeTo")
    _stub_transport(monkeypatch, records=[record])
    (sourced,) = SigmetProvider().fetch(context)

    assert sourced.value.validity.duration_hours > 0
    assert sourced.value.validity.overlaps(around_now)


# --------------------------------------------------------------------------
# Utilitaires de conversion
# --------------------------------------------------------------------------


def test_conversion_epoch():
    assert sigmet._epoch_to_utc(_VALID_FROM) == UtcDateTime(2026, 7, 21, 6, 0, tzinfo=UTC)
    assert sigmet._epoch_to_utc(None) is None
    assert sigmet._epoch_to_utc("SFC") is None


def test_horodatages_sont_bien_utc():
    moment = sigmet._epoch_to_utc(_VALID_FROM)
    assert isinstance(moment, UtcDateTime)
    assert moment.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# Réseau réel (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("AEROBRIEFER_NETWORK_TESTS") != "1",
    reason="service public : test réseau sur demande explicite (AEROBRIEFER_NETWORK_TESTS=1)",
)
def test_reseau_reel_isigmet():
    """Un aller-retour réel : le contrat de l'endpoint tient toujours.

    Aucune attente sur le NOMBRE de SIGMET touchant LFCY (souvent 0 par beau
    temps) — on vérifie seulement que la collecte aboutit et que chaque SIGMET
    remonté est bien formé et concerne effectivement la zone.
    """
    try:
        results = SigmetProvider().fetch(_context())
    except ProviderError as exc:
        if "échec réseau" in exc.message:
            pytest.skip(f"AWC injoignable : {exc.message}")
        raise

    context = _context()
    for sourced in results:
        s = sourced.value
        assert s.raw_text.strip()
        assert sourced.provenance.source == "noaa-awc"
        assert s.concerns(context.geometry, context.window)
