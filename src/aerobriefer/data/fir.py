"""Régions d'information de vol (FIR) — code OACI → nom.

Les FIR ne sont pas des aérodromes : ils n'existent pas dans la base
OurAirports, et un NOTAM de zone porte le code du FIR (LFBB, LFRR…) là où un
NOTAM de terrain porte celui de l'aérodrome. Sans cette table, le briefing
afficherait « LFBB » brut, illisible pour qui ne connaît pas le découpage.

On se limite aux FIR que la source (SOFIA / SIA) peut réellement renvoyer pour un
vol en France et outre-mer français. Un code absent retombe sur lui-même : mieux
vaut afficher le code seul qu'un nom inventé.
"""

from __future__ import annotations

#: FIR métropolitains et d'outre-mer français, plus les frontaliers courants.
FIR_NAMES: dict[str, str] = {
    # France métropolitaine
    "LFFF": "FIR Paris",
    "LFRR": "FIR Brest",
    "LFBB": "FIR Bordeaux",
    "LFMM": "FIR Marseille",
    "LFEE": "FIR Reims",
    # Outre-mer français
    "SOOO": "FIR Cayenne (Guyane)",
    "TFFF": "FIR Fort-de-France (Antilles)",
    "FMEE": "FIR La Réunion",
    "NTTT": "FIR Tahiti",
    "NWWW": "FIR Nouméa",
    # Frontaliers fréquemment cités dans les NOTAM de zone
    "EBBU": "FIR Bruxelles",
    "EDGG": "FIR Langen (Allemagne)",
    "EDMM": "FIR München (Allemagne)",
    "LSAS": "FIR Suisse",
    "LIMM": "FIR Milan",
    "LECM": "FIR Madrid",
    "LECB": "FIR Barcelone",
    "EGTT": "FIR Londres",
}


def lookup(code: str) -> str | None:
    """Nom du FIR, ou `None` si le code n'en est pas un connu."""
    return FIR_NAMES.get(code.strip().upper())
