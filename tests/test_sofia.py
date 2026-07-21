"""Tests du provider SOFIA.

Les tests hors-ligne rejouent une capture RÉELLE et figée de l'API (vol local
LFCY, 2026-07-21 08:00Z → 11:00Z, rayon 20 NM, traffic V), embarquée compressée
plus bas. Figer la vraie réponse plutôt que d'écrire un faux à la main est le
seul moyen de détecter une dérive de contrat sur une source aussi fragile.

Le test réseau est marqué `network` ET conditionné par AEROBRIEFER_NETWORK_TESTS=1 :
SOFIA est un service public, on ne le martèle pas à chaque exécution de la suite.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
from datetime import timedelta
from urllib.parse import parse_qsl

import httpx
import pytest

from aerobriefer.domain.context import BriefingContext, Purpose
from aerobriefer.domain.geo import Circle, Corridor, Position
from aerobriefer.domain.models import Severity
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers.base import ProviderError
from aerobriefer.providers.sofia import (
    PERMANENT_END,
    SESSION_URL,
    SofiaProvider,
    decode_message,
    format_latitude,
    format_longitude,
    iter_notam_nodes,
    parse_coordinates,
    severity_for,
)

# ---------------------------------------------------------------------------
# Captures réelles figées
# ---------------------------------------------------------------------------

#: Réponse HTTP 200 de postAreaAeroPibRequest pour LFCY (21 NOTAM).
_CAPTURE_LFCY_B64 = (
    "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"
    "ZchssAAA"
)

#: Réponse HTTP 500 renvoyée quand le JSESSIONID manque ou a expiré.
_CAPTURE_REFRESH_B64 = (
    "H4sIAAAAAAAC/61UwU7DMAy97yusHnZCdBy4sJALEhISSAi+wGvcJlKXlMQFIcS/4y4dGxqCIpZDk7y8F784bpTldatn"
    "yhIaPQNpih23pFWZ+5kq99ZWwbyOQ3smHPmMIlwNZBib4h1zh8WvQAaNfmTkPklA8/26Mu4ZnLksMrHQ54uFKgXUhxpB"
    "4sSwd5QSNjQh7sgs9Nv8qQ+8rLBPlIcXuYtUR0o2T05yt86qL7zr0LbhxfkG2BJ0sgyj9GSDkGeKZMAgI1h8JlgReTDU"
    "EguM3sC6TyzoJxUbdP4Ubuphg0jwggnq1jWWoWvR+yGYCVW/FkHaRHk93Pk023s/QlpvQ4Xsgv8hr3I0OfVlUaZQOyw2"
    "Od7KCp1RVeL/fNxjlBPDX+xUQXLqORvK+j1b4+oRjLGdUHQDbRd1m5R/X88D1VIlcYKBkVnoI0S9sugbug3NhLif3ELP"
    "W152keYNL4dhOY6nGZLZ3nNV7r1QqjuoQX0XjKud/E8PlEIfK8r33H2j2JbJtsRCDb+oy2xE3svhtf0AKICoRXQFAAA="
)


def _unfreeze(blob: str) -> str:
    return gzip.decompress(base64.b64decode(blob)).decode("utf-8")


CAPTURE_LFCY = _unfreeze(_CAPTURE_LFCY_B64)
CAPTURE_REFRESH = _unfreeze(_CAPTURE_REFRESH_B64)

LFCY = Position(45.628101, -0.9725)


def local_context(radius_nm: float = 20.0) -> BriefingContext:
    return BriefingContext.local(
        center=LFCY,
        radius_nm=radius_nm,
        window=TimeWindow(
            UtcDateTime.parse("2026-07-21T08:00:00Z"),
            UtcDateTime.parse("2026-07-21T11:00:00Z"),
        ),
        icao="LFCY",
    )


class Recorder:
    """Transport factice : sert des réponses scriptées et journalise les appels."""

    def __init__(self, *post_responses: tuple[int, str]) -> None:
        self._post_responses = list(post_responses)
        self.gets: list[str] = []
        self.posts: list[list[tuple[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets.append(str(request.url))
            return httpx.Response(200, text="<html>form</html>")

        self.posts.append(parse_qsl(request.content.decode("utf-8")))
        status, body = self._post_responses[min(len(self.posts) - 1, len(self._post_responses) - 1)]
        return httpx.Response(status, text=body)

    def provider(self, **kwargs) -> SofiaProvider:
        client = httpx.Client(transport=httpx.MockTransport(self))
        return SofiaProvider(client=client, **kwargs)

    @property
    def last_form(self) -> dict[str, str]:
        return dict(self.posts[-1])


# ---------------------------------------------------------------------------
# Coordonnées
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lat", "lon"),
    [
        # Le cas qui compte : LFCY est à l'OUEST de Greenwich, longitude négative.
        ("4538N00059W", 45.0 + 38 / 60, -(0.0 + 59 / 60)),
        # Hémisphère nord / est, exemple du contrat.
        ("4845N00207E", 48.0 + 45 / 60, 2.0 + 7 / 60),
        # Hémisphère sud, longitude ouest.
        ("3352S07040W", -(33.0 + 52 / 60), -(70.0 + 40 / 60)),
        # Avec secondes.
        ("453600N0010907W", 45.6, -(1.0 + 9 / 60 + 7 / 3600)),
        ("000000N0000000E", 0.0, 0.0),
    ],
)
def test_parse_coordinates(text: str, lat: float, lon: float) -> None:
    position = parse_coordinates(text)
    assert position.lat == pytest.approx(lat, abs=1e-9)
    assert position.lon == pytest.approx(lon, abs=1e-9)


def test_parse_coordinates_places_lfcy_where_lfcy_actually_is() -> None:
    """Garde-fou anti-hémisphère : traiter W comme positif décalerait de 130 NM."""
    assert parse_coordinates("4538N00059W").distance_nm(LFCY) < 1.0


@pytest.mark.parametrize(
    "text",
    ["", "4538N", "4538X00059W", "453N00059W", "4538N0059W", "4578N00059W", "4538N00099W"],
)
def test_parse_coordinates_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_coordinates(text)


def test_format_coordinates_roundtrips() -> None:
    assert format_latitude(45.0 + 38 / 60) == "4538N"
    assert format_longitude(-(0.0 + 59 / 60)) == "00059W"
    assert format_latitude(-33.0) == "3300S"
    assert format_longitude(2.0 + 7 / 60) == "00207E"

    position = parse_coordinates(format_latitude(LFCY.lat) + format_longitude(LFCY.lon))
    assert position.distance_nm(LFCY) < 1.0


# ---------------------------------------------------------------------------
# Q-codes → Severity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code23", "code45", "expected"),
    [
        ("MR", "LC", Severity.BLOCKING),  # piste fermée
        ("FA", "LC", Severity.BLOCKING),  # terrain fermé
        ("RT", "CA", Severity.BLOCKING),  # zone réglementée temporaire ACTIVE
        ("RP", "CA", Severity.BLOCKING),
        ("NV", "AS", Severity.MAJOR),  # VOR hors service
        ("IC", "AS", Severity.MAJOR),  # ILS hors service
        ("OB", "CE", Severity.MAJOR),  # grue dressée
        ("OB", "CM", Severity.MAJOR),  # obstacle déplacé : reste MAJOR
        ("MR", "LT", Severity.MINOR),  # piste à usage limité
        ("WP", "LW", Severity.MINOR),  # parachutage annoncé
        ("AZ", "AH", Severity.MINOR),  # horaires AFIS réduits
        ("LP", "AS", Severity.MINOR),  # PAPI HS : balisage plafonné à MINOR
        ("LE", "LC", Severity.MINOR),  # feux de bord de piste éteints
        ("RT", "TT", Severity.INFO),  # trigger NOTAM : annonce, pas activation
        ("FA", "TT", Severity.INFO),
        ("CA", "CF", Severity.INFO),  # changement de fréquence
        ("FW", "CM", Severity.INFO),  # manche à air déplacée
    ],
)
def test_severity_mapping(code23: str, code45: str, expected: Severity) -> None:
    assert severity_for(code23, code45) == expected


@pytest.mark.parametrize(
    ("code23", "code45"),
    [
        ("FA", "XX"),  # plain language : le sens est dans le texte libre
        ("SC", "XX"),
        ("MR", "ZZ"),  # état inconnu
        ("??", "LC"),  # sujet illisible
        ("MR", ""),
        (None, None),
        ("M", "LC"),  # longueur invalide
    ],
)
def test_severity_defaults_to_unknown(code23, code45) -> None:
    """On ne devine JAMAIS : UNKNOWN remonte en tête du briefing, c'est voulu."""
    assert severity_for(code23, code45) is Severity.UNKNOWN


