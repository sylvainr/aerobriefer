"""Le vocabulaire du domaine, indépendant de toute source.

Règle transversale : le texte brut est TOUJOURS conservé à côté du décodé. Le
décodage est un confort, jamais une autorité — en cas de doute en vol, c'est le
brut qui fait foi, et un parseur qui échoue ne doit pas faire disparaître la
donnée.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from .geo import Geometry, Position
from .window import TimeWindow, UtcDateTime


@dataclass(frozen=True, slots=True)
class WindComponents:
    """Décomposition du vent par rapport à un axe de piste."""

    headwind_kt: float
    """> 0 vent de face (favorable), < 0 vent arrière."""
    crosswind_kt: float
    """Toujours ≥ 0 ; le côté est porté par `from_right`."""
    from_right: bool
    """Vrai si le traversier vient de la droite de l'axe (référentiel QFU basse)."""
    runway_ident: str

    @property
    def is_tailwind(self) -> bool:
        return self.headwind_kt < 0.0

    @property
    def arrow(self) -> str:
        """Flèche indiquant d'où souffle le traversier : ← droite, → gauche.

        Le vent venant de la droite POUSSE l'avion vers la gauche : la flèche
        pointe donc dans le sens de la poussée, vers la gauche, quand il vient de
        droite. Sans traversier notable, pas de flèche."""
        if self.crosswind_kt < 0.5:
            return ""
        return "←" if self.from_right else "→"


class Severity(IntEnum):
    """Tri opérationnel, pas classification réglementaire.

    Sert à décider ce qui remonte en tête du briefing. L'ordre est celui de
    l'urgence pour un VFR local : ce qui empêche de voler, puis ce qui contraint,
    puis ce qui informe.
    """

    BLOCKING = 4  # piste ou terrain fermé, zone interdite active
    MAJOR = 3  # moyen d'approche hors service, obstacle significatif
    MINOR = 2  # balisage partiel, service réduit
    INFO = 1  # administratif, sans effet opérationnel direct
    UNKNOWN = 0  # non classé — remonte en tête par prudence, jamais masqué


@dataclass(frozen=True, slots=True)
class Runway:
    ident: str  # ex. "07/25"
    length_m: int
    width_m: int | None = None
    surface: str | None = None  # "asphalte", "herbe", ...
    true_bearing_deg: float | None = None  # de la QFU basse, pour le vent traversier

    @property
    def is_paved(self) -> bool | None:
        if self.surface is None:
            return None
        surface = self.surface.lower()
        paved_codes = {"asp", "con", "pem", "bit"}  # codes OurAirports
        soft = {"herbe", "grass", "terre", "gravel", "grs", "gre", "san", "dirt"}
        if surface in paved_codes:
            return True
        return surface not in soft

    def wind_components(self, wind_dir_deg: float, wind_speed_kt: float) -> WindComponents | None:
        """Composantes du vent pour CETTE piste : de face/arrière et traversier.

        Convention aéro : le vent est donné par la direction D'OÙ il vient. La
        composante traversière est positive quand le vent vient de la DROITE de
        l'axe de piste (référentiel de l'atterrissage sur la QFU basse), négative
        depuis la gauche — c'est ce signe qui oriente la flèche à l'affichage.

        Renvoie `None` si l'orientation de piste est inconnue : on ne calcule pas
        un traversier sur une piste dont on ignore le cap.
        """
        if self.true_bearing_deg is None:
            return None
        angle = math.radians((wind_dir_deg - self.true_bearing_deg + 540.0) % 360.0 - 180.0)
        headwind = wind_speed_kt * math.cos(angle)
        crosswind = wind_speed_kt * math.sin(angle)
        return WindComponents(
            headwind_kt=headwind,
            crosswind_kt=abs(crosswind),
            from_right=crosswind >= 0.0,
            runway_ident=self.ident,
        )


@dataclass(frozen=True, slots=True)
class Aerodrome:
    icao: str
    name: str
    position: Position
    elevation_ft: int
    runways: Sequence[Runway] = field(default_factory=tuple)

    @property
    def longest_runway_m(self) -> int | None:
        return max((r.length_m for r in self.runways), default=None)

    def favoured_wind_components(
        self, wind_dir_deg: float, wind_speed_kt: float
    ) -> WindComponents | None:
        """Vent décomposé sur la piste la plus FAVORABLE au vent donné.

        « Favorable » = le QFU qui maximise la composante de face (donc minimise
        le traversier et exclut le vent arrière) — c'est la piste qu'on choisit
        d'utiliser. Chaque piste physique offre deux QFU opposés ; on teste les
        deux caps (bas et +180°).

        `None` si aucune orientation de piste n'est connue. On ne fabrique pas un
        traversier là où on ignore l'orientation.
        """
        best: WindComponents | None = None
        for runway in self.runways:
            if runway.true_bearing_deg is None:
                continue
            low, high = _split_ident(runway.ident)
            for bearing, ident in (
                (runway.true_bearing_deg, low),
                ((runway.true_bearing_deg + 180.0) % 360.0, high),
            ):
                candidate = Runway(
                    ident=ident, length_m=runway.length_m, true_bearing_deg=bearing
                ).wind_components(wind_dir_deg, wind_speed_kt)
                if candidate is None:
                    continue
                if best is None or candidate.headwind_kt > best.headwind_kt:
                    best = candidate
        return best


