"""Rendu HTML du briefing de dégagement / déroutement.

Quick-reference imprimable A4 (déterministe, aucun JS de calcul) : une carte de
situation en tête, un tableau de synthèse, puis une fiche par terrain — pistes
(pictogramme d'axe + revêtement), barres de performance déco/atterro à l'échelle
de la piste, météo, radios, lien VAC. On CHOISIT son dégagement d'un coup d'œil.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..aircraft.runway import RunwayAssessment
from ..diversion import DiversionField, DiversionStudy, RunwayPerf
from ..domain.window import UtcDateTime, utcnow
from .html import format_age, format_dual

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "diversion.html.j2"
DEFAULT_ZONE = "Europe/Paris"

_VERDICT_CLASS = {"OK": "v-ok", "SERRÉ": "v-warn", "INSUFFISANT": "v-bad", "INCONNU": "v-unk"}
_VERDICT_LABEL = {
    "OK": "Posable",
    "SERRÉ": "Serré",
    "INSUFFISANT": "Insuffisant",
    "INCONNU": "Inconnu",
}
_VERDICT_COLOR = {
    "OK": "#1f8f4e",
    "SERRÉ": "#b7791f",
    "INSUFFISANT": "#b3261e",
    "INCONNU": "#64748b",
}

_BAR_MAX_PX = 190  # largeur (px) de la plus longue piste/distance de la planche


# --- pictogramme de piste ---------------------------------------------------


def _runway_icon(is_paved: bool | None, bearing: float | None, size: int = 22) -> str:
    """Petit SVG : bande orientée selon l'axe de piste, colorée par revêtement.

    Herbe → vert ; revêtue → gris foncé + axe blanc ; inconnu → clair avec « ? ».
    """
    c = size / 2
    if bearing is None:
        return (
            f'<svg class="rwyico" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
            f'<rect x="3" y="3" width="{size - 6}" height="{size - 6}" rx="3" fill="#fff" '
            f'stroke="#b0b7c0"/><text x="{c}" y="{c + 4}" text-anchor="middle" font-size="12" '
            f'fill="#8a94a0">?</text></svg>'
        )
    if is_paved is False:
        fill, stroke, dash = "#2f8f4e", "#1f6b3a", ""
    elif is_paved:
        fill, stroke, dash = "#3b4351", "#20262f", "#ffffff"
    else:
        fill, stroke, dash = "#cfd6de", "#9aa4b0", ""
    w = 6.0
    half = (size - 4) / 2
    centerline = (
        f'<line x1="{c}" y1="{c - half + 2}" x2="{c}" y2="{c + half - 2}" stroke="{dash}" '
        f'stroke-width="1" stroke-dasharray="2 2"/>'
        if dash
        else ""
    )
    return (
        f'<svg class="rwyico" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<g transform="rotate({bearing:.0f} {c} {c})">'
        f'<rect x="{c - w / 2:.1f}" y="2" width="{w:.1f}" height="{size - 4}" rx="1.5" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="0.8"/>{centerline}</g></svg>'
    )


# --- barre de performance ---------------------------------------------------


def _band(a: RunwayAssessment) -> str:
    if not a.ok:
        return "bad"
    return "tight" if a.margin_pct < 20 else "ok"


def _perf_bar(a: RunwayAssessment | None, length_m: int, scale: float) -> str:
    """Barre à l'échelle : longueur = piste, remplissage coloré = distance requise.

    Si le requis dépasse la piste, la barre déborde à droite (rouge) : « ne rentre
    pas » se voit immédiatement.
    """
    if a is None or length_m <= 0:
        return '<span class="muted small">—</span>'
    track = max(2, round(length_m * scale))
    need = max(2, round(a.required_m * scale))
    total = max(track, need)
    return (
        f'<span class="pbar" style="width:{total}px">'
        f'<span class="pbar-rwy" style="width:{track}px"></span>'
        f'<span class="pbar-need b-{_band(a)}" style="width:{need}px"></span>'
        f"</span>"
    )


# --- carte de situation (SVG) -----------------------------------------------

_MAP_W, _MAP_H, _MAP_PAD = 600, 300, 30


def _merc_world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Coordonnées « pixel monde » Web Mercator (EPSG:3857) au zoom `z` — le même
    repère que les tuiles OSM (256 px/tuile). Indispensable pour poser un fond de
    tuiles ET les vecteurs dans le MÊME système : sinon la route flotte à côté de
    la carte."""
    n = 256.0 * (2**z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    s = min(max(s, -0.9999), 0.9999)
    y = (0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)) * n
    return x, y


