"""Cache HTTP sur disque, destiné au DÉVELOPPEMENT.

Raison d'être : ne pas marteler des services publics (SOFIA, Météo-France,
NOAA) pendant qu'on itère sur le rendu. Une centaine de rendus successifs ne
justifie pas une centaine d'interrogations du SIA.

DEUX GARDE-FOUS, non négociables :

1. **Désactivé par défaut.** Il faut poser `AEROBRIEFER_CACHE` explicitement.
   Un briefing réel ne doit jamais servir une donnée d'hier sans le dire : un
   METAR périmé présenté comme frais est exactement le risque contre lequel
   tout le reste du projet est bâti.

2. **Bruyant quand il sert.** Chaque lecture depuis le cache est comptée, et
   l'agrégateur en fait une anomalie visible en tête du dossier. Un cache
   silencieux serait pire que pas de cache.

Usage :
    AEROBRIEFER_CACHE=3600 python -m aerobriefer LFCY ...   # TTL en secondes
    AEROBRIEFER_CACHE=off  (ou variable absente)            # désactivé
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import httpx

ENV_VAR = "AEROBRIEFER_CACHE"
DEFAULT_DIR = Path(".cache/http")

_lock = threading.Lock()
_hits: dict[str, int] = {}


def ttl_seconds() -> float | None:
    """TTL configuré, ou `None` si le cache est désactivé."""
    raw = (os.environ.get(ENV_VAR) or "").strip().lower()
    if not raw or raw in {"0", "off", "false", "no"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return 3600.0  # valeur posée mais illisible : on cache une heure


def is_enabled() -> bool:
    return ttl_seconds() is not None


def hits() -> dict[str, int]:
    """Nombre de réponses servies depuis le cache, par hôte."""
    with _lock:
        return dict(_hits)


def reset_hits() -> None:
    with _lock:
        _hits.clear()


def _record(host: str) -> None:
    with _lock:
        _hits[host] = _hits.get(host, 0) + 1


class CachingTransport(httpx.BaseTransport):
    """Transport httpx qui relit une réponse identique depuis le disque.

    La clé couvre méthode, URL et corps : deux requêtes SOFIA différant par la
    date de vol ne se marchent pas dessus. Les en-têtes sont volontairement
    HORS de la clé — un cookie de session qui change ne doit pas invalider un
    cache par ailleurs valable.
    """

    def __init__(self, inner: httpx.BaseTransport, *, directory: Path | None = None) -> None:
        self._inner = inner
        self._dir = Path(directory or DEFAULT_DIR)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        ttl = ttl_seconds()
        if ttl is None:
            return self._inner.handle_request(request)

        entry = self._dir / f"{self._key(request)}.json"
        cached = self._read(entry, ttl)
        if cached is not None:
            _record(request.url.host)
            return cached

        response = self._inner.handle_request(request)
        response.read()
        if not _establishes_session(response):
            self._write(entry, response)
        return response

    def _key(self, request: httpx.Request) -> str:
        digest = hashlib.sha256()
        digest.update(request.method.encode())
        digest.update(str(request.url).encode())
        digest.update(request.content or b"")
        return digest.hexdigest()[:32]

    def _read(self, entry: Path, ttl: float) -> httpx.Response | None:
        if not entry.exists():
            return None
        try:
            if entry.stat().st_mtime + ttl < _now():
                return None
            payload = json.loads(entry.read_text(encoding="utf-8"))
            body = bytes.fromhex(payload["body"])
        except Exception:  # noqa: BLE001 - un cache illisible se contourne, il ne casse rien
            return None
        return httpx.Response(
            status_code=payload["status"],
            headers=payload["headers"],
            content=body,
        )

    def _write(self, entry: Path, response: httpx.Response) -> None:
        try:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(
                json.dumps(
                    {
                        "status": response.status_code,
                        # On ne rejoue que les en-têtes utiles : réinjecter
                        # content-encoding ferait décoder deux fois un corps
                        # déjà déchiffré par httpx.
                        "headers": {
                            k: v
                            for k, v in response.headers.items()
                            if k.lower() in {"content-type", "last-modified", "expires", "date"}
                        },
                        "body": response.content.hex(),
                    }
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - échec d'écriture : on continue sans cacher
            pass

    def close(self) -> None:
        self._inner.close()


def _establishes_session(response: httpx.Response) -> bool:
    """Vrai si la réponse pose un cookie.

    Ces requêtes-là ne doivent JAMAIS être cachées. SOFIA amorce sa session par
    un GET qui pose un `JSESSIONID` : le rejouer depuis le disque rendrait une
    réponse sans cookie, et le POST suivant serait rejeté (`cause=refresh`).
    Bug observé en conditions réelles, pas hypothétique.

    Le partage est heureux : l'amorçage est une requête légère qui part sur le
    réseau, tandis que l'appel de données — le lourd, 150 Ko chez SOFIA — reste
    caché.
    """
    return "set-cookie" in response.headers


def _now() -> float:
    import time

    return time.time()


def make_client(**kwargs: Any) -> httpx.Client:
    """Client httpx, doublé du cache disque si la variable d'environnement est posée."""
    if not is_enabled():
        return httpx.Client(**kwargs)
    inner = httpx.HTTPTransport(retries=kwargs.pop("retries", 0))
    return httpx.Client(transport=CachingTransport(inner), **kwargs)
