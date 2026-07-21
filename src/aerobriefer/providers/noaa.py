"""METAR et TAF depuis l'Aviation Weather Center de la NOAA (aviationweather.gov).

Source gratuite, sans authentification, couverture mondiale. Deux endpoints :

    GET https://aviationweather.gov/api/data/metar?ids=LFPN,LFBD&format=json
    GET https://aviationweather.gov/api/data/taf?ids=LFPN&format=json

Politique de décodage
---------------------
Le brut (`rawOb` / `rawTAF`) est la seule autorité. On le conserve TOUJOURS dans
`raw_text`, et le décodage est un confort greffé par-dessus : si avwx échoue, la
donnée remonte quand même avec ses champs décodés à `None`. Un parseur cassé ne
doit jamais faire disparaître un METAR — c'est une exigence de sécurité, pas de
confort. Tout le décodage est donc enveloppé et ne peut pas propager d'exception.

On croise deux sources pour chaque champ : le JSON de la NOAA (déjà contrôlé en
qualité côté AWC, horodatages en epoch non ambigus) et le parsing avwx du brut
(unités natives du message, donc justes en Europe où la visibilité est en
mètres). Quand les deux sont disponibles, on prend le plus fiable des deux champ
par champ — c'est documenté à chaque endroit concerné.

Station sans données (cas LFCY)
-------------------------------
Un terrain sans station d'observation n'est PAS une erreur. Vérifié en réel sur
LFCY (Royan-Médis, 45.628101/-0.9725) le 2026-07-20 :

  * `metar?ids=LFCY` → **HTTP 204 No Content**, corps vide (pas un 200 avec `[]`)
  * `taf?ids=LFCY`   → **HTTP 204 No Content**, corps vide

et dans une requête groupée `ids=LFCY,LFBD`, l'AWC renvoie 200 avec le seul
LFBD : la station inconnue est silencieusement omise du tableau, sans marqueur.

Ce provider traite donc 204 et l'omission comme « pas de donnée » et rend une
liste vide, sans lever et surtout sans inventer. Seuls un échec réseau ou un
statut HTTP réellement anormal lèvent `ProviderError` (cf. règle cardinale de
`providers/base`).

Stations de report utilisables autour de LFCY
---------------------------------------------
LFCY n'a ni METAR ni TAF. L'agrégateur doit se rabattre sur les stations
voisines ; par distance croissante depuis 45.628101/-0.9725 (METAR + TAF
disponibles et vérifiés en réel pour les trois premières) :

  * LFBH  La Rochelle–Île de Ré      ~38 NM au nord       (METAR + TAF)
  * LFBD  Bordeaux–Mérignac          ~48 NM au sud-est    (METAR + TAF)
  * LFXA  Angoulême–Cognac           ~55 NM à l'est       (METAR)
  * LFBZ  Biarritz–Pays basque      ~140 NM au sud        (METAR + TAF)

À cette distance aucune ne décrit finement la météo locale de Royan : la côte
charentaise a ses propres brises de mer et son régime de brume matinale. LFBH et
LFBD encadrent le terrain et servent surtout à borner la tendance ; c'est la
raison pour laquelle le briefing doit afficher la station effectivement utilisée
et sa distance, jamais présenter un METAR voisin comme celui du terrain.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, timedelta
from typing import Any

import httpx

from ..domain.context import BriefingContext
from ..domain.models import Metar, Taf, TafPeriod
from ..domain.sourced import Provenance, Sourced
from ..domain.window import TimeWindow, UtcDateTime
from . import cache
from .base import ProviderError

try:  # avwx est un confort de décodage, jamais une dépendance dure du flux
    import avwx
except ImportError:  # pragma: no cover - chemin de dégradation
    avwx = None  # type: ignore[assignment]

SOURCE = "noaa-awc"
BASE_URL = "https://aviationweather.gov/api/data"
USER_AGENT = "aerobriefer/0.1 (+https://github.com/aerobriefer) contact: briefing VFR"
TIMEOUT_S = 10.0

_METRES_PER_STATUTE_MILE = 1609.344

# Origine du temps epoch, construite sans passer par un `datetime` nu : le lint
# TID251 bannit `datetime.datetime` hors du module qui porte l'invariant.
_EPOCH = UtcDateTime(1970, 1, 1, tzinfo=UTC)

# Couches qui constituent un plafond au sens aéronautique (BKN et au-delà).
_CEILING_COVERS = frozenset({"BKN", "OVC", "OVX", "VV"})

# Repli de validité quand ni la NOAA ni avwx ne donnent les bornes d'un TAF.
# `Taf.validity` est obligatoire dans le domaine : on ne peut pas rendre le champ
# absent. Une fenêtre de largeur nulle serait pire que tout — elle ne chevaucherait
# aucune fenêtre de vol et ferait disparaître le TAF SILENCIEUSEMENT au filtrage.
# On retient donc large (durée maximale d'un TAF long), conformément à la
# politique du domaine : mieux vaut une donnée de trop qu'une donnée manquante.
_TAF_FALLBACK_VALIDITY = timedelta(hours=30)


def _epoch_to_utc(value: Any) -> UtcDateTime | None:
    """Epoch secondes → UtcDateTime. Tolère `None`, chaînes et valeurs aberrantes."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return _EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_to_utc(value: Any) -> UtcDateTime | None:
    """ISO 8601 (suffixe Z inclus) → UtcDateTime, sans jamais lever."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UtcDateTime.parse(value)
    except (ValueError, TypeError):
        return None


def _as_int(value: Any) -> int | None:
    """Entier tolérant : rejette les non-numériques comme "VRB" sans lever."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avwx_value(node: Any) -> Any:
    """Déballe un `avwx.structs.Number` (ou assimilé) vers sa valeur scalaire.

    Renvoie `None` pour "VRB" : avwx pose alors `value=None` en gardant
    `repr='VRB'`, ce qui correspond exactement à la convention du domaine
    (`wind_dir_deg=None` signifie direction variable).
    """
    if node is None:
        return None
    return getattr(node, "value", None)


