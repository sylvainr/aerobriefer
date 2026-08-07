"""Framework de performances avion — en CODE, pas en YAML.

Un avion n'est pas un sac de constantes : c'est un comportement (interpolation de
tables du manuel de vol, corrections vent/surface/pente, limites). On code donc
une classe par appareil contre ce framework. Le repo fournit le framework + UN
exemple (le DR400, cf. `examples.dr400`). Les avions de l'utilisateur sont SA
donnée : ils vivent hors du repo et sous-classent `Aircraft`.

Le manuel du DR400 est en km/h et kg (édition 1972). Le framework parle ces
unités-là en interne quand la table les impose, et expose des mètres / nœuds /
pieds vers le reste du domaine. La frontière de conversion est explicite ici.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# --- unités ----------------------------------------------------------------

KMH_PER_KT = 1.852
FT_PER_M = 3.280839895
M_PER_FT = 0.3048


def kmh_to_kt(value: float) -> float:
    return value / KMH_PER_KT


def kt_to_kmh(value: float) -> float:
    return value * KMH_PER_KT


def isa_temperature_c(pressure_altitude_ft: float) -> float:
    """Température ISA à une altitude-pression : 15 °C au sol, −2 °C/1000 ft."""
    return 15.0 - 2.0 * (pressure_altitude_ft / 1000.0)


def isa_offset_c(pressure_altitude_ft: float, temperature_c: float) -> float:
    """Écart à l'ISA (« Std ± X ») — c'est l'axe température des tables POH."""
    return temperature_c - isa_temperature_c(pressure_altitude_ft)


# --- conditions & résultats ------------------------------------------------

Surface = str  # "paved" | "grass"


@dataclass(frozen=True, slots=True)
class Conditions:
    """Les conditions d'une opération de décollage ou d'atterrissage."""

    pressure_altitude_ft: float
    temperature_c: float
    mass_kg: float
    headwind_kt: float = 0.0
    """Composante de vent DE FACE (négative = vent arrière)."""
    surface: Surface = "paved"
    slope_pct: float = 0.0
    """Pente de piste en % (montante > 0). Corrige la distance si l'avion la porte."""


@dataclass(frozen=True, slots=True)
class DistanceResult:
    """Distances calculées, avec la décomposition des corrections appliquées."""

    ground_roll_m: float
    over_15m_m: float
    """Distance totale pour franchir 15 m (l'obstacle réglementaire VFR)."""
    base_over_15m_m: float
    """Avant corrections vent/pente — pour tracer d'où vient le résultat."""
    notes: tuple[str, ...] = ()


# --- interpolation ----------------------------------------------------------


