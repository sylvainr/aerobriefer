"""Rendu HTML du briefing de dégagement / déroutement.

Document imprimable A4 (déterministe, aucun JS de calcul) : un tableau de
synthèse des terrains de dégagement, puis une fiche par terrain avec le détail
piste par piste (décollage ET atterrissage), les conditions retenues et la
météo. Même famille visuelle que le briefing météo/NOTAM.
"""

from __future__ import annotations

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


def _assess_view(a: RunwayAssessment | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "runway_ident": a.runway_ident,
        "required_m": a.required_m,
        "available_m": a.available_m,
        "margin_m": a.margin_m,
        "margin_pct": a.margin_pct,
        "ok": a.ok,
        "roll_m": round(a.result.ground_roll_m),
        "over15_m": round(a.result.over_15m_m),
        "notes": list(a.result.notes),
    }


def _rwy_view(rp: RunwayPerf) -> dict[str, Any]:
    return {
        "ident": rp.ident,
        "length_m": rp.length_m,
        "surface": rp.surface,
        "is_paved": rp.is_paved,
        "headwind_kt": round(rp.headwind_kt),
        "landing": _assess_view(rp.landing),
        "takeoff": _assess_view(rp.takeoff),
    }


def _field_view(f: DiversionField) -> dict[str, Any]:
    wind = f.wind
    return {
        "icao": f.icao,
        "name": f.name,
        "distance_nm": round(f.distance_nm, 1),
        "bearing_mag": round(f.bearing_mag_deg),
        "bearing_true": round(f.bearing_true_deg),
        "from_icao": f.from_icao,
        "elevation_ft": f.elevation_ft,
        "longest_runway_m": f.longest_runway_m,
        "pa_ft": round(f.pressure_altitude_ft),
        "temp_c": round(f.temperature_c),
        "temp_is_isa": f.temp_is_isa,
        "qnh": round(f.qnh_hpa) if f.qnh_hpa is not None else None,
        "verdict": f.verdict,
        "verdict_class": _VERDICT_CLASS.get(f.verdict, "v-unk"),
        "verdict_label": _VERDICT_LABEL.get(f.verdict, f.verdict),
        "best_landing": _assess_view(f.best_landing),
        "best_takeoff": _assess_view(f.best_takeoff),
        "wind_dir": f.wind_dir_deg,
        "wind_speed": f.wind_speed_kt,
        "wind_gust": f.wind_gust_kt,
        "xw_kt": round(wind.crosswind_kt) if wind else None,
        "xw_rwy": wind.runway_ident if wind else None,
        "xw_arrow": wind.arrow if wind else None,
        "xw_from_right": wind.from_right if wind else None,
        "hw_kt": round(wind.headwind_kt) if wind else None,
        "hw_tail": wind.is_tailwind if wind else None,
        "crosswind_over_demo": f.crosswind_over_demo,
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
        "runways": [_rwy_view(r) for r in f.runways],
    }


def _view(study: DiversionStudy, now: UtcDateTime, tz: ZoneInfo, tz_name: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in study.fields:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    return {
        "doc_label": "BRIEFING DÉGAGEMENT / DÉROUTEMENT",
        "reference_icaos": list(study.reference_icaos),
        "reference_names": list(study.reference_names),
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
        "fields": [_field_view(f) for f in study.fields],
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
