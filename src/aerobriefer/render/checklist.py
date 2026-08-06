"""Checklist de préparation de navigation — HTML A4 portrait → PDF imprimable.

Une feuille à cocher : les étapes standard d'une prépa VFR, avec les spécificités
de LA nav pré-remplies en tête (route, date, coucher du soleil, carburant mini,
terrains dont il faut la VAC). Le pilote coche à la main.

Contenu volontairement STANDARD et stable : c'est une base que l'utilisateur
ajuste à sa main. Le rendu injecte seulement l'entête spécifique au vol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..domain.window import UtcDateTime, utcnow

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "checklist.html.j2"
DEFAULT_DISPLAY_TIMEZONE = "Europe/Paris"


def render_checklist_html(
    *,
    origin: str,
    destination: str,
    date_label: str,
    aircraft_name: str,
    sunset: str | None,
    night: str | None,
    fuel_min_l: str | None,
    vac_icaos: list[str],
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    now: UtcDateTime | None = None,
) -> str:
    tz = ZoneInfo(display_timezone)
    moment = now if now is not None else utcnow()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)), undefined=StrictUndefined, autoescape=True
    )
    view: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "date_label": date_label,
        "aircraft_name": aircraft_name,
        "sunset": sunset,
        "night": night,
        "fuel_min_l": fuel_min_l,
        "vac_icaos": vac_icaos,
        "display_timezone": display_timezone,
        "generated": f"{moment.astimezone(tz):%d/%m/%Y %H:%M}",
    }
    return env.get_template(TEMPLATE_NAME).render(**view)


def render_checklist_pdf(
    output_path: Path | str, *, chrome_path: str | None = None, **meta: Any
) -> Path:
    """Checklist → PDF A4 portrait prêt à imprimer."""
    from .pdf import PdfRenderer

    html = render_checklist_html(**meta)
    return PdfRenderer(chrome_path=chrome_path).html_to_pdf(html, output_path)