def test_unknown_sorts_ahead_of_everything() -> None:
    assert Severity.UNKNOWN.value == 0
    assert min(Severity) is Severity.UNKNOWN


# ---------------------------------------------------------------------------
# Décodage de la réponse HTML
# ---------------------------------------------------------------------------


def test_decode_message_unescapes_html_wrapped_json() -> None:
    payload = decode_message(CAPTURE_LFCY)
    assert payload["nbNotams"] == 21
    assert payload["traffic"] == "V"
    assert payload["duration"] == "0300"
    assert payload["validFrom"] == "2026-07-21T08:00:00.000Z"


def test_decode_message_handles_double_encoded_status_message() -> None:
    """Certaines réponses emballent un JSON dans la valeur de `status.message`."""
    body = (
        '<html><div id="Message">'
        "{&quot;status.message&quot;:&quot;{\\&quot;cause\\&quot;:\\&quot;refresh\\&quot;}&quot;}"
        "</div></html>"
    )
    payload = decode_message(body)
    assert payload["status.message"] == {"cause": "refresh"}


def test_decode_message_without_div_raises() -> None:
    with pytest.raises(ProviderError, match="Message"):
        decode_message("<html><body>maintenance</body></html>")


def test_decode_message_with_empty_div_raises_a_clear_error() -> None:
    """Raté transitoire observé en réel : HTTP 200 mais div Message vide."""
    with pytest.raises(ProviderError, match="vide"):
        decode_message('<html><div id="Message"></div></html>')


