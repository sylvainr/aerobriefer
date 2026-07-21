"""Seuils de péremption, par cadence de production.

Un seuil unique n'a pas de sens : chaque produit aéronautique a son rythme
d'émission, et « périmé » veut seulement dire « il en existe probablement un
plus récent que celui-ci ». Marquer un TAF de deux heures comme périmé est faux
— les TAF sortent toutes les six heures, celui-là est le plus frais publié.

Règle appliquée : seuil ≈ cadence de production + une marge de tolérance. En
deçà, la donnée est la meilleure disponible ; au-delà, on a probablement raté
une émission, et ça mérite d'être signalé.
"""

from __future__ import annotations

from .models import Chart, ForecastPoint, Metar, Notam, Taf

METAR_MINUTES = 90.0
"""METAR/SPECI émis toutes les 30 min (60 sur certains terrains). À 90 min on a
raté au moins une observation."""

TAF_MINUTES = 8 * 60.0
"""TAF émis toutes les 6 h (réseaux 00/06/12/18Z), valides 24 h ou plus. Deux
heures d'âge, c'est un TAF frais. On tolère 8 h : au-delà, un nouveau réseau est
sorti et n'a pas été récupéré."""

FORECAST_MINUTES = 4 * 60.0
"""Modèle met.no rafraîchi environ toutes les heures, mais une prévision reste
exploitable bien après le tour de modèle qui l'a produite."""

NOTAM_MINUTES = 3 * 60.0
"""Un NOTAM ne « périme » pas au sens des autres données — il porte sa propre
validité. Ce qu'on mesure ici, c'est l'âge de NOTRE interrogation : au-delà de
3 h, un NOTAM nouveau a pu paraître sans qu'on le voie."""

_CHART_MINUTES = {
    # Imagerie d'observation : produite tous les quarts d'heure.
    "radar": 45.0,
    "satellite": 45.0,
    # Produits de prévision : cadence de 3 h (TEMSI, WINTEM) à 6 h (fronts).
    "temsi": 6 * 60.0,
    "wintem": 6 * 60.0,
    "front": 12 * 60.0,
}
CHART_DEFAULT_MINUTES = 6 * 60.0


def max_age_minutes(value: object) -> float:
    """Seuil applicable à une donnée, d'après sa nature et sa cadence."""
    if isinstance(value, Metar):
        return METAR_MINUTES
    if isinstance(value, Taf):
        return TAF_MINUTES
    if isinstance(value, ForecastPoint):
        return FORECAST_MINUTES
    if isinstance(value, Notam):
        return NOTAM_MINUTES
    if isinstance(value, Chart):
        return _CHART_MINUTES.get(value.kind, CHART_DEFAULT_MINUTES)
    return METAR_MINUTES  # le plus strict, faute de mieux


def describe(value: object) -> str:
    """Seuil en clair, pour que le rendu puisse justifier un marquage.

    Un « périmé » sans seuil affiché est une affirmation qu'on ne peut pas
    vérifier ; avec le seuil, le pilote juge par lui-même.
    """
    minutes = max_age_minutes(value)
    if minutes < 120:
        # « 90 min » se lit mieux que « 1.5 h » pour un seuil court.
        return f"{int(minutes)} min"
    hours = minutes / 60
    return f"{int(hours)} h" if hours.is_integer() else f"{hours:.1f} h"
