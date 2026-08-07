"""Bilan carburant : navlog + avion → page HTML+JS autonome.

Reproduit le bilan carburant de l'utilisateur (feuille Excel), TOUT en minutes :
temps de vol + roulages + intégrations + déroutement + impact vent + marge de
sécurité + réserve finale → total minutes → litres (÷60 × conso horaire) → kg
(× densité). Les cellules éditables (fond jaune, comme sa feuille) sont les seules
saisies ; le reste se calcule. Le temps de vol est PRÉ-REMPLI depuis le log de
nav (donnée dérivée), la conso et la densité depuis le modèle de l'avion.

Aucune valeur inventée au rendu ; calcul local, aucun réseau, aucune IA.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..aircraft.model import AircraftSpec
from ..domain.navlog import NavLog

_TEMPLATE = Path(__file__).parent / "templates" / "fuelbalance.html.j2"
_MARKER = "/*__AEROBRIEFER_FUEL__*/null"


def fuelbalance_data(navlog: NavLog, aircraft: AircraftSpec) -> dict[str, Any]:
    """Contrat de données de la page, dérivé du navlog et de l'avion.

    - `flight_min` : temps de vol total du navlog (pré-remplit « Temps de vol ») ;
    - `fuel_flow_lph` : conso horaire de croisière de l'avion ;
    - `density_kg_per_l` : densité carburant (1er réservoir) ;
    - `capacity_l` : capacité totale (borne de l'emport à bord)."""
    wb = aircraft.weight_balance
    capacity_l = sum(tank.capacity_l for tank in wb.fuels) if wb else 0.0
    density = wb.fuels[0].density_kg_per_l if wb and wb.fuels else 0.72
    flight_min = sum(leg.time.total_seconds() / 60.0 for leg in navlog.legs)
    return {
        "aircraft": aircraft.name,
        "flight_min": round(flight_min),
        "fuel_flow_lph": aircraft.cruise_fuel_lph,
        "density_kg_per_l": density,
        "capacity_l": capacity_l,
    }


def render_fuelbalance(navlog: NavLog, aircraft: AircraftSpec) -> str:
    """Page bilan carburant autonome : gabarit + données JSON injectées."""
    template = _TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(fuelbalance_data(navlog, aircraft), ensure_ascii=False).replace(
        "</", "<\\/"
    )
    return template.replace(_MARKER, payload)
