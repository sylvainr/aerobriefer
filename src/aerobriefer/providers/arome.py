"""Modèle AROME à maille fine (Aeroweb « Modèle maille fine »).

AROME est le modèle à haute résolution de Météo-France sur la France. Il apporte
au briefing VFR ce que ni la TEMSI ni les TAF ne donnent : un champ CONTINU de
**visibilité**, de **plafond** et de **nébulosité basse**, heure par heure, sur
la trajectoire — au lieu d'un point d'aérodrome (TAF) ou d'un zonage grossier
(TEMSI). C'est exactement ce qui manque pour juger « est-ce que la couche se
lève entre le départ et l'arrivée ».

===========================================================================
PROTOCOLE (rétro-ingénierie vérifiée en conditions réelles le 2026-08-30)
===========================================================================
Ce n'est PAS le protocole d'`aeroweb.py` : le visualiseur maille fine est une
application OpenLayers adossée à un **service WMS**, avec sa PROPRE
authentification. La session PHP d'Aeroweb ne l'ouvre pas — un GetMap sans le
jeton WMS répond `403 {"success":false,"error":"not authorized"}`.

    GET /wms/v2/auth/login/vfr/<login>/<MD5 hex du mot de passe>
        → {"success":true,"token":"<JWT>"} et pose le cookie de session
    GET /wms/v2/auth/test/          → {"success":true} (vérification)
    GET /wms/v2/map/<champ>?champ=<champ>&TIME=<ISO Z>&RUN=<ISO Z>
        + paramètres WMS standard (SERVICE/VERSION/REQUEST/LAYERS/CRS/BBOX/
          WIDTH/HEIGHT/FORMAT/TRANSPARENT)
        → PNG à fond transparent, à superposer sur un fond de carte

`service_uid` vaut « vfr » : c'est la valeur codée dans `service_wms.js`.

===========================================================================
LE RUN : POURQUOI `issued_at` EST CONNU ICI
===========================================================================
Contrairement aux TEMSI/WINTEM d'`aeroweb.py`, dont l'heure d'émission n'est
lisible que dans les pixels, le WMS EXIGE le run en paramètre. On le connaît
donc, et `issued_at` dit la vérité : l'âge affiché au briefing est le vrai âge
du calcul, pas celui du téléchargement.

Le choix du run est une table côté client (`context_controller.js`,
`init_arome`), reprise ici telle quelle. Elle ne suit pas la cadence nominale
d'AROME — le visualiseur n'expose que quatre runs (18Z de la veille, puis 03Z,
06Z et 12Z), avec des bascules à des heures rondes bizarres (05h45, 11h05,
15h45, 23h00 UTC). On ne « corrige » pas : c'est le contrat observé, et un run
inexistant renvoie une image vide sans le dire.

===========================================================================
DISCIPLINE RÉSEAU — identique à `aeroweb.py`, et pour les mêmes raisons
===========================================================================
Une session, un login, réutilisés. Cache disque à TTL (le même réglage
`AEROBRIEFER_AEROWEB_TTL`). Aucune boucle de polling. User-Agent identifiable.
Le nombre d'images est borné par la fenêtre de vol : on ne balaie jamais
l'ensemble des échéances disponibles.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta  # noqa: TID251 - durée, pas un instant
from pathlib import Path

import httpx

from ..domain.context import BriefingContext
from ..domain.models import Chart
from ..domain.sourced import Provenance, Sourced
from ..domain.window import UtcDateTime, utcnow
from . import cache
from .aeroweb import BASE_URL, _cache_ttl
from .base import ProviderError

SOURCE = "aeroweb-arome"

#: Identifiant de service codé dans `service_wms.js` du visualiseur.
SERVICE_UID = "vfr"

_USER_AGENT = (
    "aerobriefer/0.1 (briefing VFR personnel, non commercial, usage local ; "
    "compte Aeroweb nominatif)"
)

#: Taille de l'image demandée. La HAUTEUR est recalculée d'après le rapport de
#: la bbox : demander un cadre qui ne correspond pas à l'emprise fait rendre au
#: serveur une bande vide sur le côté (constaté).
_IMAGE_WIDTH = 900
_MAX_HEIGHT = 1400

#: Rapport largeur/hauteur de l'emprise. Une emprise CARRÉE donnait, une fois
#: affichée sur toute la largeur de la page, une carte aussi haute que large —
#: interminable à l'écran pour un vol qui tient dans son tiers central. On
#: élargit donc en X ce qu'on ne réduit pas en Y : la couverture du vol est
#: conservée, la carte devient simplement moins haute.
_BBOX_ASPECT = 4.0 / 3.0

#: Marge autour du cercle englobant du vol, pour voir ce qui arrive à côté.
_BBOX_MARGIN_NM = 15.0

#: Pas de temps des échéances AROME exposées par le visualiseur.
_STEP = timedelta(hours=1)

#: Échéances hors fenêtre gardées de part et d'autre : voir la tendance juste
#: avant et juste après le vol, sans multiplier les images embarquées.
_BRACKET = 1

_NM_TO_M = 1852.0
_EARTH_R = 6378137.0


@dataclass(frozen=True, slots=True)
class _Layer:
    """Une couche AROME et son étiquette dans le briefing."""

    kind: str  # Chart.kind
    champ: str  # nom du champ WMS
    label: str


#: Les couches retenues pour le VFR : ce qui décide d'un vol à vue, dans l'ordre
#: du plus contraignant au plus descriptif. On n'embarque PAS tout le catalogue
#: (73 champs) — un briefing n'est pas un atlas.
LAYERS: tuple[_Layer, ...] = (
    _Layer("arome_visi", "gafor.visi_metropole", "AROME — visibilité"),
    _Layer("arome_plafond", "gafor.plafond_metropole", "AROME — plafond"),
    _Layer("arome_nebul_bas", "model.nebul_bas", "AROME — nébulosité basse"),
)


def _to_mercator(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 → EPSG:3857, le référentiel du visualiseur."""
    x = math.radians(lon) * _EARTH_R
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * _EARTH_R
    return x, y


