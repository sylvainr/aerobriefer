"""Configuration commune aux tests.

Les données de référence (aérodromes, pistes) sont normalement téléchargées et
mises en cache au runtime (`refdata`). Pour les tests, on pointe `refdata` vers
une FIXTURE régionale pré-remplie — aucun accès réseau, résultat déterministe.
"""

import os
from pathlib import Path

_FIXTURE = Path(__file__).parent / "fixtures" / "refdata"

# Posé AVANT tout import de aerobriefer.data.refdata dans les tests.
os.environ.setdefault("AEROBRIEFER_REFDATA_DIR", str(_FIXTURE))
_AIRSPACE_FIXTURE = Path(__file__).parent / "fixtures" / "airspace"
os.environ.setdefault("AEROBRIEFER_AIRSPACE_DIR", str(_AIRSPACE_FIXTURE))
