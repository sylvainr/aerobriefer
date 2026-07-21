"""Tests du provider Aeroweb.

Tout tourne hors ligne sur `httpx.MockTransport` alimenté par des fragments
d'index RÉELS (capturés le 2026-07-20 sur aviation.meteo.fr, tronqués). Un
seul test, marqué `network`, tape le vrai service — il exige
AEROWEB_LOGIN / AEROWEB_PASSWORD et se saute proprement sinon.
"""

import hashlib
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Position
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers.aeroweb import (
    PRODUCTS,
    AerowebProvider,
    _parse_echeance,
    select_forecast_echeances,
    select_observation_echeances,
)
from aerobriefer.providers.base import ProviderError

LFCY = Position(45.628101, -0.9725)

# Le créneau cible : demain 08:00Z → 11:00Z.
WINDOW = TimeWindow(
    UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC),
    UtcDateTime(2026, 7, 21, 11, 0, tzinfo=UTC),
)

CONTEXT = BriefingContext.local(center=LFCY, radius_nm=25.0, window=WINDOW, icao="LFCY")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
NOT_PNG = b"<html><head>\n<title>500 Internal Server Error</title></html>"

# --- fragments d'index réels (tronqués) --------------------------------------

FRONT_INDEX = (
    "<div class='messaerohead'>Cartes des fronts (heures en UTC)</div>"
    '<img src="affiche_image.php?type=front/europeouest&date=20260720000000&mode=pdf&comment=">'
    '<img src="affiche_image.php?type=front/europeouest&date=20260720060000&mode=pdf&comment=">'
    '<img src="affiche_image.php?type=front/europeouest&date=20260720120000&mode=pdf&comment=">'
    '<img src="affiche_image.php?type=front/europeouest&date=20260721000000&mode=pdf&comment=">'
    '<img src="affiche_image.php?type=front/europeouest&date=20260721120000&mode=pdf&comment=">'
    '<img src="affiche_image.php?type=front/europeouest&date=20260722000000&mode=pdf&comment=">'
)

# Domaine 19 : TEMSI et WINTEM y cohabitent — le provider doit trier par `type`.
LAYERS_19_INDEX = (
    "<span>TEMSI SFC -FL 150</span><br>"
    "<img onclick=\"return newwindow('affiche_image.php?mode=pdf"
    "&type=sigwx/fr/france&date=20260721090000&time=1784548924');\">"
    "<span>WINTEM FL 020 - 100</span><br>"
    "<img onclick=\"return newwindow('affiche_image.php?mode=pdf"
    "&type=wintemp/fr/france/fl020&date=20260721060000&time=1784548924');\">"
    "<img onclick=\"return newwindow('affiche_image.php?mode=pdf"
    "&type=wintemp/fr/france/fl020&date=20260721150000&time=1784548924');\">"
)

SAT_INDEX = (
    "<img src='affiche_image.php?time=1784549063&type=satellite/france/cc"
    "&date=20260720120000&mode=img'>"
    "<img src='affiche_image.php?time=1784549063&type=radar/france"
    "&date=20260720120000&mode=img'>"
    "<img src='affiche_image.php?time=1784549063&type=satellite/france/cc"
    "&date=20260720114500&mode=img'>"
    "<img src='affiche_image.php?time=1784549063&type=radar/france"
    "&date=20260720114500&mode=img'>"
)

RADAR_INDEX = (
    "<img src='affiche_image.php?time=1784549063&type=radar/france"
    "&date=20260720114500&mode=img'>"
    "<img src='affiche_image.php?time=1784549063&type=radar/france"
    "&date=20260720120000&mode=img'>"
)


class FakeAeroweb:
    """Aeroweb en miniature : exige la session, compte les requêtes."""

    def __init__(self, *, login="pilote", password="secret", image=PNG):
        self.login = login
        self.password = password
        self.image = image
        self.authenticated = False
        self.image_requests: list[str] = []
        self.index_requests: list[str] = []
        self.login_posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login.php":
            return httpx.Response(200, text="<html>formulaire</html>")

        if path == "/ajax/login_valid.php":
            self.login_posts += 1
            body = dict(
                pair.split("=", 1) for pair in request.content.decode().split("&") if "=" in pair
            )
            expected = hashlib.md5(self.password.encode()).hexdigest()
            if body.get("login") == self.login and body.get("password") == expected:
                self.authenticated = True
                return httpx.Response(200, text="ok")
            return httpx.Response(200, text="ko")

        if not self.authenticated:
            return httpx.Response(401, text="401 Unauthorized")

        if path == "/affiche_image.php":
            self.image_requests.append(str(request.url))
            return httpx.Response(200, content=self.image, headers={"content-type": "image/png"})

        self.index_requests.append(str(request.url))
        if path == "/anim_carte_front.php":
            return httpx.Response(200, text=FRONT_INDEX)
        if path == "/get_domaine_layers_echeances.php":
            return httpx.Response(200, text=LAYERS_19_INDEX)
        if path == "/anim_sat.php":
            type_image = request.url.params.get("type_image")
            return httpx.Response(200, text=RADAR_INDEX if type_image == "3" else SAT_INDEX)
        return httpx.Response(404, text="not found")


