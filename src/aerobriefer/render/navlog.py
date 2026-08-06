"""Rendu du log de navigation : `NavLog` → HTML A4 paysage → PDF imprimable.

Même chaîne que le briefing : un gabarit Jinja produit un HTML autosuffisant, que
Chrome headless transforme en PDF (moteur d'impression identique à l'écran). Le
navlog est fait pour être IMPRIMÉ et emporté — d'où le format paysage et une
table dense et lisible.
"""

from __future__ import annotations

from datetime import timedelta  # noqa: TID251 - timedelta est une durée, pas un instant
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..domain.navlog import LegComputation, NavLog
from ..domain.window import UtcDateTime, utcnow

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "navlog.html.j2"
DEFAULT_DISPLAY_TIMEZONE = "Europe/Paris"


def _dual(instant: UtcDateTime, tz: ZoneInfo, *, with_date: bool = False) -> str:
    fmt = "%d/%m %H:%M" if with_date else "%H:%M"
    local = instant.astimezone(tz)
    return f"{local.strftime(fmt)} ({instant:%H:%M}Z)"


def _duration(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


def _mag_var_label(magnetic_variation_deg: float) -> str:
    if magnetic_variation_deg == 0.0:
        return "0° (à régler)"
    hemisphere = "E" if magnetic_variation_deg > 0 else "W"
    return f"{abs(magnetic_variation_deg):.0f}°{hemisphere}"


def _wind_label(leg: LegComputation) -> str:
    if leg.wind.speed_kt < 1.0:
        return "calme"
    return f"{leg.wind.from_deg:03.0f}°/{leg.wind.speed_kt:.0f}"


def _drift_label(wind_correction_deg: float) -> str:
    value = round(wind_correction_deg)
    if value == 0:
        return "0°"
    return f"{value:+d}° {'▶' if value > 0 else '◀'}"


def _rows(
    navlog: NavLog,
    tz: ZoneInfo,
    annotations: list[Any] | None,
    safety_ft: list[float] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = navlog.total_distance_nm
    cumulative = 0.0
    cumulative_fuel = 0.0
    for index, leg in enumerate(navlog.legs):
        cumulative += leg.distance_nm
        if leg.fuel_l is not None:
            cumulative_fuel += leg.fuel_l
        note = annotations[index] if annotations and index < len(annotations) else None
        zmin = safety_ft[index] if safety_ft and index < len(safety_ft) else None
        rows.append(
            _row(
                leg,
                tz,
                cumulative,
                remaining_nm=total - cumulative,
                cumulative_fuel=cumulative_fuel,
                note=note,
                zmin_ft=zmin,
            )
        )
    return rows


def _minutes(hours: float) -> str:
    return f"{round(hours * 60)}"


def _row(
    leg: LegComputation,
    tz: ZoneInfo,
    cumulative_nm: float,
    *,
    remaining_nm: float,
    cumulative_fuel: float,
    note: Any | None,
    zmin_ft: float | None,
) -> dict[str, Any]:
    local = leg.eta.astimezone(tz)
    tsv_h = leg.distance_nm / leg.tas_kt if leg.tas_kt else 0.0
    return {
        "branch": f"{leg.leg.start.name} → {leg.leg.end.name}",
        "altitude": f"{leg.altitude_ft:.0f}" if leg.altitude_ft is not None else "—",
        "zmin": f"{zmin_ft:.0f}" if zmin_ft is not None else None,
        "true_track": f"{leg.true_track_deg:03.0f}",
        "magnetic_track": f"{leg.magnetic_track_deg:03.0f}",
        "magnetic_heading": f"{leg.magnetic_heading_deg:03.0f}",
        "wind": _wind_label(leg),
        "drift": _drift_label(leg.wind_correction_deg),
        "distance": f"{leg.distance_nm:.1f}",
        "cumulative": f"{cumulative_nm:.1f}",
        "dtg": f"{max(0.0, remaining_nm):.1f}",
        "ground_speed": f"{leg.ground_speed_kt:.0f}",
        "tsv": _minutes(tsv_h),  # temps sans vent
        "tav": _minutes(leg.time.total_seconds() / 3600.0),  # temps avec vent
        "eto_local": local.strftime("%H:%M"),
        "eto_zulu": f"{leg.eta:%H:%M}Z",
        "fuel": f"{leg.fuel_l:.1f}" if leg.fuel_l is not None else "—",
        "fuel_cumulative": f"{cumulative_fuel:.1f}" if leg.fuel_l is not None else "—",
        # Auto-remplis depuis la donnée de référence.
        "vor": getattr(note, "vor", None),
        "radios": getattr(note, "radios", None),
        "diversion": getattr(note, "diversion", None),
    }


def _fuel_view(plan: Any | None) -> dict[str, Any] | None:
    if plan is None:
        return None

    def endurance() -> str:
        minutes = round(plan.endurance_min)
        return f"{minutes // 60} h {minutes % 60:02d}"

    return {
        "taxi": f"{plan.taxi_l:.1f}",
        "trip": f"{plan.trip_l:.1f}",
        "alternate": f"{plan.alternate_l:.1f}",
        "reserve": f"{plan.reserve_l:.1f}",
        "total": f"{plan.total_l:.1f}",
        "capacity": f"{plan.capacity_l:.0f}",
        "margin": f"{plan.margin_l:.1f}",
        "sufficient": plan.sufficient,
        "endurance": endurance(),
    }


def render_navlog_html(
    navlog: NavLog,
    *,
    origin: str,
    destination: str,
    aircraft_name: str,
    tas_kt: float,
    fuel_flow_lph: float | None,
    magnetic_variation_deg: float = 0.0,
    annotations: list[Any] | None = None,
    vac_links: list[Any] | None = None,
    safety_ft: list[float] | None = None,
    fuel_plan: Any | None = None,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    now: UtcDateTime | None = None,
) -> str:
    """Log de navigation en HTML A4 portrait autosuffisant."""
    tz = ZoneInfo(display_timezone)
    moment = now if now is not None else utcnow()
    total_fuel = navlog.total_fuel_l

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    view: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "aircraft_name": aircraft_name,
        "tas_kt": f"{tas_kt:.0f}",
        "fuel_flow_lph": f"{fuel_flow_lph:.0f}" if fuel_flow_lph is not None else "—",
        "mag_var_label": _mag_var_label(magnetic_variation_deg),
        "departure_dual": _dual(navlog.departure_time, tz, with_date=True),
        "arrival_dual": _dual(navlog.eta_arrival, tz, with_date=True),
        "arrival_local": _dual(navlog.eta_arrival, tz),
        "rows": _rows(navlog, tz, annotations, safety_ft),
        "vac_links": [{"icao": v.icao, "name": v.name, "url": v.url} for v in (vac_links or [])],
        "fuel_plan": _fuel_view(fuel_plan),
        "total_distance": f"{navlog.total_distance_nm:.1f}",
        "total_time": _duration(navlog.total_time),
        "total_fuel": f"{total_fuel:.1f} L" if total_fuel is not None else "—",
        "display_timezone": display_timezone,
        "generated_dual": _dual(moment, tz, with_date=True),
        "page_footer": (
            f"NAVLOG {origin} → {destination} — départ {navlog.departure_time:%d/%m %H:%M}Z"
        ),
    }
    return env.get_template(TEMPLATE_NAME).render(**view)


def render_navlog_pdf(
    navlog: NavLog,
    output_path: Path | str,
    *,
    chrome_path: str | None = None,
    **meta: Any,
) -> Path:
    """`NavLog` → PDF A4 paysage prêt à imprimer (via Chrome headless)."""
    from .pdf import PdfRenderer

    html = render_navlog_html(navlog, **meta)
    return PdfRenderer(chrome_path=chrome_path).html_to_pdf(html, output_path)
