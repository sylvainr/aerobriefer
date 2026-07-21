"""Le cache de développement : utile, mais jamais silencieux."""

from datetime import UTC

import httpx
import pytest

from aerobriefer.providers import cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv(cache.ENV_VAR, raising=False)
    monkeypatch.setattr(cache, "DEFAULT_DIR", tmp_path / "http")
    cache.reset_hits()
    yield
    cache.reset_hits()


class CountingTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        return httpx.Response(200, json={"n": self.calls}, request=request)

    def close(self):
        pass


def _client(inner, tmp_path):
    return httpx.Client(transport=cache.CachingTransport(inner, directory=tmp_path / "http"))


def test_disabled_by_default():
    """Sans variable d'environnement, aucun cache : un vrai briefing collecte."""
    assert not cache.is_enabled()
    assert cache.ttl_seconds() is None


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no"])
def test_explicit_off_values(monkeypatch, value):
    monkeypatch.setenv(cache.ENV_VAR, value)
    assert not cache.is_enabled()


def test_passthrough_when_disabled(tmp_path):
    inner = CountingTransport()
    client = _client(inner, tmp_path)
    for _ in range(3):
        client.get("https://example.test/a")
    assert inner.calls == 3, "sans cache, chaque appel doit sortir"
    assert cache.hits() == {}


def test_second_call_is_served_from_disk(monkeypatch, tmp_path):
    monkeypatch.setenv(cache.ENV_VAR, "3600")
    inner = CountingTransport()
    client = _client(inner, tmp_path)
    first = client.get("https://example.test/a")
    second = client.get("https://example.test/a")
    assert inner.calls == 1, "le second appel ne doit pas sortir"
    assert first.content == second.content
    assert cache.hits() == {"example.test": 1}


def test_different_bodies_do_not_collide(monkeypatch, tmp_path):
    """Deux requêtes SOFIA pour des dates différentes sont distinctes."""
    monkeypatch.setenv(cache.ENV_VAR, "3600")
    inner = CountingTransport()
    client = _client(inner, tmp_path)
    client.post("https://sofia.test/x", content=b"valid_from=2026-07-21")
    client.post("https://sofia.test/x", content=b"valid_from=2026-07-22")
    assert inner.calls == 2


def test_expired_entry_is_refetched(monkeypatch, tmp_path):
    monkeypatch.setenv(cache.ENV_VAR, "3600")
    inner = CountingTransport()
    client = _client(inner, tmp_path)
    client.get("https://example.test/a")
    monkeypatch.setattr(cache, "_now", lambda: 10**12)  # très loin dans le futur
    client.get("https://example.test/a")
    assert inner.calls == 2


def test_cache_use_is_reported_as_a_critical_anomaly(monkeypatch, tmp_path):
    """Un cache silencieux serait pire que pas de cache : le dossier doit crier."""
    from datetime import timedelta

    from aerobriefer.assemble import assemble_briefing
    from aerobriefer.domain.context import BriefingContext
    from aerobriefer.domain.geo import Position
    from aerobriefer.domain.window import TimeWindow, UtcDateTime

    monkeypatch.setenv(cache.ENV_VAR, "3600")
    cache._record("sofia.test")

    start = UtcDateTime(2026, 7, 21, 8, 0, tzinfo=UTC)
    context = BriefingContext.local(
        center=Position(45.6, -0.97),
        radius_nm=20,
        window=TimeWindow(start, start + timedelta(hours=3)),
        icao="LFCY",
    )

    class Silent:
        name, is_critical, category = "muet", False, "notam"

        def fetch(self, _):
            cache._record("sofia.test")
            return []

    package = assemble_briefing(context, [Silent()], parallel=False)
    anomalies = [f for f in package.failures if f.source == "cache"]
    assert anomalies, "l'usage du cache doit apparaître dans le dossier"
    assert anomalies[0].is_critical, "et rendre le dossier INCOMPLET"
    assert "NE PAS UTILISER EN VOL" in anomalies[0].reason


def test_session_bootstrap_is_never_cached(monkeypatch, tmp_path):
    """Régression : cacher le GET qui pose le JSESSIONID cassait SOFIA — la
    réponse rejouée n'avait plus de cookie, et le POST suivant était rejeté."""
    monkeypatch.setenv(cache.ENV_VAR, "3600")

    class SessionTransport(httpx.BaseTransport):
        def __init__(self):
            self.calls = 0

        def handle_request(self, request):
            self.calls += 1
            return httpx.Response(
                200,
                content=b"ok",
                headers={"set-cookie": f"JSESSIONID=abc{self.calls}; Path=/"},
                request=request,
            )

        def close(self):
            pass

    inner = SessionTransport()
    client = _client(inner, tmp_path)
    client.get("https://sofia.test/pages/notamform.html")
    client.get("https://sofia.test/pages/notamform.html")
    assert inner.calls == 2, "une réponse posant un cookie ne doit jamais être rejouée"
