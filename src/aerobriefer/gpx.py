"""Export d'une route de navigation au format GPX 1.1.

Un GPX de route = une `<rte>` de `<rtept>` (lat/lon, nom, altitude). ForeFlight,
SkyDemon et Garmin Pilot lisent la `<rte>` pour recréer le plan de vol ; les
points définis par coordonnées (nos « travers ») sont repris tels quels, sans
avoir à exister dans une base de données.

Rendu déterministe, aucun appel réseau. Altitudes converties en mètres (le GPX
impose le mètre pour `<ele>`)."""

from __future__ import annotations

from xml.sax.saxutils import escape

from .domain.route import Route

_CREATOR = "aerobriefer"
_FT_TO_M = 0.3048


def route_to_gpx(route: Route, *, title: str) -> str:
    """Sérialise une `Route` en document GPX 1.1 (chaîne UTF-8)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gpx version="1.1" creator="{_CREATOR}"'
        ' xmlns="http://www.topografix.com/GPX/1/1"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="http://www.topografix.com/GPX/1/1'
        ' http://www.topografix.com/GPX/1/1/gpx.xsd">',
        "  <rte>",
        f"    <name>{escape(title)}</name>",
    ]
    for wp in route.waypoints:
        ele = ""
        if wp.altitude_ft is not None:
            ele = f"<ele>{wp.altitude_ft * _FT_TO_M:.1f}</ele>"
        lines.append(
            f'    <rtept lat="{wp.position.lat:.6f}" lon="{wp.position.lon:.6f}">'
            f"<name>{escape(wp.name)}</name>{ele}</rtept>"
        )
    lines.append("  </rte>")
    lines.append("</gpx>")
    return "\n".join(lines) + "\n"
