"""Prévisions ponctuelles depuis l'API Norvégienne MET (api.met.no).

Endpoint : ``locationforecast/2.0/compact``, interrogé une seule fois par
briefing, au centre du cercle englobant de la géométrie. C'est un modèle global
(pas une observation) : la donnée est une PRÉVISION de maille, pas une mesure au
terrain. Elle n'a pas vocation à remplacer un METAR ou un TAF, seulement à
combler les échéances qu'ils ne couvrent pas.

Format réel constaté (sondage du 2026-07-20, lat=45.628 lon=-0.9725)
--------------------------------------------------------------------
``properties.meta.units`` déclare explicitement les unités, et elles ne sont PAS
aéronautiques — d'où les conversions ci-dessous ::

    air_pressure_at_sea_level  hPa      -> qnh_hpa (tel quel)
    air_temperature            celsius  -> temperature_c (tel quel)
    cloud_area_fraction        %        -> cloud_cover_pct (tel quel)
    precipitation_amount       mm       -> precipitation_mm (tel quel)
    relative_humidity          %        -> sert au point de rosée estimé
    wind_from_direction        degrees  -> wind_dir_deg (direction D'OÙ vient le vent)
    wind_speed                 m/s      -> wind_speed_kt  (× 1.943844)

Chaque entrée de ``properties.timeseries`` porte un ``time`` ISO 8601 en Z et un
bloc ``data.instant.details`` (valeurs INSTANTANÉES à cette échéance), plus des
blocs de période optionnels ``next_1_hours`` / ``next_6_hours`` /
``next_12_hours``. Seuls les deux premiers portent un
``details.precipitation_amount`` ; ``next_12_hours`` n'a qu'un ``summary``.

Pas de temps : HORAIRE sur les ~60 premières échéances (≈ 2,5 jours), puis
6-HORAIRE jusqu'à ~J+10. Au-delà de la bascule, ``next_1_hours`` disparaît et
seul ``next_6_hours`` subsiste — l'accumulation de précipitation reste donc
homogène au pas courant, ce qui est la raison pour laquelle on peut retomber de
l'un sur l'autre sans mentir sur l'unité (cf. `_precipitation_mm`).

Le endpoint ``compact`` ne fournit PAS de rafales : ``wind_speed_of_gust``
n'existe que dans ``complete``. `wind_gust_kt` reste donc toujours None ici — un
None honnête plutôt qu'une rafale égale au vent moyen, qui se lirait comme une
absence de rafale avérée.

Cache et politesse (conditions d'utilisation met.no)
----------------------------------------------------
met.no impose deux choses, et coupe l'accès en cas de manquement :

1. Un User-Agent IDENTIFIABLE. Sans lui la réponse est un 403. `DEFAULT_USER_AGENT`
   en fournit un ; l'appelant peut le remplacer par le sien.
2. Le respect des en-têtes de cache. La réponse porte ::

       Last-Modified: Mon, 20 Jul 2026 12:00:54 GMT
       Expires:       Mon, 20 Jul 2026 12:31:21 GMT

   `Expires` (≈ 30 min plus tard) est la date avant laquelle il est INTERDIT de
   refaire la requête : le modèle n'aura pas bougé. `Last-Modified` date la
   réponse et sert de valeur d'``If-Modified-Since`` pour la requête suivante,
   à laquelle met.no répond 304 sans corps.

Le provider tient ces deux engagements : il mémorise le dernier couple, sert le
payload mémorisé tant que `Expires` n'est pas dépassé, et revalide ensuite en
conditionnel. Un 304 réutilise le payload mémorisé. Noter que ``meta.updated_at``
(dernier tour du modèle) est distinct de `Last-Modified` (fabrication de la
réponse) : c'est `updated_at` qui alimente `Provenance.issued_at`, parce que
c'est lui qui donne l'âge réel de l'information.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, cast

import httpx

from ..domain.context import BriefingContext
from ..domain.models import ForecastPoint
from ..domain.sourced import Provenance, Sourced
from ..domain.window import TimeWindow, UtcDateTime, utcnow
from . import cache
from .base import ProviderError, sanity_check

ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

DEFAULT_USER_AGENT = "aerobriefer/0.1 (https://github.com/sylvainr/aerobriefer)"

FORECAST_PADDING_HOURS = 2.0
"""Marge d'échéances rendues DE PART ET D'AUTRE de la fenêtre de vol.

