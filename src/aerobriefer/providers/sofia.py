"""Provider NOTAM SOFIA-Briefing (SIA / DGAC).

Source publique française des NOTAM, sous Licence Ouverte Etalab v2.0. Pas de
compte requis, mais une session anonyme (JSESSIONID) obligatoire.

Le contrat HTTP est un dispatcher RPC form-encoded (Adobe AEM) : un endpoint
unique `/sofia`, l'opération étant portée par le champ `:operation`. La réponse
n'est PAS du JSON : c'est une page HTML dont une `<div id="Message">` contient le
JSON, HTML-échappé. Certaines réponses d'erreur ajoutent un second niveau
d'encodage (`status.message` est une chaîne contenant du JSON).

C'est la source la plus fragile du projet — scraping d'un rendu HTML, double
encodage, arborescence métier profonde. D'où l'usage insistant de
`sanity_check()` : au moindre écart de contrat on lève, plutôt que de rendre un
briefing amputé. Un « 0 NOTAM » silencieux est le pire résultat possible.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from ..domain.context import BriefingContext, Purpose
from ..domain.geo import Position
from ..domain.models import Notam, Severity
from ..domain.sourced import Provenance, Sourced
from ..domain.window import TimeWindow, UtcDateTime, utcnow
from . import cache
from .base import ProviderError, sanity_check

SOURCE = "sofia"

BASE_URL = "https://sofia-briefing.aviation-civile.gouv.fr"
ENDPOINT = f"{BASE_URL}/sofia"
SESSION_URL = f"{BASE_URL}/sofia/pages/notamform.html"

USER_AGENT = "aerobriefer/0.1 (+https://github.com/aerobriefer; briefing VFR non commercial)"
DEFAULT_TIMEOUT = 20.0

#: Au-delà, SOFIA refuse la demande — et un briefing VFR n'a pas de sens si loin.
MAX_DURATION_HOURS = 96

_MESSAGE_RE = re.compile(r'<div id="Message">(.*?)</div>', re.S)

#: Fins de validité ouvertes. `PERM` (item C = PERM) et `UFN` (until further
#: notice) signifient « sans échéance connue ».
_OPEN_ENDED = frozenset({"PERM", "UFN"})

#: Le domaine exige une `TimeWindow` bornée aux deux bouts. On donne aux NOTAM
#: sans fin une borne conventionnelle très lointaine : `overlaps()` se comporte
#: alors exactement comme s'il n'y avait pas de fin, sans introduire de `None`
#: qui contaminerait tous les comparatifs en aval. Sur la capture LFCY, 5 des
#: 21 NOTAM sont dans ce cas — ce n'est pas un cas marginal.
PERMANENT_END = "2099-12-31T23:59:00Z"

# ---------------------------------------------------------------------------
# Coordonnées : "4845N00207E" / "453600N 0010907W"
# ---------------------------------------------------------------------------

#: DDMM[SS]{N|S} puis DDDMM[SS]{E|W}. Les secondes sont facultatives.
_COORD_RE = re.compile(
    r"^(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_s>\d{2})?(?P<lat_h>[NS])"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_s>\d{2})?(?P<lon_h>[EW])$"
)


def parse_coordinates(text: str) -> Position:
    """Décode le champ `coordinates` de SOFIA en `Position`.

    Le piège est l'hémisphère : LFCY (Royan) est à 000°59'W, soit une longitude
    NÉGATIVE de -0.98. Traiter W comme positif place le terrain en Allemagne.
    """
    match = _COORD_RE.match(text.strip())
    if match is None:
        raise ValueError(f"coordonnées SOFIA non reconnues : {text!r}")

    parts = match.groupdict()
    lat = _to_degrees(parts["lat_d"], parts["lat_m"], parts["lat_s"])
    lon = _to_degrees(parts["lon_d"], parts["lon_m"], parts["lon_s"])

    if parts["lat_h"] == "S":
        lat = -lat
    if parts["lon_h"] == "W":
        lon = -lon

    return Position(lat, lon)


def _to_degrees(degrees: str, minutes: str, seconds: str | None) -> float:
    mins = int(minutes)
    secs = int(seconds) if seconds else 0
    if mins >= 60 or secs >= 60:
        raise ValueError(f"minutes/secondes hors bornes : {degrees}{minutes}{seconds or ''}")
    return int(degrees) + mins / 60.0 + secs / 3600.0


def format_latitude(degrees: float) -> str:
    """Inverse de `parse_coordinates` pour la latitude : "4538N"."""
    return _format_angle(degrees, width=2, positive="N", negative="S")


def format_longitude(degrees: float) -> str:
    """Inverse de `parse_coordinates` pour la longitude : "00059W"."""
    return _format_angle(degrees, width=3, positive="E", negative="W")


def _format_angle(degrees: float, *, width: int, positive: str, negative: str) -> str:
    hemisphere = positive if degrees >= 0 else negative
    total_minutes = round(abs(degrees) * 60)
    deg, minutes = divmod(total_minutes, 60)
    return f"{deg:0{width}d}{minutes:02d}{hemisphere}"


# ---------------------------------------------------------------------------
# Q-codes OACI → Severity
# ---------------------------------------------------------------------------
#
# Le champ Q d'un NOTAM porte deux paires de lettres :
#   code23 = le SUJET (ce dont on parle : piste, balisage, zone, obstacle...)
#   code45 = l'ÉTAT  (ce qui lui arrive : fermé, hors service, limité...)
#
# La sévérité est majoritairement portée par l'ÉTAT, puis modulée par le SUJET :
# « fermé » n'a pas la même portée pour une piste que pour un feu de taxiway.
#
# Principe directeur : ne JAMAIS deviner. Tout code absent des tables retombe sur
# Severity.UNKNOWN, qui remonte en tête du briefing — c'est voulu : un NOTAM non
# classé doit être lu par le pilote, pas enterré en bas de page.

#: Aire de manœuvre critique : piste, seuil, prolongement, distances déclarées.
_SUBJECT_RUNWAY = frozenset({"MR", "MS", "MT", "MU", "MW", "MD"})

#: Reste de l'aire de mouvement : taxiways, aires de trafic, parkings.
_SUBJECT_MOVEMENT_AREA = frozenset(
    {"MA", "MB", "MC", "MG", "MH", "MK", "MM", "MN", "MO", "MP", "MX"}
)

#: Zones réglementées / interdites / dangereuses.
_SUBJECT_RESTRICTED = frozenset({"RA", "RD", "RM", "RO", "RP", "RR", "RT"})

#: Obstacles et leur balisage.
_SUBJECT_OBSTACLE = frozenset({"OA", "OB", "OL"})

#: Le terrain lui-même (FA = aerodrome).
_SUBJECT_AERODROME = frozenset({"FA"})

#: ÉTAT (code45) → sévérité de base, avant modulation par le sujet.
_STATE_SEVERITY: dict[str, Severity] = {
    # --- empêche de voler ---
    "LC": Severity.BLOCKING,  # closed
    "LP": Severity.BLOCKING,  # prohibited
    # --- contraint fortement ---
    "AS": Severity.MAJOR,  # unserviceable
    "AU": Severity.MAJOR,  # not available
    "CA": Severity.MAJOR,  # activated
    "CE": Severity.MAJOR,  # erected (obstacle nouvellement dressé)
    # --- contraint modérément ---
    "LT": Severity.MINOR,  # limited
    "LR": Severity.MINOR,  # reserved for
    "LS": Severity.MINOR,  # subject to interruption
    "LW": Severity.MINOR,  # will take place (activité annoncée : para, treuil, drone)
    "AH": Severity.MINOR,  # hours of service changed (service réduit)
    # --- informatif ---
    "CC": Severity.INFO,  # completed
    "CF": Severity.INFO,  # frequency changed
    "CH": Severity.INFO,  # changed
    "CM": Severity.INFO,  # displaced / moved
    "CS": Severity.INFO,  # installed
    "AK": Severity.INFO,  # resumed normal operation
    "AO": Severity.INFO,  # operational
    "AP": Severity.INFO,  # available on prior request
    # TT = TRIGGER NOTAM : annonce un amendement AIP/SUP AIP à venir. L'effet
    # opérationnel réel est porté par l'AIP ou par un NOTAM d'activation
    # distinct, pas par celui-ci. Vérifié sur capture réelle : tous les QRTTT
    # observés disent « ACTIVATION ANNONCEE PAR NOTAM ».
    "TT": Severity.INFO,
    # XX = plain language. Par construction non classable : le sens est dans le
    # texte libre. On refuse de deviner.
    "XX": Severity.UNKNOWN,
}

#: Paires (sujet, état) dont la sévérité générique serait trompeuse.
_PAIR_OVERRIDES: dict[tuple[str, str], Severity] = {
    # Une zone réglementée ACTIVE est bloquante, pas seulement « majeure ».
    **{(subject, "CA"): Severity.BLOCKING for subject in _SUBJECT_RESTRICTED},
    # Un obstacle « changé » ou « déplacé » reste un obstacle à connaître.
    ("OB", "CH"): Severity.MAJOR,
    ("OB", "CM"): Severity.MAJOR,
}


def severity_for(code23: str | None, code45: str | None) -> Severity:
    """Classe un NOTAM à partir des lettres 2-3 et 4-5 du champ Q.

    Renvoie `Severity.UNKNOWN` dès que le couple n'est pas explicitement connu.
    """
    if not code23 or not code45:
        return Severity.UNKNOWN

    subject = code23.strip().upper()
    state = code45.strip().upper()
    if len(subject) != 2 or len(state) != 2 or not subject.isalpha() or not state.isalpha():
        return Severity.UNKNOWN

    override = _PAIR_OVERRIDES.get((subject, state))
    if override is not None:
        return override

    base = _STATE_SEVERITY.get(state)
    if base is None:
        return Severity.UNKNOWN

    return _modulate(subject, base)


def _modulate(subject: str, base: Severity) -> Severity:
    """Ajuste la sévérité de l'état selon le sujet concerné."""
    # Balisage (L*) : « balisage partiel » est explicitement MINOR au domaine. Un
    # balisage hors service ne cloue pas un VFR de jour.
    if subject.startswith("L") and subject not in _SUBJECT_RUNWAY:
        return min(base, Severity.MINOR, key=lambda s: s.value)

    # Piste / terrain fermés : bloquant, sans discussion.
    if subject in _SUBJECT_RUNWAY or subject in _SUBJECT_AERODROME:
        return base

    # Obstacle : « obstacle significatif » est MAJOR au domaine.
    if subject in _SUBJECT_OBSTACLE and base >= Severity.MINOR:
        return max(base, Severity.MAJOR, key=lambda s: s.value)

    # Taxiways et aires de trafic : gênant, rarement bloquant pour un VFR.
    if subject in _SUBJECT_MOVEMENT_AREA:
        return min(base, Severity.MAJOR, key=lambda s: s.value)

    return base


