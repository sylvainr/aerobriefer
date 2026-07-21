"""HTML → PDF via Chrome headless.

Chrome plutôt qu'une bibliothèque Python : le moteur d'impression est le même
que celui qui a servi à régler la feuille, donc ce qu'on relit à l'écran est ce
qui sortira de l'imprimante. Les moteurs alternatifs (WeasyPrint, wkhtmltopdf)
divergent sur `break-inside` et sur les data: URI volumineuses, précisément les
deux points sur lesquels ce rendu ne peut pas se permettre de surprise.

Contrainte non négociable : le PDF doit être exploitable HORS LIGNE, sans
réseau ni cache. Les images sont donc embarquées en data: URI par le renderer
HTML, et Chrome est lancé sans accès distant.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.package import BriefingPackage
from ..domain.window import UtcDateTime
from .html import DEFAULT_DISPLAY_TIMEZONE, DEFAULT_STALE_AFTER_MINUTES, HtmlRenderer

DEFAULT_CHROME_PATHS: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

DEFAULT_TIMEOUT_SECONDS = 120.0

PDF_MAGIC = b"%PDF-"


class PdfRenderError(RuntimeError):
    """Chrome n'a pas produit de PDF exploitable. Jamais silencieux : un dossier
    de vol qui échoue à s'imprimer doit le dire bruyamment."""


def find_chrome(candidates: Sequence[str] = DEFAULT_CHROME_PATHS) -> str:
    """Premier navigateur utilisable, chemin absolu ou binaire du PATH."""
    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    raise PdfRenderError("Aucun Chrome/Chromium trouvé. Chemins essayés : " + ", ".join(candidates))


def count_pdf_pages(pdf_bytes: bytes) -> int:
    """Nombre de pages, lu sans dépendance externe.

    On compte les objets `/Type /Page` (en excluant `/Pages`, le nœud d'arbre).
    Suffisant pour la vérification qui nous intéresse — « le PDF est-il non
    vide et plausible » — et sans embarquer un parseur PDF complet.
    """
    count = 0
    index = 0
    while True:
        index = pdf_bytes.find(b"/Type", index)
        if index == -1:
            break
        index += len(b"/Type")
        rest = pdf_bytes[index : index + 12].lstrip(b" \r\n\t")
        if rest.startswith(b"/Page") and not rest.startswith(b"/Pages"):
            count += 1
    return count


@dataclass(frozen=True, slots=True)
class PdfRenderer:
    """HTML → PDF. Ne connaît rien du domaine au-delà du `BriefingPackage`."""

    chrome_path: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    extra_args: Sequence[str] = field(default_factory=tuple)

    def _binary(self) -> str:
        return self.chrome_path or find_chrome()

    def html_to_pdf(self, html: str, output_path: Path | str) -> Path:
        """Écrit le PDF et vérifie POUR DE VRAI qu'il est exploitable."""
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="aerobriefer-pdf-") as tmp:
            tmp_dir = Path(tmp)
            source = tmp_dir / "briefing.html"
            source.write_text(html, encoding="utf-8")

            command = [
                self._binary(),
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--no-pdf-header-footer",
                # PAS de --user-data-dir : sur Chrome 150 headless, un profil
                # neuf fait écrire le PDF puis laisse le processus vivant
                # indéfiniment. Le rendu « réussissait » en apparence tout en
                # bloquant l'appelant jusqu'au timeout. Le headless moderne
                # isole déjà son profil, l'option n'apportait rien.
                #
                # Le dossier doit être imprimable hors ligne : rien ne doit
                # partir sur le réseau pendant le rendu.
                "--disable-extensions",
                "--disable-background-networking",
                "--no-first-run",
                "--virtual-time-budget=5000",
                *self.extra_args,
                f"--print-to-pdf={output}",
                source.as_uri(),
            ]

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PdfRenderError(
                    f"Chrome n'a pas rendu le PDF en {self.timeout_seconds:g} s"
                ) from exc

        if not output.exists():
            raise PdfRenderError(
                "Chrome n'a produit aucun fichier "
                f"(code {completed.returncode}) : "
                f"{completed.stderr.decode('utf-8', 'replace')[-800:]}"
            )

        payload = output.read_bytes()
        if not payload.startswith(PDF_MAGIC):
            raise PdfRenderError(f"Le fichier produit n'est pas un PDF : {output}")
        if len(payload) < 1000:
            raise PdfRenderError(f"PDF suspicieusement vide ({len(payload)} octets) : {output}")

        return output

    def render(
        self,
        package: BriefingPackage,
        output_path: Path | str,
        *,
        now: UtcDateTime | None = None,
        display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
        stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
    ) -> Path:
        """`BriefingPackage` → PDF A4 prêt à imprimer."""
        html = HtmlRenderer(
            display_timezone=display_timezone,
            stale_after_minutes=stale_after_minutes,
        ).render(package, now=now)
        return self.html_to_pdf(html, output_path)


def render_pdf(
    package: BriefingPackage,
    output_path: Path | str,
    *,
    now: UtcDateTime | None = None,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
    chrome_path: str | None = None,
) -> Path:
    """Raccourci fonctionnel pour le cas courant."""
    return PdfRenderer(chrome_path=chrome_path).render(
        package,
        output_path,
        now=now,
        display_timezone=display_timezone,
        stale_after_minutes=stale_after_minutes,
    )


__all__ = [
    "DEFAULT_CHROME_PATHS",
    "PdfRenderError",
    "PdfRenderer",
    "count_pdf_pages",
    "find_chrome",
    "render_pdf",
]