Voir la tendance juste avant et juste après le créneau aide à décider (un front
qui arrive une heure après l'ETA change la donne). Ces échéances hors fenêtre
sont rendues, mais l'affichage les grise pour qu'on ne les confonde pas avec le
vol lui-même."""


def _padded(window: TimeWindow, hours: float) -> TimeWindow:
    """Fenêtre élargie de `hours` de chaque côté (0 = inchangée)."""
    if hours <= 0:
        return window
    from datetime import timedelta  # noqa: TID251 - une durée, pas un instant

    pad = timedelta(hours=hours)
    return TimeWindow(window.start - pad, window.end + pad)


"""met.no renvoie 403 sur un User-Agent générique ou absent. Il doit permettre de
nous joindre en cas d'abus — c'est la contrepartie de la gratuité."""

MS_TO_KT = 1.943844
"""1 m/s = 1.943844 kt (1 kt = 1852 m/h exactement)."""

DEFAULT_TIMEOUT_S = 10.0

_CLOUD_BASE_FT_PER_C = 400.0
"""Écart température/point de rosée -> hauteur de la base : ≈ 400 ft par °C.

Équivalent de la forme métrique 125 m par °C. Voir `estimate_cloud_base_ft`, et
lire l'avertissement qui s'y trouve avant d'utiliser la valeur.
"""

_CLOUD_BASE_MIN_COVER_PCT = 25.0
"""En dessous de SCT (2 octas ≈ 25 %), on ne calcule pas de base.

La formule décrit la base des cumulus de convection. Par ciel clair ou FEW il
n'y a pas de plafond à annoncer, et sortir un nombre reviendrait à inventer une
couche qui n'existe pas.
"""

_CLOUD_BASE_MAX_SPREAD_C = 15.0
"""Au-delà de ~15 °C d'écart la formule extrapole bien au-dessus de son domaine
de validité. On préfère None à un plafond fantaisiste."""


def ms_to_knots(value_ms: float) -> float:
    """m/s -> nœuds. Le domaine est en unités aéro, la source en SI."""
    return value_ms * MS_TO_KT


def dewpoint_c(temperature_c: float, relative_humidity_pct: float) -> float | None:
    """Point de rosée par la formule de Magnus-Tetens.

    met.no ne le donne pas ; il se déduit de la température et de l'humidité
    relative. Renvoie None si l'humidité est hors de son domaine physique.
    """
    if not 0.0 < relative_humidity_pct <= 100.0:
        return None
    a, b = 17.625, 243.04
    gamma = math.log(relative_humidity_pct / 100.0) + (a * temperature_c) / (b + temperature_c)
    return (b * gamma) / (a - gamma)


def estimate_cloud_base_ft(
    temperature_c: float | None,
    relative_humidity_pct: float | None,
    cloud_cover_pct: float | None,
) -> float | None:
    """ESTIMATION de la base des nuages — grandeur DÉRIVÉE, jamais observée.

    ATTENTION, information de sécurité : met.no ne fournit aucune hauteur de
    base. Ce qui sort d'ici est le produit d'une règle du pouce de convection
    (≈ 400 ft par °C d'écart température/point de rosée), appliquée à un point
    de rosée lui-même reconstruit depuis l'humidité relative. Deux
    approximations empilées, sur une prévision de modèle.

    Elle vaut pour une base de cumulus par convection diurne. Elle ne décrit ni
    une couche stratiforme, ni un plafond d'advection, ni une inversion — et
    c'est précisément dans ces cas-là qu'un plafond bas est dangereux. Elle ne
    remplace donc PAS un plafond METAR/TAF, et tout rendu doit l'annoncer comme
    estimée.

    Renvoie None dès que le calcul n'est pas défendable — cf.
    `_CLOUD_BASE_MIN_COVER_PCT` et `_CLOUD_BASE_MAX_SPREAD_C`. La règle est
    d'omettre plutôt que de deviner : une base absente se voit, une base fausse
    se croit.
    """
    if temperature_c is None or relative_humidity_pct is None:
        return None
    if cloud_cover_pct is None or cloud_cover_pct < _CLOUD_BASE_MIN_COVER_PCT:
        return None

    dewpoint = dewpoint_c(temperature_c, relative_humidity_pct)
    if dewpoint is None:
        return None

    spread = temperature_c - dewpoint
    if spread < 0.0 or spread > _CLOUD_BASE_MAX_SPREAD_C:
        return None
    return round(spread * _CLOUD_BASE_FT_PER_C)


class MetNoProvider:
    """Provider de `ForecastPoint` depuis api.met.no. Conforme au Protocol `Provider`.

    Non critique : son absence dégrade le briefing sans le rendre inexploitable,
    parce que METAR et TAF restent les sources qui font foi.
    """

    name = "met.no"
    category = "forecast"
    is_critical = False

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        endpoint: str = ENDPOINT,
        padding_hours: float = 0.0,
    ) -> None:
        sanity_check(
            self.name,
            bool(user_agent) and "aerobriefer" in user_agent.lower(),
            "User-Agent non identifiable : met.no répond 403 sans contact joignable",
        )
        self._user_agent = user_agent
        self._client = client
        self._timeout_s = timeout_s
        self._endpoint = endpoint
        # Échéances de contexte rendues de part et d'autre de la fenêtre. 0 par
        # défaut = sélection stricte (le contrat historique) ; le CLI l'active
        # pour montrer la tendance juste avant/après le vol.
        self._padding_hours = padding_hours

        # État de cache imposé par les conditions d'utilisation met.no.
        self._cached_payload: Mapping[str, Any] | None = None
        self._cached_key: tuple[float, float] | None = None
        self.last_modified: UtcDateTime | None = None
        self.expires: UtcDateTime | None = None

    # -- Protocol -------------------------------------------------------

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[ForecastPoint]]:
        """Une prévision par échéance dans la fenêtre, au centre de la géométrie.

        Lève `ProviderError` sur tout échec, y compris « la fenêtre est hors de
        portée du modèle » : rendre une liste vide laisserait croire à une
        absence de météo plutôt qu'à une absence de donnée.
        """
        center = context.geometry.bounding_circle().center
        payload = self._payload(center.lat, center.lon)
        url = f"{self._endpoint}?lat={center.lat:.4f}&lon={center.lon:.4f}"

        raw_properties = payload.get("properties")
        sanity_check(self.name, isinstance(raw_properties, dict), "réponse sans 'properties'")
        properties = cast("dict[str, Any]", raw_properties)

        series = properties.get("timeseries")
        sanity_check(
            self.name,
            isinstance(series, list) and bool(series),
            "réponse sans échéances exploitables dans 'properties.timeseries'",
        )
        series = cast("list[Any]", series)

        issued_at = self._issued_at(properties)
        provenance = Provenance(
            source=self.name,
            retrieved_at=utcnow(),
            issued_at=issued_at,
            url=url,
        )

        entries = self._parse_entries(series)
        selected = self._select(entries, _padded(context.window, self._padding_hours))
        if not selected:
            first, last = entries[0][0], entries[-1][0]
            raise ProviderError(
                self.name,
                f"aucune échéance dans la fenêtre {context.window.start:%Y-%m-%dT%H:%MZ}"
                f"/{context.window.end:%Y-%m-%dT%H:%MZ} ; le modèle couvre "
                f"{first:%Y-%m-%dT%H:%MZ} à {last:%Y-%m-%dT%H:%MZ}",
            )

        return tuple(
            Sourced(value=self._to_forecast_point(valid_at, data, center), provenance=provenance)
            for valid_at, data in selected
        )

    # -- Sélection temporelle -------------------------------------------

    def _select(
        self,
        entries: Sequence[tuple[UtcDateTime, Mapping[str, Any]]],
        window: TimeWindow,
    ) -> list[tuple[UtcDateTime, Mapping[str, Any]]]:
        """Les échéances qui concernent la fenêtre de vol.

        Prédicat principal : `window.contains` sur l'échéance elle-même. Les
        valeurs de `instant` sont ponctuelles, pas des moyennes de période — les
        retenir sur leur propre horodatage est donc le filtrage exact.

        Repli : si la fenêtre est plus courte que le pas du modèle (un vol de 40
        min entre deux échéances horaires), `contains` ne retient rien alors que
        la donnée existe. On repasse alors par `overlaps` en donnant à chaque
        échéance sa validité [t, t_suivante], ce qui encadre la fenêtre. Mieux
        vaut la prévision encadrante que pas de météo du tout.
        """
        contained = [(t, data) for t, data in entries if window.contains(t)]
        if contained:
            return contained

        bracketing = []
        for index, (valid_at, data) in enumerate(entries):
            next_at = entries[index + 1][0] if index + 1 < len(entries) else valid_at
            if TimeWindow(valid_at, next_at).overlaps(window):
                bracketing.append((valid_at, data))
        return bracketing

    # -- Conversions ----------------------------------------------------

    def _to_forecast_point(
        self,
        valid_at: UtcDateTime,
        data: Mapping[str, Any],
        center: Any,
    ) -> ForecastPoint:
        instant = data.get("instant", {})
        details = instant.get("details", {}) if isinstance(instant, dict) else {}

        wind_ms = _number(details.get("wind_speed"))
        temperature_c = _number(details.get("air_temperature"))
        humidity_pct = _number(details.get("relative_humidity"))
        cloud_cover_pct = _number(details.get("cloud_area_fraction"))

        return ForecastPoint(
            valid_at=valid_at,
            position=center,
            wind_dir_deg=_number(details.get("wind_from_direction")),
            wind_speed_kt=None if wind_ms is None else round(ms_to_knots(wind_ms), 1),
            # `compact` ne porte pas de rafale : cf. docstring du module.
            wind_gust_kt=None,
            temperature_c=temperature_c,
            cloud_cover_pct=cloud_cover_pct,
            cloud_base_ft=estimate_cloud_base_ft(temperature_c, humidity_pct, cloud_cover_pct),
            precipitation_mm=self._precipitation_mm(data),
            qnh_hpa=_number(details.get("air_pressure_at_sea_level")),
        )

    @staticmethod
    def _precipitation_mm(data: Mapping[str, Any]) -> float | None:
        """Cumul de précipitation sur le pas courant du modèle.

        `next_1_hours` tant que le modèle est horaire, `next_6_hours` une fois
        passé au pas 6-horaire — le bloc retenu est celui qui couvre l'intervalle
        jusqu'à l'échéance suivante, donc la valeur reste homogène au pas. La
        dernière échéance de la série ne porte aucun des deux : None.
        """
        for block in ("next_1_hours", "next_6_hours"):
            section = data.get(block)
            if isinstance(section, dict):
                amount = _number(section.get("details", {}).get("precipitation_amount"))
                if amount is not None:
                    return amount
        return None

    # -- Parsing --------------------------------------------------------

    def _parse_entries(self, series: Sequence[Any]) -> list[tuple[UtcDateTime, Mapping[str, Any]]]:
        entries: list[tuple[UtcDateTime, Mapping[str, Any]]] = []
        for item in series:
            if not isinstance(item, dict):
                continue
            raw_time = item.get("time")
            data = item.get("data")
            if not isinstance(raw_time, str) or not isinstance(data, dict):
                continue
            try:
                # UtcDateTime.parse et non fromisoformat : un ISO sans offset
                # produirait un naïf, interdit dans le domaine.
                valid_at = UtcDateTime.parse(raw_time, "timeseries[].time")
            except (ValueError, TypeError):
                continue
            entries.append((valid_at, data))

        sanity_check(
            self.name,
            bool(entries),
            "aucune échéance horodatée exploitable dans la série",
        )
        entries.sort(key=lambda pair: pair[0])
        return entries

    def _issued_at(self, properties: Mapping[str, Any]) -> UtcDateTime | None:
        """`meta.updated_at` : dernier tour de modèle, l'âge réel de la donnée."""
        meta = properties.get("meta")
        raw = meta.get("updated_at") if isinstance(meta, dict) else None
        if not isinstance(raw, str):
            return self.last_modified
        try:
            return UtcDateTime.parse(raw, "meta.updated_at")
        except (ValueError, TypeError):
            return self.last_modified

    # -- Transport ------------------------------------------------------

    def _payload(self, lat: float, lon: float) -> Mapping[str, Any]:
        """Requête HTTP, dans le respect du cache demandé par met.no."""
        key = (round(lat, 4), round(lon, 4))
        if self._is_fresh(key):
            assert self._cached_payload is not None
            return self._cached_payload

        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if self._cached_key == key and self.last_modified is not None:
            headers["If-Modified-Since"] = format_datetime(self.last_modified, usegmt=True)

        try:
            response = self._request(key, headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, f"délai de {self._timeout_s:g} s dépassé : {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"erreur de transport : {exc}") from exc

        if response.status_code == 304 and self._cached_payload is not None:
            self._remember_cache_headers(response)
            return self._cached_payload

        if response.status_code == 403:
            raise ProviderError(
                self.name,
                "403 : met.no refuse ce User-Agent ; il doit identifier "
                f"l'application et un moyen de contact (reçu {self._user_agent!r})",
            )
        if response.status_code == 429:
            raise ProviderError(
                self.name, "429 : quota dépassé ; respecter l'en-tête Expires entre deux appels"
            )
        if response.status_code != 200:
            raise ProviderError(self.name, f"statut HTTP inattendu : {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(self.name, f"réponse non JSON : {exc}") from exc

        sanity_check(self.name, isinstance(payload, dict), "racine JSON inattendue")

        self._remember_cache_headers(response)
        typed_payload = cast("Mapping[str, Any]", payload)
        self._cached_payload = typed_payload
        self._cached_key = key
        return typed_payload

    def _request(self, key: tuple[float, float], headers: Mapping[str, str]) -> httpx.Response:
        params = {"lat": f"{key[0]:.4f}", "lon": f"{key[1]:.4f}"}
        if self._client is not None:
            return self._client.get(
                self._endpoint, params=params, headers=dict(headers), timeout=self._timeout_s
            )
        # Timeout explicite : sans lui httpx attendrait indéfiniment et le
        # briefing resterait suspendu à une source non critique.
        with cache.make_client(timeout=self._timeout_s, follow_redirects=True) as client:
            return client.get(self._endpoint, params=params, headers=dict(headers))

    def _is_fresh(self, key: tuple[float, float]) -> bool:
        """Vrai tant que `Expires` n'est pas atteint : met.no interdit de
        redemander avant, le modèle n'ayant pas été retourné."""
        return (
            self._cached_payload is not None
            and self._cached_key == key
            and self.expires is not None
            and utcnow() < self.expires
        )

    def _remember_cache_headers(self, response: httpx.Response) -> None:
        self.last_modified = _http_date(response.headers.get("Last-Modified"))
        self.expires = _http_date(response.headers.get("Expires"))


def _http_date(value: str | None) -> UtcDateTime | None:
    """En-tête HTTP RFC 1123 -> UtcDateTime. None si absent ou illisible."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return UtcDateTime.of(parsed, "en-tête de cache")


def _number(value: Any) -> float | None:
    """Un champ absent ou non numérique devient None plutôt que 0.

    Distinction qui compte : un vent nul et un vent inconnu ne se pilotent pas
    pareil.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