# ---------------------------------------------------------------------------
# Décodage de la réponse
# ---------------------------------------------------------------------------


def decode_message(body: str) -> dict[str, Any]:
    """Extrait et désérialise le JSON caché dans la `<div id="Message">`.

    Deux niveaux d'encodage à défaire : l'échappement HTML de la div, puis —
    sur certaines réponses — un JSON sérialisé en chaîne sous `status.message`.
    """
    match = _MESSAGE_RE.search(body)
    sanity_check(SOURCE, match is not None, 'aucune <div id="Message"> dans la réponse HTML')
    assert match is not None  # pour les type-checkers ; sanity_check a déjà levé

    raw = html.unescape(match.group(1)).strip()
    # Observé en conditions réelles : SOFIA renvoie parfois un HTTP 200 avec une
    # div Message VIDE. C'est un raté transitoire du service, pas un changement
    # de contrat — mais il ne doit surtout pas se traduire par « 0 NOTAM ».
    sanity_check(SOURCE, bool(raw), '<div id="Message"> vide (réponse transitoire de SOFIA)')

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(SOURCE, f'JSON illisible dans <div id="Message"> : {exc}') from exc

    sanity_check(
        SOURCE, isinstance(payload, dict), f"objet JSON attendu, reçu {type(payload).__name__}"
    )

    result: dict[str, Any] = payload
    inner = result.get("status.message")
    if isinstance(inner, str) and inner.lstrip().startswith("{"):
        try:
            result = {**result, "status.message": json.loads(inner)}
        except json.JSONDecodeError:
            # Second niveau optionnel : s'il n'est pas du JSON, on garde la chaîne.
            pass

    return result


