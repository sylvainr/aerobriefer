"""Données de RÉFÉRENCE (aérodromes, pistes) — téléchargées puis mises en cache.

Distinction avec les *providers* : les observations (METAR, NOTAM…) sont
collectées POUR un briefing donné. Les aérodromes et pistes, eux, sont un socle
de RÉFÉRENCE consulté par lookup (« les pistes de LFCY »), et ce AVANT même
d'avoir un contexte de vol — la position du terrain sert à construire la
géométrie. Ce n'est donc pas un provider, mais ça ne doit pas non plus vivre
committé dans le repo : on le télécharge une fois depuis la source et on le met
en cache localement.

Sources :
- **OurAirports** (domaine public) — base mondiale d'aérodromes + pistes.
- **SIA / DGAC** (AIXM 4.5, Licence Ouverte Etalab) — pistes françaises
  COMPLÈTES (elle a les bandes herbe qu'OurAirports rate). Elle remplace
  OurAirports pour les terrains qu'elle couvre. Voir la fusion dans `airports.py`.

Le cache (`.cache/refdata/`) est construit au premier usage et jamais
re-téléchargé ensuite. Premier run = réseau requis ; ensuite tout est hors ligne.

Pour les TESTS : poser `AEROBRIEFER_REFDATA_DIR` vers un répertoire pré-rempli
(fixture) — dans ce cas rien n'est téléchargé, on lit ce qui est fourni.
"""

from __future__ import annotations

import csv
import io
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx

OURAIRPORTS_AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_RUNWAYS = "https://davidmegginson.github.io/ourairports-data/runways.csv"
#: Export officiel de la base SIA, miroir ouvert (sans compte). Cycle daté : la
#: géométrie des pistes ne bouge quasi jamais, un export ancien reste valable.
#: Pour rafraîchir au cycle AIRAC courant : store officiel sia.aviation-civile.gouv.fr.
SIA_AIXM_ZIP = "http://data.cquest.org/dgac/aip/export_xml_bd_sia_2023-10-05-s2.zip"

ENV_DIR = "AEROBRIEFER_REFDATA_DIR"
_DEFAULT_DIR = Path(".cache/refdata")

#: Europe de l'Ouest : on ne garde qu'une région, pas la planète.
_EU_PREFIXES = ("LF", "EG", "ED", "LE", "LI", "EB", "EH", "LS", "LP", "LK", "LO")
_KEEP_TYPES = {"small_airport", "medium_airport", "large_airport"}

#: Surfaces AIXM (SIA) → codes du domaine (cf. Runway.is_paved).
_SIA_SURFACE = {
    "ASPH": "ASP",
    "BITUM": "ASP",
    "MACADAM": "ASP",
    "CONC": "CON",
    "CONC+ASPH": "CON",
    "GRASS": "herbe",
    "SAND": "sable",
    "GRAVEL": "gravel",
}

_TIMEOUT = httpx.Timeout(120.0)
_UA = {"User-Agent": "aerobriefer/0.1 (reference data, personal use)"}


def data_dir() -> Path:
    """Répertoire actif : override d'environnement (fixture) ou cache par défaut."""
    override = os.environ.get(ENV_DIR)
    return Path(override) if override else _DEFAULT_DIR


def airports_csv() -> Path:
    _ensure_built()
    return data_dir() / "airports_eu.csv"


def runways_eu_csv() -> Path:
    _ensure_built()
    return data_dir() / "runways_eu.csv"


def runways_fr_csv() -> Path:
    _ensure_built()
    return data_dir() / "runways_fr.csv"


def _ensure_built() -> None:
    """Construit le cache si absent. Ne fait rien si un override est fourni : la
    fixture est censée être pré-remplie, on ne télécharge jamais par surprise."""
    directory = data_dir()
    targets = ["airports_eu.csv", "runways_eu.csv", "runways_fr.csv"]
    if all((directory / name).exists() for name in targets):
        return
    if os.environ.get(ENV_DIR):
        missing = [n for n in targets if not (directory / n).exists()]
        raise FileNotFoundError(
            f"{ENV_DIR}={directory} incomplet : manquent {missing}. "
            "Une fixture doit être pré-remplie ; aucun téléchargement en mode override."
        )
    directory.mkdir(parents=True, exist_ok=True)
    icaos = _build_airports(directory / "airports_eu.csv")
    _build_runways_eu(directory / "runways_eu.csv", icaos)
    _build_runways_fr(directory / "runways_fr.csv")


