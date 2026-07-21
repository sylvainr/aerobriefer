"""SIGMET internationaux depuis l'Aviation Weather Center (aviationweather.gov).

Même source que les METAR/TAF NOAA : gratuite, sans authentification, couverture
mondiale. Un seul endpoint, vérifié en réel le 2026-07-20 :

    GET https://aviationweather.gov/api/data/isigmet?format=json

Il rend la liste des SIGMET internationaux en cours. Structure d'un élément
(champs réellement observés) :

    {"icaoId":"UBBB","firId":"UBBB","firName":"UBBA BAKU",
     "receiptTime":"2026-07-19T20:20:40.968Z",
     "validTimeFrom":1784491200,"validTimeTo":1784592000,
     "seriesId":"2","hazard":"TS","qualifier":"EMBD",
     "base":null,"top":34000,"geom":"AREA",
     "coords":[{"lon":46.4,"lat":41.57}, ...],"rawSigmet":"..."}

Politique de conservation
-------------------------
Comme partout dans aerobriefer, le texte brut (`rawSigmet`) fait foi et est
TOUJOURS conservé dans `raw_text`. Les champs décodés (zone, altitudes, aléa)
sont un confort greffé par-dessus. Un SIGMET orage manquant est bien plus grave
qu'un SIGMET de trop : le filtrage géographique de `Sigmet.concerns` conserve
d'ailleurs tout SIGMET dont on ne connaît pas le polygone.

Réponse vide
------------
Par beau temps anticyclonique, aucun SIGMET ne touche la zone : `fetch` rend
alors une liste vide, ce qui est un résultat parfaitement valide et non une
panne. De même, un 204 ou un corps vide signifient « aucun SIGMET actif » — pas
une erreur. Seuls un échec réseau ou un statut HTTP réellement anormal lèvent
`ProviderError` (règle cardinale de `providers/base`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, timedelta
from typing import Any

import httpx

from ..domain.context import BriefingContext
from ..domain.geo import Position
from ..domain.models import Sigmet
from ..domain.sourced import Provenance, Sourced
from ..domain.window import TimeWindow, UtcDateTime
from . import cache
from .base import ProviderError

SOURCE = "noaa-awc"
BASE_URL = "https://aviationweather.gov/api/data"
USER_AGENT = "aerobriefer/0.1 (+https://github.com/aerobriefer) contact: briefing VFR"
TIMEOUT_S = 10.0

# Origine du temps epoch, construite sans passer par un `datetime` nu : le lint
# TID251 bannit `datetime.datetime` hors du module qui porte l'invariant.
_EPOCH = UtcDateTime(1970, 1, 1, tzinfo=UTC)

# Repli de validité quand la source ne donne pas de bornes exploitables.
# `Sigmet.validity` est obligatoire dans le domaine : on ne peut pas rendre le
# champ absent. Une fenêtre de largeur nulle serait pire — elle ne chevaucherait
# aucune fenêtre de vol et ferait disparaître le SIGMET SILENCIEUSEMENT au
# filtrage temporel. On retient donc large, conformément à la politique du
# domaine : mieux vaut une donnée de trop qu'une donnée manquante.
_VALIDITY_FALLBACK = timedelta(hours=12)


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
    """Entier tolérant : rejette les non-numériques (`None`, "SFC"...) sans lever."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    """Chaîne nettoyée, ou `None` si vide — pour hazard/qualifier/fir facultatifs."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fetch() -> tuple[list[dict[str, Any]], str]:
    """Appelle l'AWC et rend (enregistrements, url). Lève `ProviderError` si la
    collecte a échoué — jamais de liste vide pour masquer une panne.

    Un 204 ou un corps vide signifient « aucun SIGMET actif » : réponse valide,
    pas une panne (même distinction que les METAR NOAA pour les stations sans
    données).
    """
    url = f"{BASE_URL}/isigmet"
    params = {"format": "json"}
    try:
        with cache.make_client(timeout=TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ProviderError(SOURCE, f"échec réseau sur isigmet : {exc}") from exc

    effective_url = str(response.url)

    # Aucun SIGMET actif : réponse légitimement vide.
    if response.status_code == 204 or not response.content.strip():
        return [], effective_url

    if response.status_code != 200:
        raise ProviderError(
            SOURCE, f"statut HTTP {response.status_code} sur isigmet ({effective_url})"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(SOURCE, f"réponse illisible sur isigmet : {exc}") from exc

    if not isinstance(payload, list):
        raise ProviderError(
            SOURCE,
            f"format inattendu sur isigmet : liste attendue, reçu {type(payload).__name__}",
        )

    return [item for item in payload if isinstance(item, dict)], effective_url


def _polygon(record: dict[str, Any]) -> tuple[Position, ...]:
    """Mappe les `coords` [{lon, lat}] de la source vers des `Position(lat, lon)`.

    ATTENTION à l'ordre : la source donne lon puis lat, le domaine attend lat
    puis lon. Un sommet aberrant est ignoré plutôt que de faire tomber tout le
    SIGMET — sans polygone exploitable, `Sigmet.concerns` conserve de toute façon.
    """
    coords = record.get("coords")
    if not isinstance(coords, list):
        return ()
    vertices: list[Position] = []
    for point in coords:
        if not isinstance(point, dict):
            continue
        lat = point.get("lat")
        lon = point.get("lon")
        if lat is None or lon is None:
            continue
        try:
            vertices.append(Position(float(lat), float(lon)))
        except (TypeError, ValueError):
            continue
    return tuple(vertices)


def _identifier(record: dict[str, Any]) -> str:
    """Identifiant lisible d'un SIGMET : FIR + aléa + numéro de série.

    La source n'expose pas d'identifiant unique dédié ; on en compose un stable à
    partir des champs disponibles (`firId`/`icaoId`, `hazard`, `seriesId`).
    """
    fir = _clean(record.get("firId")) or _clean(record.get("icaoId")) or "?"
    parts = [fir]
    hazard = _clean(record.get("hazard"))
    if hazard:
        parts.append(hazard)
    series = _clean(record.get("seriesId"))
    if series:
        parts.append(series)
    return " ".join(parts)


def _validity(record: dict[str, Any], retrieved_at: UtcDateTime) -> TimeWindow:
    """Fenêtre de validité depuis les epochs de la source, avec repli large.

    Sans bornes exploitables (ou incohérentes), on retient large plutôt que de
    laisser le SIGMET disparaître au filtrage temporel — cf. `_VALIDITY_FALLBACK`.
    """
    start = _epoch_to_utc(record.get("validTimeFrom"))
    end = _epoch_to_utc(record.get("validTimeTo"))
    if start is None or end is None or end < start:
        start = start or retrieved_at
        end = start + _VALIDITY_FALLBACK
    return TimeWindow(start=start, end=end)


class SigmetProvider:
    """SIGMET internationaux (orage, turbulence, givrage, onde de relief, cendres).

    Non critique : un dossier reste exploitable sans, en s'appuyant sur les
    cartes TEMSI et le jugement du pilote. Mais toujours collecté — un phénomène
    dangereux en route qu'on aurait tu serait le pire des oublis.
    """

    name = SOURCE
    category = "sigmet"
    is_critical = False

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[Sigmet]]:
        records, url = _fetch()
        retrieved_at = UtcDateTime.now()

        results: list[Sourced[Sigmet]] = []
        for record in records:
            sigmet = self._to_sigmet(record, retrieved_at)
            # L'endpoint renvoie parfois des enregistrements vides (`{}` observé
            # en réel). Sans texte brut NI polygone, il n'y a ni phénomène à
            # décrire ni zone à situer : ce n'est pas un SIGMET, on l'écarte. La
            # règle « conserver sans géométrie » vaut pour un vrai SIGMET dont on
            # ignore la zone, pas pour une coquille vide — la garder ferait
            # apparaître un aléa fantôme dans le dossier.
            if not sigmet.raw_text and not sigmet.polygon:
                continue
            # Filtrage géographique ET temporel : on ne garde que ce qui touche
            # la zone du briefing pendant la fenêtre de vol. Un SIGMET sans
            # polygone connu est conservé (cf. `Sigmet.concerns`).
            if not sigmet.concerns(context.geometry, context.window):
                continue
            results.append(self._as_sourced(record, sigmet, retrieved_at, url))
        return results

    def _to_sigmet(self, record: dict[str, Any], retrieved_at: UtcDateTime) -> Sigmet:
        # Le brut fait foi et est toujours conservé ; `rawAirSigmet` est un nom de
        # repli observé sur certaines variantes de l'endpoint.
        raw_text = str(record.get("rawSigmet") or record.get("rawAirSigmet") or "").strip()

        return Sigmet(
            identifier=_identifier(record),
            hazard=_clean(record.get("hazard")) or "",
            raw_text=raw_text,
            validity=_validity(record, retrieved_at),
            polygon=_polygon(record),
            fir=_clean(record.get("firId")) or _clean(record.get("icaoId")),
            lower_ft=_as_int(record.get("base")),
            upper_ft=_as_int(record.get("top")),
            qualifier=_clean(record.get("qualifier")),
        )

    def _as_sourced(
        self,
        record: dict[str, Any],
        sigmet: Sigmet,
        retrieved_at: UtcDateTime,
        url: str,
    ) -> Sourced[Sigmet]:
        # Heure d'émission depuis la source, quand elle est connue : jamais notre
        # horloge, qui n'est que l'heure de récupération.
        issued_at = _iso_to_utc(record.get("creationTime")) or _iso_to_utc(
            record.get("receiptTime")
        )
        return Sourced(
            value=sigmet,
            provenance=Provenance(
                source=SOURCE,
                retrieved_at=retrieved_at,
                issued_at=issued_at,
                url=url,
            ),
        )
