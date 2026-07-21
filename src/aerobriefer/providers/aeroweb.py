"""Provider Aeroweb (Météo-France) — cartes météo aéronautiques.

===========================================================================
CADRE JURIDIQUE — À LIRE AVANT TOUTE MODIFICATION
===========================================================================
Les cartes servies par https://aviation.meteo.fr sont © Météo-France. Leurs
conditions générales d'utilisation interdisent, sauf accord écrit préalable :

  - « toute extraction répétée et systématique » de la base ;
  - « toute utilisation dans un environnement informatique en réseau » ;
  - « l'insertion d'une image dans une page ne lui appartenant pas » ;
  - « toute exploitation à but commercial ».

Ce module est donc écrit pour un usage STRICTEMENT PERSONNEL ET LOCAL : un
pilote, son propre compte, sa propre machine, son propre dossier de vol. Il
n'est pas un service, il ne doit pas être exposé derrière une API, et les
images qu'il rapatrie ne doivent pas être republiées.

Ces interdictions ne sont pas seulement documentées, elles sont INCARNÉES
dans le code — c'est le seul moyen qu'elles survivent à une relecture
distraite :

  1. Cache disque obligatoire. Une échéance déjà téléchargée n'est JAMAIS
     re-téléchargée : le couple (produit, validité) désigne une image
     immuable. Le cache n'est pas une optimisation, c'est la garantie
     structurelle qu'aucune boucle appelante ne peut transformer ce module
     en aspirateur.
  2. Les index d'échéances portent un TTL égal à la cadence de PRODUCTION du
     produit (15 min radar/satellite, 3 h TEMSI/WINTEM, 6 h fronts).
     Interroger plus vite que la production est impossible : la réponse
     vient du disque. Voir `_Product.cadence`.
  3. Une seule session HTTP, un seul login, réutilisés. Aucune boucle de
     polling n'existe dans ce module et il ne faut pas en ajouter.
  4. User-Agent identifiable (`_USER_AGENT`) : on ne se déguise pas en
     navigateur.

===========================================================================
PROTOCOLE (rétro-ingénierie vérifiée en conditions réelles le 2026-07-20)
===========================================================================
Login — pile PHP historique :
    GET  /login.php                 pose PHPSESSID + TS01436cc2
    POST /ajax/login_valid.php      login=<login>&password=<MD5 hex>
                                    corps de réponse littéral « ok »
Le MD5 est calculé CÔTÉ CLIENT par le JavaScript de la page : ce que le
serveur attend est le condensat hexadécimal, jamais le mot de passe en
clair. Pas de CSRF, pas de captcha.

Images — endpoint unique, octets PNG bruts, session obligatoire (401 sinon) :
    GET /affiche_image.php?type=<produit>&date=<YYYYMMDDHHMMSS>&mode=img&comment=
`date` est l'heure de VALIDITÉ en UTC.

Index des échéances — interrogés AVANT de construire la moindre URL d'image.
C'est indispensable et non cosmétique : sur une échéance inexistante
l'endpoint image répond **500 + text/html**, et non une image de
remplacement. Deviner les URLs produirait donc des échecs, et surtout on
n'a aucun autre moyen de savoir jusqu'où porte la prévision.
    /get_domaine_layers_echeances.php?domaine=19   TEMSI/WINTEM FRANCE
    /get_domaine_layers_echeances.php?domaine=20   TEMSI/WINTEM EUROC
    /anim_carte_front.php?...&layer=front/europeouest&prof_avant=-18
    /anim_sat.php?domaine=<3 France|2 Europe>&type_image=<1 CC|2 IR|3 radar|4 VIS>
Les quatre servent du HTML où l'échéance apparaît toujours sous la forme
`type=<produit>&date=<YYYYMMDDHHMMSS>` — une seule regex les couvre.

===========================================================================
L'HEURE D'ÉMISSION : POURQUOI `issued_at` EST SOUVENT None
===========================================================================
Point tranché par la mesure, pas par hypothèse.

L'URL ne porte que la VALIDITÉ. L'heure d'émission (le réseau) n'est gravée
que dans les pixels de la carte. On a donc testé le header HTTP
`Last-Modified` d'une réponse image authentifiée : il vaut
`Tue, 11 Jun 2024 08:33:45 GMT` — **identiquement sur tous les produits et
toutes les échéances**, y compris une image radar vieille de quinze
minutes. C'est la date du script PHP, pas celle de la donnée. L'utiliser
comme `issued_at` daterait toutes les cartes de juin 2024.

L'émission n'est pas perdue pour autant : elle est LISIBLE PAR L'HUMAIN, dans
les pixels. La carte de front la titre en clair
(« Fronts et isobares pour le 21/07/2026 - 00 UTC (réseau: 20/07/2026 -
00 UTC) »), le TEMSI la porte dans son en-tête OMM (« QGFE96 LFPW 200000 »,
soit le 20 à 00:00Z). L'extraire demanderait un OCR, dépendance qu'on refuse
pour une donnée que le pilote lit d'un coup d'œil sur la carte elle-même.

Et le risque est mesuré, pas théorique : le 2026-07-20, la carte de front
valide le 21 à 00 UTC provenait du réseau du 20 à 00 UTC — VINGT-QUATRE
heures d'âge, sur un fichier téléchargé à l'instant.

Conséquence assumée, par famille de produit :

  - Produits d'OBSERVATION (radar, satellite) : `is_observation=True`.
    L'échéance EST l'instant d'observation, à la minute de diffusion près.
    `issued_at = valid_at` est donc exact, et `Provenance.age_minutes()`
    dit la vérité.
  - Produits de PRÉVISION (front, TEMSI, WINTEM) : `issued_at = None`.
    LIMITE CONNUE ET NON CONTOURNABLE par cette source. Une carte de front
    valide 12 UTC peut provenir du réseau de 00 UTC : elle a douze heures
    d'âge alors que rien dans le protocole ne permet de le savoir.
    `age_minutes()` retombe alors sur `retrieved_at` et SOUS-ESTIME l'âge.
    Le briefing doit donc présenter ces cartes par leur validité
    (`Chart.valid_at`, toujours renseignée) et ne jamais laisser croire
    qu'une fraîcheur de téléchargement vaut fraîcheur de prévision.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path

import httpx

from ..domain.context import BriefingContext
from ..domain.models import Chart
from ..domain.sourced import Provenance, Sourced
from ..domain.window import TimeWindow, UtcDateTime, utcnow
from . import cache
from .base import ProviderError, sanity_check

SOURCE = "aeroweb"
BASE_URL = "https://aviation.meteo.fr"

_USER_AGENT = (
    "aerobriefer/0.1 (briefing VFR personnel, non commercial, usage local ; "
    "compte Aeroweb nominatif)"
)

#: Une échéance apparaît toujours sous cette forme dans les quatre index.
_ECHEANCE_RE = re.compile(r"type=([\w/]+)&(?:amp;)?date=(\d{14})")

#: Réponse littérale de /ajax/login_valid.php en cas de succès.
_LOGIN_OK = "ok"


@dataclass(frozen=True, slots=True)
class _Product:
    """Un produit Aeroweb et la manière d'en lister les échéances."""

    kind: str  # Chart.kind
    image_type: str  # valeur du paramètre `type=`
    index_path: str  # URL relative de l'index d'échéances
    cadence: timedelta  # cadence de PRODUCTION — sert de TTL au cache d'index
    is_observation: bool  # observation (échéance = émission) vs prévision
    area: str | None = None
    flight_level: str | None = None
    bracket: int = 1  # nb d'échéances avant/après la fenêtre (fronts : évolution)