def _error_cause(payload: dict[str, Any]) -> str | None:
    """Cause d'erreur déclarée par SOFIA, quel que soit le niveau d'encodage."""
    cause = payload.get("cause")
    if isinstance(cause, str):
        return cause
    inner = payload.get("status.message")
    if isinstance(inner, dict):
        inner_cause = inner.get("cause")
        if isinstance(inner_cause, str):
            return inner_cause
    return None


#: Rubriques métier de SOFIA → libellé d'affichage. Ce sont les catégories que
#: LA SOURCE attribue elle-même aux NOTAM ; on les reprend telles quelles plutôt
#: que d'inventer une échelle de gravité maison.
RUBRIC_LABELS: dict[str, str] = {
    "aerodromes_services": "Aérodrome & services",
    "aire_mouvement": "Aire de mouvement",
    "aire_trafic": "Aire de trafic",
    "balisage": "Balisage",
    "aides_atter_instal_radionav_GNSS": "Aides atterrissage / radionav / GNSS",
    "GNSS_installations_radionav": "GNSS / radionavigation",
    "procedures": "Procédures",
    "organisation_espace_services_circulation": "Espace & circulation",
    "organisation_espace_procedures": "Espace & procédures",
    "services_circulation_aerienne_VOLMET": "Circulation aérienne / VOLMET",
    "installations_com_surveillance": "Communications & surveillance",
    "meteorologie_equipements": "Équipements météo",
    "reglementation_espace_aerien": "Réglementation espace aérien",
    "avertissements_navigation": "Avertissements navigation",
    "obstacles": "Obstacles",
    "autres_info": "Autres informations",
}