def test_empty_div_never_becomes_zero_notams() -> None:
    recorder = Recorder((200, '<html><div id="Message"></div></html>'))
    with pytest.raises(ProviderError):
        recorder.provider().fetch(local_context())


def test_decode_message_with_broken_json_raises() -> None:
    with pytest.raises(ProviderError, match="JSON illisible"):
        decode_message('<html><div id="Message">{not json</div></html>')


def test_iter_notam_nodes_reaches_every_leaf() -> None:
    payload = decode_message(CAPTURE_LFCY)
    pairs = list(iter_notam_nodes(payload["listnotams"]))
    assert len(pairs) == payload["nbNotams"] == 21
    nodes = [node for node, _ in pairs]
    # Les NOTAM viennent de branches de formes différentes (AD à plat, FIR
    # imbriqué sur 4 niveaux) : vérifions qu'on a bien les deux.
    sections = {node.get("pibSection") for node in nodes}
    assert {"AD", "FIR"} <= sections


def test_iter_notam_nodes_captures_source_rubric() -> None:
    """La rubrique métier de SOFIA doit être remontée avec chaque NOTAM."""
    payload = decode_message(CAPTURE_LFCY)
    rubrics = {rubric for _, rubric in iter_notam_nodes(payload["listnotams"])}
    # Au moins quelques rubriques métier reconnues, pas uniquement None.
    assert any(r is not None for r in rubrics)


# ---------------------------------------------------------------------------
# fetch() hors-ligne sur la capture figée
# ---------------------------------------------------------------------------


def test_fetch_returns_every_notam_of_the_capture() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    results = recorder.provider().fetch(local_context())

    assert len(results) == 21
    assert all(sourced.provenance.source == "sofia" for sourced in results)
    # issued_at vient de la source, pas de notre horloge.
    assert results[0].provenance.issued_at == UtcDateTime.parse("2026-07-20T12:01:44Z")
    assert results[0].provenance.retrieved_at >= results[0].provenance.issued_at


