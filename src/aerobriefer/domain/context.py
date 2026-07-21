"""La demande de briefing : ce que l'utilisateur veut couvrir.

Objet unique passé à tous les providers. C'est lui qui rend les produits
interchangeables : un brief local est un `Circle`, une nav est un `Corridor`, un
déroutement est un `Circle` autour de chaque alternate. Un seul moteur.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .geo import Circle, Geometry, Position
from .route import Route
from .window import TimeWindow


class Purpose(StrEnum):
    LOCAL = "local"
    NAVIGATION = "navigation"
    DIVERSION = "diversion"


@dataclass(frozen=True, slots=True)
class BriefingContext:
    geometry: Geometry
    window: TimeWindow
    purpose: Purpose = Purpose.LOCAL

    origin_icao: str | None = None
    destination_icao: str | None = None
    alternates_icao: Sequence[str] = field(default_factory=tuple)
    aircraft_id: str | None = None
    """Immatriculation ou modèle. Absent = pas de calcul de performances, le
    briefing reste produit sans."""

    observation_stations: Sequence[str] = field(default_factory=tuple)
    """Stations météo de repli, quand le terrain du vol n'observe pas lui-même.

    Cas très courant sur les petits terrains : LFCY (Royan) n'émet ni METAR ni
    TAF, la station exploitable la plus proche est à ~38 NM. Ces stations ne
    sont PAS des terrains du vol — elles n'entrent pas dans les dégagements et
    le rendu doit afficher leur distance, car une observation à 40 NM ne décrit
    pas forcément le régime local (brise de mer, brume matinale côtière)."""

    route: Route | None = None
    """Route de navigation (points tournants + altitudes), pour une nav. La
    géométrie du contexte est alors un `Corridor` le long de cette route."""

    @classmethod
    def navigation(
        cls,
        *,
        route: Route,
        window: TimeWindow,
        half_width_nm: float = 10.0,
        origin_icao: str | None = None,
        destination_icao: str | None = None,
        alternates_icao: Sequence[str] = (),
        aircraft_id: str | None = None,
    ) -> BriefingContext:
        """Un vol de navigation : couloir le long de la route."""
        return cls(
            geometry=route.corridor(half_width_nm),
            window=window,
            purpose=Purpose.NAVIGATION,
            origin_icao=origin_icao,
            destination_icao=destination_icao,
            alternates_icao=tuple(alternates_icao),
            aircraft_id=aircraft_id,
            route=route,
        )

    @classmethod
    def local(
        cls,
        *,
        center: Position,
        radius_nm: float,
        window: TimeWindow,
        icao: str | None = None,
        aircraft_id: str | None = None,
    ) -> BriefingContext:
        """Le cas courant : un vol local autour d'un terrain."""
        return cls(
            geometry=Circle(center, radius_nm),
            window=window,
            purpose=Purpose.LOCAL,
            origin_icao=icao,
            destination_icao=icao,
            aircraft_id=aircraft_id,
        )

    @property
    def flight_aerodromes(self) -> tuple[str, ...]:
        """Terrains du vol : départ, destination, dégagements. Sans les stations
        de repli, qui ne sont pas des terrains où l'on se pose."""
        return _dedupe(self.origin_icao, self.destination_icao, *self.alternates_icao)

    @property
    def stations_of_interest(self) -> tuple[str, ...]:
        """Stations à interroger pour METAR/TAF, sans doublon et dans l'ordre.

        L'ordre porte du sens : départ d'abord, puis destination, puis
        dégagements, puis les stations de repli — c'est celui du briefing rendu.
        """
        return _dedupe(*self.flight_aerodromes, *self.observation_stations)


def _dedupe(*icaos: str | None) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for icao in icaos:
        if icao:
            seen.setdefault(icao.upper(), None)
    return tuple(seen)