_FRONT_INDEX = (
    "/anim_carte_front.php?tt=xxx&width=600&height=539&layer=front/europeouest&prof_avant=-18"
)

#: Les produits pertinents pour un vol VFR en France métropolitaine.
PRODUCTS: tuple[_Product, ...] = (
    _Product(
        kind="front",
        image_type="front/europeouest",
        index_path=_FRONT_INDEX,
        cadence=timedelta(hours=6),
        is_observation=False,
        area="EUROPE-OUEST",
        bracket=2,  # évolution du système : 2 échéances avant + 2 après
    ),
    _Product(
        # TEMSI France basse couche (SFC-FL150) — la carte pertinente pour le VFR.
        # Aeroweb ne publie souvent qu'UNE échéance à la fois (produit « instantané »
        # réémis quelques fois par jour), contrairement au WINTEM résolu heure par
        # heure. Le bracket=2 prend l'avant/après quand plusieurs sont disponibles ;
        # s'il n'y en a qu'une, on n'en affiche qu'une — ce n'est pas un manque.
        kind="temsi",
        image_type="sigwx/fr/france",
        index_path="/get_domaine_layers_echeances.php?domaine=19",
        cadence=timedelta(hours=3),
        is_observation=False,
        area="FRANCE",
        flight_level="SFC-FL150",
        bracket=2,
    ),
    _Product(
        kind="wintem",
        image_type="wintemp/fr/france/fl020",
        index_path="/get_domaine_layers_echeances.php?domaine=19",
        cadence=timedelta(hours=3),
        is_observation=False,
        area="FRANCE",
        flight_level="FL020",
        bracket=2,
    ),
    _Product(
        kind="satellite",
        image_type="satellite/france/cc",
        index_path="/anim_sat.php?domaine=3&type_image=1",
        cadence=timedelta(minutes=15),
        is_observation=True,
        area="FRANCE",
    ),
    _Product(
        kind="radar",
        image_type="radar/france",
        index_path="/anim_sat.php?domaine=3&type_image=3",
        cadence=timedelta(minutes=15),
        is_observation=True,
        area="FRANCE",
    ),
)