def iter_notam_nodes(
    listnotams: Any, rubric: str | None = None
) -> Iterator[tuple[dict, str | None]]:
    """Parcourt l'arborescence et rend `(notam, rubrique)`, NOTAM aux FEUILLES.

    L'arbre a plusieurs formes selon la branche :
      AD    → [{code, name, <12 rubriques>: [notam...]}]
      FIR   → {rubrique: [{sortedNotamsByImpactedAerodromes: [
                            {sortedNotamsByPurpose: [{notam: [...]}]}]}]}
      ADDeg / Other → mêmes formes, souvent vides.

    Plutôt que de coder en dur ces chemins — qui changeraient au prochain
    remaniement du site — on descend récursivement et on reconnaît un NOTAM à la
    présence de sa `qLine`. En chemin, on mémorise la dernière RUBRIQUE traversée
    (`aire_mouvement`, `balisage`…) : c'est la catégorie que la source attribue au
    NOTAM, celle qu'on affichera. Le compte est ensuite confronté à `nbNotams`.
    """
    if isinstance(listnotams, dict):
        if "qLine" in listnotams:
            yield listnotams, rubric
            return
        for key, value in listnotams.items():
            # Une clé reconnue comme rubrique devient la catégorie du sous-arbre ;
            # sinon on conserve la rubrique héritée du parent.
            child_rubric = key if key in RUBRIC_LABELS else rubric
            yield from iter_notam_nodes(value, child_rubric)
    elif isinstance(listnotams, list):
        for value in listnotams:
            yield from iter_notam_nodes(value, rubric)


def _build_notam(node: dict[str, Any], rubric: str | None = None) -> Notam:
    for required in ("qLine", "startValidity", "endValidity"):
        sanity_check(SOURCE, required in node, f"champ NOTAM manquant : {required}")

    q_line = node["qLine"]
    sanity_check(SOURCE, isinstance(q_line, dict), "qLine n'est pas un objet")

    code23 = q_line.get("code23")
    code45 = q_line.get("code45")
    q_code = f"Q{code23}{code45}" if code23 and code45 else None

    validity = TimeWindow(
        _parse_validity(node["startValidity"], "startValidity"),
        _parse_validity(node["endValidity"], "endValidity"),
    )

    center: Position | None = None
    raw_coordinates = node.get("coordinates")
    if raw_coordinates:
        try:
            center = parse_coordinates(raw_coordinates)
        except ValueError as exc:
            # Une coordonnée présente mais illisible signale un changement de
            # contrat : on lève plutôt que d'élargir silencieusement le filtre.
            raise ProviderError(SOURCE, str(exc)) from exc

    # multiLanguage porte le français ; itemE reste l'anglais qui fait foi.
    french = (node.get("multiLanguage") or {}).get("itemE")
    english = node.get("itemE") or ""

    return Notam(
        identifier=_identifier(node),
        raw_text=english,
        validity=validity,
        center=center,
        radius_nm=_as_float(node.get("radius")),
        q_code=q_code,
        severity=severity_for(code23, code45),
        source_category=RUBRIC_LABELS.get(rubric) if rubric else None,
        decoded_text=french or None,
        affected_icao=_primary_icao(node.get("itemA")),
        lower_limit_ft=_flight_level_to_ft(q_line.get("lower")),
        upper_limit_ft=_flight_level_to_ft(q_line.get("upper")),
    )