def _split_ident(ident: str) -> tuple[str, str]:
    """« 10/28 » → (« 10 », « 28 »). Pour nommer le QFU retenu à l'affichage."""
    parts = ident.replace(" ", "").split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return ident, ident


@dataclass(frozen=True, slots=True)
class Notam:
    identifier: str  # ex. "A1234/25"
    raw_text: str
    validity: TimeWindow

    center: Position | None = None
    radius_nm: float | None = None
    """Géométrie déclarée. Absente sur certains NOTAM de FIR — dans ce cas on ne
    peut pas filtrer géométriquement, et la politique est de conserver (ne jamais
    écarter faute d'information)."""

    q_code: str | None = None  # les 5 lettres du champ Q, ex. "QMRLC"
    severity: Severity = Severity.UNKNOWN
    """Classification PROPRE à aerobriefer, déduite des Q-codes. Conservée pour
    d'éventuels usages internes, mais N'EST PLUS la catégorie affichée : le
    briefing montre `source_category`, la rubrique fournie par la source, plutôt
    qu'un jugement maison."""

    source_category: str | None = None
    """Rubrique métier telle que la source la classe (SOFIA : `aire_mouvement`,
    `balisage`, `obstacles`…). C'est une donnée de la source, pas une déduction :
    on l'affiche telle quelle, sans y ajouter de jugement de gravité."""

    decoded_text: str | None = None
    affected_icao: str | None = None
    lower_limit_ft: int | None = None
    upper_limit_ft: int | None = None

    def concerns(self, geometry: Geometry, window: TimeWindow) -> bool:
        """Les deux prédicats du filtrage : géométrie ET temps.

        Sans géométrie connue, on retient — mieux vaut un NOTAM de trop qu'un
        NOTAM manquant.
        """
        if not self.validity.overlaps(window):
            return False
        if self.center is None:
            return True
        return geometry.contains(self.center, self.radius_nm or 0.0)

    @property
    def is_open_ended(self) -> bool:
        """Validité sans fin annoncée (PERM / UFN).

        La source encode ces cas par une date sentinelle très lointaine (année
        2099). Au-delà de ce seuil, l'affichage doit dire « permanent » plutôt
        qu'une date de 2099 qui n'informe personne."""
        return self.validity.end.year >= OPEN_ENDED_YEAR


OPEN_ENDED_YEAR = 2099
"""Seuil au-delà duquel une fin de validité est traitée comme « sans fin »."""


@dataclass(frozen=True, slots=True)
class Sigmet:
    """Phénomène météo dangereux en route : orage, turbulence, givrage, cendres.

    Contrairement au NOTAM, un SIGMET est une zone POLYGONALE assortie d'un
    plancher/plafond. On garde le texte brut (qui fait foi) et les champs
    décodés par la source.
    """

    identifier: str
    hazard: str  # "TS", "TURB", "ICE", "MTW", "VA", "CONVECTIVE"...
    raw_text: str
    validity: TimeWindow

    polygon: Sequence[Position] = field(default_factory=tuple)
    fir: str | None = None
    lower_ft: int | None = None
    upper_ft: int | None = None
    qualifier: str | None = None  # "ISOL", "EMBD", "OBSC", "SEV"...

    def concerns(self, geometry: Geometry, window: TimeWindow) -> bool:
        """Retenu si la zone touche notre géométrie ET la fenêtre temporelle.

        Sans polygone connu, on conserve : un SIGMET orage manquant est bien plus
        grave qu'un SIGMET de trop. Le test spatial est approché — un sommet du
        polygone dans notre zone, ou notre centre dans le polygone — ce qui suffit
        à ne pas rater un phénomène qui nous concerne.
        """
        if not self.validity.overlaps(window):
            return False
        if not self.polygon:
            return True
        bounding = geometry.bounding_circle()
        if any(geometry.contains(vertex) for vertex in self.polygon):
            return True
        return _point_in_polygon(bounding.center, self.polygon)


