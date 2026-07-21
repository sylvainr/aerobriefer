"""Le dossier de briefing : le contrat central du projet.

Produit par la couche d'agrégation, consommé indépendamment par le renderer
déterministe (feuille A4 emportée en vol) et par la couche conversationnelle.

Deux règles de conception :

1. Il doit être AUTOSUFFISANT. Si un consommateur doit rappeler un provider pour
   rendre le dossier, c'est que le dossier est sous-spécifié.
2. Un provider en échec produit un `ProviderFailure`, jamais une liste vide
   silencieuse. Un briefing qui affiche « 0 NOTAM » parce qu'un parseur a cassé
   est plus dangereux que pas de briefing du tout.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .context import BriefingContext
from .freshness import max_age_minutes
from .models import (
    Aerodrome,
    Airspace,
    Chart,
    ForecastPoint,
    Metar,
    Notam,
    Severity,
    Sigmet,
    Taf,
)
from .sourced import Sourced
from .window import UtcDateTime, utcnow


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """Une source qui n'a pas répondu. Rendue visible dans le briefing."""

    source: str
    reason: str
    occurred_at: UtcDateTime
    category: str | None = None
    """Rubrique amputée : "notam", "metar", "taf", "forecast", "chart".

    Permet d'avertir DANS la section concernée, et pas seulement dans le bandeau
    global : une section NOTAM vide pour cause de panne doit le dire sur place,
    là où le lecteur la cherche."""

    is_critical: bool = False
    """Vrai si l'absence de cette source compromet le briefing (NOTAM, METAR du
    terrain de départ). Le renderer doit alors marquer le dossier INCOMPLET."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", UtcDateTime.of(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class BriefingPackage:
    context: BriefingContext
    assembled_at: UtcDateTime = field(default_factory=utcnow)

    aerodromes: Sequence[Aerodrome] = field(default_factory=tuple)
    airspaces: Sequence[Airspace] = field(default_factory=tuple)
    """Espaces aériens touchant la zone. Donnée de RÉFÉRENCE (pas collectée avec
    provenance par un provider) — d'où le type nu, sans `Sourced`."""
    metars: Sequence[Sourced[Metar]] = field(default_factory=tuple)
    tafs: Sequence[Sourced[Taf]] = field(default_factory=tuple)
    notams: Sequence[Sourced[Notam]] = field(default_factory=tuple)
    sigmets: Sequence[Sourced[Sigmet]] = field(default_factory=tuple)
    forecasts: Sequence[Sourced[ForecastPoint]] = field(default_factory=tuple)
    charts: Sequence[Sourced[Chart]] = field(default_factory=tuple)
    failures: Sequence[ProviderFailure] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        """Faux dès qu'une source critique manque. Le renderer l'affiche en tête."""
        return not any(f.is_critical for f in self.failures)

    def notams_by_severity(self) -> tuple[Sourced[Notam], ...]:
        """Du plus bloquant au plus anodin ; à sévérité égale, du plus imminent.

        Les non classés (UNKNOWN = 0) seraient relégués en fin par un tri naïf
        alors qu'ils sont précisément ceux qu'il faut lire. On les hisse en tête.

        NB : la sévérité est une classification PROPRE à aerobriefer, déduite des
        Q-codes OACI — ni le SIA ni l'OACI ne classent ainsi. Voir
        `notams_by_activation` pour un tri fondé sur la seule donnée officielle :
        la date.
        """

        def key(item: Sourced[Notam]) -> tuple[int, UtcDateTime]:
            n = item.value
            rank = 5 if n.severity is Severity.UNKNOWN else int(n.severity)
            return (-rank, n.validity.start)

        return tuple(sorted(self.notams, key=key))

    def notams_by_activation(self) -> tuple[Sourced[Notam], ...]:
        """Par date d'entrée en vigueur (startValidity), la plus récente d'abord.

        Contrairement au tri par sévérité, celui-ci ne repose sur AUCUN jugement :
        la date d'activation est une donnée officielle du NOTAM. Les plus
        récemment activés remontent — ce sont les changements dont il faut
        prendre connaissance en priorité, la « nouveauté » depuis le dernier vol.
        """
        return tuple(sorted(self.notams, key=lambda i: i.value.validity.start, reverse=True))

    def metar_for(self, icao: str) -> Sourced[Metar] | None:
        target = icao.upper()
        return next((m for m in self.metars if m.value.station.upper() == target), None)

    def charts_covering_window(self) -> tuple[Sourced[Chart], ...]:
        """Cartes dont l'échéance tombe dans la fenêtre de vol."""
        return tuple(
            item
            for item in self.charts
            if item.value.valid_at is not None and self.context.window.contains(item.value.valid_at)
        )

    def charts_outside_window(self) -> tuple[Sourced[Chart], ...]:
        """Cartes hors fenêtre — à AFFICHER, mais jamais sans le dire.

        Cas réel et dangereux : TEMSI et WINTEM ne portent que quelques heures.
        Pour un vol préparé la veille, les seules cartes disponibles sont celles
        du jour même, et un pilote pressé peut les lire comme si elles couvraient
        son créneau. On les conserve — elles restent informatives sur la tendance
        — mais le rendu doit les marquer comme NE COUVRANT PAS le vol, et
        rappeler qu'il faut rebriefer avant le départ.
        """
        covering = set(id(item) for item in self.charts_covering_window())
        return tuple(item for item in self.charts if id(item) not in covering)

    def missing_chart_kinds(self) -> tuple[str, ...]:
        """Familles de cartes dont AUCUNE échéance ne couvre le vol.

        Une absence est une information de briefing : mieux vaut « pas de TEMSI
        pour ce créneau » qu'un silence qu'on prend pour une couverture.
        """
        covering = {item.value.kind for item in self.charts_covering_window()}
        present = {item.value.kind for item in self.charts}
        return tuple(sorted(present - covering))

    def failures_for(self, category: str) -> tuple[ProviderFailure, ...]:
        """Échecs ayant amputé une rubrique donnée."""
        return tuple(f for f in self.failures if f.category == category)

    def stale_items(self, now: UtcDateTime | None = None) -> tuple[Sourced, ...]:
        """Tout ce qui a dépassé son seuil de péremption PROPRE.

        Le seuil dépend de la cadence de production (cf. `freshness`) : 90 min
        pour un METAR, 8 h pour un TAF qui n'est émis que toutes les 6 h. Un
        seuil unique marquerait « périmé » des données parfaitement fraîches, et
        un dossier qui crie au loup partout finit par n'être plus lu.

        Ne masque rien : signale, et laisse le consommateur décider.
        """
        now = now or utcnow()
        everything: tuple[Sourced[Any], ...] = (
            *self.metars,
            *self.tafs,
            *self.notams,
            *self.sigmets,
            *self.forecasts,
            *self.charts,
        )
        return tuple(item for item in everything if item.is_stale(max_age_minutes(item.value), now))