def make_provider(tmp_path, fake=None, **kwargs):
    fake = fake or FakeAeroweb()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    provider = AerowebProvider(
        login=fake.login,
        password=fake.password,
        cache_dir=tmp_path / "aeroweb",
        client=client,
        **kwargs,
    )
    return provider, fake


# --- conformité au contrat ---------------------------------------------------


def test_provider_identity_matches_protocol():
    assert AerowebProvider.name == "aeroweb"
    assert AerowebProvider.is_critical is False


def test_fetch_returns_charts_with_embedded_bytes(tmp_path):
    provider, _ = make_provider(tmp_path)
    charts = provider.fetch(CONTEXT)

    assert charts, "le provider doit rendre des cartes"
    for sourced in charts:
        chart = sourced.value
        assert chart.is_embedded, f"{chart.kind} non embarquée : inutilisable en vol"
        assert chart.content.startswith(b"\x89PNG")
        assert chart.media_type == "image/png"
        assert chart.valid_at is not None, "une carte sans échéance est inexploitable"
        assert sourced.provenance.source == "aeroweb"


def test_fetch_covers_every_configured_product(tmp_path):
    provider, _ = make_provider(tmp_path)
    kinds = {s.value.kind for s in provider.fetch(CONTEXT)}
    assert kinds == {"front", "temsi", "wintem", "satellite", "radar"}


# --- l'heure d'émission ------------------------------------------------------


def test_observation_charts_carry_issued_at(tmp_path):
    """Radar et satellite : l'échéance EST l'observation, donc l'émission."""
    provider, _ = make_provider(tmp_path)
    for sourced in provider.fetch(CONTEXT):
        if sourced.value.kind in {"radar", "satellite"}:
            assert sourced.value.issued_at == sourced.value.valid_at
            assert sourced.provenance.issued_at is not None


def test_forecast_charts_leave_issued_at_none(tmp_path):
    """Front/TEMSI/WINTEM : l'émission n'est PAS dans le protocole.

    `Last-Modified` vaut invariablement juin 2024 (date du script PHP). La
    renseigner serait un mensonge ; on assume le None. Cf. l'en-tête du module.
    """
    provider, _ = make_provider(tmp_path)
    for sourced in provider.fetch(CONTEXT):
        if sourced.value.kind in {"front", "temsi", "wintem"}:
            assert sourced.value.issued_at is None
            assert sourced.provenance.issued_at is None


def test_provenance_age_falls_back_on_retrieval_for_forecasts(tmp_path):
    provider, _ = make_provider(tmp_path)
    front = next(s for s in provider.fetch(CONTEXT) if s.value.kind == "front")
    # Sans émission connue, age_minutes() repart de retrieved_at : c'est une
    # SOUS-ESTIMATION assumée, le test la fige pour qu'elle reste consciente.
    assert front.age_minutes() < 1.0


# --- sélection des échéances -------------------------------------------------


def test_forecast_selection_brackets_the_window():
    """Un vol 08→11 UTC avec des cartes à 00 et 12 UTC doit garder les deux."""
    available = [
        _parse_echeance(s)
        for s in ("20260720120000", "20260721000000", "20260721120000", "20260722000000")
    ]
    chosen = select_forecast_echeances(available, WINDOW)
    assert [c.strftime("%Y%m%d%H") for c in chosen] == ["2026072100", "2026072112"]


def test_forecast_selection_keeps_echeances_inside_the_window():
    available = [
        _parse_echeance(s)
        for s in ("20260721060000", "20260721090000", "20260721100000", "20260721150000")
    ]
    chosen = select_forecast_echeances(available, WINDOW)
    assert [c.strftime("%H") for c in chosen] == ["06", "09", "10", "15"]


