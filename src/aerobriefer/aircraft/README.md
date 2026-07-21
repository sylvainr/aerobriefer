# Modèles d'avion

Un avion est du **code**, pas un fichier de données : il porte de la logique
(interpolation des tables du manuel de vol, corrections vent/pente, limites). Le
repo fournit le **framework** + **un exemple** (le DR400). Les avions réels de
l'utilisateur sont **sa donnée** : ils vivent hors du repo et sous-classent
`Aircraft`.

## Contenu

| Fichier | Rôle |
|---|---|
| `model.py` | Framework : `Conditions`, `PerfTable` (interpolation trilinéaire alt × écart-ISA × masse), `Aircraft` (base), corrections vent/pente, limites, unités (km/h↔kt, ISA). |
| `examples/dr400.py` | **Exemple** : DR400/160 (F-GGJY), codé depuis le manuel de vol réel. |
| `runway.py` | `assess_runway()` — go/no-go longueur de piste : distance requise vs disponible. |

## Principes

- **On n'extrapole jamais.** Hors des bornes des tables, la valeur est clampée et
  le résultat le signale (`HORS TABLE`). Extrapoler une distance de décollage
  serait dangereux.
- **On n'invente aucune valeur.** Ce qui n'est pas dans le manuel reste `None`
  (ex. le vent traversier démontré du DR400).
- **La surface prise en compte est celle de la PISTE**, pas celle demandée : on
  ne calcule pas une perf béton sur de l'herbe.
- Le manuel du DR400 est en km/h et kg (1972) ; la conversion vers nœuds/mètres
  est explicite dans l'exemple.

## Coder son propre avion

```python
from aerobriefer.aircraft.model import Aircraft, PerfTable, SpeedCard, Surface, kmh_to_kt

_TAKEOFF = PerfTable(
    altitudes_ft=(0, 4000, 8000),
    isa_offsets_c=(-20, 0, 20),
    masses_kg=(900, 750),
    cells={  # (roulement_m, distance_15m_m) par (alt, écart ISA, masse)
        (0, 0, 900): (250, 480),
        # ... toutes les cases du manuel
    },
)

class MonAvion(Aircraft):
    name = "F-XXXX"
    max_takeoff_mass_kg = 900
    max_landing_mass_kg = 900
    speeds = SpeedCard(vne_kt=..., vno_kt=..., va_kt=..., vfe_kt=...)
    headwind_factors = {0.0: 1.0, 10.0: 0.85, 20.0: 0.7}  # courbe du manuel

    def _takeoff_table(self, surface: Surface) -> PerfTable: ...
    def _landing_table(self, surface: Surface) -> PerfTable: ...
```

## Go/no-go piste

```python
from aerobriefer.aircraft.examples.dr400 import DR400_160
from aerobriefer.aircraft.model import Conditions
from aerobriefer.aircraft.runway import assess_runway
from aerobriefer.data import airports

ac = DR400_160()
lfcy = airports.require("LFCY")
conditions = Conditions(pressure_altitude_ft=72, temperature_c=25, mass_kg=1050)
for rwy in lfcy.runways:
    a = assess_runway(ac, rwy, conditions, operation="takeoff")
    print(rwy.ident, "GO" if a.ok else "NO-GO", f"{a.required_m}/{a.available_m} m")
# 10/28   GO 660/1255 m
# 10R/28L GO 794/1000 m  (l'herbe rallonge)
```
