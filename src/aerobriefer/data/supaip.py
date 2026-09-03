"""Index des SUPPLÉMENTS À L'AIP (SUP AIP) publiés par le SIA.

Un NOTAM qui cite un SUP AIP ne CONTIENT pas l'information — il l'annonce.
Le détail (zones, coordonnées, conditions) vit dans un document séparé. Sans
lien, il faut aller le chercher à la main dans une liste de plus de cent
entrées ; avec, c'est un clic.

===========================================================================
POURQUOI UNE TABLE, ET PAS UNE URL CALCULÉE
===========================================================================
Vérifié le 2026-08-31 sur la liste officielle :

    <a class="lien_sup_aip" href="…/documents/download/f/d/15066172/">
      <b>162/2026</b> <span>Création de 2 ZRT dans la région de COGNAC…</span>
    </a>

L'identifiant `15066172` est OPAQUE : rien dans « 162/2026 » ne permet de le
retrouver. Il existe bien un motif de nom de fichier repérable ailleurs sur le
site (`lf_sup_a_2026_014_fr.pdf`), mais ce n'est PAS ce que le site utilise
pour lier ses documents, et il suppose un supplément AIRAC — or les NOTAM en
citent aussi qui ne le sont pas. Fabriquer une URL, c'est risquer un 404, ou
pire : le mauvais supplément dans un dossier de vol. On construit donc une
table depuis la source, et quand une référence n'y est pas, on lie la LISTE,
jamais une URL inventée.

===========================================================================
NORMALISATION
===========================================================================
Les NOTAM écrivent « 162/26 », le SIA écrit « 162/2026 ». On aligne sur quatre
chiffres EN GARDANT les zéros de tête : « 065/2025 », jamais « 65/2025 ».

Le réseau n'a lieu QU'ICI, jamais au rendu : le renderer lit le JSON déjà
construit.
"""

from __future__ import annotations

import html as html_mod
import os
import re
import shutil
import time
from collections.abc import Iterator
from datetime import timedelta  # noqa: TID251 - durée, pas un instant
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

SOURCE = "sia-supaip"

#: Liste des SUP AIP MÉTROPOLE. Il existe d'autres listes (CAR/SAM, PAC N,
#: PAC P, RUN) : une référence d'outre-mer ne résoudra pas ici, et c'est dit au
#: briefing plutôt que deviné.
LIST_URL = "https://www.sia.aviation-civile.gouv.fr/documents/supaip/aip/id/6"

#: Repli quand une référence ne résout pas : la liste elle-même. Toujours juste.
FALLBACK_URL = LIST_URL

ENV_DIR = "AEROBRIEFER_SUPAIP_DIR"
_DEFAULT_DIR = Path(".cache/supaip")
_FILENAME = "supaip-metropole.json"

#: Les PDF vivent UNE fois dans le magasin de référence, pas dans chaque
#: dossier : les suppléments « 95 ZRT » pèsent une vingtaine de méga-octets
#: pièce, et les mêmes reviennent de vol en vol. Chaque dossier n'en garde
#: qu'une référence (cf. `link_into`).
_STORE_SUBDIR = "pdf"

#: Les SUP AIP changent au rythme des cycles AIRAC (28 jours) et l'identifiant
#: peut bouger si le SIA republie. Un jour de cache borne le risque sans
#: retélécharger la liste à chaque génération.
_TTL = timedelta(days=1)

_TIMEOUT = httpx.Timeout(60.0)
_UA = {"User-Agent": "aerobriefer/0.1 (briefing VFR personnel, non commercial)"}

#: Le lien et le titre : le titre est dans un <span>, avant l'icône « lien
#: externe » qu'il faut donc exclure.
_ENTRY_RE = re.compile(
    r'<a[^>]*class="lien_sup_aip"[^>]*href="(?P<url>[^"]+)"[^>]*>\s*'
    r"<b>(?P<ref>[^<]+)</b>\s*<span>(?P<title>.*?)(?:<i|</span>)",
    re.DOTALL,
)