def test_forecast_selection_survives_window_beyond_forecast_range():
    """Toutes les échéances derrière nous : on garde la dernière, pas rien."""
    available = [_parse_echeance(s) for s in ("20260720000000", "20260720120000")]
    chosen = select_forecast_echeances(available, WINDOW)
    assert [c.strftime("%d%H") for c in chosen] == ["2012"]


def test_forecast_selection_wider_bracket_shows_evolution():
    """bracket=2 garde 2 échéances avant et 2 après — évolution du front."""
    available = [
        _parse_echeance(s)
        for s in (
            "20260720120000",
            "20260720180000",
            "20260721000000",  # avant
            "20260721120000",  # après
            "20260721180000",
            "20260722000000",
        )
    ]
    chosen = select_forecast_echeances(available, WINDOW, bracket=2)
    got = [c.strftime("%d%H") for c in chosen]
    assert got == ["2018", "2100", "2112", "2118"]  # 2 avant + 2 après


def test_observation_selection_returns_recent_frames_in_order():
    """Pour animer : les N plus récentes, dans l'ordre chronologique."""
    stamps = ["20260720113000", "20260720114500", "20260720120000", "20260720121500"]
    available = [_parse_echeance(s) for s in stamps]
    got = select_observation_echeances(available, count=3)
    assert [g.strftime("%H%M") for g in got] == ["1145", "1200", "1215"]


def test_observation_selection_handles_empty():
    assert select_observation_echeances([]) == ()


def test_radar_is_dated_at_observation_not_at_flight(tmp_path):
    """Le radar rapatrié précède le vol : le briefing doit le voir. Plusieurs
    frames sont désormais rendues (animation) ; toutes précèdent la fenêtre."""
    provider, _ = make_provider(tmp_path)
    radars = [s for s in provider.fetch(CONTEXT) if s.value.kind == "radar"]
    assert radars, "au moins une frame radar"
    assert all(s.value.valid_at < WINDOW.start for s in radars)
    latest = max(s.value.valid_at for s in radars)
    assert latest == _parse_echeance("20260720120000")


# --- l'index fait autorité ---------------------------------------------------


def test_index_is_queried_before_any_image(tmp_path):
    provider, fake = make_provider(tmp_path)
    provider.fetch(CONTEXT)
    assert fake.index_requests, "les URLs d'image ne doivent pas être devinées"


def test_products_are_filtered_by_type_within_a_shared_index(tmp_path):
    """Domaine 19 sert TEMSI et WINTEM ensemble ; pas de confusion possible."""
    provider, _ = make_provider(tmp_path)
    charts = provider.fetch(CONTEXT)
    temsi = [s.value for s in charts if s.value.kind == "temsi"]
    wintem = [s.value for s in charts if s.value.kind == "wintem"]
    assert all("sigwx/fr/france" in c.url for c in temsi)
    assert all("wintemp/fr/france/fl020" in c.url for c in wintem)
    assert {c.strftime("%H") for c in (x.valid_at for x in temsi)} == {"09"}
    assert {c.strftime("%H") for c in (x.valid_at for x in wintem)} == {"06", "15"}


def test_wintem_carries_its_flight_level(tmp_path):
    provider, _ = make_provider(tmp_path)
    wintem = next(s for s in provider.fetch(CONTEXT) if s.value.kind == "wintem")
    assert wintem.value.flight_level == "FL020"


# --- les CGU, incarnées ------------------------------------------------------


def test_downloaded_echeance_is_never_downloaded_twice(tmp_path):
    """Le cache disque est la garantie structurelle contre l'extraction répétée."""
    provider, fake = make_provider(tmp_path)
    provider.fetch(CONTEXT)
    first = len(fake.image_requests)
    assert first > 0

    provider.fetch(CONTEXT)
    assert len(fake.image_requests) == first, "une échéance a été re-téléchargée"


def test_cache_survives_a_fresh_provider_instance(tmp_path):
    """Le garde-fou est sur le disque, pas dans l'instance : un script relancé
    en boucle ne peut pas contourner la cadence."""
    provider, fake = make_provider(tmp_path)
    provider.fetch(CONTEXT)
    before = len(fake.image_requests)

    other, _ = make_provider(tmp_path, fake=fake)
    other.fetch(CONTEXT)
    assert len(fake.image_requests) == before


def test_index_is_not_refetched_faster_than_production(tmp_path):
    provider, fake = make_provider(tmp_path)
    provider.fetch(CONTEXT)
    before = len(fake.index_requests)
    provider.fetch(CONTEXT)
    assert len(fake.index_requests) == before, "index re-interrogé sous son TTL"


