"""Web-app masse & centrage : données de l'avion → page HTML+JS autonome.

Une page par avion : on injecte ses postes de pesage et son enveloppe de centrage
en JSON, le JavaScript fait le calcul en direct (sliders → point de centrage qui
bouge, AVEC et SANS carburant, comme SDVFR). Ouvrable en local, aucun réseau.

Le calcul de référence (masse, bras, respect d'enveloppe) vit dans le domaine
(`aircraft.model.WeightBalance.state`), testé ; le JS le reproduit pour
l'interactivité. L'immatriculation de l'avion (ex. F-ZZZZ, fictif) suffit à
signaler des données d'exemple ; la page n'ajoute pas d'avertissement séparé.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..aircraft.model import AircraftSpec

_TEMPLATE = Path(__file__).parent / "templates" / "massbalance.html.j2"
_MARKER = "/*__AEROBRIEFER_WB__*/null"


def massbalance_data(aircraft: AircraftSpec) -> dict[str, Any]:
    """Contrat de données de la page, dérivé du masse & centrage de l'avion."""
    wb = aircraft.weight_balance
    if wb is None:
        raise ValueError(f"{aircraft.name} n'a pas de données de masse & centrage")
    return {
        "aircraft": aircraft.name,
        "empty": {"mass_kg": wb.empty_mass_kg, "arm_m": wb.empty_arm_m},
        "max_mass_kg": wb.max_mass_kg,
        "stations": [
            {"name": s.name, "arm_m": s.arm_m, "max_kg": s.max_kg, "default_kg": s.default_kg}
            for s in wb.stations
        ],
        "fuel": {
            "name": wb.fuel.name,
            "arm_m": wb.fuel.arm_m,
            "capacity_l": wb.fuel.capacity_l,
            "density_kg_per_l": wb.fuel.density_kg_per_l,
            "default_l": wb.fuel.default_l,
        },
        "envelope": [[arm, mass] for arm, mass in wb.envelope],
    }


def render_massbalance(aircraft: AircraftSpec) -> str:
    """Page masse & centrage autonome : gabarit + données JSON injectées."""
    template = _TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(massbalance_data(aircraft), ensure_ascii=False).replace("</", "<\\/")
    return template.replace(_MARKER, payload)