def _parse_validity(value: Any, field: str) -> UtcDateTime:
    """Horodatage de validité SOFIA, `PERM`/`UFN` compris."""
    if isinstance(value, str) and value.strip().upper() in _OPEN_ENDED:
        return UtcDateTime.parse(PERMANENT_END, field)
    if not isinstance(value, str):
        raise ProviderError(SOURCE, f"{field} n'est pas une chaîne : {value!r}")
    try:
        return UtcDateTime.parse(value, field)
    except ValueError as exc:
        raise ProviderError(SOURCE, f"{field} illisible ({value!r}) : {exc}") from exc


def _identifier(node: dict[str, Any]) -> str:
    series = node.get("series") or "?"
    number = node.get("number")
    year = node.get("year")
    number_text = f"{number:04d}" if isinstance(number, int) else str(number)
    year_text = f"{year:02d}" if isinstance(year, int) else str(year)
    return f"{series}{number_text}/{year_text}"


def _primary_icao(item_a: Any) -> str | None:
    """`itemA` peut lister plusieurs FIR ("LFRR LFFF LFEE LFBB").

    Le domaine ne porte qu'un `affected_icao`; on retient le premier, qui est le
    plus proche du sujet dans la pratique SIA.
    """
    if not isinstance(item_a, str):
        return None
    tokens = item_a.split()
    return tokens[0] if tokens else None


