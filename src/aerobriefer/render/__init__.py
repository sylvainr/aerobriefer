"""Rendu déterministe du `BriefingPackage` : HTML A4 imprimable, puis PDF.

Aucun LLM dans la boucle, aucun appel réseau, aucun rappel de provider : tout
ce qui est affiché provient du dossier passé en argument.
"""

from .html import (
    DEFAULT_DISPLAY_TIMEZONE,
    DEFAULT_STALE_AFTER_MINUTES,
    HtmlRenderer,
    render_html,
)
from .pdf import PdfRenderer, PdfRenderError, render_pdf

__all__ = [
    "DEFAULT_DISPLAY_TIMEZONE",
    "DEFAULT_STALE_AFTER_MINUTES",
    "HtmlRenderer",
    "PdfRenderError",
    "PdfRenderer",
    "render_html",
    "render_pdf",
]