#: « Valide du <strong>2026-09-10</strong> au <strong>2026-12-31</strong> ».
_VALIDITY_RE = re.compile(
    r"Valide\s+du\s*<strong>([^<]+)</strong>\s*au\s*<strong>([^<]+)</strong>", re.DOTALL
)

#: Les drapeaux IFR / VFR / AIRAC sont des cases cochées ou non. Le drapeau VFR
#: est le plus utile ici : il dit si le supplément concerne le vol à vue.
_FLAG_RE = re.compile(r'<input[^>]*name="(IFR|VFR|AIRAC)"(?P<attrs>[^>]*)>', re.DOTALL)

_REFERENCE_RE = re.compile(r"^(?P<num>\d{1,4})\s*/\s*(?P<year>\d{2}|\d{4})$")


class SupAipEntry(BaseModel):
    """Un supplément à l'AIP, où le lire, et ce qu'il concerne."""

    reference: str
    """Forme normalisée à quatre chiffres d'année (« 162/2026 »)."""
    url: str
    title: str = ""
    valid_from: str = ""
    valid_to: str = ""
    """Bornes de validité telles qu'imprimées par le SIA (AAAA-MM-JJ), reprises
    verbatim — jamais reformatées ni interprétées."""
    rules: list[str] = Field(default_factory=list)
    """Drapeaux cochés par le SIA : IFR, VFR, AIRAC. Le VFR dit si le
    supplément concerne le vol à vue — c'est le tri utile ici."""


class SupAipIndex(BaseModel):
    """Table `référence → document`, construite depuis la liste du SIA."""

    source_url: str = LIST_URL
    entries: dict[str, SupAipEntry] = Field(default_factory=dict)

    def lookup(self, raw_reference: str) -> SupAipEntry | None:
        """Retrouve un supplément depuis l'écriture d'un NOTAM (« 162/26 »)."""
        key = normalize_reference(raw_reference)
        return self.entries.get(key) if key else None


def normalize_reference(raw: str) -> str:
    """« 162/26 » et « 162/2026 » → « 162/2026 ». Chaîne vide si non reconnu.

    Les zéros de tête sont CONSERVÉS : la liste du SIA écrit « 065/2025 », et
    un « 65/2025 » ne s'y retrouverait pas.
    """
    match = _REFERENCE_RE.match(raw.strip())
    if match is None:
        return ""
    year = match.group("year")
    if len(year) == 2:
        year = f"20{year}"
    return f"{match.group('num')}/{year}"


def parse_list(document: str) -> dict[str, SupAipEntry]:
    """Extrait les entrées de la page de liste. FONCTION PURE.

    C'est le point testable : le parsing s'éprouve sur du HTML figé, sans
    réseau et sans dépendre de la disponibilité du SIA.
    """
    entries: dict[str, SupAipEntry] = {}
    # On découpe par LIGNE : dates et drapeaux vivent dans une cellule voisine
    # du lien, une regex unique sur tout le document les apparierait au hasard.
    for row in re.split(r"(?=<tr)", document):
        match = _ENTRY_RE.search(row)
        if match is None:
            continue
        reference = normalize_reference(match.group("ref"))
        if not reference or reference in entries:
            continue
        title = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", match.group("title"))).split())
        validity = _VALIDITY_RE.search(row)
        rules = [m.group(1) for m in _FLAG_RE.finditer(row) if "checked" in m.group("attrs")]
        entries[reference] = SupAipEntry(
            reference=reference,
            url=match.group("url").strip(),
            title=title,
            valid_from=validity.group(1).strip() if validity else "",
            valid_to=validity.group(2).strip() if validity else "",
            rules=rules,
        )
    return dict(sorted(entries.items()))


def index_path() -> Path:
    override = os.environ.get(ENV_DIR)
    directory = Path(override) if override else _DEFAULT_DIR
    return directory / _FILENAME


