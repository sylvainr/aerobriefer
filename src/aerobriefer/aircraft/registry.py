"""Registre des avions, par IMMATRICULATION.

Un avion est identifié par son immatriculation (ex. « F-ZZZZ ») — c'est elle qui
porte SA fiche de pesée, SES vitesses, SON centrage, pas la catégorie. Chaque
avion est une CLASSE codée (cf. `aircraft.model`) ; ce registre associe une immat
à son instance. Les avions de l'utilisateur s'ajoutent ici (sa donnée).
"""

from __future__ import annotations

from collections.abc import Callable

from .examples.dr400 import DR400_160
from .model import Aircraft

#: Immatriculation → constructeur d'avion. F-ZZZZ = l'exemple (DR400/160 fictif).
_REGISTRY: dict[str, Callable[[], Aircraft]] = {
    "F-ZZZZ": DR400_160,
    "DR400": DR400_160,  # tolérance : ancienne référence par modèle
}

#: Immatriculation par défaut quand aucune n'est précisée (l'avion d'exemple).
DEFAULT_REGISTRATION = "F-ZZZZ"


def resolve(registration: str | None) -> Aircraft:
    """Avion pour une immatriculation. `None` → l'avion par défaut (F-ZZZZ).

    Immat inconnue → erreur claire (on n'invente pas d'avion)."""
    key = (registration or DEFAULT_REGISTRATION).strip().upper()
    factory = _REGISTRY.get(key)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"aéronef inconnu : {registration!r} — immatriculations connues : {known}")
    return factory()