def _parse_echeance(stamp: str) -> UtcDateTime:
    """« YYYYMMDDHHMMSS » (UTC) vers UtcDateTime.

    Découpage manuel plutôt que strptime : le domaine bannit `datetime` nu, et
    `UtcDateTime` est le seul point de construction autorisé.
    """
    return UtcDateTime(
        int(stamp[0:4]),
        int(stamp[4:6]),
        int(stamp[6:8]),
        int(stamp[8:10]),
        int(stamp[10:12]),
        int(stamp[12:14]),
        tzinfo=UTC,
    )


def _format_echeance(instant: UtcDateTime) -> str:
    return instant.strftime("%Y%m%d%H%M%S")


def _image_query(image_type: str, stamp: str) -> str:
    return f"/affiche_image.php?type={image_type}&date={stamp}&mode=img&comment="


def select_forecast_echeances(
    available: Sequence[UtcDateTime], window: TimeWindow, *, bracket: int = 1
) -> tuple[UtcDateTime, ...]:
    """Échéances de prévision à retenir pour encadrer la fenêtre de vol.

    On garde tout ce qui tombe DANS la fenêtre, plus les `bracket` échéances
    immédiatement antérieures et postérieures. L'encadrement est délibéré : un
    vol de 08 à 11 UTC avec des cartes à 06 et 12 UTC n'a aucune échéance
    interne, et se retrouverait sans carte du tout si on filtrait strictement.

    `bracket` > 1 sert aux cartes de front, où voir l'ÉVOLUTION (plusieurs
    échéances avant/après) renseigne sur le déplacement du système, pas seulement
    sur l'instant. Radar/satellite ne passent pas par ici (observation, une
    seule image temps réel).
    """
    ordered = sorted(set(available))
    inside = [e for e in ordered if window.contains(e)]
    before = [e for e in ordered if e < window.start]
    after = [e for e in ordered if e > window.end]

    chosen = set(inside)
    chosen.update(before[-bracket:])
    chosen.update(after[:bracket])
    return tuple(sorted(chosen))


def select_observation_echeances(
    available: Sequence[UtcDateTime], *, count: int = 6
) -> tuple[UtcDateTime, ...]:
    """Les `count` observations les plus récentes, pour une ANIMATION.

    Radar et satellite sont des images successives (~15 min) : la boucle des
    dernières frames montre le déplacement et l'évolution, bien plus parlant
    qu'une image figée. On prend les plus fraîches, dans l'ordre chronologique
    pour que le lecteur les joue de la plus ancienne à la plus récente.
    """
    return tuple(sorted(available)[-count:]) if available else ()


