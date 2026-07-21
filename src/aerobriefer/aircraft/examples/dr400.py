"""EXEMPLE d'avion codé : le DR400/160 Chevalier (F-GGJY, aéroclub de Royan).

C'est un EXEMPLE de référence — la façon dont un utilisateur code SON avion. Les
avions réels de l'utilisateur sont SA donnée : ils vivent hors du repo et
sous-classent `Aircraft` de la même manière.

Toutes les valeurs viennent du MANUEL DE VOL réel (DR400/160, édition 1972). Les
tables de distances sont recopiées telles quelles : chaque case est
(roulement_m, distance_pour_15 m_m). Les vitesses limites du manuel sont en km/h
EAS et converties en nœuds ici. Aucune valeur n'est inventée ; ce qui n'est pas
dans le manuel (ex. le vent traversier démontré) reste `None`.
"""

from __future__ import annotations

from ..model import Aircraft, PerfTable, SpeedCard, Surface, kmh_to_kt

_ALT = (0.0, 4000.0, 8000.0)
_ISA = (-20.0, 0.0, 20.0)

# --- Décollage (vent nul, volets 1er cran), masses 1050 / 850 kg ------------
# Manuel p.5.2. Cases : (roulement_m, distance_15m_m).
_TAKEOFF_PAVED = PerfTable(
    _ALT,
    _ISA,
    (1050.0, 850.0),
    {
        (0.0, -20.0, 1050.0): (280, 560),
        (0.0, 0.0, 1050.0): (330, 620),
        (0.0, 20.0, 1050.0): (350, 690),
        (4000.0, -20.0, 1050.0): (375, 750),
        (4000.0, 0.0, 1050.0): (420, 840),
        (4000.0, 20.0, 1050.0): (475, 940),
        (8000.0, -20.0, 1050.0): (510, 1030),
        (8000.0, 0.0, 1050.0): (580, 1165),
        (8000.0, 20.0, 1050.0): (650, 1310),
        (0.0, -20.0, 850.0): (175, 360),
        (0.0, 0.0, 850.0): (195, 395),
        (0.0, 20.0, 850.0): (215, 435),
        (4000.0, -20.0, 850.0): (230, 470),
        (4000.0, 0.0, 850.0): (260, 525),
        (4000.0, 20.0, 850.0): (290, 580),
        (8000.0, -20.0, 850.0): (315, 635),
        (8000.0, 0.0, 850.0): (355, 710),
        (8000.0, 20.0, 850.0): (400, 790),
    },
)
_TAKEOFF_GRASS = PerfTable(
    _ALT,
    _ISA,
    (1050.0, 850.0),
    {
        (0.0, -20.0, 1050.0): (380, 660),
        (0.0, 0.0, 1050.0): (435, 745),
        (0.0, 20.0, 1050.0): (490, 830),
        (4000.0, -20.0, 1050.0): (550, 925),
        (4000.0, 0.0, 1050.0): (635, 1055),
        (4000.0, 20.0, 1050.0): (730, 1195),
        (8000.0, -20.0, 1050.0): (835, 1355),
        (8000.0, 0.0, 1050.0): (980, 1565),
        (8000.0, 20.0, 1050.0): (1145, 1805),
        (0.0, -20.0, 850.0): (220, 405),
        (0.0, 0.0, 850.0): (250, 450),
        (0.0, 20.0, 850.0): (280, 500),
        (4000.0, -20.0, 850.0): (310, 550),
        (4000.0, 0.0, 850.0): (350, 615),
        (4000.0, 20.0, 850.0): (400, 690),
        (8000.0, -20.0, 850.0): (445, 765),
        (8000.0, 0.0, 850.0): (515, 870),
        (8000.0, 20.0, 850.0): (590, 980),
    },
)

# --- Atterrissage (vent nul, volets 2e cran), masses 1045 / 845 kg ----------
# Manuel p.5.5. « béton » = freinage modéré ; « herbe » = sans frein sur herbe.
_LANDING_PAVED = PerfTable(
    _ALT,
    _ISA,
    (1045.0, 845.0),
    {
        (0.0, -20.0, 1045.0): (230, 510),
        (0.0, 0.0, 1045.0): (250, 545),
        (0.0, 20.0, 1045.0): (270, 575),
        (4000.0, -20.0, 1045.0): (260, 565),
        (4000.0, 0.0, 1045.0): (280, 600),
        (4000.0, 20.0, 1045.0): (300, 635),
        (8000.0, -20.0, 1045.0): (295, 620),
        (8000.0, 0.0, 1045.0): (320, 660),
        (8000.0, 20.0, 1045.0): (340, 700),
        (0.0, -20.0, 845.0): (190, 435),
        (0.0, 0.0, 845.0): (205, 460),
        (0.0, 20.0, 845.0): (215, 485),
        (4000.0, -20.0, 845.0): (210, 475),
        (4000.0, 0.0, 845.0): (230, 505),
        (4000.0, 20.0, 845.0): (245, 535),
        (8000.0, -20.0, 845.0): (240, 520),
        (8000.0, 0.0, 845.0): (260, 555),
        (8000.0, 20.0, 845.0): (275, 585),
    },
)
_LANDING_GRASS = PerfTable(
    _ALT,
    _ISA,
    (1045.0, 845.0),
    {
        (0.0, -20.0, 1045.0): (350, 630),
        (0.0, 0.0, 1045.0): (375, 670),
        (0.0, 20.0, 1045.0): (400, 705),
        (4000.0, -20.0, 1045.0): (390, 695),
        (4000.0, 0.0, 1045.0): (420, 740),
        (4000.0, 20.0, 1045.0): (450, 785),
        (8000.0, -20.0, 1045.0): (445, 770),
        (8000.0, 0.0, 1045.0): (480, 820),
        (8000.0, 20.0, 1045.0): (515, 875),
        (0.0, -20.0, 845.0): (285, 530),
        (0.0, 0.0, 845.0): (305, 560),
        (0.0, 20.0, 845.0): (325, 595),
        (4000.0, -20.0, 845.0): (315, 580),
        (4000.0, 0.0, 845.0): (340, 615),
        (4000.0, 20.0, 845.0): (365, 655),
        (8000.0, -20.0, 845.0): (360, 640),
        (8000.0, 0.0, 845.0): (390, 685),
        (8000.0, 20.0, 845.0): (415, 725),
    },
)


class DR400_160(Aircraft):
    """Robin DR400/160 Chevalier — F-GGJY. Données du manuel de vol (1972)."""

    name = "DR400/160 (F-GGJY)"
    max_takeoff_mass_kg = 1050.0
    max_landing_mass_kg = 1045.0
    # Vitesses limites du manuel (km/h EAS) → nœuds.
    speeds = SpeedCard(
        vne_kt=kmh_to_kt(308.0),
        vno_kt=kmh_to_kt(260.0),
        va_kt=kmh_to_kt(215.0),
        vfe_kt=kmh_to_kt(170.0),
    )
    #: Le manuel ne donne pas de vent traversier démontré → on ne l'invente pas.
    demonstrated_crosswind_kt = None
    #: Correction de vent de face (manuel p.5.2/5.5), identique déco/atterro.
    headwind_factors = {0.0: 1.0, 10.0: 0.8, 20.0: 0.66, 30.0: 0.55}

    def _takeoff_table(self, surface: Surface) -> PerfTable:
        return _TAKEOFF_GRASS if surface == "grass" else _TAKEOFF_PAVED

    def _landing_table(self, surface: Surface) -> PerfTable:
        return _LANDING_GRASS if surface == "grass" else _LANDING_PAVED