def _interp1(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def _bracket(value: float, axis: Sequence[float]) -> tuple[float, float]:
    """Les deux valeurs de l'axe qui encadrent `value` (clampé aux bornes).

    Hors table, on n'EXTRAPOLE pas : on clampe et on le signale à l'appelant via
    `clamped_axes`. Extrapoler une distance de décollage serait dangereux.
    """
    lo = axis[0]
    hi = axis[-1]
    if value <= lo:
        return lo, lo
    if value >= hi:
        return hi, hi
    for a, b in zip(axis, axis[1:], strict=False):
        if a <= value <= b:
            return a, b
    return hi, hi


class PerfTable:
    """Table de performance à 3 axes : altitude-pression, écart ISA, masse.

    Les points sont donnés aux nœuds d'une grille (les cases du manuel). La
    lecture fait une interpolation trilinéaire, en clampant hors bornes (jamais
    d'extrapolation). Chaque case porte (roulement, distance 15 m) en mètres.
    """

    def __init__(
        self,
        altitudes_ft: Sequence[float],
        isa_offsets_c: Sequence[float],
        masses_kg: Sequence[float],
        cells: Mapping[tuple[float, float, float], tuple[float, float]],
    ) -> None:
        # Axes TOUJOURS croissants : le manuel donne parfois la masse en ordre
        # décroissant (1050 puis 850), et `_bracket`/l'interpolation supposent
        # un ordre croissant. On trie ici, les clés de `cells` restent valides.
        self.altitudes = sorted(altitudes_ft)
        self.isa_offsets = sorted(isa_offsets_c)
        self.masses = sorted(masses_kg)
        self._cells = dict(cells)

    def lookup(self, alt_ft: float, offset_c: float, mass_kg: float) -> tuple[float, float]:
        """(roulement_m, distance_15m_m) interpolés, hors bornes clampés."""
        a0, a1 = _bracket(alt_ft, self.altitudes)
        o0, o1 = _bracket(offset_c, self.isa_offsets)
        m0, m1 = _bracket(mass_kg, self.masses)

        # Trilinéaire explicite : 8 coins → 1 valeur, pour roulement et pour 15 m.
        def corner(a: float, o: float, m: float, idx: int) -> float:
            return self._cell(a, o, m)[idx]

        def trilinear(idx: int) -> float:
            # masse d'abord
            c00 = _interp1(mass_kg, m0, m1, corner(a0, o0, m0, idx), corner(a0, o0, m1, idx))
            c01 = _interp1(mass_kg, m0, m1, corner(a0, o1, m0, idx), corner(a0, o1, m1, idx))
            c10 = _interp1(mass_kg, m0, m1, corner(a1, o0, m0, idx), corner(a1, o0, m1, idx))
            c11 = _interp1(mass_kg, m0, m1, corner(a1, o1, m0, idx), corner(a1, o1, m1, idx))
            # puis offset ISA
            c0 = _interp1(offset_c, o0, o1, c00, c01)
            c1 = _interp1(offset_c, o0, o1, c10, c11)
            # puis altitude
            return _interp1(alt_ft, a0, a1, c0, c1)

        return trilinear(0), trilinear(1)

    def _cell(self, alt: float, offset: float, mass: float) -> tuple[float, float]:
        return self._cells[(alt, offset, mass)]


# --- avion (spec validée : données + comportement) --------------------------
#
# Un avion se décrit en DONNÉE (JSON strict, clés françaises) et se construit
# aussi en Python (noms de champs anglais, grâce à populate_by_name). Le modèle
# porte AUSSI son comportement (interpolation des tables, corrections, centrage) :
# c'est le contrat UNIQUE consommé en aval, il n'y a pas de seconde structure.


class Speeds(BaseModel):
    """Vitesses de référence dans l'unité du manuel (`unite`), exposées aussi en
    nœuds. Le calcul reste en nœuds ; l'affichage suit `AircraftSpec.speed_unit`."""

    model_config = ConfigDict(extra="forbid")

    vne: float
    vno: float
    va: float
    vfe: float
    vs0: float | None = None
    vs1: float | None = None
    vx: float | None = None
    vy: float | None = None
    finesse_max: float | None = None
    approche: float | None = None
    unite: str = "kt"

    def _kt(self, value: float) -> float:
        return kmh_to_kt(value) if self.unite == "km/h" else value

    @property
    def vne_kt(self) -> float:
        return self._kt(self.vne)

    @property
    def vno_kt(self) -> float:
        return self._kt(self.vno)

    @property
    def va_kt(self) -> float:
        return self._kt(self.va)

    @property
    def vfe_kt(self) -> float:
        return self._kt(self.vfe)


class Cruise(BaseModel):
    """Croisière pour le log de nav : TAS + conso horaire. `unite` = unité de la TAS."""

    model_config = ConfigDict(extra="forbid")

    tas: float
    conso_horaire_l: float
    unite: str = "kt"

    @property
    def tas_kt(self) -> float:
        return kmh_to_kt(self.tas) if self.unite == "km/h" else self.tas


class Station(BaseModel):
    """Poste de chargement fixe : bras de levier au datum (m), charge max (kg)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(alias="nom")
    arm_m: float = Field(alias="bras_m")
    max_kg: float
    default_kg: float = Field(default=0.0, alias="defaut_kg")


class FuelTank(BaseModel):
    """Réservoir : bras, capacité, densité. Poste distinct pour centrer AVEC et
    SANS carburant (consommation en vol)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(default="Carburant", alias="nom")
    arm_m: float = Field(alias="bras_m")
    capacity_l: float = Field(alias="capacite_l")
    density_kg_per_l: float = Field(default=0.72, alias="densite_kg_l")
    default_l: float = Field(default=0.0, alias="defaut_l")


@dataclass(frozen=True, slots=True)
class WBState:
    """Point de centrage résultant : masse, bras (CG), et respect de l'enveloppe."""

    mass_kg: float
    arm_m: float
    within_envelope: bool


class WeightBalance(BaseModel):
    """Masse & centrage : masse à vide, postes, réservoir et enveloppe de centrage
    (polygone bras×masse)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    empty_mass_kg: float = Field(alias="masse_vide_kg")
    empty_arm_m: float = Field(alias="bras_vide_m")
    stations: tuple[Station, ...] = Field(alias="postes")
    # Un ou PLUSIEURS réservoirs : leurs bras diffèrent (ex. DR400 : nourrice
    # arrière vs réservoirs avant), donc le carburant est saisi par réservoir.
    fuels: tuple[FuelTank, ...] = Field(alias="reservoirs")
    # sommets (bras_m, masse_kg) du polygone d'enveloppe
    envelope: tuple[tuple[float, float], ...] = Field(alias="enveloppe")
    max_mass_kg: float = Field(alias="max_masse_kg")

    def state(self, loads_kg: Mapping[str, float], fuels_l: Mapping[str, float]) -> WBState:
        """Masse, bras et respect d'enveloppe. `fuels_l` = litres PAR réservoir (clé
        = nom du réservoir) — les bras diffèrent, on ne peut pas agréger."""
        mass = self.empty_mass_kg
        moment = self.empty_mass_kg * self.empty_arm_m
        for station in self.stations:
            weight = loads_kg.get(station.name, 0.0)
            mass += weight
            moment += weight * station.arm_m
        for tank in self.fuels:
            fuel_kg = fuels_l.get(tank.name, 0.0) * tank.density_kg_per_l
            mass += fuel_kg
            moment += fuel_kg * tank.arm_m
        arm = moment / mass if mass > 0 else 0.0
        return WBState(
            mass_kg=mass, arm_m=arm, within_envelope=_in_polygon(arm, mass, self.envelope)
        )


def _in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Point (x, y) dans le polygone (lancer de rayon). Bord = dedans par tolérance."""
    inside = False
    count = len(polygon)
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x <= x_cross:
                inside = not inside
        j = i
    return inside


