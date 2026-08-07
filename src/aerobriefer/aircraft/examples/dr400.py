"""EXEMPLE d'avion codé : un DR400/160 Chevalier fictif, immatriculé F-ZZZZ.

C'est un EXEMPLE de référence — la façon dont un utilisateur code SON avion. Les
avions réels de l'utilisateur sont SA donnée : ils vivent hors du repo et
sous-classent `Aircraft` de la même manière. L'immatriculation **F-ZZZZ est
volontairement invalide** : on ne doit JAMAIS prendre cet exemple pour un vrai
appareil.

Provenance MIXTE, à ne pas confondre :
- Tables de distances décollage/atterrissage + vitesses limites : recopiées d'un
  MANUEL DE VOL réel (DR400/160, éd. 1972), chaque case = (roulement_m,
  distance_pour_15 m_m). Vitesses en km/h EAS converties en nœuds ici.
- Croisière (TAS/conso) et **masse & centrage** (bras, enveloppe) : valeurs
  typiques, PAS celles d'un appareil réel (l'immat F-ZZZZ le signale). À remplacer
  par les données de votre appareil.
"""

from __future__ import annotations

from ..model import (
    Aircraft,
    FuelTank,
    PerfTable,
    SpeedCard,
    Station,
    Surface,
    WeightBalance,
    kmh_to_kt,
)

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
    """DR400/160 Chevalier FICTIF (F-ZZZZ). Perfs piste du manuel réel ; croisière
    et masse & centrage encore PLACEHOLDERS."""

    name = "DR400/160 (F-ZZZZ)"
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
    #: PROVISOIRE — valeurs typiques DR400/160 à ~75 %, PAS encore tirées du
    #: manuel F-ZZZZ. À remplacer par une table croisière (TAS/conso par altitude
    #: et régime). Utilisées telles quelles par le log de navigation.
    cruise_tas_kt = 130.0
    cruise_fuel_lph = 32.0
    #: Masse & centrage — valeurs typiques, PAS celles d'un appareil réel (F-ZZZZ
    #: est fictif). Le cadre est bon, les nombres sont à remplacer par la fiche de
    #: pesée et les bras du manuel de VOTRE avion.
    weight_balance = WeightBalance(
        empty_mass_kg=620.0,
        empty_arm_m=0.32,
        stations=(
            Station(name="Sièges avant", arm_m=0.41, max_kg=180.0, default_kg=150.0),
            Station(name="Sièges arrière", arm_m=1.19, max_kg=160.0),
            Station(name="Bagages", arm_m=1.90, max_kg=40.0),
        ),
        fuel=FuelTank(arm_m=1.12, capacity_l=110.0, density_kg_per_l=0.72, default_l=80.0),
        # Enveloppe FICTIVE (bras_m, masse_kg), polygone fermé.
        envelope=(
            (0.205, 500.0),
            (0.205, 1050.0),
            (0.430, 1050.0),
            (0.520, 800.0),
            (0.520, 500.0),
        ),
        max_mass_kg=1050.0,
    )

    def _takeoff_table(self, surface: Surface) -> PerfTable:
        return _TAKEOFF_GRASS if surface == "grass" else _TAKEOFF_PAVED

    def _landing_table(self, surface: Surface) -> PerfTable:
        return _LANDING_GRASS if surface == "grass" else _LANDING_PAVED
