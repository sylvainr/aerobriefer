"""Tests du provider NOAA/AWC.

L'essentiel tourne hors ligne sur des fixtures figées en dur (captures réelles du
2026-07-20), pour que la logique de décodage reste testable sans réseau. Un seul
test, marqué `network`, tape la vraie API.
"""

from __future__ import annotations

from datetime import UTC, timedelta

import httpx
import pytest

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Position
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers import noaa
from aerobriefer.providers.base import ProviderError
from aerobriefer.providers.noaa import NoaaMetarProvider, NoaaTafProvider

# --------------------------------------------------------------------------
# Fixtures figées — captures réelles de aviationweather.gov le 2026-07-20
# --------------------------------------------------------------------------

METAR_LFBD = {
    "icaoId": "LFBD",
    "receiptTime": "2026-07-20T11:36:10.823Z",
    "obsTime": 1784547000,
    "reportTime": "2026-07-20T11:30:00.000Z",
    "temp": 29,
    "dewp": 10,
    "wdir": 30,
    "wspd": 10,
    "visib": "6+",  # CHAÎNE, pas un nombre
    "altim": 1017,
    "metarType": "METAR",
    "rawOb": "METAR LFBD 201130Z AUTO 03010KT 360V080 CAVOK 29/10 Q1017",
    "lat": 44.831,
    "lon": -0.691,
    "name": "Bordeaux/Mérignac Arpt, NA, FR",
    "clouds": [],
    "fltCat": "VFR",
}

# Vent variable + plafond bas + temps significatif : le cas qui exerce le décodage.
METAR_VRB = {
    "icaoId": "LFBH",
    "obsTime": 1784547000,
    "reportTime": "2026-07-20T11:30:00.000Z",
    "temp": 12,
    "dewp": 11,
    "wdir": "VRB",  # CHAÎNE non numérique
    "wspd": 3,
    "visib": 1.24,  # milles terrestres côté NOAA, soit les 2000 m du brut
    "altim": 1013,
    "rawOb": "METAR LFBH 201130Z VRB03KT 2000 BR BKN008 OVC015 12/11 Q1013",
    "clouds": [{"cover": "BKN", "base": 800}, {"cover": "OVC", "base": 1500}],
}

TAF_LFBD = {
    "icaoId": "LFBD",
    "bulletinTime": "2026-07-20T11:00:00.000Z",
    "issueTime": "2026-07-20T11:00:00.000Z",
    "validTimeFrom": 1784548800,
    "validTimeTo": 1784656800,
    "rawTAF": "TAF LFBD 201100Z 2012/2118 03010KT CAVOK TX34/2015Z TN19/2105Z",
    "lat": 44.831,
    "lon": -0.691,
}

# LFCY (Royan-Médis) — le terrain du vol réel, sans station d'observation.
LFCY = Position(45.628101, -0.9725)


def _window() -> TimeWindow:
    """La fenêtre du vol réel : 2026-07-21 de 08:00Z à 11:00Z."""
    return TimeWindow(
        start=UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC),
        end=UtcDateTime(2026, 7, 21, 11, 0, tzinfo=UTC),
    )


def _context(icao: str | None = "LFBD") -> BriefingContext:
    return BriefingContext.local(center=LFCY, radius_nm=25, window=_window(), icao=icao)


