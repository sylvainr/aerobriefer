"""Définition d'une NAVIGATION en donnée (fichier JSON), validée par Pydantic.

Une nav n'est pas un comportement, c'est de la donnée : points tournants,
altitudes, date/heure, avion, déclinaison. Contrairement à l'avion (qui est du
CODE — interpolation de tables), la nav se décrit dans un JSON.

Parsing STRICT et intransigeant : tout écart casse le chargement, bruyamment —
- champ inconnu → erreur (`extra="forbid"`),
- type faux, borne dépassée, format date/heure/OACI invalide → erreur,
- point à coordonnées sans `lat` ET `lon` → erreur,
- aucun point → erreur.
On ne « devine » jamais : un fichier douteux est refusé, pas rattrapé.

Schéma :

    {
      "titre": "LFCY → LFBD",
      "date": "2026-08-07",            # AAAA-MM-JJ (locale)
      "heure_locale": "15:00",          # HH:MM
      "duree_h": 3.0,
      "fuseau": "Europe/Paris",
      "aeronef": "DR400",
      "declinaison_deg": 1.0,           # ° Est positif
      "demi_couloir_nm": 10.0,
      "depart": "LFCY",                 # OACI (4 lettres)
      "points": [                       # points APRÈS le départ (tournants + arrivée)
        {"nom": "MARENNES", "lat": 45.82, "lon": -1.10, "altitude_ft": 2000},
        {"nom": "LFBD", "altitude_ft": 2500}
      ]
    }
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ICAO = re.compile(r"[A-Z]{4}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM = re.compile(r"\d{2}:\d{2}")


class Waypoint(BaseModel):
    """Un point : soit un code OACI (`nom` seul), soit un point nommé par
    coordonnées (`nom` + `lat` + `lon`). `altitude_ft` en pieds AMSL.

    `travers: true` → le point n'est PAS la référence `nom` elle-même, mais le
    « travers » de cette référence : le pied de sa perpendiculaire sur la droite
    des DEUX points précédents (projection orthogonale). Exige deux points avant
    lui et que la projection tombe DANS le segment (sinon : erreur au parsing)."""

    model_config = ConfigDict(extra="forbid")

    nom: str = Field(min_length=1)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    altitude_ft: float | None = Field(default=None, ge=0.0, le=60000.0)
    travers: bool = False

    @model_validator(mode="after")
    def _coords_paired(self) -> Waypoint:
        if (self.lat is None) != (self.lon is None):
            raise ValueError(f"point « {self.nom} » : lat ET lon requis ensemble (ou aucun)")
        return self


class NavPlan(BaseModel):
    """Une navigation complète, validée strictement."""

    model_config = ConfigDict(extra="forbid")

    titre: str | None = None
    date: str
    heure_locale: str = "10:00"
    duree_h: float = Field(gt=0.0, le=24.0)
    fuseau: str = "Europe/Paris"
    aeronef: str | None = None
    declinaison_deg: float | None = Field(default=None, ge=-30.0, le=30.0)
    """Déclinaison magnétique (° Est positif). `null`/absent → calculée
    automatiquement (WMM, offline) d'après la position et la date."""
    demi_couloir_nm: float = Field(default=10.0, gt=0.0, le=100.0)
    depart: str
    points: list[Waypoint] = Field(min_length=1)

    @model_validator(mode="after")
    def _travers_needs_two_preceding(self) -> NavPlan:
        if self.points and self.points[0].travers:
            raise ValueError(
                "le premier point ne peut pas être un « travers » : il faut deux points "
                "avant lui, or le départ n'en fournit qu'un"
            )
        return self

    @field_validator("date")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        if not _DATE.fullmatch(value):
            raise ValueError("date attendue au format AAAA-MM-JJ")
        return value

    @field_validator("heure_locale")
    @classmethod
    def _valid_heure(cls, value: str) -> str:
        if not _HHMM.fullmatch(value):
            raise ValueError("heure attendue au format HH:MM")
        return value

    @field_validator("depart")
    @classmethod
    def _valid_depart(cls, value: str) -> str:
        if not _ICAO.fullmatch(value):
            raise ValueError("« depart » attendu : code OACI de 4 lettres majuscules")
        return value

    @property
    def title(self) -> str:
        return self.titre or f"{self.depart} → {self.points[-1].nom}"

    @property
    def heure(self) -> str:
        return self.heure_locale

    @property
    def zone(self) -> str:
        return self.fuseau

    @property
    def route_spec(self) -> str:
        """Chaîne de route pour `cli.parse_route` (« NOM:lat/lon@alt,… »).

        Un « travers » est préfixé par « ~ » (ex. « ~LFBD@2500 ») : `parse_route`
        le reconnaît et projette la référence sur la branche précédente."""
        parts: list[str] = []
        for point in self.points:
            altitude = f"@{point.altitude_ft:g}" if point.altitude_ft is not None else ""
            prefix = "~" if point.travers else ""
            if point.lat is not None and point.lon is not None:
                parts.append(f"{prefix}{point.nom}:{point.lat:g}/{point.lon:g}{altitude}")
            else:
                parts.append(f"{prefix}{point.nom}{altitude}")
        return ",".join(parts)


def load_navplan(path: Path | str) -> NavPlan:
    """Charge et VALIDE un fichier de navigation JSON. Lève `pydantic.ValidationError`
    au moindre écart (champ inconnu, type, borne, format) — jamais de rattrapage."""
    return NavPlan.model_validate_json(Path(path).read_text(encoding="utf-8"))
