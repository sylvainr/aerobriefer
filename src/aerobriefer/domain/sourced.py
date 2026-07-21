"""Traçabilité : d'où vient chaque donnée, et depuis quand.

Principe non négociable du projet — aucune donnée n'entre dans un briefing sans
sa provenance. Un METAR de trois heures affiché comme s'il était frais est pire
que pas de METAR du tout, parce qu'il inspire une confiance injustifiée.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from .window import UtcDateTime, utcnow

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    """Identifiant du provider, ex. 'sofia', 'noaa-awc', 'met.no', 'aeroweb'."""

    retrieved_at: UtcDateTime
    """Quand NOUS avons récupéré la donnée."""

    issued_at: UtcDateTime | None = None
    """Quand la SOURCE l'a émise. Distinct de `retrieved_at` : un TAF émis à 05h
    et lu à 11h a six heures d'âge, même si on vient de le télécharger."""

    url: str | None = None
    """Pour rejouer ou auditer après le vol."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", UtcDateTime.of(self.retrieved_at, "retrieved_at"))
        object.__setattr__(self, "issued_at", UtcDateTime.optional(self.issued_at, "issued_at"))

    def age_minutes(self, now: UtcDateTime | None = None) -> float:
        """Âge réel de l'information, fondé sur l'émission quand elle est connue."""
        reference = self.issued_at or self.retrieved_at
        return ((now or utcnow()) - reference).total_seconds() / 60.0


@dataclass(frozen=True, slots=True)
class Sourced(Generic[T]):
    """Une donnée et sa provenance, indissociables."""

    value: T
    provenance: Provenance

    def age_minutes(self, now: UtcDateTime | None = None) -> float:
        return self.provenance.age_minutes(now)

    def is_stale(self, max_age_minutes: float, now: UtcDateTime | None = None) -> bool:
        return self.age_minutes(now) > max_age_minutes
