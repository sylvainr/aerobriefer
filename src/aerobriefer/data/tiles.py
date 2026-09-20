"""Tuiles OpenStreetMap — téléchargées ICI, jamais par le navigateur.

Le fond de carte sous les couches AROME et sous la carte de dégagement vient du
serveur de tuiles d'OpenStreetMap. Il était jusqu'ici tiré par le NAVIGATEUR :
le HTML portait des `https://tile.openstreetmap.org/z/x/y.png` que la page
allait chercher à l'ouverture.

Deux raisons de ne plus faire ça :

1. **OSM refuse.** Sa politique d'usage veut un client identifiable et une
   volumétrie modeste. Une page de briefing ouvre quinze cartes AROME d'un coup,
   soit plus de cent balises `<img>` de tuiles tirées en rafale par un
   User-Agent de navigateur anonyme. Le serveur répond alors non pas une erreur,
   mais **une tuile « Access blocked — App is not following the tile usage
   policy » en HTTP 200** : le dossier s'affiche, barré de rouge, sans que rien
   ne signale la panne. Un 403 déguisé en image est le pire des cas — on croit
   lire une carte.
2. **Un dossier de vol doit marcher hors ligne.** Le fond chargé par le
   navigateur manquait en vol, là précisément où on le regarde.

On télécharge donc les tuiles à la GÉNÉRATION, avec un User-Agent identifiable,
une fois pour toutes, et on les embarque en `data:` URI dans le HTML. Le nombre
de requêtes tombe de « cent et quelques par ouverture de page » à « les quelques
tuiles distinctes du dossier, une seule fois dans la vie du cache ».

Le cache (`.cache/tiles/`) est partagé par tous les dossiers et n'expire pas :
le fond de carte d'OSM ne bouge pas à l'échelle d'une préparation de vol, et une
route dessinée l'an dernier reste une route. `AEROBRIEFER_TILES_DIR` le déplace
(tests : pointer vers un répertoire pré-rempli et poser `allow_network=False`).
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

import httpx

ENV_DIR = "AEROBRIEFER_TILES_DIR"
_DEFAULT_DIR = Path(".cache/tiles")

#: Gabarit d'URL du fond. Seul OSM est utilisé ; l'attribution « © OpenStreetMap
#: contributors » qui l'accompagne dans les rendus est obligatoire.
URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

#: La politique d'OSM exige un agent qui identifie l'application. On ne se
#: déguise JAMAIS en navigateur : c'est ce déguisement qui fait servir la tuile
#: « Access blocked ».
USER_AGENT = "aerobriefer/0.1 (VFR flight briefing, personal use)"

_TIMEOUT = httpx.Timeout(20.0)

#: Plafond de téléchargements par magasin. Garde-fou contre l'aspiration en
#: masse que la politique d'OSM interdit : un dossier tient dans quelques
#: dizaines de tuiles distinctes, pas dans des milliers. Au-delà, on ARRÊTE de
#: télécharger (le cache disque, lui, reste servi sans limite).
MAX_DOWNLOADS = 256

_URL = re.compile(r"^https://tile\.openstreetmap\.org/(\d+)/(\d+)/(\d+)\.png$")

#: Une tuile OSM légitime dépasse toujours largement cette taille ; la tuile de
#: blocage, elle, pèse ~7 ko. Le seuil ne sert pas à trancher — le texte
#: « Access blocked » est cherché dans les octets — il borne juste l'inspection.
_BLOCKED_MAX_BYTES = 40_000
_BLOCKED_MARKERS = (b"Access blocked", b"tile usage policy", b"osm.wiki/Blocked")


def cache_dir() -> Path:
    """Répertoire du cache : override d'environnement ou `.cache/tiles`."""
    override = os.environ.get(ENV_DIR)
    return Path(override) if override else _DEFAULT_DIR


def tile_url(z: int, x: int, y: int) -> str:
    return URL_TEMPLATE.format(z=z, x=x, y=y)


def looks_blocked(payload: bytes) -> bool:
    """La tuile est-elle la page « Access blocked » servie en HTTP 200 ?

    OSM ne renvoie PAS d'erreur quand il refuse : il renvoie une image qui dit
    « App is not following the tile usage policy ». La reconnaître est la seule
    façon de ne pas la mettre en cache — et donc de ne pas la coller dans un
    dossier de vol pour les mois à venir.
    """
    if len(payload) > _BLOCKED_MAX_BYTES:
        return False
    return any(marker in payload for marker in _BLOCKED_MARKERS)


class TileStore:
    """Tuiles OSM sur disque, servies en `data:` URI.

    Ne LÈVE JAMAIS : une tuile manquante n'invalide pas un briefing. Elle est
    simplement absente du fond, et le rendu le dit.
    """

    def __init__(
        self,
        *,
        directory: Path | None = None,
        allow_network: bool = True,
        max_downloads: int = MAX_DOWNLOADS,
    ) -> None:
        self._dir = directory if directory is not None else cache_dir()
        self._allow_network = allow_network
        self._budget = max_downloads
        self._memo: dict[str, str | None] = {}
        self.downloaded = 0
        """Tuiles réellement tirées du réseau (le reste venait du cache)."""
        self.missing = 0
        """Tuiles qu'on n'a pas pu obtenir — comptées pour être DITES."""

    def _path(self, z: str, x: str, y: str) -> Path:
        return self._dir / z / x / f"{y}.png"

    def data_uri(self, url: str) -> str | None:
        """`data:` URI de la tuile, ou `None` si elle est hors d'atteinte."""
        if url in self._memo:
            return self._memo[url]
        result = self._resolve(url)
        self._memo[url] = result
        if result is None:
            self.missing += 1
        return result

    def _resolve(self, url: str) -> str | None:
        match = _URL.match(url)
        if match is None:
            return None
        z, x, y = match.groups()
        path = self._path(z, x, y)
        if path.exists():
            try:
                return _to_data_uri(path.read_bytes())
            except OSError:
                return None
        payload = self._download(url)
        if payload is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError:
            pass  # cache non inscriptible : on sert quand même la tuile de ce run
        return _to_data_uri(payload)

    def _download(self, url: str) -> bytes | None:
        if not self._allow_network or self._budget <= 0:
            return None
        self._budget -= 1
        try:
            response = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/*"},
                timeout=_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        payload = response.content
        if not payload or looks_blocked(payload):
            # Refus d'OSM : on ne le met SURTOUT pas en cache. Et on épuise le
            # budget, parce qu'insister sur un blocage, c'est exactement ce que
            # la politique reproche.
            self._budget = 0
            return None
        self.downloaded += 1
        return payload


def _to_data_uri(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


__all__ = [
    "ENV_DIR",
    "MAX_DOWNLOADS",
    "URL_TEMPLATE",
    "USER_AGENT",
    "TileStore",
    "cache_dir",
    "looks_blocked",
    "tile_url",
]