class PerfCell(BaseModel):
    """Une case de table POH : conditions (alt/écart ISA/masse) → distances (m)."""

    model_config = ConfigDict(extra="forbid")

    alt_ft: float
    isa_c: float
    masse_kg: float
    roulement_m: float
    distance_15m_m: float


class PerfTableSpec(BaseModel):
    """Table de performance (DONNÉE) : axes + cases. `build()` donne le moteur
    d'interpolation (`PerfTable`)."""

    model_config = ConfigDict(extra="forbid")

    altitudes_ft: tuple[float, ...]
    ecarts_isa_c: tuple[float, ...]
    masses_kg: tuple[float, ...]
    cases: tuple[PerfCell, ...]

    def build(self) -> PerfTable:
        cells = {
            (c.alt_ft, c.isa_c, c.masse_kg): (c.roulement_m, c.distance_15m_m) for c in self.cases
        }
        return PerfTable(self.altitudes_ft, self.ecarts_isa_c, self.masses_kg, cells)


class PerfSurfaces(BaseModel):
    """Table par revêtement : dur (béton) et herbe."""

    model_config = ConfigDict(extra="forbid")

    dur: PerfTableSpec
    herbe: PerfTableSpec

    def for_surface(self, surface: Surface) -> PerfTableSpec:
        return self.herbe if surface == "grass" else self.dur


class Performances(BaseModel):
    """Tables POH : décollage et atterrissage, chacune par revêtement."""

    model_config = ConfigDict(extra="forbid")

    decollage: PerfSurfaces
    atterrissage: PerfSurfaces


class Corrections(BaseModel):
    """Corrections communes appliquées par-dessus la table : vent de face (nœuds →
    facteur multiplicatif) et pente (% de distance par % de pente)."""

    model_config = ConfigDict(extra="forbid")

    vent_face: dict[float, float] = Field(default_factory=lambda: {0.0: 1.0})
    pente_pct_par_pct: float = 0.0