def load_index(*, allow_network: bool = True) -> SupAipIndex:
    """Index courant : depuis le cache s'il est frais, sinon retéléchargé.

    Ne LÈVE JAMAIS : un briefing reste valide sans les liens vers les
    suppléments. En cas d'échec on rend un index vide, et le rendu retombe sur
    le lien vers la liste.
    """
    path = index_path()
    cached: SupAipIndex | None = None
    if path.exists():
        try:
            cached = SupAipIndex.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - cache corrompu : on le traite comme absent
            cached = None
        else:
            if time.time() - path.stat().st_mtime <= _TTL.total_seconds():
                return cached

    # Le cache est périmé (ou absent). Un cache PÉRIMÉ reste infiniment
    # préférable à rien : les SUP AIP suivent le cycle AIRAC (28 jours) et
    # l'identifiant d'un document publié ne bouge pas. Afficher « non résolu »
    # sur un index de deux jours, c'est jeter une donnée juste et priver le
    # pilote de tous ses liens. On ne l'abandonne donc QUE si on a mieux.
    if not allow_network:
        return cached if cached is not None else SupAipIndex()
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=_UA, follow_redirects=True) as client:
            response = client.get(LIST_URL)
        if response.status_code != 200:
            return cached if cached is not None else SupAipIndex()
        index = SupAipIndex(entries=parse_list(response.text))
    except httpx.HTTPError:
        return cached if cached is not None else SupAipIndex()
    if not index.entries:
        # Page servie mais illisible (refonte du site) : le cache d'hier vaut
        # mieux qu'un index vide, et on ne l'écrase pas avec du vide.
        return cached if cached is not None else index
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(index.model_dump_json(indent=2), encoding="utf-8")
    return index


def iter_references(*texts: str | None) -> Iterator[str]:
    """Références normalisées citées dans des textes de NOTAM."""
    from ..render.html import aip_sup_references

    for reference in aip_sup_references(*texts):
        normalized = normalize_reference(reference)
        if normalized:
            yield normalized


def download(entry: SupAipEntry, directory: Path) -> Path | None:
    """Télécharge un supplément dans `directory`. `None` si ça échoue.

    Un dossier de vol doit rester lisible SANS réseau : les SUP AIP cités
    décrivent des zones réglementées, c'est exactement ce qu'on veut pouvoir
    relire en vol. On ne télécharge que ceux que les NOTAM du dossier citent —
    jamais la liste entière.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"sup_aip_{entry.reference.replace('/', '-')}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=_UA, follow_redirects=True) as client:
            response = client.get(entry.url)
    except httpx.HTTPError:
        return None
    # On refuse tout ce qui n'est pas un PDF : une page d'erreur enregistrée
    # sous un nom de PDF serait pire que l'absence de fichier.
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    path.write_bytes(response.content)
    return path


def store_dir() -> Path:
    """Magasin des PDF : un seul exemplaire pour tous les dossiers."""
    return index_path().parent / _STORE_SUBDIR


def link_into(pdf: Path, directory: Path) -> tuple[Path, bool]:
    """Place dans `directory` une référence vers `pdf`. Renvoie (chemin, est_un_lien).

    On pose un **lien symbolique RELATIF** : relatif pour que déplacer toute
    l'arborescence (sauvegarde, copie sur une autre machine) ne casse rien, un
    lien absolu pointant alors dans le vide.

    Portabilité : `os.symlink` marche nativement sur macOS et Linux. Sur
    Windows il exige le mode développeur ou des droits d'administrateur et lève
    sinon. On RETOMBE alors sur une vraie copie — un dossier de vol qui pèse
    lourd vaut infiniment mieux qu'un dossier dont les liens pointent dans le
    vide. Le format des liens dans le HTML est le même dans les deux cas.
    """
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / pdf.name
    if destination.exists() or destination.is_symlink():
        return destination, destination.is_symlink()
    relative = os.path.relpath(pdf.resolve(), directory.resolve())
    try:
        os.symlink(relative, destination)
        return destination, True
    except OSError:
        # Windows sans droits, système de fichiers sans liens (exFAT d'une clé
        # USB, partage réseau) : on duplique plutôt que de laisser un trou.
        shutil.copy2(pdf, destination)
        return destination, False


__all__ = [
    "FALLBACK_URL",
    "download",
    "LIST_URL",
    "SOURCE",
    "SupAipEntry",
    "SupAipIndex",
    "index_path",
    "link_into",
    "iter_references",
    "load_index",
    "normalize_reference",
    "parse_list",
    "store_dir",
]