def _client_raising(boom):
    """Fabrique un client dont chaque requête lève, pour tester les pannes réseau."""

    def handler(request: httpx.Request) -> httpx.Response:
        return boom(str(request.url))

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _stub_transport(monkeypatch, *, records, status=200, content=None):
    """Intercepte la FABRIQUE de clients plutôt que `httpx.get`.

    Le provider passe par `cache.make_client()` afin de bénéficier du cache de
    développement ; stubber `httpx.get` contournerait ce chemin et ne testerait
    plus le code réellement exécuté.
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

    monkeypatch.setattr(noaa.cache, "make_client", fake_make_client)
    return captured


# --------------------------------------------------------------------------
# Conformité au contrat Provider
# --------------------------------------------------------------------------


def test_noms_et_criticite():
    assert NoaaMetarProvider.name == "noaa-awc"
    assert NoaaTafProvider.name == "noaa-awc"
    assert NoaaMetarProvider.is_critical is True
    assert NoaaTafProvider.is_critical is False


def test_requete_reprend_les_stations_du_contexte(monkeypatch):
    captured = _stub_transport(monkeypatch, records=[METAR_LFBD])
    context = BriefingContext(
        geometry=_context().geometry,
        window=_window(),
        origin_icao="LFBD",
        destination_icao="LFBD",
        alternates_icao=("LFBH",),
    )
    NoaaMetarProvider().fetch(context)

    assert captured["params"]["ids"] == "LFBD,LFBH"  # dédoublonné, ordre préservé
    assert captured["params"]["format"] == "json"
    assert captured["timeout"] == 10.0
    assert "aerobriefer" in captured["headers"]["User-Agent"]


# --------------------------------------------------------------------------
# Décodage METAR
# --------------------------------------------------------------------------


def test_metar_decode_les_champs_nominaux(monkeypatch):
    _stub_transport(monkeypatch, records=[METAR_LFBD])
    (sourced,) = NoaaMetarProvider().fetch(_context())
    metar = sourced.value

    assert metar.station == "LFBD"
    assert metar.raw_text == METAR_LFBD["rawOb"]
    assert metar.observed_at == UtcDateTime(2026, 7, 20, 11, 30, tzinfo=UTC)
    assert metar.wind_dir_deg == 30
    assert metar.wind_speed_kt == 10
    assert metar.wind_gust_kt is None
    assert metar.temperature_c == 29.0
    assert metar.dewpoint_c == 10.0
    assert metar.qnh_hpa == 1017.0
    assert metar.ceiling_ft is None  # CAVOK
    assert metar.visibility_m == 9999  # CAVOK décodé par avwx, pas le "6+" NOAA


def test_visib_chaine_six_plus_ne_plante_pas():
    """Le champ `visib` est parfois la CHAÎNE "6+" : borne basse retenue."""
    assert noaa._visibility_from_noaa("6+") == 9656
    assert noaa._visibility_from_noaa(2.5) == 4023
    assert noaa._visibility_from_noaa(None) is None
    assert noaa._visibility_from_noaa("nawak") is None


def test_wdir_vrb_donne_direction_none(monkeypatch):
    """`wdir` peut valoir "VRB" : direction variable, pas un crash."""
    _stub_transport(monkeypatch, records=[METAR_VRB])
    (sourced,) = NoaaMetarProvider().fetch(_context())
    metar = sourced.value

    assert metar.wind_dir_deg is None  # convention du domaine pour « variable »
    assert metar.wind_speed_kt == 3
    assert metar.visibility_m == 2000
    assert metar.ceiling_ft == 800  # première couche BKN, en pieds
    assert "Mist" in metar.conditions


def test_provenance_portee_par_chaque_donnee(monkeypatch):
    _stub_transport(monkeypatch, records=[METAR_LFBD])
    (sourced,) = NoaaMetarProvider().fetch(_context())

    assert sourced.provenance.source == "noaa-awc"
    assert sourced.provenance.issued_at == UtcDateTime(2026, 7, 20, 11, 30, tzinfo=UTC)
    assert sourced.provenance.url is not None
    # L'âge se calcule sur l'émission, pas sur le téléchargement.
    now = UtcDateTime(2026, 7, 20, 12, 30, tzinfo=UTC)
    assert sourced.age_minutes(now) == pytest.approx(60.0)


# --------------------------------------------------------------------------
# Robustesse du décodage : le brut ne doit JAMAIS se perdre
# --------------------------------------------------------------------------


def test_brut_illisible_remonte_quand_meme(monkeypatch):
    """Exigence de sécurité : un parseur en échec ne fait pas disparaître la donnée."""
    record = {
        "icaoId": "LFBD",
        "obsTime": 1784547000,
        "rawOb": "CECI N EST PAS UN METAR VALIDE",
    }
    _stub_transport(monkeypatch, records=[record])
    (sourced,) = NoaaMetarProvider().fetch(_context())
    metar = sourced.value

    assert metar.raw_text == "CECI N EST PAS UN METAR VALIDE"  # brut conservé
    assert metar.station == "LFBD"
    assert metar.observed_at == UtcDateTime(2026, 7, 20, 11, 30, tzinfo=UTC)
    assert metar.wind_dir_deg is None  # décodé à None, mais la donnée existe
    assert metar.visibility_m is None
    assert metar.ceiling_ft is None


def test_avwx_qui_explose_ne_fait_pas_perdre_le_metar(monkeypatch):
    """Même si avwx lève, le METAR remonte avec son brut."""

    class Exploding:
        @staticmethod
        def from_report(_raw):
            raise RuntimeError("avwx cassé")

    monkeypatch.setattr(noaa.avwx, "Metar", Exploding)
    _stub_transport(monkeypatch, records=[METAR_LFBD])

    (sourced,) = NoaaMetarProvider().fetch(_context())
    assert sourced.value.raw_text == METAR_LFBD["rawOb"]
    assert sourced.value.qnh_hpa == 1017.0  # le JSON NOAA prend le relais
    assert sourced.value.visibility_m == 9656  # repli sur le "6+" en milles


def test_avwx_absent_ne_bloque_pas(monkeypatch):
    monkeypatch.setattr(noaa, "avwx", None)
    _stub_transport(monkeypatch, records=[METAR_LFBD])

    (sourced,) = NoaaMetarProvider().fetch(_context())
    assert sourced.value.raw_text == METAR_LFBD["rawOb"]
    assert sourced.value.wind_dir_deg == 30


def test_metar_sans_horodatage_remonte_sans_pretendre_etre_frais(monkeypatch):
    _stub_transport(monkeypatch, records=[{"icaoId": "LFBD", "rawOb": "METAR LFBD NIL"}])
    (sourced,) = NoaaMetarProvider().fetch(_context())

    assert sourced.value.raw_text == "METAR LFBD NIL"
    # Pas d'heure d'émission inventée dans la provenance.
    assert sourced.provenance.issued_at is None


# --------------------------------------------------------------------------
# Station sans données (cas LFCY) — ne pas planter, ne pas inventer
# --------------------------------------------------------------------------


def test_station_sans_donnees_204_rend_liste_vide(monkeypatch):
    """LFCY renvoie un 204 à corps vide : réponse valide, pas une panne."""
    _stub_transport(monkeypatch, records=None, status=204, content=b"")
    assert list(NoaaMetarProvider().fetch(_context("LFCY"))) == []
    assert list(NoaaTafProvider().fetch(_context("LFCY"))) == []


def test_station_omise_du_tableau_ne_produit_rien(monkeypatch):
    """En requête groupée, l'AWC omet silencieusement la station inconnue."""
    _stub_transport(monkeypatch, records=[METAR_LFBD])
    context = BriefingContext(
        geometry=_context().geometry,
        window=_window(),
        origin_icao="LFCY",
        alternates_icao=("LFBD",),
    )
    results = NoaaMetarProvider().fetch(context)

    assert [s.value.station for s in results] == ["LFBD"]  # rien d'inventé pour LFCY