def _get(url: str) -> bytes:
    with httpx.Client(timeout=_TIMEOUT, headers=_UA, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _build_airports(dest: Path) -> set[str]:
    """OurAirports → sous-ensemble Europe de l'Ouest. Retourne les OACI retenus."""
    text = _get(OURAIRPORTS_AIRPORTS).decode("utf-8")
    kept: set[str] = set()
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["icao", "name", "lat", "lon", "elev_ft", "country", "type"]
        )
        writer.writeheader()
        for row in csv.DictReader(io.StringIO(text)):
            ident = row["ident"]
            if len(ident) == 4 and ident.startswith(_EU_PREFIXES) and row["type"] in _KEEP_TYPES:
                kept.add(ident)
                writer.writerow(
                    {
                        "icao": ident,
                        "name": row["name"],
                        "lat": row["latitude_deg"],
                        "lon": row["longitude_deg"],
                        "elev_ft": row["elevation_ft"] or "",
                        "country": row["iso_country"],
                        "type": row["type"],
                    }
                )
    return kept


def _build_runways_eu(dest: Path, icaos: set[str]) -> None:
    """OurAirports runways → filtré aux aérodromes retenus, pistes ouvertes."""
    text = _get(OURAIRPORTS_RUNWAYS).decode("utf-8")
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "icao",
                "le_ident",
                "he_ident",
                "le_heading",
                "he_heading",
                "length_m",
                "width_m",
                "surface",
                "lighted",
            ],
        )
        writer.writeheader()
        for row in csv.DictReader(io.StringIO(text)):
            icao = row["airport_ident"]
            if icao not in icaos or row["closed"] == "1" or not row["length_ft"]:
                continue
            writer.writerow(
                {
                    "icao": icao,
                    "le_ident": row["le_ident"],
                    "he_ident": row["he_ident"],
                    "le_heading": row.get("le_heading_degT", ""),
                    "he_heading": row.get("he_heading_degT", ""),
                    "length_m": _ft_to_m(row["length_ft"]),
                    "width_m": _ft_to_m(row["width_ft"]),
                    "surface": row["surface"],
                    "lighted": row["lighted"],
                }
            )


def _build_runways_fr(dest: Path) -> None:
    """SIA AIXM 4.5 → pistes françaises complètes (bandes herbe comprises).

    Structure plate : <Rwy> (dimensions/surface) + <Rdn> (caps vrais par QFU),
    indexés par (OACI, désignation). Streaming iterparse, mémoire bornée.
    """
    archive = zipfile.ZipFile(io.BytesIO(_get(SIA_AIXM_ZIP)))
    name = next(n for n in archive.namelist() if n.startswith("AIXM4.5_all_FR_OM_"))
    runways: dict[tuple[str, str], dict[str, str | None]] = {}
    headings: dict[tuple[str, str], float] = {}
    with archive.open(name) as stream:
        for _event, el in ET.iterparse(stream, events=("end",)):
            if el.tag == "Rwy":
                icao = el.findtext(".//AhpUid/codeId")
                desig = el.findtext(".//RwyUid/txtDesig")
                if icao and desig:
                    runways[(icao, desig)] = {
                        "len": el.findtext("valLen"),
                        "wid": el.findtext("valWid"),
                        "surf": el.findtext("codeComposition"),
                    }
                el.clear()
            elif el.tag == "Rdn":
                icao = el.findtext(".//AhpUid/codeId")
                qfu = el.findtext(".//RdnUid/txtDesig")
                brg = el.findtext("valTrueBrg")
                if icao and qfu and brg:
                    try:
                        headings[(icao, qfu)] = float(brg)
                    except ValueError:
                        pass
                el.clear()

    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "icao",
                "le_ident",
                "he_ident",
                "le_heading",
                "he_heading",
                "length_m",
                "width_m",
                "surface",
                "lighted",
            ],
        )
        writer.writeheader()
        for (icao, desig), data in sorted(runways.items()):
            if data["len"] is None:
                continue
            ends = desig.split("/")
            le = ends[0].strip()
            he = ends[1].strip() if len(ends) == 2 else ""
            writer.writerow(
                {
                    "icao": icao,
                    "le_ident": le,
                    "he_ident": he,
                    "le_heading": _brg(headings.get((icao, le))),
                    "he_heading": _brg(headings.get((icao, he))),
                    "length_m": _int(data["len"]),
                    "width_m": _int(data["wid"]),
                    "surface": _SIA_SURFACE.get((data["surf"] or "").upper(), data["surf"] or ""),
                    "lighted": "",
                }
            )


def _ft_to_m(value: str | None) -> str:
    try:
        return str(round(float(value) * 0.3048)) if value else ""
    except (TypeError, ValueError):
        return ""


def _brg(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""


def _int(value: str | None) -> str:
    try:
        return str(round(float(value))) if value else ""
    except (TypeError, ValueError):
        return ""
