"""Index du « Complément aux cartes aéronautiques » (SIA).

Le complément est un PDF d'environ 270 pages publié par édition (cycle AIRAC).
Il porte la fiche de CHAQUE zone P/R/D : plafond, plancher, horaires
d'activation, conditions de pénétration. C'est de la donnée du briefing LONG —
celle qu'on consulte à J-15, pas à H-1 — et son cycle de vie est l'ÉDITION,
pas la minute. Elle ne doit donc jamais hériter de l'affichage de fraîcheur
(âges, badges « PÉRIMÉ ») réservé aux METAR, TAF et NOTAM.

Ce module construit un INDEX `identifiant de zone → page(s) du PDF`, écrit à
côté du PDF sous forme de fichier compagnon `<nom>.pdf.index.json`. L'index se
périme ainsi AVEC son PDF : nouvelle édition = nouveau fichier = nouvel index,
et l'appariement reste évident à l'œil dans le répertoire. Pas de cache central
qui dériverait en silence par rapport au document qu'il indexe.

DEUX PIÈGES, tous deux vérifiés sur le document réel :

1. **Le folio imprimé n'est pas le numéro de page du fichier.** Les pages de
   couverture décalent les deux. Or le fragment `#page=N` d'un lien adresse la
   page du FICHIER. On ne stocke donc que la page PDF, jamais le folio.
2. **Le complément écrit `LF R 49 L1`, les NOTAM écrivent `LF-R49L1`.** Toute
   comparaison passe par `normalize_zone_id`, qui réduit les deux à `LFR49L1`.

Le PDF n'est lu QU'ICI, hors de la chaîne de rendu : un briefing ne parse jamais
un PDF, il lit le JSON déjà construit. C'est ce qui garde le rendu déterministe
et sans dépendance lourde.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

INDEX_SUFFIX = ".index.json"

#: Mois anglais des dates de couverture du SIA (« 16 APR 2026 »).
_MONTHS: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip

#: En-tête de fiche de zone tel que le PDF le rend une fois les colonnes
#: fusionnées : « Plafond / Upper limit FL 195LF R 49 H2 COGNAC Plancher /
#: Lower limit FL 065 ». Le nom est optionnel — « LF R 4 A » n'en a pas.
_ZONE_FULL = re.compile(
    r"Plafond\s*/\s*Upper\s+limit\s*(?P<haut>.*?)"
    r"LF\s+(?P<type>[PRD])\s+(?P<num>\d{1,3})"
    r"(?:\s+(?P<suffixe>[A-Z]{1,2}\d{0,2})\b)?"
    r"(?:\s+(?P<nom>.*?))??"
    r"\s*Plancher\s*/\s*Lower\s+limit\s*(?P<bas>.*?)\s*$"
)

#: Repli : la même fiche quand la mise en page isole l'en-tête sur sa ligne.
_ZONE_BARE = re.compile(
    r"^LF\s+(?P<type>[PRD])\s+(?P<num>\d{1,3})"
    r"(?:\s+(?P<suffixe>[A-Z]{1,2}\d{0,2})\b)?"
    r"(?:\s+(?P<nom>\S.*?))?\s*$"
)

_EN_VIGUEUR = re.compile(r"en\s+vigueur\s*:?\s*(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", re.IGNORECASE)
_EDITION = re.compile(r"(\d+\s*(?:ère|ere|ème|eme|e))\s*[ÉEe]dition\s*(\d{4})", re.IGNORECASE)


class ZoneEntry(BaseModel):
    """Une zone P/R/D et l'endroit où la lire dans le complément."""

    identifier: str
    """Forme normalisée, comparable à celle d'un NOTAM (ex. « LFR49L1 »)."""
    label: str
    """Libellé tel qu'imprimé (ex. « LF R 49 L1 COGNAC ») — jamais reconstruit."""
    name: str = ""
    upper_limit: str = ""
    lower_limit: str = ""
    pages: list[int] = Field(default_factory=list)
    """Pages DU FICHIER PDF, croissantes. C'est ce qu'attend `#page=N`."""


class DocumentInfo(BaseModel):
    """Identité de l'édition indexée — pour qu'un index ne puisse pas être
    confondu avec celui d'une autre édition."""

    title: str = "Complément aux cartes aéronautiques"
    edition: str = ""
    effective_date: str = ""
    """Date d'entrée en vigueur, ISO (« 2026-04-16 »), vide si illisible."""
    effective_raw: str = ""
    filename: str = ""
    page_count: int = 0
    sha256: str = ""


class ComplementIndex(BaseModel):
    """Index complet, sérialisé dans le fichier compagnon."""

    document: DocumentInfo
    zones: dict[str, ZoneEntry] = Field(default_factory=dict)

    def lookup(self, raw_identifier: str) -> ZoneEntry | None:
        """Retrouve une zone depuis n'importe quelle écriture de son
        identifiant (`LF-R49L1`, `LF R 49 L1`, `lfr49l1`…)."""
        return self.zones.get(normalize_zone_id(raw_identifier))