def _map_svg(study: DiversionStudy) -> str:
    points: list[tuple[float, float]] = [(la, lo) for _, la, lo in study.reference_points]
    points += list(study.route_path)
    points += [(f.lat, f.lon) for f in study.fields]
    if len(points) < 2:
        return ""

    lats = [la for la, _ in points]
    lons = [lo for _, lo in points]
    minlat, maxlat, minlon, maxlon = min(lats), max(lats), min(lons), max(lons)
    lat0 = sum(lats) / len(lats)

    # Zoom natif = le plus grand où l'emprise tient dans la fenêtre sans agrandir
    # les tuiles ; on ré-étire ensuite (facteur < 2) pour REMPLIR le cadre. Sans ce
    # ré-étirement, un pas de zoom OSM (×2) laisse la route minuscule au centre dès
    # que l'emprise dépasse d'un cheveu le zoom supérieur.
    avail_w, avail_h = _MAP_W - 2 * _MAP_PAD, _MAP_H - 2 * _MAP_PAD
    # On autorise l'emprise à dépasser un peu la fenêtre au moment de CHOISIR le
    # zoom : sinon un dépassement d'un cheveu au zoom supérieur nous coince au zoom
    # inférieur et force un ré-étirement ×2 (tuiles floues). Le facteur `s` ci-dessous,
    # lui, est calculé sur la surface RÉELLE : il redimensionne pile, sans débord.
    tol = 1.25
    z = 1
    for zz in range(1, 17):  # 16 = plafond : évite de sur-zoomer une emprise minuscule
        xa, ya = _merc_world_px(maxlat, minlon, zz)  # coin haut-gauche
        xb, yb = _merc_world_px(minlat, maxlon, zz)  # coin bas-droite
        if (xb - xa) <= avail_w * tol and (yb - ya) <= avail_h * tol:
            z = zz
        else:
            break

    world = [_merc_world_px(la, lo, z) for la, lo in points]
    cminx, cmaxx = min(w[0] for w in world), max(w[0] for w in world)
    cminy, cmaxy = min(w[1] for w in world), max(w[1] for w in world)
    bbox_w = (cmaxx - cminx) or 1.0
    bbox_h = (cmaxy - cminy) or 1.0
    s = min(avail_w / bbox_w, avail_h / bbox_h)  # ré-étirement (∈ [1, 2))
    off_x = _MAP_PAD + (avail_w - bbox_w * s) / 2.0
    off_y = _MAP_PAD + (avail_h - bbox_h * s) / 2.0

    def px(la: float, lo: float) -> tuple[float, float]:
        wx, wy = _merc_world_px(la, lo, z)
        return (off_x + (wx - cminx) * s, off_y + (wy - cminy) * s)

    parts = [
        f'<svg class="sitmap" width="100%" viewBox="0 0 {_MAP_W} {_MAP_H}" '
        f'preserveAspectRatio="xMidYMid meet" role="img">',
        # Repli sous les tuiles : si le réseau manque (impression, vol), le fond
        # gris clair garde la carte lisible plutôt qu'un rectangle blanc.
        f'<defs><clipPath id="mapclip"><rect x="0" y="0" width="{_MAP_W}" height="{_MAP_H}" '
        f'rx="6"/></clipPath></defs>',
        f'<rect x="0" y="0" width="{_MAP_W}" height="{_MAP_H}" fill="#eef1f4" rx="6"/>',
    ]

    # Fond OSM : tuiles chargées EN DIRECT depuis le serveur (léger, mais vide hors
    # ligne). Attribution « © OpenStreetMap » obligatoire (voir plus bas).
    tile_parts: list[str] = ['<g clip-path="url(#mapclip)">']
    n_tiles = 2**z
    tsz = 256.0 * s
    # Coins de la fenêtre visible → coordonnées monde, pour couvrir tout le cadre.
    w_left, w_right = cminx - off_x / s, cminx + (_MAP_W - off_x) / s
    w_top, w_bottom = cminy - off_y / s, cminy + (_MAP_H - off_y) / s
    tx0, tx1 = int(w_left // 256), int(w_right // 256)
    ty0, ty1 = int(w_top // 256), int(w_bottom // 256)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if not (0 <= tx < n_tiles and 0 <= ty < n_tiles):
                continue
            sx = off_x + (tx * 256 - cminx) * s
            sy = off_y + (ty * 256 - cminy) * s
            tile_parts.append(
                f'<image href="https://tile.openstreetmap.org/{z}/{tx}/{ty}.png" '
                f'x="{sx:.1f}" y="{sy:.1f}" width="{tsz:.1f}" height="{tsz:.1f}"/>'
            )
    tile_parts.append("</g>")
    parts.extend(tile_parts)
    parts.append(
        f'<rect x="0" y="0" width="{_MAP_W}" height="{_MAP_H}" fill="none" '
        f'stroke="#d3dae2" rx="6"/>'
    )

    if study.is_route and len(study.route_path) >= 2:
        line = " ".join(f"{px(la, lo)[0]:.1f},{px(la, lo)[1]:.1f}" for la, lo in study.route_path)
        # Double trait : liseré blanc dessous pour ressortir sur les tuiles colorées.
        parts.append(f'<polyline points="{line}" fill="none" stroke="#fff" stroke-width="4.5"/>')
        parts.append(f'<polyline points="{line}" fill="none" stroke="#ff5a1f" stroke-width="2.4"/>')
    elif study.reference_points:
        # Vol local : cercle du rayon de recherche autour du terrain.
        _icao, la, lo = study.reference_points[0]
        cx, cy = px(la, lo)
        ground_res = 156543.03392 * math.cos(math.radians(lat0)) / (2**z)  # m/px natif
        r_px = (study.radius_nm * 1852.0) / ground_res * s
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r_px:.1f}" fill="none" stroke="#12232e" '
            f'stroke-width="1.4" stroke-dasharray="4 3" opacity="0.7"/>'
        )

    # Halo blanc sous tous les libellés : lisibles par-dessus n'importe quelle tuile.
    halo = 'stroke="#fff" stroke-width="2.5" paint-order="stroke"'
    for f in study.fields:
        cx, cy = px(f.lat, f.lon)
        color = _VERDICT_COLOR.get(f.verdict, "#64748b")
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{color}" stroke="#fff" '
            f'stroke-width="1.4"/>'
            f'<text x="{cx:.1f}" y="{cy - 7:.1f}" text-anchor="middle" font-size="9" '
            f'font-family="monospace" fill="#12232e" {halo}>{f.icao}</text>'
        )

    for icao, la, lo in study.reference_points:
        cx, cy = px(la, lo)
        parts.append(
            f'<rect x="{cx - 4:.1f}" y="{cy - 4:.1f}" width="8" height="8" fill="#12232e" '
            f'stroke="#fff" stroke-width="1.4"/>'
            f'<text x="{cx:.1f}" y="{cy + 15:.1f}" text-anchor="middle" font-size="9.5" '
            f'font-family="monospace" font-weight="700" fill="#12232e" {halo}>{icao}</text>'
        )

    # Flèche du nord (haut-gauche) — Mercator : le nord est en haut.
    parts.append(
        '<g transform="translate(22 26)">'
        '<line x1="0" y1="10" x2="0" y2="-10" stroke="#12232e" stroke-width="1.4"/>'
        '<path d="M0,-12 L3,-6 L-3,-6 Z" fill="#12232e"/>'
        f'<text x="0" y="22" text-anchor="middle" font-size="9" fill="#12232e" {halo}>N</text></g>'
    )
    # Attribution OSM — exigée par la licence (ODbL) dès qu'on affiche les tuiles.
    parts.append(
        f'<text x="{_MAP_W - 5}" y="{_MAP_H - 5}" text-anchor="end" font-size="7.5" '
        f'fill="#333" {halo}>© OpenStreetMap contributors</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --- vues ------------------------------------------------------------------


def _assess_view(a: RunwayAssessment | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "runway_ident": a.runway_ident,
        "required_m": a.required_m,
        "available_m": a.available_m,
        "margin_pct": a.margin_pct,
        "ok": a.ok,
        "band": _band(a),
        "roll_m": round(a.result.ground_roll_m),
        "over15_m": round(a.result.over_15m_m),
    }


def _rwy_view(rp: RunwayPerf, scale: float) -> dict[str, Any]:
    return {
        "ident": rp.ident,
        "length_m": rp.length_m,
        "surface_label": ("herbe" if rp.is_paved is False else (rp.surface or "?")),
        "icon": _runway_icon(rp.is_paved, rp.true_bearing_deg),
        "headwind_kt": round(rp.headwind_kt),
        "landing": _assess_view(rp.landing),
        "takeoff": _assess_view(rp.takeoff),
        "landing_bar": _perf_bar(rp.landing, rp.length_m, scale),
        "takeoff_bar": _perf_bar(rp.takeoff, rp.length_m, scale),
    }


def _primary_runway(f: DiversionField) -> RunwayPerf | None:
    """Piste « principale » du terrain (la plus longue) pour le pictogramme de synthèse."""
    if not f.runways:
        return None
    return max(f.runways, key=lambda r: r.length_m)


def _field_view(f: DiversionField, scale: float) -> dict[str, Any]:
    wind = f.wind
    has_xwind = bool(wind and f.wind_speed_kt)
    primary = _primary_runway(f)
    return {
        "icao": f.icao,
        "name": f.name,
        "distance_nm": round(f.distance_nm, 1),
        "bearing_mag": round(f.bearing_mag_deg),
        "bearing_true": round(f.bearing_true_deg),
        "from_label": f.from_label,
        "elevation_ft": f.elevation_ft,
        "longest_runway_m": f.longest_runway_m,
        "primary_icon": _runway_icon(primary.is_paved, primary.true_bearing_deg) if primary else "",
        "pa_ft": round(f.pressure_altitude_ft),
        "temp_c": round(f.temperature_c),
        "temp_is_isa": f.temp_is_isa,
        "qnh": round(f.qnh_hpa) if f.qnh_hpa is not None else None,
        "verdict": f.verdict,
        "verdict_class": _VERDICT_CLASS.get(f.verdict, "v-unk"),
        "verdict_label": _VERDICT_LABEL.get(f.verdict, f.verdict),
        "best_landing": _assess_view(f.best_landing),
        "best_takeoff": _assess_view(f.best_takeoff),
        "best_landing_bar": _best_perf_bar(f, scale, landing=True),
        "best_takeoff_bar": _best_perf_bar(f, scale, landing=False),
        "wind_dir": f.wind_dir_deg,
        "wind_speed": f.wind_speed_kt,
        "wind_gust": f.wind_gust_kt,
        "has_xwind": has_xwind,
        "xw_kt": round(wind.crosswind_kt) if wind else None,
        "xw_rwy": wind.runway_ident if wind else None,
        "xw_arrow": wind.arrow if wind else None,
        "xw_side": ("droite" if wind and wind.from_right else "gauche") if wind else None,
        "crosswind_over_demo": f.crosswind_over_demo,
        "frequencies": [f"{kind} {mhz}" for kind, mhz in f.frequencies],
        "flight_category": f.flight_category,
        "metar_station": f.metar_station,
        "metar_is_proxy": f.metar_is_proxy,
        "metar_distance_nm": round(f.metar_distance_nm) if f.metar_distance_nm else None,
        "metar_age": format_age(f.metar_age_min) if f.metar_age_min is not None else None,
        "metar_raw": f.metar_raw,
        "taf_station": f.taf_station,
        "taf_is_proxy": f.taf_is_proxy,
        "taf_raw": f.taf_raw,
        "vac_url": f.vac_url,
        "runways": [_rwy_view(r, scale) for r in f.runways],
    }


def _best_perf_bar(f: DiversionField, scale: float, *, landing: bool) -> str:
    """Barre de la meilleure piste (pour la synthèse), à l'échelle de cette piste."""
    assessment = f.best_landing if landing else f.best_takeoff
    if assessment is None:
        return '<span class="muted small">—</span>'
    return _perf_bar(assessment, assessment.available_m, scale)


def _scale_for(study: DiversionStudy) -> float:
    longest = 1.0
    for f in study.fields:
        for r in f.runways:
            longest = max(longest, r.length_m)
            if r.landing:
                longest = max(longest, r.landing.required_m)
            if r.takeoff:
                longest = max(longest, r.takeoff.required_m)
    return _BAR_MAX_PX / longest


def _view(study: DiversionStudy, now: UtcDateTime, tz: ZoneInfo, tz_name: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in study.fields:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    scale = _scale_for(study)
    return {
        "doc_label": "BRIEFING DÉGAGEMENT / DÉROUTEMENT",
        "reference_icaos": list(study.reference_icaos),
        "reference_names": list(study.reference_names),
        "is_route": study.is_route,
        "aircraft_name": study.aircraft_name,
        "registration": study.registration,
        "radius_nm": round(study.radius_nm),
        "safety_factor": study.safety_factor,
        "demo_xw": round(study.demonstrated_crosswind_kt)
        if study.demonstrated_crosswind_kt
        else None,
        "variation_deg": round(study.variation_deg, 1),
        "generated_dual": format_dual(now, tz, with_date=True),
        "display_timezone": tz_name,
        "notes": list(study.notes),
        "counts": counts,
        "map_svg": _map_svg(study),
        "fields": [_field_view(f, scale) for f in study.fields],
    }


def render_diversion_html(
    study: DiversionStudy,
    *,
    now: UtcDateTime | None = None,
    display_timezone: str = DEFAULT_ZONE,
) -> str:
    """Étude de dégagement → page HTML autonome et imprimable."""
    now = now or utcnow()
    tz = ZoneInfo(display_timezone)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        undefined=StrictUndefined,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(view=_view(study, now, tz, display_timezone))