def _visibility_m(node: Any, unit: str | None) -> int | None:
    """Visibilité en mètres depuis avwx, en tenant compte de l'unité du message.

    Un METAR européen est en mètres (9999, 2000), un METAR américain en milles
    terrestres (10SM) : convertir à l'aveugle donnerait un facteur 1600.
    """
    raw = _as_float(_avwx_value(node))
    if raw is None:
        return None
    if unit == "sm":
        return int(round(raw * _METRES_PER_STATUTE_MILE))
    return int(round(raw))


def _visibility_from_noaa(value: Any) -> int | None:
    """Champ `visib` de la NOAA, en milles terrestres — parfois la CHAÎNE "6+".

    Le "+" signifie « au moins » : on retient la borne basse, qui est la lecture
    prudente pour un minimum VFR.
    """
    if isinstance(value, str):
        value = value.strip().rstrip("+").strip()
    miles = _as_float(value)
    if miles is None:
        return None
    return int(round(miles * _METRES_PER_STATUTE_MILE))


def _ceiling_from_avwx(clouds: Iterable[Any] | None) -> int | None:
    """Plafond en pieds : base de la première couche BKN/OVC/VV.

    avwx exprime les bases en centaines de pieds (BKN008 → base=8) : d'où le ×100.
    """
    bases: list[int] = []
    for layer in clouds or ():
        cover = (getattr(layer, "type", None) or "").upper()
        if cover not in _CEILING_COVERS:
            continue
        base = _as_int(getattr(layer, "base", None))
        if base is not None:
            bases.append(base * 100)
    return min(bases) if bases else None


_FLIGHT_CATEGORIES = frozenset({"VFR", "MVFR", "IFR", "LIFR"})


def _flight_category(noaa_value: Any, avwx_data: Any) -> str | None:
    """Catégorie de vol : NOAA d'abord, avwx en repli, sinon None.

    On ne recalcule pas : NOAA et avwx appliquent tous deux les seuils
    officiels (plafond, visibilité). Un recalcul maison ne ferait qu'ajouter une
    source de divergence.
    """
    candidate = str(noaa_value or "").upper().strip()
    if candidate in _FLIGHT_CATEGORIES:
        return candidate
    rules = str(getattr(avwx_data, "flight_rules", "") or "").upper().strip()
    return rules if rules in _FLIGHT_CATEGORIES else None