def test_fetch_builds_domain_notams_faithfully() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    by_id = {s.value.identifier: s.value for s in recorder.provider().fetch(local_context())}

    # Numérotation NOTAM sur 4 chiffres, série + année.
    assert "D6248/25" in by_id
    assert "W0578/26" in by_id
    assert "P0914/26" in by_id

    crane = by_id["P0914/26"]  # QOBCE : grue à Breuillet, non balisée
    assert crane.q_code == "QOBCE"
    assert crane.severity is Severity.MAJOR
    assert crane.center is not None
    assert crane.center.lon < 0  # ouest de Greenwich
    assert crane.radius_nm == 1.0
    assert crane.affected_icao == "LFBB"
    assert crane.lower_limit_ft == 0
    assert crane.upper_limit_ft == 300  # FL3 → 300 ft
    assert crane.validity.start < crane.validity.end

    # Le français est privilégié pour le décodé, l'anglais reste le brut.
    assert "GRUE" in (crane.decoded_text or "")
    assert "CRANE" in crane.raw_text
    assert crane.decoded_text != crane.raw_text


def test_fetch_classifies_the_real_capture_as_expected() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    by_id = {s.value.identifier: s.value for s in recorder.provider().fetch(local_context())}

    assert by_id["R1552/26"].severity is Severity.BLOCKING  # QRTCA : ZRT activée
    assert by_id["R1000/26"].severity is Severity.INFO  # QRTTT : trigger
    assert by_id["W1285/26"].severity is Severity.MINOR  # QWPLW : parachutage
    assert by_id["D6248/25"].severity is Severity.UNKNOWN  # QFAXX : plain language
    assert by_id["D2071/26"].severity is Severity.INFO  # QFWCM : manche à air


def test_permanent_notams_get_an_open_ended_validity() -> None:
    """5 des 21 NOTAM de la capture ont endValidity="PERM", pas une date ISO."""
    recorder = Recorder((200, CAPTURE_LFCY))
    by_id = {s.value.identifier: s.value for s in recorder.provider().fetch(local_context())}

    permanent = by_id["D2071/26"]  # manche à air déplacée, PERM
    assert permanent.validity.end == UtcDateTime.parse(PERMANENT_END)
    # La borne conventionnelle doit se comporter comme « sans fin ».
    assert permanent.validity.overlaps(local_context().window)


def test_unparseable_validity_raises_rather_than_leaking_valueerror() -> None:
    tampered = CAPTURE_LFCY.replace("2026-07-09T00:00:00Z", "pas-une-date")
    assert tampered != CAPTURE_LFCY

    recorder = Recorder((200, tampered))
    with pytest.raises(ProviderError, match="startValidity illisible"):
        recorder.provider().fetch(local_context())


def test_fetch_keeps_notams_without_geometry() -> None:
    """Sans centre connu, la politique du domaine est de conserver."""
    recorder = Recorder((200, CAPTURE_LFCY))
    notams = [s.value for s in recorder.provider().fetch(local_context())]
    context = local_context()
    for notam in notams:
        if notam.center is None:
            assert notam.concerns(context.geometry, context.window) is True


# ---------------------------------------------------------------------------
# Contrôles de cohérence
# ---------------------------------------------------------------------------


def test_fetch_raises_when_count_disagrees_with_nbnotams() -> None:
    """Le garde-fou central : une branche ratée ne doit jamais passer inaperçue."""
    tampered = CAPTURE_LFCY.replace("&quot;nbNotams&quot;:21", "&quot;nbNotams&quot;:22")
    assert tampered != CAPTURE_LFCY

    recorder = Recorder((200, tampered))
    with pytest.raises(ProviderError, match="21 NOTAM extraits pour 22 annoncés"):
        recorder.provider().fetch(local_context())


def test_fetch_raises_when_listnotams_missing() -> None:
    body = '<html><div id="Message">{&quot;nbNotams&quot;:0}</div></html>'
    recorder = Recorder((200, body))
    with pytest.raises(ProviderError, match="listnotams"):
        recorder.provider().fetch(local_context())


