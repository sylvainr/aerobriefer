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
import math
from pathlib import Path
from typing import Any

from ..domain.context import BriefingContext
from ..domain.package import BriefingPackage

_M_PER_DEG_LAT = 111320.0

# Couches de fond de carte pour le SOL du viewer (outil en ligne). Modèles d'URL
# WMS/export : {minLon},{minLat},{maxLon},{maxLat} remplis par tuile. Aucune clé.
# Chaque tuile est demandée en 2048×2048 : découper le sol en grille N×N multiplie
# la résolution effective (une seule image sur 100+ km serait floue de près).
BASE_LAYERS: dict[str, dict[str, str]] = {
    "ign-ortho": {
        "label": "Satellite IGN (France, HD)",
        "url": (
            "https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
            "&LAYERS=ORTHOIMAGERY.ORTHOPHOTOS&STYLES=&CRS=EPSG:4326"
            "&BBOX={minLat},{minLon},{maxLat},{maxLon}&WIDTH=2048&HEIGHT=2048&FORMAT=image/jpeg"
        ),
    },
    "esri": {
        "label": "Satellite Esri (monde)",
        "url": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
            "?bbox={minLon},{minLat},{maxLon},{maxLat}&bboxSR=4326&size=2048,2048"
            "&format=jpg&f=image"
        ),
    },
}

#: Taille de sol visée par tuile (m). Grille N×N pour couvrir l'emprise à cette
#: granularité, bornée pour ne pas exploser le nombre de requêtes.
_TILE_TARGET_M = 28000.0
_MAX_GRID = 6

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


#: Le viewer montre une RÉGION plus large que la zone filtrée du briefing : on
#: veut voir tous les terrains et espaces alentour (jusqu'à ~50 NM autour d'un
#: vol local), pas seulement ceux retenus pour le dossier.
VIEWER_REGION_MARGIN_NM = 30.0


def viewer_data(package: BriefingPackage) -> dict[str, Any]:
    """Contrat de données du viewer, dérivé d'un `BriefingPackage`.

    Coordonnées en [lon, lat] (convention GeoJSON). Altitudes en pieds AMSL, prêtes
    à l'extrusion.

    À la différence du briefing (qui filtre serré autour du vol), le viewer est un
    outil de VISUALISATION : il interroge la donnée de référence sur une région
    élargie pour montrer TOUS les aérodromes et TOUS les espaces alentour.
    """
    from ..data import airports, airspace
    from ..domain.geo import Circle

    context = package.context
    center = context.geometry.bounding_circle().center
    region_radius_nm = context.geometry.bounding_circle().radius_nm + VIEWER_REGION_MARGIN_NM
    region = Circle(center, region_radius_nm)

    flight_icaos = {i.upper() for i in context.flight_aerodromes}

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
        for a in _region_airspaces(airspace, region)
    ]

    aerodromes = [
        {
            "icao": ad.icao,
            "name": ad.name,
            "lat": ad.position.lat,
            "lon": ad.position.lon,
            "elevation_ft": ad.elevation_ft,
            "is_flight_aerodrome": ad.icao.upper() in flight_icaos,
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
        for ad, _dist in airports.nearest(center, within_nm=region_radius_nm, limit=200)
    ]

    ground = _ground(center, region_radius_nm, airspaces)

    return {
        "center": {"lat": center.lat, "lon": center.lon},
        "flight": {
            "radius_nm": _radius_nm(context),
            "aerodromes": aerodromes,
        },
        "airspaces": airspaces,
        "route": _route(context),
        "class_colors": CLASS_COLORS,
        "ground": ground,
    }


def _route(context: BriefingContext) -> dict[str, Any] | None:
    """La route de nav pour le viewer : points (lon/lat/altitude) + branches.

    Le viewer trace une polyligne 3D aux altitudes planifiées, ce qui montre d'un
    coup d'œil quels espaces la trajectoire traverse et à quelle hauteur.
    """
    route = context.route
    if route is None:
        return None
    waypoints = [
        {
            "name": w.name,
            "lon": w.position.lon,
            "lat": w.position.lat,
            "altitude_ft": w.altitude_ft,
        }
        for w in route.waypoints
    ]
    legs = [
        {
            "from": leg.start.name,
            "to": leg.end.name,
            "distance_nm": round(leg.distance_nm, 1),
            "true_track_deg": round(leg.true_track_deg),
        }
        for leg in route.legs()
    ]
    return {
        "waypoints": waypoints,
        "legs": legs,
        "total_distance_nm": round(route.total_distance_nm(), 1),
    }


def _region_airspaces(airspace_module: Any, region: Any) -> list[Any]:
    """Tous les espaces touchant la région du viewer (plus large que le briefing)."""
    return list(airspace_module.intersecting(region))


def _ground(
    center: Any, region_radius_nm: float, airspaces: list[dict[str, Any]]
) -> dict[str, Any]:
    """Emprise carrée (en mètres, centrée) couvrant la région ET les espaces, plus
    les URLs de couches de fond prêtes à charger.

    Le sol du viewer est un carré de côté 2·half_extent_m ; une image satellite de
    la MÊME emprise (bbox lon/lat correspondant à ce carré en mètres) s'y plaque
    sans distorsion sensible à cette échelle.
    """
    lat0, lon0 = center.lat, center.lon
    cos_lat = math.cos(math.radians(lat0))
    reach_m = region_radius_nm * 1852.0
    for airspace in airspaces:
        for lon, lat in airspace["polygon"]:
            dx = (lon - lon0) * _M_PER_DEG_LAT * cos_lat
            dy = (lat - lat0) * _M_PER_DEG_LAT
            reach_m = max(reach_m, math.hypot(dx, dy))
    half_extent_m = reach_m * 1.12  # petite marge

    dlat = half_extent_m / _M_PER_DEG_LAT
    dlon = half_extent_m / (_M_PER_DEG_LAT * cos_lat)
    bbox = {
        "minLon": lon0 - dlon,
        "minLat": lat0 - dlat,
        "maxLon": lon0 + dlon,
        "maxLat": lat0 + dlat,
    }

    grid = max(1, min(_MAX_GRID, round(2 * half_extent_m / _TILE_TARGET_M)))
    layers = {
        key: {
            "label": spec["label"],
            "grid": grid,
            "tiles": _tiles(spec["url"], bbox, grid),
        }
        for key, spec in BASE_LAYERS.items()
    }
    return {"half_extent_m": round(half_extent_m), "bbox": bbox, "grid": grid, "layers": layers}


def _tiles(url_template: str, bbox: dict[str, float], grid: int) -> list[dict[str, Any]]:
    """Découpe l'emprise en grille `grid`×`grid` de tuiles, URL prête par tuile.

    gridX croît vers l'EST, gridY vers le NORD (repère bas-gauche), pour un
    placement direct du plan de sol côté 3D.
    """
    span_lon = (bbox["maxLon"] - bbox["minLon"]) / grid
    span_lat = (bbox["maxLat"] - bbox["minLat"]) / grid
    tiles: list[dict[str, Any]] = []
    for gy in range(grid):
        for gx in range(grid):
            cell = {
                "minLon": bbox["minLon"] + gx * span_lon,
                "maxLon": bbox["minLon"] + (gx + 1) * span_lon,
                "minLat": bbox["minLat"] + gy * span_lat,
                "maxLat": bbox["minLat"] + (gy + 1) * span_lat,
            }
            tiles.append({"gridX": gx, "gridY": gy, "url": url_template.format(**cell)})
    return tiles


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