class AerowebProvider:
    """Cartes Aeroweb pour un `BriefingContext`. Conforme au Protocol `Provider`.

    Les octets sont embarqués dans `Chart.content` : une carte non embarquée
    n'est pas consultable hors ligne, donc inutilisable en vol (cf.
    `Chart.is_embedded`).
    """

    name = SOURCE
    category = "chart"
    is_critical = False

    def __init__(
        self,
        *,
        login: str | None = None,
        password: str | None = None,
        cache_dir: Path | str = ".cache/aeroweb",
        client: httpx.Client | None = None,
        products: Sequence[_Product] = PRODUCTS,
        timeout: float = 30.0,
    ) -> None:
        self._login = login if login is not None else os.environ.get("AEROWEB_LOGIN")
        self._password = password if password is not None else os.environ.get("AEROWEB_PASSWORD")
        self._cache_dir = Path(cache_dir)
        self._products = tuple(products)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._authenticated = False

    # -- session ------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        missing = [
            var
            for var, value in (
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
                + " dans l'environnement (compte personnel aviation.meteo.fr). "
                "Ils ne sont volontairement pas stockés dans le code.",
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
        """Une session, un login, réutilisés pour tout le `fetch`."""
        if self._authenticated:
            return
        login, password = self._credentials()
        client = self._ensure_client()
        try:
            client.get(f"{BASE_URL}/login.php")
            response = client.post(
                f"{BASE_URL}/ajax/login_valid.php",
                data={
                    "login": login,
                    # Le MD5 est fait côté client par le JS d'origine : le
                    # serveur attend le condensat, pas le mot de passe.
                    "password": hashlib.md5(password.encode()).hexdigest(),
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(SOURCE, f"login injoignable : {exc}") from exc

        body = response.text.strip().lower()
        if response.status_code != 200 or body != _LOGIN_OK:
            raise ProviderError(
                SOURCE,
                f"login refusé (HTTP {response.status_code}, corps {body[:80]!r}) — "
                "vérifier AEROWEB_LOGIN / AEROWEB_PASSWORD",
            )
        self._authenticated = True

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None
        self._authenticated = False

    def __enter__(self) -> AerowebProvider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- cache --------------------------------------------------------------

    def _cache_path(self, *parts: str) -> Path:
        path = self._cache_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _slug(image_type: str) -> str:
        return image_type.replace("/", "_")

    def _cached_index(self, key: str, max_age: timedelta) -> str | None:
        """Index encore valide au regard de la cadence de production.

        C'est ici que se joue l'interdiction d'extraction systématique : tant
        que le TTL court, aucune requête ne part, quelle que soit l'insistance
        de l'appelant.
        """
        path = self._cache_path("index", f"{key}.html")
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > max_age.total_seconds():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def _store_index(self, key: str, body: str) -> None:
        self._cache_path("index", f"{key}.html").write_text(
            body, encoding="utf-8", errors="replace"
        )

    def image_path(self, product: _Product, valid_at: UtcDateTime) -> Path:
        """Emplacement disque d'une échéance — publique, le rendu PDF la relit."""
        stamp = _format_echeance(valid_at)
        return self._cache_path("images", f"{self._slug(product.image_type)}_{stamp}.png")

    # -- réseau -------------------------------------------------------------

    def _fetch_index(self, product: _Product) -> tuple[UtcDateTime, ...]:
        # Le produit seul ne suffit pas comme clé (radar et satellite partagent
        # anim_sat.php), l'URL brute ferait un nom de fichier illégal : on
        # concatène le produit et un condensat court de son index.
        digest = hashlib.sha256(product.index_path.encode()).hexdigest()[:8]
        key = f"{self._slug(product.image_type)}_{digest}"
        body = self._cached_index(key, product.cadence)
        if body is None:
            self._authenticate()
            client = self._ensure_client()
            try:
                response = client.get(f"{BASE_URL}{product.index_path}")
            except httpx.HTTPError as exc:
                raise ProviderError(SOURCE, f"index {product.kind} injoignable : {exc}") from exc
            if response.status_code != 200:
                raise ProviderError(
                    SOURCE,
                    f"index {product.kind} : HTTP {response.status_code}",
                )
            body = response.text
            self._store_index(key, body)

        stamps = [
            stamp
            for image_type, stamp in _ECHEANCE_RE.findall(body)
            if image_type == product.image_type
        ]
        return tuple(sorted({_parse_echeance(s) for s in stamps}))

    def _fetch_image(self, product: _Product, valid_at: UtcDateTime) -> bytes:
        """Octets PNG d'une échéance, depuis le cache si déjà rapatriée.

        Le couple (produit, validité) désigne une image immuable : une fois sur
        disque, elle n'est jamais redemandée. C'est ce qui rend structurellement
        impossible l'« extraction répétée » que les CGU prohibent.
        """
        path = self.image_path(product, valid_at)
        if path.exists() and path.stat().st_size > 0:
            return path.read_bytes()

        self._authenticate()
        client = self._ensure_client()
        url = f"{BASE_URL}{_image_query(product.image_type, _format_echeance(valid_at))}"
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(SOURCE, f"image {product.kind} injoignable : {exc}") from exc

        if response.status_code != 200:
            # 500 = échéance inexistante, 401 = session perdue.
            raise ProviderError(
                SOURCE,
                f"image {product.kind} {_format_echeance(valid_at)} : HTTP {response.status_code}",
            )

        content = response.content
        # Source fragile : le même endpoint sait répondre 200 text/html. On
        # vérifie la signature PNG plutôt que de faire confiance au statut.
        sanity_check(
            SOURCE,
            content.startswith(b"\x89PNG\r\n\x1a\n"),
            f"réponse non-PNG pour {product.image_type} à "
            f"{_format_echeance(valid_at)} ({len(content)} octets, "
            f"content-type {response.headers.get('content-type')!r})",
        )
        path.write_bytes(content)
        return content

    # -- collecte -----------------------------------------------------------

    def _charts_for(self, product: _Product, window: TimeWindow) -> list[Sourced[Chart]]:
        available = self._fetch_index(product)
        if not available:
            return []

        if product.is_observation:
            wanted = select_observation_echeances(available)
        else:
            wanted = select_forecast_echeances(available, window, bracket=product.bracket)

        collected: list[Sourced[Chart]] = []
        for valid_at in wanted:
            content = self._fetch_image(product, valid_at)
            url = f"{BASE_URL}{_image_query(product.image_type, _format_echeance(valid_at))}"
            # Observation : l'échéance vaut émission. Prévision : émission
            # inconnue, cf. l'en-tête du module — surtout ne pas inventer.
            issued_at = valid_at if product.is_observation else None
            chart = Chart(
                kind=product.kind,
                url=url,
                issued_at=issued_at,
                valid_at=valid_at,
                area=product.area,
                flight_level=product.flight_level,
                media_type="image/png",
                content=content,
            )
            collected.append(
                Sourced(
                    value=chart,
                    provenance=Provenance(
                        source=SOURCE,
                        retrieved_at=utcnow(),
                        issued_at=issued_at,
                        url=url,
                    ),
                )
            )
        return collected

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[Chart]]:
        """Cartes couvrant `context.window`. Lève `ProviderError` en cas d'échec.

        Un produit sans échéance pour la fenêtre est une absence réelle, pas une
        panne : on l'ignore (typiquement le TEMSI France, dont la portée de
        prévision ne dépasse pas quelques heures, alors que les cartes de front
        vont à trois jours). En revanche l'absence TOTALE de carte signale une
        collecte cassée, et lève — jamais de liste vide silencieuse.
        """
        charts: list[Sourced[Chart]] = []
        for product in self._products:
            charts.extend(self._charts_for(product, context.window))

        if not charts:
            raise ProviderError(
                SOURCE,
                "aucune carte disponible pour la fenêtre "
                f"{context.window.start:%Y-%m-%d %H:%M}Z → "
                f"{context.window.end:%Y-%m-%d %H:%M}Z "
                f"({len(self._products)} produits interrogés)",
            )
        return tuple(charts)