def select_run(now: UtcDateTime) -> UtcDateTime:
    """Run AROME servi par le visualiseur à l'instant `now` (UTC).

    Table reprise de `context_controller.js` (`init_arome`, origin='fr') :

        00:00 → 05:45   run 18Z de la VEILLE
        05:45 → 11:05   run 03Z
        11:05 → 15:45   run 06Z
        15:45 → 23:00   run 12Z
        23:00 → 24:00   run 18Z

    Ces bornes sont celles du client, pas une cadence théorique. Les inventer
    ferait demander un run inexistant — que le serveur rend en image vide, sans
    erreur.
    """
    minutes = now.hour * 60 + now.minute
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if minutes < 5 * 60 + 45:
        return UtcDateTime.of(day - timedelta(hours=6))  # veille 18Z
    if minutes < 11 * 60 + 5:
        return UtcDateTime.of(day + timedelta(hours=3))
    if minutes < 15 * 60 + 45:
        return UtcDateTime.of(day + timedelta(hours=6))
    if minutes < 23 * 60:
        return UtcDateTime.of(day + timedelta(hours=12))
    return UtcDateTime.of(day + timedelta(hours=18))


def echeances(context: BriefingContext, run: UtcDateTime) -> list[UtcDateTime]:
    """Échéances horaires rondes couvrant la fenêtre de vol, plus le bracket.

    Une échéance ANTÉRIEURE au run n'existe pas : AROME ne rétro-prévoit pas.
    On les écarte plutôt que de demander des images vides.
    """
    start = context.window.start.replace(minute=0, second=0, microsecond=0)
    start = UtcDateTime.of(start - _STEP * _BRACKET)
    stop = UtcDateTime.of(context.window.end + _STEP * _BRACKET)
    out: list[UtcDateTime] = []
    cursor = start
    while cursor <= stop:
        if cursor >= run:
            out.append(cursor)
        cursor = UtcDateTime.of(cursor + _STEP)
    return out