def _ceiling_from_noaa(clouds: Any) -> int | None:
    """Repli sur le tableau `clouds` du JSON, dont les bases sont déjà en pieds."""
    if not isinstance(clouds, list):
        return None
    bases: list[int] = []
    for layer in clouds:
        if not isinstance(layer, dict):
            continue
        cover = str(layer.get("cover") or "").upper()
        if cover not in _CEILING_COVERS:
            continue
        base = _as_int(layer.get("base"))
        if base is not None:
            bases.append(base)
    return min(bases) if bases else None


def _decode_metar(raw_text: str) -> Any:
    """Parse le brut avec avwx. Rend `None` à la moindre difficulté.

    Volontairement tolérant à TOUT : c'est le point où un parseur cassé pourrait
    faire disparaître un METAR, donc rien ne remonte d'ici. avwx a deux modes
    d'échec observés — une exception sur brut vide, et un parse « réussi » sur du
    texte non-METAR dont tous les champs sont None. Les deux se traitent pareil :
    pas de décodé, le brut suffit.

    On passe par `from_report`, qui déduit la station du message lui-même :
    instancier `avwx.Metar(code)` impose un code OACI connu de sa base et
    échouerait sur les terrains qu'elle ne référence pas.
    """
    if avwx is None or not raw_text.strip():
        return None
    try:
        return avwx.Metar.from_report(raw_text)
    except Exception:  # noqa: BLE001 - le décodage ne doit JAMAIS faire perdre le brut
        return None


def _decode_taf(raw_text: str) -> Any:
    """Pendant TAF de `_decode_metar`, avec la même garantie d'innocuité."""
    if avwx is None or not raw_text.strip():
        return None
    try:
        return avwx.Taf.from_report(raw_text)
    except Exception:  # noqa: BLE001 - idem : le brut prime sur le décodage
        return None


def _avwx_time(report: Any, attribute: str) -> UtcDateTime | None:
    """Extrait un horodatage d'avwx (déjà timezone-aware) sans jamais lever."""
    try:
        data = getattr(report, "data", None)
        node = getattr(data, attribute, None)
        moment = getattr(node, "dt", None)
        return UtcDateTime.optional(moment)
    except (AttributeError, TypeError, ValueError):
        return None