def test_fetch_raises_when_nbnotams_missing() -> None:
    body = '<html><div id="Message">{&quot;listnotams&quot;:{}}</div></html>'
    recorder = Recorder((200, body))
    with pytest.raises(ProviderError, match="nbNotams"):
        recorder.provider().fetch(local_context())


def test_fetch_raises_on_unparseable_coordinates() -> None:
    tampered = CAPTURE_LFCY.replace("4542N00103W", "4542X00103W")
    assert tampered != CAPTURE_LFCY

    recorder = Recorder((200, tampered))
    with pytest.raises(ProviderError, match="coordonnées SOFIA non reconnues"):
        recorder.provider().fetch(local_context())


def test_provider_never_returns_silently_empty() -> None:
    """Règle cardinale de base.py : en échec on LÈVE, on ne rend pas [] ."""
    recorder = Recorder((500, "<html>Internal Server Error</html>"))
    with pytest.raises(ProviderError):
        recorder.provider().fetch(local_context())


# ---------------------------------------------------------------------------
# Session et expiration
# ---------------------------------------------------------------------------


def test_session_is_opened_before_posting() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    recorder.provider().fetch(local_context())
    assert recorder.gets == [SESSION_URL]


def test_session_is_reused_across_fetches() -> None:
    """Politesse : une seule ouverture de session pour plusieurs briefings."""
    recorder = Recorder((200, CAPTURE_LFCY))
    provider = recorder.provider()
    provider.fetch(local_context())
    provider.fetch(local_context())
    assert len(recorder.gets) == 1
    assert len(recorder.posts) == 2


def test_refresh_triggers_exactly_one_retry_with_a_new_session() -> None:
    recorder = Recorder((500, CAPTURE_REFRESH), (200, CAPTURE_LFCY))
    results = recorder.provider().fetch(local_context())

    assert len(results) == 21
    assert len(recorder.posts) == 2  # l'appel initial puis le rejeu
    assert len(recorder.gets) == 2  # session rouverte entre les deux


def test_repeated_refresh_gives_up_without_looping() -> None:
    recorder = Recorder((500, CAPTURE_REFRESH))
    with pytest.raises(ProviderError, match="refresh"):
        recorder.provider().fetch(local_context())

    # Deux POST au maximum : aucune boucle de retry contre un service public.
    assert len(recorder.posts) == 2


def test_refresh_capture_is_the_documented_signal() -> None:
    assert (
        json.loads(
            re.search(r'<div id="Message">(.*?)</div>', CAPTURE_REFRESH, re.S)
            .group(1)
            .replace("&quot;", '"')
        )["cause"]
        == "refresh"
    )


# ---------------------------------------------------------------------------
# Construction de la requête
# ---------------------------------------------------------------------------


def test_local_flight_uses_area_aero_operation() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    recorder.provider().fetch(local_context(radius_nm=20))

    form = recorder.last_form
    assert form[":operation"] == "postAreaAeroPibRequest"
    assert form["valid_from"] == "2026-07-21T08:00:00Z"
    assert form["duration"] == "0300"  # 3 h
    assert form["traffic"] == "V"
    assert form["radius"] == "20"
    assert form["adep"] == "LFCY"
    assert form["isFromSofia"] == "true"
    assert ("aero[]", "LFCY") in recorder.posts[-1]
    # uuid v4 aléatoire, généré côté client.
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", form["uuid"]
    )


def test_alternates_are_sent_as_repeated_alt_fields() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    context = BriefingContext(
        geometry=Circle(LFCY, 20.0),
        window=local_context().window,
        purpose=Purpose.LOCAL,
        origin_icao="LFCY",
        alternates_icao=("LFDN", "LFBH"),
    )
    recorder.provider().fetch(context)
    assert [v for k, v in recorder.posts[-1] if k == "alt[]"] == ["LFDN", "LFBH"]


