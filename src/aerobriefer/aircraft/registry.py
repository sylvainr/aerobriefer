"""Registre des avions, par IMMATRICULATION.

Un avion est identifié par son immatriculation (ex. « F-ZZZZ ») — c'est elle qui
porte SA fiche de pesée, SES vitesses, SON centrage. Le repo fournit UN exemple
(F-ZZZZ, cf. `examples.dr400`). Les avions RÉELS de l'utilisateur sont SA donnée :
des fichiers JSON validés par `AircraftSpec`, hors repo, dans le répertoire pointé
par la variable d'environnement `AEROBRIEFER_AIRCRAFT_DIR`. Pour une immat donnée
« F-GGJY », on cherche `<dir>/f-ggjy/f-ggjy.json` puis `<dir>/f-ggjy.json`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .examples.dr400 import DR400_160
from .model import AircraftSpec

ENV_DIR = "AEROBRIEFER_AIRCRAFT_DIR"

#: Avions FOURNIS par le repo (exemples). F-ZZZZ = DR400/160 fictif.
_BUILTINS: dict[str, Callable[[], AircraftSpec]] = {
    "F-ZZZZ": DR400_160,
    "DR400": DR400_160,  # tolérance : ancienne référence par modèle
}

#: Immatriculation par défaut quand aucune n'est précisée (l'avion d'exemple).
DEFAULT_REGISTRATION = "F-ZZZZ"


def resolve(registration: str | None) -> AircraftSpec:
    """Avion pour une immatriculation. `None` → l'avion par défaut (F-ZZZZ).

    Ordre : exemples fournis, puis fichiers JSON de l'utilisateur. Immat inconnue
    → erreur claire (on n'invente jamais un avion)."""
    key = (registration or DEFAULT_REGISTRATION).strip().upper()
    builtin = _BUILTINS.get(key)
    if builtin is not None:
        return builtin()
    spec = _load_user_aircraft(key)
    if spec is not None:
        return spec
    known = ", ".join(sorted(_BUILTINS))
    hint = f" ; ou un fichier dans ${ENV_DIR}" if not os.environ.get(ENV_DIR) else ""
    raise KeyError(f"aéronef inconnu : {registration!r} — connus : {known}{hint}")


def _load_user_aircraft(key: str) -> AircraftSpec | None:
    directory = os.environ.get(ENV_DIR)
    if not directory:
        return None
    slug = key.lower()
    base = Path(directory)
    for candidate in (base / slug / f"{slug}.json", base / f"{slug}.json"):
        if candidate.exists():
            return AircraftSpec.model_validate_json(candidate.read_text(encoding="utf-8"))
    return None