def test_index_ttl_matches_production_cadence():
    """Aucun produit ne peut être rafraîchi plus vite que sa production."""
    expected = {
        "radar": timedelta(minutes=15),
        "satellite": timedelta(minutes=15),
        "temsi": timedelta(hours=3),
        "wintem": timedelta(hours=3),
        "front": timedelta(hours=6),
    }
    assert {p.kind: p.cadence for p in PRODUCTS} == expected


def test_index_is_refetched_once_the_cadence_has_elapsed(tmp_path):
    provider, fake = make_provider(tmp_path)
    provider.fetch(CONTEXT)
    before = len(fake.index_requests)

    # On vieillit le cache d'index au-delà du TTL le plus long (6 h).
    stale = datetime.now(UTC) - timedelta(hours=7)
    for path in (tmp_path / "aeroweb" / "index").iterdir():
        os.utime(path, (stale.timestamp(), stale.timestamp()))

    provider.fetch(CONTEXT)
    assert len(fake.index_requests) > before


def test_single_login_is_reused_across_products(tmp_path):
    """Une seule session : pas de re-login à chaque produit."""
    provider, fake = make_provider(tmp_path)
    provider.fetch(CONTEXT)
    assert fake.login_posts == 1


def test_user_agent_is_identifiable():
    from aerobriefer.providers.aeroweb import _USER_AGENT

    assert "aerobriefer" in _USER_AGENT
    # On ne se déguise pas en navigateur.
    assert "Mozilla" not in _USER_AGENT


def test_module_documents_the_terms_of_use():
    """Les interdictions doivent rester visibles en tête du module."""
    from aerobriefer.providers import aeroweb

    doc = aeroweb.__doc__ or ""
    for clause in (
        "extraction répétée et systématique",
        "environnement informatique en réseau",
        "insertion d'une image dans une page ne lui appartenant pas",
        "exploitation à but commercial",
    ):
        assert clause in doc, f"clause CGU absente de la docstring : {clause}"


# --- authentification --------------------------------------------------------