def _flight_level_to_ft(level: Any) -> int | None:
    """La qLine borne en niveaux de vol ; le domaine borne en pieds."""
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    return int(level) * 100


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SofiaProvider:
    """Collecte les NOTAM SOFIA pour un `BriefingContext`.

    Politesse assumée envers un service public : une seule session réutilisée
    pour toute la durée de vie du provider, un seul aller-retour par briefing,
    un unique retry sur expiration de session (jamais de boucle), timeouts
    courts et User-Agent identifiable.
    """

    name = SOURCE
    category = "notam"
    is_critical = True

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        traffic: str = "V",
        fl_lower: int = 0,
        fl_upper: int = 999,
    ) -> None:
        if traffic not in {"V", "I", "VI"}:
            raise ValueError(f"traffic doit valoir V, I ou VI — reçu {traffic!r}")

        self._owns_client = client is None
        self._client = client or cache.make_client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._traffic = traffic
        self._fl_lower = fl_lower
        self._fl_upper = fl_upper
        self._session_ready = False

    # -- cycle de vie ------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SofiaProvider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- session -----------------------------------------------------------

    def _ensure_session(self) -> None:
        """Pose le JSESSIONID. Sans lui, SOFIA répond 500 {"cause":"refresh"}."""
        if self._session_ready:
            return
        try:
            response = self._client.get(SESSION_URL)
        except httpx.HTTPError as exc:
            raise ProviderError(SOURCE, f"session inaccessible : {exc}") from exc

        sanity_check(
            SOURCE,
            response.status_code == 200,
            f"ouverture de session : HTTP {response.status_code}",
        )
        self._session_ready = True

    def _reset_session(self) -> None:
        self._client.cookies.clear()
        self._session_ready = False

    # -- collecte ----------------------------------------------------------

    def fetch(self, context: BriefingContext) -> Sequence[Sourced[Notam]]:
        payload = self._request(self._build_form(context))

        sanity_check(SOURCE, "listnotams" in payload, "clé 'listnotams' absente de la réponse")
        expected = payload.get("nbNotams")
        sanity_check(
            SOURCE,
            isinstance(expected, int) and not isinstance(expected, bool),
            f"'nbNotams' absent ou non entier : {expected!r}",
        )

        nodes = list(iter_notam_nodes(payload["listnotams"]))
        # LE contrôle qui compte : si notre descente d'arbre rate une branche,
        # on rend un briefing amputé sans le savoir. SOFIA nous donne le compte
        # attendu, on le confronte.
        sanity_check(
            SOURCE,
            len(nodes) == expected,
            f"{len(nodes)} NOTAM extraits pour {expected} annoncés par nbNotams "
            f"— l'arborescence a probablement changé",
        )

        provenance = Provenance(
            source=SOURCE,
            retrieved_at=utcnow(),
            issued_at=self._issued_at(payload),
            url=ENDPOINT,
        )
        return tuple(Sourced(_build_notam(node, rubric), provenance) for node, rubric in nodes)

    @staticmethod
    def _issued_at(payload: dict[str, Any]) -> UtcDateTime | None:
        issued = payload.get("issued")
        if not isinstance(issued, str) or not issued:
            return None
        try:
            return UtcDateTime.parse(issued, "issued")
        except ValueError:
            return None

    # -- transport ---------------------------------------------------------

    def _request(self, form: dict[str, Any]) -> dict[str, Any]:
        """Un POST, et au plus UN retry si la session a expiré."""
        payload = self._post_once(form)
        if _error_cause(payload) != "refresh":
            return payload

        # Session expirée (ou jamais valide) : on la rouvre et on rejoue une
        # seule fois. Pas de boucle — si ça retombe en 'refresh', c'est que le
        # contrat a changé et insister ne ferait que marteler un service public.
        self._reset_session()
        payload = self._post_once(form)
        cause = _error_cause(payload)
        sanity_check(
            SOURCE,
            cause != "refresh",
            "session refusée deux fois de suite (cause=refresh) — contrat probablement modifié",
        )
        return payload

    def _post_once(self, form: dict[str, Any]) -> dict[str, Any]:
        self._ensure_session()
        try:
            response = self._client.post(ENDPOINT, data=form)
        except httpx.HTTPError as exc:
            raise ProviderError(SOURCE, f"requête NOTAM échouée : {exc}") from exc

        payload = decode_message(response.text)

        # Un 500 porteur de cause=refresh est une expiration de session, pas une
        # panne : on le laisse remonter à _request pour le retry.
        if response.status_code != 200 and _error_cause(payload) != "refresh":
            raise ProviderError(
                SOURCE,
                f"HTTP {response.status_code} : {payload.get('message') or payload}",
            )
        return payload

    # -- construction de la requête ---------------------------------------

    def _build_form(self, context: BriefingContext) -> dict[str, Any]:
        """Choisit l'opération RPC selon ce que le contexte permet d'exprimer.

        SOFIA raisonne en terrains et points nommés ; le domaine raisonne en
        géométrie. On privilégie l'opération la plus précise que le contexte
        permette de renseigner, et on retombe sur le cylindre lat/long sinon.
        """
        circle = context.geometry.bounding_circle()
        # httpx encode une valeur de type liste en clé répétée — c'est ainsi
        # qu'on produit `aero[]=X&alt[]=Y&alt[]=Z`, l'ordre étant préservé.
        form: dict[str, Any] = {
            "isFromSofia": "true",
            "valid_from": self._format_instant(context.window.start),
            "duration": self._format_duration(context.window),
            "traffic": self._traffic,
            "fl_lower": str(self._fl_lower),
            "fl_upper": str(self._fl_upper),
            "uuid": str(uuid.uuid4()),
        }
        radius = max(1, round(circle.radius_nm))

        origin = (context.origin_icao or "").upper()
        destination = (context.destination_icao or "").upper()
        alternates = [icao.upper() for icao in context.alternates_icao if icao]

        # Vol local autour d'un terrain : cylindre centré sur l'AD.
        if context.purpose is Purpose.LOCAL and origin:
            form[":operation"] = "postAreaAeroPibRequest"
            form["radius"] = str(radius)
            form["adep"] = origin
            form["width"] = str(radius)
            form["aero[]"] = [origin]
            if alternates:
                form["alt[]"] = alternates
            return form

        # Navigation / déroutement : couloir le long d'une route nommée.
        if origin and destination and destination != origin:
            half_width = getattr(context.geometry, "half_width_nm", None)
            form[":operation"] = "postNarrowRoutePibRequest"
            form["width"] = str(max(1, round(half_width)) if half_width else radius)
            form["radiusAD"] = str(radius)
            # route[] est ORDONNÉ : départ, puis destination.
            form["route[]"] = [origin, destination]
            if alternates:
                form["alt[]"] = alternates
            return form

        # Aucun terrain exploitable : cylindre pur, en coordonnées SOFIA.
        form[":operation"] = "postAreaPibRequest"
        form["radius"] = str(radius)
        form["lat"] = format_latitude(circle.center.lat)
        form["long"] = format_longitude(circle.center.lon)
        return form

    @staticmethod
    def _format_instant(instant: UtcDateTime) -> str:
        """ISO 8601 UTC tel que le produit moment.js côté SOFIA."""
        return instant.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _format_duration(window: TimeWindow) -> str:
        """Durée au format HHMM ("0300" = 3 h)."""
        minutes = round((window.end - window.start).total_seconds() / 60)
        sanity_check(SOURCE, minutes > 0, "fenêtre de briefing de durée nulle")
        sanity_check(
            SOURCE,
            minutes <= MAX_DURATION_HOURS * 60,
            f"fenêtre de {minutes // 60} h au-delà du maximum SOFIA ({MAX_DURATION_HOURS} h)",
        )
        hours, mins = divmod(minutes, 60)
        return f"{hours:02d}{mins:02d}"
