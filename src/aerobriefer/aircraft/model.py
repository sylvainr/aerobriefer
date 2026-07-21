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

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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


# --- avion ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpeedCard:
    """Vitesses de référence, en nœuds (converties depuis le manuel si besoin)."""

    vne_kt: float
    vno_kt: float
    va_kt: float
    vfe_kt: float


class Aircraft(ABC):
    """Base d'un modèle d'avion. Sous-classer pour coder un appareil réel.

    Un appareil fournit ses tables POH et ses limites ; le framework applique les
    corrections communes (vent, pente) par-dessus le résultat de table.
    """

    name: str
    max_takeoff_mass_kg: float
    max_landing_mass_kg: float
    speeds: SpeedCard
    demonstrated_crosswind_kt: float | None = None

    # Courbe de correction de vent de face : nœuds → facteur multiplicatif.
    # Défaut neutre ; la plupart des appareils la surchargent (DR400 : 0.8 à 10 kt).
    headwind_factors: Mapping[float, float] = {0.0: 1.0}

    # Effet de pente : +/- % de distance par % de pente (montante rallonge au
    # décollage, raccourcit à l'atterrissage). 0 = non modélisé.
    slope_pct_per_pct: float = 0.0

    @abstractmethod
    def _takeoff_table(self, surface: Surface) -> PerfTable: ...

    @abstractmethod
    def _landing_table(self, surface: Surface) -> PerfTable: ...

    def takeoff(self, c: Conditions) -> DistanceResult:
        return self._compute(self._takeoff_table(c.surface), c, is_takeoff=True)

    def landing(self, c: Conditions) -> DistanceResult:
        return self._compute(self._landing_table(c.surface), c, is_takeoff=False)

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

        if self.slope_pct_per_pct and c.slope_pct:
            sign = 1.0 if is_takeoff else -1.0
            slope_factor = 1.0 + sign * self.slope_pct_per_pct * c.slope_pct
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
        knots = sorted(self.headwind_factors)
        lo, hi = _bracket(headwind_kt, knots)
        return _interp1(headwind_kt, lo, hi, self.headwind_factors[lo], self.headwind_factors[hi])


def _is_clamped(table: PerfTable, alt: float, offset: float, mass: float) -> bool:
    return (
        alt < table.altitudes[0]
        or alt > table.altitudes[-1]
        or offset < table.isa_offsets[0]
        or offset > table.isa_offsets[-1]
        or mass < table.masses[0]
        or mass > table.masses[-1]
    )
