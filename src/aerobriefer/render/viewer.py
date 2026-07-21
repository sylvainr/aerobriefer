"""Viewer d'espaces aériens — export de données + assemblage HTML.

Deux livrables partagent la même donnée :
- une **carte 2D** (SVG, dans le briefing),
- un **viewer 3D** three.js (page autonome, outil de préparation en ligne).

Ce module produit le CONTRAT de données (`viewer_data`) que les deux consomment,
et assemble la page 3D en injectant ce JSON dans un gabarit HTML.

Le viewer 3D est un outil de PRÉPARATION (au bureau, en ligne) : il a le droit de
charger three.js depuis un CDN, contrairement au briefing emporté qui reste
autosuffisant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.context import BriefingContext
from ..domain.package import BriefingPackage

_TEMPLATE = Path(__file__).parent / "templates" / "viewer.html"

#: Couleurs par classe d'espace (cohérentes 2D / 3D).
CLASS_COLORS: dict[str, str] = {
    "A": "#d11a2a",  # rouge — le plus restrictif
    "B": "#e0562d",
    "C": "#e08a2d",  # orange
    "D": "#3d7bd1",  # bleu
    "E": "#4aa564",  # vert
    "F": "#8a8a8a",
    "G": "#b0b0b0",  # gris — non contrôlé
    "UNC": "#8e44ad",  # violet — zones R/P/D
}


def viewer_data(package: BriefingPackage) -> dict[str, Any]:
    """Contrat de données du viewer, dérivé d'un `BriefingPackage`.

    Coordonnées en [lon, lat] (convention GeoJSON). Altitudes en pieds AMSL, prêtes
    à l'extrusion. Tout ce que 2D et 3D affichent vient d'ici — pas d'appel réseau
    côté rendu.
    """
    context = package.context
    center = context.geometry.bounding_circle().center

    airspaces = [
        {
            "name": a.name,
            "class": a.airspace_class,
            "type": a.airspace_type,
            "color": CLASS_COLORS.get(a.airspace_class, "#888888"),
            "lower_ft": a.lower.feet_amsl,
            "upper_ft": a.upper.feet_amsl,
            "lower_label": a.lower.label,
            "upper_label": a.upper.label,
            "frequency": a.frequency,
            "polygon": [[p.lon, p.lat] for p in a.polygon],
        }
        for a in package.airspaces
    ]

    aerodromes = [
        {
            "icao": ad.icao,
            "name": ad.name,
            "lat": ad.position.lat,
            "lon": ad.position.lon,
            "elevation_ft": ad.elevation_ft,
            "runways": [
                {
                    "ident": r.ident,
                    "length_m": r.length_m,
                    "bearing": r.true_bearing_deg,
                    "surface": r.surface,
                }
                for r in ad.runways
            ],
        }
        for ad in package.aerodromes
    ]

    return {
        "center": {"lat": center.lat, "lon": center.lon},
        "flight": {
            "radius_nm": _radius_nm(context),
            "aerodromes": aerodromes,
        },
        "airspaces": airspaces,
        "class_colors": CLASS_COLORS,
    }


def _radius_nm(context: BriefingContext) -> float:
    circle = context.geometry.bounding_circle()
    return round(circle.radius_nm, 1)


def render_viewer(package: BriefingPackage) -> str:
    """Page 3D autonome : le gabarit HTML avec le JSON de données injecté."""
    template = _TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(viewer_data(package), ensure_ascii=False)
    # Marqueur unique remplacé par les données. On échappe `</script>` par
    # prudence si un nom d'espace en contenait (il n'y en a pas, mais coûte 0).
    payload = payload.replace("</", "<\\/")
    return template.replace("/*__AEROBRIEFER_DATA__*/null", payload)