def _fetch(endpoint: str, stations: Sequence[str]) -> tuple[list[dict[str, Any]], str]:
    """Appelle l'AWC et rend (enregistrements, url). Lève `ProviderError` si la
    collecte a échoué — jamais de liste vide pour masquer une panne.

    Distinction essentielle : un 204 signifie « aucune station demandée n'a de
    données » (cas LFCY), ce qui est une réponse valide et non une panne.
    """
    url = f"{BASE_URL}/{endpoint}"
    params = {"ids": ",".join(stations), "format": "json"}
    try:
        with cache.make_client(timeout=TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ProviderError(SOURCE, f"échec réseau sur {endpoint} : {exc}") from exc

    effective_url = str(response.url)

    # Station connue mais sans données : réponse légitimement vide.
    if response.status_code == 204 or not response.content.strip():
        return [], effective_url

    if response.status_code != 200:
        raise ProviderError(
            SOURCE, f"statut HTTP {response.status_code} sur {endpoint} ({effective_url})"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(SOURCE, f"réponse illisible sur {endpoint} : {exc}") from exc

    if not isinstance(payload, list):
        raise ProviderError(
            SOURCE,
            f"format inattendu sur {endpoint} : liste attendue, reçu {type(payload).__name__}",
        )

    return [item for item in payload if isinstance(item, dict)], effective_url


class NoaaMetarProvider:
    """METAR observés. Critique : sans observation, pas de décision de départ."""

    name = SOURCE
    category = "metar"
    is_critical = True

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[Metar]]:
        stations = context.stations_of_interest
        if not stations:
            return []

        records, url = _fetch("metar", stations)
        retrieved_at = UtcDateTime.now()
        return [self._to_sourced(record, retrieved_at, url) for record in records]

    def _to_sourced(
        self, record: dict[str, Any], retrieved_at: UtcDateTime, url: str
    ) -> Sourced[Metar]:
        raw_text = str(record.get("rawOb") or "").strip()
        station = str(record.get("icaoId") or "").upper()

        report = _decode_metar(raw_text)
        data = getattr(report, "data", None)
        units = getattr(report, "units", None)

        # Horodatage : l'epoch `obsTime` de la NOAA prime sur avwx, qui doit
        # deviner le mois à partir du seul quantième du groupe `201130Z` et se
        # trompe donc en début de mois.
        observed_at = (
            _epoch_to_utc(record.get("obsTime"))
            or _iso_to_utc(record.get("reportTime"))
            or _avwx_time(report, "time")
            # Dernier recours pour un enregistrement sans aucun horodatage
            # exploitable. `Metar.observed_at` est obligatoire : on ne peut pas
            # rendre le champ absent sans perdre la donnée. `issued_at` reste à
            # None dans la provenance ci-dessous, pour ne pas faire passer cette
            # heure fabriquée pour une heure d'émission.
            or retrieved_at
        )
        has_real_time = (
            _epoch_to_utc(record.get("obsTime")) is not None
            or _iso_to_utc(record.get("reportTime")) is not None
        )

        # `wdir` vaut "VRB" quand le vent est variable : `_as_int` rend None,
        # ce qui est exactement la convention du domaine.
        wind_dir = _as_int(record.get("wdir"))
        if wind_dir is None:
            wind_dir = _as_int(_avwx_value(getattr(data, "wind_direction", None)))

        wind_speed = _as_int(record.get("wspd"))
        if wind_speed is None:
            wind_speed = _as_int(_avwx_value(getattr(data, "wind_speed", None)))

        wind_gust = _as_int(record.get("wgst"))
        if wind_gust is None:
            wind_gust = _as_int(_avwx_value(getattr(data, "wind_gust", None)))

        # Visibilité : avwx d'abord, parce qu'il rend l'unité native du message
        # (mètres en Europe) là où la NOAA plafonne à "6+" milles terrestres.
        visibility = _visibility_m(
            getattr(data, "visibility", None), getattr(units, "visibility", None)
        )
        if visibility is None:
            visibility = _visibility_from_noaa(record.get("visib"))

        temperature = _as_float(record.get("temp"))
        if temperature is None:
            temperature = _as_float(_avwx_value(getattr(data, "temperature", None)))

        dewpoint = _as_float(record.get("dewp"))
        if dewpoint is None:
            dewpoint = _as_float(_avwx_value(getattr(data, "dewpoint", None)))

        # `altim` est déjà en hPa côté NOAA ; avwx peut rendre des pouces de
        # mercure sur un message américain, d'où le contrôle d'unité.
        qnh = _as_float(record.get("altim"))
        if qnh is None and getattr(units, "altimeter", None) == "hPa":
            qnh = _as_float(_avwx_value(getattr(data, "altimeter", None)))

        ceiling = _ceiling_from_avwx(getattr(data, "clouds", None))
        if ceiling is None:
            ceiling = _ceiling_from_noaa(record.get("clouds"))

        conditions = tuple(
            str(code.value)
            for code in (getattr(data, "wx_codes", None) or ())
            if getattr(code, "value", None)
        )

        # Catégorie de vol : la NOAA la calcule (`fltCat`), on la préfère à un
        # recalcul maison. Repli sur avwx (`flight_rules`) si absente. Jamais
        # inventée : None si aucune des deux ne la donne.
        flight_category = _flight_category(record.get("fltCat"), data)

        metar = Metar(
            station=station,
            raw_text=raw_text,
            observed_at=observed_at,
            wind_dir_deg=wind_dir,
            wind_speed_kt=wind_speed,
            wind_gust_kt=wind_gust,
            visibility_m=visibility,
            temperature_c=temperature,
            dewpoint_c=dewpoint,
            qnh_hpa=qnh,
            ceiling_ft=ceiling,
            conditions=conditions,
            flight_category=flight_category,
        )
        return Sourced(
            value=metar,
            provenance=Provenance(
                source=SOURCE,
                retrieved_at=retrieved_at,
                issued_at=observed_at if has_real_time else None,
                url=url,
            ),
        )


class NoaaTafProvider:
    """TAF prévus. Non critique : un dossier reste exploitable sans prévision
    de terrain, en s'appuyant sur les modèles et les cartes."""

    name = SOURCE
    category = "taf"
    is_critical = False

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[Taf]]:
        stations = context.stations_of_interest
        if not stations:
            return []

        records, url = _fetch("taf", stations)
        retrieved_at = UtcDateTime.now()
        return [self._to_sourced(record, retrieved_at, url) for record in records]

    def _to_sourced(
        self, record: dict[str, Any], retrieved_at: UtcDateTime, url: str
    ) -> Sourced[Taf]:
        raw_text = str(record.get("rawTAF") or "").strip()
        station = str(record.get("icaoId") or "").upper()

        report = _decode_taf(raw_text)

        issued_from_source = _iso_to_utc(record.get("issueTime")) or _iso_to_utc(
            record.get("bulletinTime")
        )
        issued_at = issued_from_source or _avwx_time(report, "time") or retrieved_at

        # Bornes de validité : les epochs de la NOAA priment, avwx sert de repli.
        start = _epoch_to_utc(record.get("validTimeFrom")) or _avwx_time(report, "start_time")
        end = _epoch_to_utc(record.get("validTimeTo")) or _avwx_time(report, "end_time")

        if start is None or end is None or end < start:
            # Ni la NOAA ni avwx n'ont donné de bornes exploitables. Voir la note
            # sur `_TAF_FALLBACK_VALIDITY` : on retient large plutôt que de
            # laisser le TAF disparaître au filtrage temporel.
            start = issued_at
            end = issued_at + _TAF_FALLBACK_VALIDITY

        validity = TimeWindow(start=start, end=end)
        taf = Taf(
            station=station,
            raw_text=raw_text,
            issued_at=issued_at,
            validity=validity,
            periods=_taf_periods(report, validity),
        )
        return Sourced(
            value=taf,
            provenance=Provenance(
                source=SOURCE,
                retrieved_at=retrieved_at,
                issued_at=issued_from_source,
                url=url,
            ),
        )