class AromeProvider:
    """Cartes AROME maille fine (visibilité, plafond, nébulosité basse)."""

    name = SOURCE
    is_critical = False
    """Non critique : le briefing reste valide sans AROME — TEMSI, TAF et METAR
    restent les produits de référence. Son absence est signalée, pas fatale."""

    def __init__(
        self,
        *,
        login: str | None = None,
        password: str | None = None,
        cache_dir: Path | str = ".cache/arome",
        client: httpx.Client | None = None,
        layers: Sequence[_Layer] = LAYERS,
        timeout: float = 30.0,
    ) -> None:
        self._login = login if login is not None else os.environ.get("AEROWEB_LOGIN")
        self._password = password if password is not None else os.environ.get("AEROWEB_PASSWORD")
        self._cache_dir = Path(cache_dir)
        self._layers = tuple(layers)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._authenticated = False

    # -- session ------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        missing = [
            name
            for name, value in (
                ("AEROWEB_LOGIN", self._login),
                ("AEROWEB_PASSWORD", self._password),
            )
            if not value
        ]
        if missing:
            raise ProviderError(
                SOURCE,
                "identifiants absents : définir "
                + " et ".join(missing)
                + " dans l'environnement (compte personnel aviation.meteo.fr).",
            )
        return self._login, self._password  # type: ignore[return-value]

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = cache.make_client(
                follow_redirects=True,
                timeout=self._timeout,
                headers={"User-Agent": _USER_AGENT},
            )
        return self._client

    def _authenticate(self) -> None:
        """Login WMS. DISTINCT du login PHP d'Aeroweb : la session PHP ne donne
        aucun droit sur `/wms/`, le GetMap répond 403."""
        if self._authenticated:
            return
        login, password = self._credentials()
        client = self._ensure_client()
        digest = hashlib.md5(password.encode()).hexdigest()
        url = f"{BASE_URL}/wms/v2/auth/login/{SERVICE_UID}/{urllib.parse.quote(login)}/{digest}"
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(SOURCE, f"login WMS injoignable : {exc}") from exc
        if response.status_code != 200 or not response.json().get("success"):
            raise ProviderError(
                SOURCE,
                f"login WMS refusé (HTTP {response.status_code}) — "
                "vérifier AEROWEB_LOGIN / AEROWEB_PASSWORD",
            )
        self._authenticated = True

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None
        self._authenticated = False

    def __enter__(self) -> AromeProvider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- requête ------------------------------------------------------------

    def _bbox(self, context: BriefingContext) -> tuple[str, int]:
        """Emprise EPSG:3857 du vol, et la HAUTEUR d'image qui lui correspond.

        La hauteur est dérivée du rapport de l'emprise : imposer un cadre qui ne
        colle pas à la bbox fait rendre au serveur une bande vide sur le côté.
        """
        circle = context.geometry.bounding_circle()
        reach_m = (circle.radius_nm + _BBOX_MARGIN_NM) * _NM_TO_M
        cx, cy = _to_mercator(circle.center.lat, circle.center.lon)
        # En Mercator l'échelle croît avec la latitude : on dilate la demi-taille
        # du même facteur, sinon l'emprise réelle est trop petite vers le nord.
        stretch = 1.0 / max(0.2, math.cos(math.radians(circle.center.lat)))
        half_y = reach_m * stretch
        half_x = half_y * _BBOX_ASPECT
        x0, y0, x1, y1 = cx - half_x, cy - half_y, cx + half_x, cy + half_y
        ratio = (y1 - y0) / (x1 - x0)
        height = min(_MAX_HEIGHT, max(1, round(_IMAGE_WIDTH * ratio)))
        return f"{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}", height

    def _params(
        self, layer: _Layer, valid_at: UtcDateTime, run: UtcDateTime, bbox: str, height: int
    ) -> dict[str, str]:
        return {
            "champ": layer.champ,
            "TIME": _iso(valid_at),
            "RUN": _iso(run),
            "SERVICE": "WMS",
            "VERSION": "1.3.0",
            "REQUEST": "GetMap",
            "LAYERS": layer.champ,
            "CRS": "EPSG:3857",
            "BBOX": bbox,
            "WIDTH": str(_IMAGE_WIDTH),
            "HEIGHT": str(height),
            "FORMAT": "image/png",
            "TRANSPARENT": "TRUE",
        }

    def _cache_path(self, layer: _Layer, valid_at: UtcDateTime, run: UtcDateTime) -> Path:
        slug = layer.champ.replace(".", "_")
        name = f"{slug}_{run.strftime('%Y%m%d%H%M')}_{valid_at.strftime('%Y%m%d%H%M')}.png"
        path = self._cache_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _image(
        self, layer: _Layer, valid_at: UtcDateTime, run: UtcDateTime, bbox: str, height: int
    ) -> tuple[bytes, str]:
        """Octets PNG, depuis le cache s'il est encore frais. Renvoie aussi l'URL."""
        params = self._params(layer, valid_at, run, bbox, height)
        url = f"{BASE_URL}/wms/v2/map/{layer.champ}?{urllib.parse.urlencode(params)}"
        path = self._cache_path(layer, valid_at, run)
        ttl = _cache_ttl().total_seconds()
        if ttl > 0 and path.exists() and time.time() - path.stat().st_mtime <= ttl:
            return path.read_bytes(), url

        self._authenticate()
        client = self._ensure_client()
        try:
            response = client.get(f"{BASE_URL}/wms/v2/map/{layer.champ}", params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(SOURCE, f"{layer.champ} injoignable : {exc}") from exc
        media = response.headers.get("content-type", "")
        if response.status_code != 200 or not media.startswith("image/"):
            raise ProviderError(
                SOURCE,
                f"{layer.champ} {valid_at:%d/%m %H:%M}Z : HTTP {response.status_code}, "
                f"type {media!r}, corps {response.text[:120]!r}",
            )
        path.write_bytes(response.content)
        return response.content, url

    # -- collecte -----------------------------------------------------------

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[Chart]]:
        now = utcnow()
        run = select_run(now)
        stamps = echeances(context, run)
        if not stamps:
            raise ProviderError(
                SOURCE,
                f"aucune échéance AROME dans la fenêtre : run {run:%d/%m %H:%M}Z "
                f"postérieur au vol ({context.window.end:%d/%m %H:%M}Z)",
            )
        bbox, height = self._bbox(context)

        out: list[Sourced[Chart]] = []
        for layer in self._layers:
            for valid_at in stamps:
                content, url = self._image(layer, valid_at, run, bbox, height)
                out.append(
                    Sourced(
                        Chart(
                            kind=layer.kind,
                            url=url,
                            # Le run EST connu ici : l'âge affiché sera le vrai
                            # âge du calcul, pas celui du téléchargement.
                            issued_at=run,
                            valid_at=valid_at,
                            area="FRANCE (maille fine)",
                            media_type="image/png",
                            content=content,
                        ),
                        Provenance(
                            source=SOURCE,
                            retrieved_at=now,
                            issued_at=run,
                            url=url,
                        ),
                    )
                )
        return out


def _iso(instant: UtcDateTime) -> str:
    """Format attendu par le WMS : ISO 8601 UTC à la seconde, suffixe Z."""
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["LAYERS", "SOURCE", "AromeProvider", "echeances", "select_run"]
