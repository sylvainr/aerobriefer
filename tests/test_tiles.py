"""Magasin de tuiles OSM : cache, refus d'OSM, et absence de réseau au rendu."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from aerobriefer.data import tiles
from aerobriefer.data.tiles import TileStore

# En-tête PNG minimal : le contenu importe peu, on teste le transport.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200

#: Ce qu'OSM sert quand il refuse — une IMAGE, en HTTP 200, pas une erreur.
_BLOCKED = b"\x89PNG\r\n\x1a\nAccess blocked App is not following the tile usage policy"


def _store(tmp_path: Path, **kwargs: object) -> TileStore:
    return TileStore(directory=tmp_path, **kwargs)  # type: ignore[arg-type]


def test_tile_url_is_the_osm_scheme() -> None:
    assert tiles.tile_url(8, 126, 90) == "https://tile.openstreetmap.org/8/126/90.png"


def test_blocked_tile_is_recognised() -> None:
    assert tiles.looks_blocked(_BLOCKED)
    assert not tiles.looks_blocked(_PNG)
    # Une vraie tuile, même lourde, n'est jamais prise pour un refus.
    assert not tiles.looks_blocked(b"Access blocked" + b"\x00" * 50_000)


def test_download_then_cache_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, content=_PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    url = tiles.tile_url(8, 126, 90)

    store = _store(tmp_path)
    assert store.data_uri(url) == "data:image/png;base64," + base64.b64encode(_PNG).decode()
    assert store.downloaded == 1
    assert (tmp_path / "8" / "126" / "90.png").read_bytes() == _PNG

    # Un SECOND magasin (autre dossier de vol, autre run) ne retéléphone pas.
    again = _store(tmp_path)
    assert again.data_uri(url) is not None
    assert again.downloaded == 0
    assert calls == [url]


def test_identifiable_user_agent_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        seen.update(kwargs["headers"])  # type: ignore[arg-type]
        return httpx.Response(200, content=_PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    _store(tmp_path).data_uri(tiles.tile_url(8, 126, 90))
    # La politique d'OSM l'exige, et se déguiser en navigateur est ce qui fait
    # servir la tuile « Access blocked ».
    assert "aerobriefer" in seen["User-Agent"]
    assert "Mozilla" not in seen["User-Agent"]


def test_blocked_tile_is_never_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=_BLOCKED, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    store = _store(tmp_path)
    assert store.data_uri(tiles.tile_url(8, 126, 90)) is None
    assert store.missing == 1
    assert list(tmp_path.rglob("*.png")) == []


def test_refusal_stops_further_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, content=_BLOCKED, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    store = _store(tmp_path)
    for x in range(5):
        assert store.data_uri(tiles.tile_url(8, 126 + x, 90)) is None
    # Insister sur un refus est précisément ce que la politique reproche.
    assert len(calls) == 1


def test_network_error_is_never_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("pas de réseau")

    monkeypatch.setattr(httpx, "get", fake_get)
    store = _store(tmp_path)
    assert store.data_uri(tiles.tile_url(8, 126, 90)) is None
    assert store.missing == 1


def test_offline_store_never_touches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(url: str, **kwargs: object) -> httpx.Response:
        raise AssertionError("allow_network=False doit interdire tout appel")

    monkeypatch.setattr(httpx, "get", explode)
    assert _store(tmp_path, allow_network=False).data_uri(tiles.tile_url(8, 126, 90)) is None


def test_foreign_url_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(url: str, **kwargs: object) -> httpx.Response:
        raise AssertionError("seules les tuiles OSM sont servies")

    monkeypatch.setattr(httpx, "get", explode)
    store = _store(tmp_path)
    assert store.data_uri("https://example.invalid/8/126/90.png") is None


def test_download_budget_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, content=_PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    store = _store(tmp_path, max_downloads=2)
    for x in range(5):
        store.data_uri(tiles.tile_url(8, 126 + x, 90))
    assert len(calls) == 2
    assert store.downloaded == 2