_CHANGE_LABELS = {"FROM": "FM", "BECMG": "BECMG", "TEMPO": "TEMPO", "INTER": "TEMPO"}


def _taf_periods(report: Any, enclosing: TimeWindow) -> tuple[TafPeriod, ...]:
    """Groupes d'évolution décodés depuis avwx.

    Le décodage reste un confort : à la moindre anomalie on rend un tuple vide,
    et le texte brut — toujours conservé — redevient seul à faire foi.

    Un groupe dont avwx ne résout pas les bornes (cas observé sur certains
    PROB30) est BORNÉ SUR LE TAF ENGLOBANT plutôt qu'écarté : perdre un groupe
    de probabilité en silence serait pire qu'une borne approchée, et le brut du
    groupe reste affiché à côté.
    """
    if report is None:
        return ()
    forecast = getattr(getattr(report, "data", None), "forecast", None)
    if not forecast:
        return ()

    periods: list[TafPeriod] = []
    for group in forecast:
        try:
            start = _period_bound(group, "start_time", enclosing.start)
            end = _period_bound(group, "end_time", enclosing.end)
            if end < start:
                start, end = enclosing.start, enclosing.end

            change = _CHANGE_LABELS.get(str(getattr(group, "type", "") or "").upper(), "FM")
            probability = _as_int(_avwx_value(getattr(group, "probability", None)))
            if probability is not None:
                change = f"PROB{probability}"

            periods.append(
                TafPeriod(
                    validity=TimeWindow(start=start, end=end),
                    change_type=change,
                    probability=probability,
                    wind_dir_deg=_as_int(_avwx_value(getattr(group, "wind_direction", None))),
                    wind_speed_kt=_as_int(_avwx_value(getattr(group, "wind_speed", None))),
                    wind_gust_kt=_as_int(_avwx_value(getattr(group, "wind_gust", None))),
                    visibility_m=_visibility_m(getattr(group, "visibility", None), report),
                    ceiling_ft=_ceiling_from_avwx(getattr(group, "clouds", None)),
                    clouds=tuple(c.repr for c in (getattr(group, "clouds", None) or [])),
                    conditions=tuple(
                        w.value for w in (getattr(group, "wx_codes", None) or []) if w.value
                    ),
                    raw_text=str(getattr(group, "sanitized", "") or "").strip(),
                )
            )
        except Exception:  # noqa: BLE001 - un groupe illisible ne doit pas tuer les autres
            continue
    return tuple(periods)


def _period_bound(group: Any, attribute: str, fallback: UtcDateTime) -> UtcDateTime:
    node = getattr(group, attribute, None)
    moment = getattr(node, "dt", None) if node is not None else None
    return UtcDateTime.of(moment) if moment is not None else fallback