def test_route_flight_uses_narrow_route_operation_with_ordered_points() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    context = BriefingContext(
        geometry=Corridor((LFCY, Position(46.7, -1.38)), half_width_nm=10.0),
        window=local_context().window,
        purpose=Purpose.NAVIGATION,
        origin_icao="LFCY",
        destination_icao="LFDN",
    )
    recorder.provider().fetch(context)

    form = recorder.last_form
    assert form[":operation"] == "postNarrowRoutePibRequest"
    assert form["width"] == "10"  # DEMI-couloir
    assert [v for k, v in recorder.posts[-1] if k == "route[]"] == ["LFCY", "LFDN"]


def test_geometry_without_aerodrome_falls_back_to_lat_long_cylinder() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    context = BriefingContext(
        geometry=Circle(LFCY, 25.0),
        window=local_context().window,
        purpose=Purpose.LOCAL,
    )
    recorder.provider().fetch(context)

    form = recorder.last_form
    assert form[":operation"] == "postAreaPibRequest"
    assert form["radius"] == "25"
    assert form["lat"] == "4538N"
    assert form["long"] == "00058W"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-07-21T08:00:00Z", "2026-07-21T11:00:00Z", "0300"),
        ("2026-07-21T08:00:00Z", "2026-07-21T20:00:00Z", "1200"),
        ("2026-07-21T08:00:00Z", "2026-07-21T08:45:00Z", "0045"),
        ("2026-07-21T08:00:00Z", "2026-07-23T08:00:00Z", "4800"),
    ],
)
def test_duration_is_formatted_hhmm(start: str, end: str, expected: str) -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    context = BriefingContext.local(
        center=LFCY,
        radius_nm=20.0,
        window=TimeWindow(UtcDateTime.parse(start), UtcDateTime.parse(end)),
        icao="LFCY",
    )
    recorder.provider().fetch(context)
    assert recorder.last_form["duration"] == expected


def test_zero_length_window_is_refused() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    instant = UtcDateTime.parse("2026-07-21T08:00:00Z")
    context = BriefingContext.local(
        center=LFCY, radius_nm=20.0, window=TimeWindow(instant, instant), icao="LFCY"
    )
    with pytest.raises(ProviderError, match="durée nulle"):
        recorder.provider().fetch(context)


def test_absurdly_long_window_is_refused() -> None:
    recorder = Recorder((200, CAPTURE_LFCY))
    context = BriefingContext.local(
        center=LFCY,
        radius_nm=20.0,
        window=TimeWindow(
            UtcDateTime.parse("2026-07-21T08:00:00Z"),
            UtcDateTime.parse("2026-08-21T08:00:00Z"),
        ),
        icao="LFCY",
    )
    with pytest.raises(ProviderError, match="maximum SOFIA"):
        recorder.provider().fetch(context)


def test_invalid_traffic_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="traffic"):
        SofiaProvider(traffic="X")


def test_provider_satisfies_the_provider_protocol() -> None:
    assert SofiaProvider.name == "sofia"
    assert SofiaProvider.is_critical is True


# ---------------------------------------------------------------------------
# Test réseau (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("AEROBRIEFER_NETWORK_TESTS") != "1",
    reason="service public : test réseau sur demande explicite (AEROBRIEFER_NETWORK_TESTS=1)",
)
def test_live_local_flight_at_lfcy() -> None:
    """Un seul aller-retour réel, pour vérifier que le contrat tient toujours."""
    # Demain 08:00Z → 11:00Z : une fenêtre future, donc stable pendant le test.
    tomorrow = (UtcDateTime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    window = TimeWindow(
        UtcDateTime.parse(f"{tomorrow}T08:00:00Z"),
        UtcDateTime.parse(f"{tomorrow}T11:00:00Z"),
    )
    context = BriefingContext.local(center=LFCY, radius_nm=20.0, window=window, icao="LFCY")

    with SofiaProvider() as provider:
        results = provider.fetch(context)

    assert results, "SOFIA renvoie toujours au moins les NOTAM FIR"
    for sourced in results:
        notam = sourced.value
        assert notam.identifier and notam.raw_text
        assert notam.validity.start <= notam.validity.end
        assert sourced.provenance.source == "sofia"