def normalize_zone_id(raw: str) -> str:
    """Réduit un identifiant de zone à sa forme comparable.

    Le complément imprime « LF R 49 L1 », les NOTAM écrivent « LF-R49L1 » ou
    « LFR49L1 ». Sans cette réduction, aucun des trois ne se retrouve.
    """
    return re.sub(r"[^0-9A-Za-z]", "", raw).upper()


def index_path_for(pdf_path: Path) -> Path:
    """Chemin du fichier compagnon : `complement.pdf` → `complement.pdf.index.json`.

    L'extension `.json` vient EN DERNIER pour que les outils la reconnaissent ;
    `.index` au milieu dit ce que le fichier est."""
    return pdf_path.with_name(pdf_path.name + INDEX_SUFFIX)


def parse_pages(pages: Sequence[str]) -> dict[str, ZoneEntry]:
    """Extrait les zones du texte page par page. FONCTION PURE.

    `pages[i]` est le texte de la page PDF `i + 1` — c'est cette numérotation,
    celle du fichier, qui est stockée. Aucune E/S : c'est ici que se teste le
    parsing, sans avoir à fabriquer un PDF.
    """
    zones: dict[str, ZoneEntry] = {}
    for offset, text in enumerate(pages):
        page_number = offset + 1
        for line in text.splitlines():
            line = line.strip()
            if "LF" not in line:
                continue
            match = _ZONE_FULL.search(line) or _ZONE_BARE.match(line)
            if match is None:
                continue
            _record(zones, match, page_number)
    for entry in zones.values():
        entry.pages.sort()
    return dict(sorted(zones.items()))


def _record(zones: dict[str, ZoneEntry], match: re.Match[str], page_number: int) -> None:
    groups = match.groupdict()
    suffix = (groups.get("suffixe") or "").strip()
    identifier = f"LF{groups['type']}{groups['num']}{suffix}"
    label = " ".join(
        part
        for part in ("LF", groups["type"], groups["num"], suffix, (groups.get("nom") or "").strip())
        if part
    )
    entry = zones.get(identifier)
    if entry is None:
        entry = ZoneEntry(
            identifier=identifier,
            label=label,
            name=(groups.get("nom") or "").strip(),
            upper_limit=(groups.get("haut") or "").strip(),
            lower_limit=(groups.get("bas") or "").strip(),
        )
        zones[identifier] = entry
    if page_number not in entry.pages:
        entry.pages.append(page_number)


def parse_document_info(cover: str) -> tuple[str, str, str]:
    """Édition, date ISO et date brute, lues sur la page de couverture.

    Renvoie des chaînes vides plutôt que d'inventer : un index qui ment sur son
    édition est pire qu'un index qui l'ignore.
    """
    edition = ""
    if (m := _EDITION.search(cover)) is not None:
        # L'ordinal est repris TEL QU'IMPRIMÉ (« 1ère »), pas reconstruit.
        edition = f"{' '.join(m.group(1).split())} édition {m.group(2)}"
    iso = raw = ""
    if (m := _EN_VIGUEUR.search(cover)) is not None:
        day, month, year = m.group(1), m.group(2).upper(), m.group(3)
        raw = f"{day} {month} {year}"
        if (number := _MONTHS.get(month)) is not None:
            iso = f"{year}-{number:02d}-{int(day):02d}"
    return edition, iso, raw


def extract_pages(pdf_path: Path) -> list[str]:
    """Texte de chaque page du PDF. Seul point d'entrée dépendant de `pypdf`."""
    from pypdf import PdfReader  # import local : dépendance de l'outillage, pas du rendu

    reader = PdfReader(str(pdf_path))
    return [page.extract_text() or "" for page in reader.pages]


def build_index(pdf_path: Path) -> ComplementIndex:
    """Construit l'index d'un complément. Lit le PDF, n'écrit rien."""
    pages = extract_pages(pdf_path)
    edition, iso, raw = parse_document_info(pages[0] if pages else "")
    return ComplementIndex(
        document=DocumentInfo(
            edition=edition,
            effective_date=iso,
            effective_raw=raw,
            filename=pdf_path.name,
            page_count=len(pages),
            sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        ),
        zones=parse_pages(pages),
    )


def write_index(index: ComplementIndex, path: Path) -> None:
    """Écrit l'index, trié et indenté : un diff entre deux éditions doit être lisible."""
    payload: dict[str, Any] = index.model_dump(mode="json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def load_index(path: Path) -> ComplementIndex:
    """Relit un index déjà construit. C'est la SEULE lecture faite par le rendu."""
    return ComplementIndex.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = [
    "INDEX_SUFFIX",
    "ComplementIndex",
    "DocumentInfo",
    "ZoneEntry",
    "build_index",
    "index_path_for",
    "load_index",
    "normalize_zone_id",
    "parse_document_info",
    "parse_pages",
    "write_index",
]
