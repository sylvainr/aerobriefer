"""Contrat commun des providers.

Un provider convertit une source externe en objets du domaine. Il ne décide
jamais de ce qui est pertinent : il collecte pour un `BriefingContext` donné et
rend des `Sourced[...]`.

Règle cardinale — un provider en échec LÈVE. Il ne retourne jamais une liste
vide silencieuse. C'est l'agrégateur qui capture l'exception et la convertit en
`ProviderFailure` visible dans le briefing. Un « 0 NOTAM » dû à un parseur cassé
est plus dangereux que pas de briefing du tout.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..domain.context import BriefingContext
from ..domain.sourced import Sourced


class ProviderError(RuntimeError):
    """Échec de collecte. Porte le nom de la source pour le rapport d'anomalie."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.message = message


class Provider(Protocol):
    """Toute source de données du briefing."""

    name: str
    """Identifiant court, repris tel quel dans `Provenance.source`."""

    is_critical: bool
    """Si vrai, son échec rend le dossier INCOMPLET (cf. BriefingPackage.is_complete)."""

    def fetch(self, context: BriefingContext) -> Sequence[Sourced]:
        """Collecte pour ce contexte. Lève `ProviderError` en cas d'échec."""
        ...


def sanity_check(source: str, condition: bool, message: str) -> None:
    """Assertion de sanité destinée aux sources fragiles (scraping, double
    encodage). Échouer bruyamment est le comportement voulu."""
    if not condition:
        raise ProviderError(source, f"contrôle de cohérence échoué : {message}")