def test_password_is_sent_as_md5_never_in_clear(tmp_path):
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ajax/login_valid.php":
            sent.update(
                dict(
                    pair.split("=", 1)
                    for pair in request.content.decode().split("&")
                    if "=" in pair
                )
            )
            return httpx.Response(200, text="ok")
        return httpx.Response(200, text=FRONT_INDEX)

    provider = AerowebProvider(
        login="pilote",
        password="s3cr3t",
        cache_dir=tmp_path / "aeroweb",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider._authenticate()

    assert sent["password"] == hashlib.md5(b"s3cr3t").hexdigest()
    assert "s3cr3t" not in sent.values()


def test_missing_credentials_raise_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("AEROWEB_LOGIN", raising=False)
    monkeypatch.delenv("AEROWEB_PASSWORD", raising=False)
    provider = AerowebProvider(cache_dir=tmp_path / "aeroweb")

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch(CONTEXT)

    message = str(excinfo.value)
    assert "AEROWEB_LOGIN" in message and "AEROWEB_PASSWORD" in message
    assert "aeroweb" in message


def test_credentials_are_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AEROWEB_LOGIN", "depuis_env")
    monkeypatch.setenv("AEROWEB_PASSWORD", "motdepasse")
    provider = AerowebProvider(cache_dir=tmp_path / "aeroweb")
    assert provider._credentials() == ("depuis_env", "motdepasse")


def test_no_credentials_are_hardcoded_in_the_module():
    """Le secret vit dans l'environnement, jamais dans le dépôt.

    On ne cite évidemment aucun identifiant en dur ICI non plus — ce serait
    reproduire la fuite qu'on prétend interdire. On vérifie donc que le module
    passe bien par l'environnement, et que les valeurs réellement configurées
    (quand elles le sont) ne s'y trouvent pas.
    """
    from pathlib import Path

    from aerobriefer.providers import aeroweb

    source = Path(aeroweb.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("AEROWEB_LOGIN")' in source
    assert 'os.environ.get("AEROWEB_PASSWORD")' in source

    for var in ("AEROWEB_LOGIN", "AEROWEB_PASSWORD"):
        secret = os.environ.get(var)
        if secret:
            assert secret not in source, f"{var} se retrouve en dur dans le module"


def test_rejected_login_raises(tmp_path):
    fake = FakeAeroweb(password="le_bon")
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    provider = AerowebProvider(
        login="pilote",
        password="le_mauvais",
        cache_dir=tmp_path / "aeroweb",
        client=client,
    )
    with pytest.raises(ProviderError, match="login refusé"):
        provider.fetch(CONTEXT)


# --- échecs : lever, jamais rendre une liste vide ----------------------------


def test_empty_index_raises_rather_than_returning_nothing(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ajax/login_valid.php":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, text="<html>aucune échéance</html>")

    provider = AerowebProvider(
        login="pilote",
        password="secret",
        cache_dir=tmp_path / "aeroweb",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="aucune carte disponible"):
        provider.fetch(CONTEXT)


def test_non_png_response_is_caught_by_the_sanity_check(tmp_path):
    """Le vrai service répond 200 text/html sur certains cas : on ne s'y fie pas."""
    fake = FakeAeroweb(image=NOT_PNG)
    provider, _ = make_provider(tmp_path, fake=fake)
    with pytest.raises(ProviderError, match="contrôle de cohérence"):
        provider.fetch(CONTEXT)


def test_corrupt_image_is_not_written_to_cache(tmp_path):
    fake = FakeAeroweb(image=NOT_PNG)
    provider, _ = make_provider(tmp_path, fake=fake)
    with pytest.raises(ProviderError):
        provider.fetch(CONTEXT)
    images = tmp_path / "aeroweb" / "images"
    assert not images.exists() or not list(images.iterdir())


def test_http_error_on_image_raises(tmp_path):
    """Une échéance inexistante répond 500 : ne jamais l'avaler en silence."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ajax/login_valid.php":
            return httpx.Response(200, text="ok")
        if request.url.path == "/affiche_image.php":
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, text=FRONT_INDEX)

    provider = AerowebProvider(
        login="pilote",
        password="secret",
        cache_dir=tmp_path / "aeroweb",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="HTTP 500"):
        provider.fetch(CONTEXT)


def test_transport_error_raises_provider_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("réseau coupé")

    provider = AerowebProvider(
        login="pilote",
        password="secret",
        cache_dir=tmp_path / "aeroweb",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="injoignable"):
        provider.fetch(CONTEXT)


def test_session_is_required_for_images(tmp_path):
    """Sans cookie le vrai service renvoie 401 : notre faux le simule."""
    fake = FakeAeroweb()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    response = client.get("https://aviation.meteo.fr/affiche_image.php?type=radar/france")
    assert response.status_code == 401


# --- horodatage --------------------------------------------------------------


def test_echeance_parsing_is_utc_aware():
    parsed = _parse_echeance("20260721080000")
    assert parsed.utcoffset() == timedelta(0)
    assert (parsed.year, parsed.month, parsed.day, parsed.hour) == (2026, 7, 21, 8)


def test_parsed_echeance_is_a_utcdatetime():
    assert isinstance(_parse_echeance("20260721080000"), UtcDateTime)


# --- réseau ------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(
    not (os.environ.get("AEROWEB_LOGIN") and os.environ.get("AEROWEB_PASSWORD")),
    reason="AEROWEB_LOGIN / AEROWEB_PASSWORD absents",
)
def test_real_aeroweb_round_trip(tmp_path):
    """Un aller-retour réel : login, index, une image.

    Volontairement minimal — un seul produit, une seule échéance. Les CGU
    interdisent l'extraction systématique, et une suite de tests ne doit pas
    marteler le service.
    """
    provider = AerowebProvider(cache_dir=tmp_path / "aeroweb")
    try:
        radar = next(p for p in PRODUCTS if p.kind == "radar")
        echeances = provider._fetch_index(radar)
        assert echeances, "le radar doit exposer des échéances"

        latest = max(echeances)
        content = provider._fetch_image(radar, latest)
        assert content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(content) > 10_000

        # Le cache doit avoir absorbé l'image.
        assert provider.image_path(radar, latest).exists()

        # `Last-Modified` n'est PAS l'heure d'émission : constat qui fonde le
        # choix de `issued_at`. Si ce test venait à échouer, c'est que Aeroweb
        # a changé et qu'il faut reconsidérer la question.
        response = provider._client.get(
            "https://aviation.meteo.fr/affiche_image.php"
            f"?type={radar.image_type}&date={latest:%Y%m%d%H%M%S}&mode=img&comment="
        )
        last_modified = response.headers.get("last-modified")
        if last_modified is not None:
            assert "2024" in last_modified, (
                "Last-Modified semble avoir changé de sémantique : "
                f"{last_modified!r} — revoir issued_at"
            )
    finally:
        provider.close()