def _point_in_polygon(point: Position, polygon: Sequence[Position]) -> bool:
    """Ray casting en lon/lat. Suffisant à l'échelle d'un SIGMET (pas de passage
    par les pôles ni l'antiméridien dans nos latitudes)."""
    inside = False
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        if (a.lat > point.lat) != (b.lat > point.lat):
            x_cross = (b.lon - a.lon) * (point.lat - a.lat) / (b.lat - a.lat) + a.lon
            if point.lon < x_cross:
                inside = not inside
    return inside


@dataclass(frozen=True, slots=True)
class Metar:
    station: str
    raw_text: str
    observed_at: UtcDateTime

    wind_dir_deg: int | None = None  # None si variable
    wind_speed_kt: int | None = None
    wind_gust_kt: int | None = None
    visibility_m: int | None = None
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    qnh_hpa: float | None = None
    ceiling_ft: int | None = None
    conditions: Sequence[str] = field(default_factory=tuple)
    flight_category: str | None = None
    """Catégorie de vol OACI/FAA : VFR, MVFR, IFR, LIFR. Fournie par la source
    (NOAA `fltCat`) ou déduite par avwx du plafond et de la visibilité. `None`
    si ni l'une ni l'autre ne l'établit — jamais deviné."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", UtcDateTime.of(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class TafPeriod:
    """Un groupe d'évolution d'un TAF : période initiale, FM, BECMG, TEMPO, PROB.

    `change_type` conserve le type OACI tel quel, car il porte un sens
    opérationnel qu'on ne doit pas aplatir : un TEMPO n'engage que par
    intermittence, un BECMG décrit une transition, un PROB30 reste une
    probabilité. Les fusionner en « conditions prévues » ferait perdre au pilote
    l'information qui lui sert à décider.
    """

    validity: TimeWindow
    change_type: str = "FM"  # FM | BECMG | TEMPO | PROB30 | PROB40 | INITIAL
    probability: int | None = None

    wind_dir_deg: int | None = None  # None si VRB
    wind_speed_kt: int | None = None
    wind_gust_kt: int | None = None
    visibility_m: int | None = None
    ceiling_ft: int | None = None
    clouds: Sequence[str] = field(default_factory=tuple)
    conditions: Sequence[str] = field(default_factory=tuple)
    raw_text: str = ""

    @property
    def is_transient(self) -> bool:
        """Vrai pour les groupes qui n'engagent pas en continu (TEMPO, PROB)."""
        return self.change_type.startswith(("TEMPO", "PROB"))


@dataclass(frozen=True, slots=True)
class Taf:
    station: str
    raw_text: str
    issued_at: UtcDateTime
    validity: TimeWindow
    periods: Sequence[TafPeriod] = field(default_factory=tuple)
    """Groupes décodés. Vide si le parseur a échoué — le brut reste alors seul
    à faire foi, conformément à la règle du module."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", UtcDateTime.of(self.issued_at, "issued_at"))

    def periods_overlapping(self, window: TimeWindow) -> tuple[TafPeriod, ...]:
        """Groupes concernant une fenêtre de vol donnée."""
        return tuple(p for p in self.periods if p.validity.overlaps(window))


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """Prévision ponctuelle à une échéance donnée (met.no et similaires)."""

    valid_at: UtcDateTime
    position: Position

    wind_dir_deg: float | None = None
    wind_speed_kt: float | None = None
    wind_gust_kt: float | None = None
    temperature_c: float | None = None
    cloud_cover_pct: float | None = None
    cloud_base_ft: float | None = None
    precipitation_mm: float | None = None
    qnh_hpa: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_at", UtcDateTime.of(self.valid_at, "valid_at"))


@dataclass(frozen=True, slots=True)
class Chart:
    """Produit graphique : TEMSI, WINTEM, carte de front, satellite, radar.

    Non parsable par nature. On le traite comme un artefact référencé : on garde
    l'URL, éventuellement les octets, et surtout les horodatages — une carte de
    front dont on ignore l'échéance est inexploitable.
    """

    kind: str  # "temsi" | "wintem" | "front" | "satellite" | "radar"
    url: str
    issued_at: UtcDateTime | None = None
    valid_at: UtcDateTime | None = None
    area: str | None = None  # "FRANCE", "EUROC", ...
    flight_level: str | None = None  # pertinent pour WINTEM
    media_type: str | None = None  # "image/png", "application/pdf", ...
    content: bytes | None = None  # rempli seulement si on embarque le rendu

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", UtcDateTime.optional(self.issued_at, "issued_at"))
        object.__setattr__(self, "valid_at", UtcDateTime.optional(self.valid_at, "valid_at"))

    @property
    def is_embedded(self) -> bool:
        """Une carte non embarquée n'est pas consultable hors ligne — donc pas
        utilisable dans un dossier emporté en vol."""
        return self.content is not None