class AircraftSpec(BaseModel):
    """Un avion : DONNÉES validées (immat, masses, vitesses, centrage, perfs) ET
    COMPORTEMENT (interpolation des tables POH, corrections vent/pente, centrage).

    Contrat UNIQUE consommé en aval. Se construit depuis un JSON strict (clés
    françaises) ou en Python (noms de champs anglais, via populate_by_name)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    registration: str = Field(alias="immatriculation")
    type_name: str = Field(alias="type")
    max_takeoff_mass_kg: float = Field(alias="max_masse_decollage_kg")
    max_landing_mass_kg: float = Field(alias="max_masse_atterrissage_kg")
    # Unité d'AFFICHAGE des vitesses propres (TAS, sol) : "kt" ou "km/h" (badin).
    # Le calcul reste en nœuds ; le VENT est TOUJOURS affiché en nœuds.
    speed_unit: str = Field(default="kt", alias="unite_vitesse")
    demonstrated_crosswind_kt: float | None = Field(
        default=None, alias="vent_traversier_demontre_kt"
    )
    speeds: Speeds = Field(alias="vitesses")
    cruise: Cruise = Field(alias="croisiere")
    weight_balance: WeightBalance | None = Field(default=None, alias="masse_centrage")
    performances: Performances = Field(alias="performances")
    corrections: Corrections = Field(default_factory=Corrections, alias="corrections")

    # Moteurs d'interpolation construits une fois (par revêtement), non sérialisés.
    _takeoff_tables: dict[str, PerfTable] = PrivateAttr(default_factory=dict)
    _landing_tables: dict[str, PerfTable] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        for surface in ("paved", "grass"):
            self._takeoff_tables[surface] = self.performances.decollage.for_surface(surface).build()
            self._landing_tables[surface] = self.performances.atterrissage.for_surface(
                surface
            ).build()

    @property
    def name(self) -> str:
        return f"{self.type_name} ({self.registration})"

    @property
    def cruise_tas_kt(self) -> float:
        return self.cruise.tas_kt

    @property
    def cruise_fuel_lph(self) -> float:
        return self.cruise.conso_horaire_l

    def takeoff(self, c: Conditions) -> DistanceResult:
        return self._compute(self._takeoff_tables[c.surface], c, is_takeoff=True)

    def landing(self, c: Conditions) -> DistanceResult:
        return self._compute(self._landing_tables[c.surface], c, is_takeoff=False)

    def _compute(self, table: PerfTable, c: Conditions, *, is_takeoff: bool) -> DistanceResult:
        offset = isa_offset_c(c.pressure_altitude_ft, c.temperature_c)
        roll, over15 = table.lookup(c.pressure_altitude_ft, offset, c.mass_kg)
        base = over15
        notes: list[str] = []

        wind_factor = self._headwind_factor(c.headwind_kt)
        if wind_factor != 1.0:
            roll *= wind_factor
            over15 *= wind_factor
            notes.append(f"vent {c.headwind_kt:+.0f} kt → ×{wind_factor:.2f}")

        slope_per_pct = self.corrections.pente_pct_par_pct
        if slope_per_pct and c.slope_pct:
            sign = 1.0 if is_takeoff else -1.0
            slope_factor = 1.0 + sign * slope_per_pct * c.slope_pct
            slope_factor = max(0.5, slope_factor)
            roll *= slope_factor
            over15 *= slope_factor
            notes.append(f"pente {c.slope_pct:+.1f}% → ×{slope_factor:.2f}")

        if _is_clamped(table, c.pressure_altitude_ft, offset, c.mass_kg):
            notes.append("HORS TABLE (valeur clampée aux bornes — ne pas extrapoler)")

        return DistanceResult(
            ground_roll_m=round(roll),
            over_15m_m=round(over15),
            base_over_15m_m=round(base),
            notes=tuple(notes),
        )

    def _headwind_factor(self, headwind_kt: float) -> float:
        if headwind_kt <= 0:
            return 1.0  # on ne bonifie pas un vent arrière : marge de sécurité
        factors = self.corrections.vent_face
        knots = sorted(factors)
        lo, hi = _bracket(headwind_kt, knots)
        return _interp1(headwind_kt, lo, hi, factors[lo], factors[hi])


def _is_clamped(table: PerfTable, alt: float, offset: float, mass: float) -> bool:
    return (
        alt < table.altitudes[0]
        or alt > table.altitudes[-1]
        or offset < table.isa_offsets[0]
        or offset > table.isa_offsets[-1]
        or mass < table.masses[0]
        or mass > table.masses[-1]
    )