def test_contexte_sans_station_ne_tape_pas_le_reseau(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("aucune requête ne devrait partir")

    monkeypatch.setattr(noaa.cache, "make_client", _client_raising(boom))
    context = BriefingContext(geometry=_context().geometry, window=_window())
    assert list(NoaaMetarProvider().fetch(context)) == []


# --------------------------------------------------------------------------
# Échecs : lever, jamais une liste vide silencieuse
# --------------------------------------------------------------------------


def test_erreur_reseau_leve(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("pas de réseau")

    monkeypatch.setattr(noaa.cache, "make_client", _client_raising(boom))
    with pytest.raises(ProviderError) as excinfo:
        NoaaMetarProvider().fetch(_context())
    assert excinfo.value.source == "noaa-awc"


def test_timeout_leve(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ReadTimeout("trop lent")

    monkeypatch.setattr(noaa.cache, "make_client", _client_raising(boom))
    with pytest.raises(ProviderError):
        NoaaTafProvider().fetch(_context())


@pytest.mark.parametrize("status", [400, 403, 500, 503])
def test_statut_http_anormal_leve(monkeypatch, status):
    _stub_transport(monkeypatch, records=None, status=status, content=b"boom")
    with pytest.raises(ProviderError):
        NoaaMetarProvider().fetch(_context())


def test_json_illisible_leve(monkeypatch):
    _stub_transport(monkeypatch, records=None, status=200, content=b"<html>oups</html>")
    with pytest.raises(ProviderError):
        NoaaMetarProvider().fetch(_context())


def test_format_inattendu_leve(monkeypatch):
    _stub_transport(monkeypatch, records={"pas": "une liste"})
    with pytest.raises(ProviderError):
        NoaaMetarProvider().fetch(_context())


# --------------------------------------------------------------------------
# Décodage TAF
# --------------------------------------------------------------------------


def test_taf_decode_validite_et_emission(monkeypatch):
    _stub_transport(monkeypatch, records=[TAF_LFBD])
    (sourced,) = NoaaTafProvider().fetch(_context())
    taf = sourced.value

    assert taf.station == "LFBD"
    assert taf.raw_text == TAF_LFBD["rawTAF"]
    assert taf.issued_at == UtcDateTime(2026, 7, 20, 11, 0, tzinfo=UTC)
    assert taf.validity.start == UtcDateTime(2026, 7, 20, 12, 0, tzinfo=UTC)
    assert taf.validity.end == UtcDateTime(2026, 7, 21, 18, 0, tzinfo=UTC)
    # Le TAF couvre bien la fenêtre du vol LFCY de demain.
    assert taf.validity.overlaps(_window())


def test_taf_sans_bornes_reste_visible_dans_la_fenetre(monkeypatch):
    """Sans bornes exploitables, on retient large : un TAF ne doit pas
    disparaître silencieusement au filtrage temporel."""
    record = {
        "icaoId": "LFBD",
        "issueTime": "2026-07-21T05:00:00.000Z",
        "rawTAF": "TAF LFBD RAW ILLISIBLE",
    }
    _stub_transport(monkeypatch, records=[record])
    (sourced,) = NoaaTafProvider().fetch(_context())

    assert sourced.value.raw_text == "TAF LFBD RAW ILLISIBLE"
    assert sourced.value.validity.overlaps(_window())
    assert sourced.value.validity.duration_hours > 0


def test_taf_brut_conserve_meme_si_avwx_echoue(monkeypatch):
    monkeypatch.setattr(noaa, "avwx", None)
    _stub_transport(monkeypatch, records=[TAF_LFBD])
    (sourced,) = NoaaTafProvider().fetch(_context())

    assert sourced.value.raw_text == TAF_LFBD["rawTAF"]
    assert sourced.value.validity.start == UtcDateTime(2026, 7, 20, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Utilitaires de conversion
# --------------------------------------------------------------------------


def test_conversion_epoch():
    assert noaa._epoch_to_utc(1784547000) == UtcDateTime(2026, 7, 20, 11, 30, tzinfo=UTC)
    assert noaa._epoch_to_utc(None) is None
    assert noaa._epoch_to_utc("VRB") is None


def test_conversion_iso():
    assert noaa._iso_to_utc("2026-07-20T11:30:00.000Z") == UtcDateTime(
        2026, 7, 20, 11, 30, tzinfo=UTC
    )
    assert noaa._iso_to_utc(None) is None
    assert noaa._iso_to_utc("") is None
    assert noaa._iso_to_utc("pas une date") is None


def test_horodatages_sont_bien_utc():
    moment = noaa._epoch_to_utc(1784547000)
    assert isinstance(moment, UtcDateTime)
    assert moment.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# Réseau réel
# --------------------------------------------------------------------------


def _live(provider, context):
    """Appelle la vraie API en distinguant panne de transport et rupture de contrat.

    L'AWC répond en général en moins d'une seconde mais connaît des pics au-delà
    de 5 s : le délai de 10 s est parfois dépassé. Une panne de transport n'est
    pas une régression du provider, donc on passe le test — en revanche un statut
    ou un format inattendu reste un échec franc.
    """
    try:
        return provider.fetch(context)
    except ProviderError as exc:
        if "échec réseau" in exc.message:
            pytest.skip(f"AWC injoignable : {exc.message}")
        raise


@pytest.mark.network
def test_reseau_reel_lfcy_sans_donnees_et_voisins_disponibles():
    """Vérifie sur la vraie API : LFCY n'a rien, LFBD/LFBH ont un METAR."""
    assert list(_live(NoaaMetarProvider(), _context("LFCY"))) == []
    assert list(_live(NoaaTafProvider(), _context("LFCY"))) == []

    context = BriefingContext(
        geometry=_context().geometry,
        window=_window(),
        origin_icao="LFBD",
        alternates_icao=("LFBH",),
    )
    metars = _live(NoaaMetarProvider(), context)
    assert {s.value.station for s in metars} == {"LFBD", "LFBH"}
    for sourced in metars:
        assert sourced.value.raw_text.strip()
        assert sourced.provenance.source == "noaa-awc"
        assert sourced.value.observed_at <= UtcDateTime.now()

    tafs = _live(NoaaTafProvider(), context)
    assert tafs, "LFBD/LFBH devraient publier un TAF"
    for sourced in tafs:
        assert sourced.value.raw_text.strip()
        assert sourced.value.validity.duration_hours > 0
