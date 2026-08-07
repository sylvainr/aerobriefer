"""EXEMPLE d'avion : un DR400/160 Chevalier fictif, immatriculé F-ZZZZ.

C'est un EXEMPLE de référence — la façon dont on décrit un avion. Les avions
RÉELS de l'utilisateur sont SA donnée : ils vivent hors du repo, en JSON validé
par le même `AircraftSpec`. L'immatriculation **F-ZZZZ est volontairement
invalide** : on ne doit JAMAIS prendre cet exemple pour un vrai appareil.

Ici l'avion est bâti en Python (pour rester dans le repo), mais c'est le MÊME
`AircraftSpec` qu'un fichier JSON : le stockage ne change pas le modèle.

Provenance MIXTE, à ne pas confondre :
- Tables de distances décollage/atterrissage + vitesses limites : recopiées d'un
  MANUEL DE VOL réel (DR400/160, éd. 1972), chaque case = (roulement_m,
  distance_pour_15 m_m). Vitesses en km/h EAS.
- Croisière (TAS/conso) et **masse & centrage** (bras, enveloppe) : valeurs
  typiques, PAS celles d'un appareil réel (l'immat F-ZZZZ le signale).
"""

from __future__ import annotations

from ..model import (
    AircraftSpec,
    Corrections,
    Cruise,
    FuelTank,
    PerfCell,
    Performances,
    PerfSurfaces,
    PerfTableSpec,
    Speeds,
    Station,
    WeightBalance,
)

_ALT = (0.0, 4000.0, 8000.0)
_ISA = (-20.0, 0.0, 20.0)

# --- Décollage (vent nul, volets 1er cran), masses 1050 / 850 kg (manuel p.5.2)
# Cases : (roulement_m, distance_15m_m).
_TAKEOFF_PAVED = {
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
}
_TAKEOFF_GRASS = {
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
}

# --- Atterrissage (vent nul, volets 2e cran), masses 1045 / 845 kg (manuel p.5.5)
# « dur » = béton, freinage modéré ; « herbe » = sans frein sur herbe.
_LANDING_PAVED = {
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
}
_LANDING_GRASS = {
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
}


def _table(
    masses: tuple[float, ...], cells: dict[tuple[float, float, float], tuple[int, int]]
) -> PerfTableSpec:
    return PerfTableSpec(
        altitudes_ft=_ALT,
        ecarts_isa_c=_ISA,
        masses_kg=masses,
        cases=tuple(
            PerfCell(alt_ft=a, isa_c=o, masse_kg=m, roulement_m=roll, distance_15m_m=over15)
            for (a, o, m), (roll, over15) in cells.items()
        ),
    )


def DR400_160() -> AircraftSpec:
    """L'avion d'exemple (DR400/160 fictif, F-ZZZZ) comme AircraftSpec."""
    return AircraftSpec(
        registration="F-ZZZZ",
        type_name="DR400/160",
        max_takeoff_mass_kg=1050.0,
        max_landing_mass_kg=1045.0,
        speed_unit="km/h",  # badin en km/h ; le vent reste en nœuds
        demonstrated_crosswind_kt=None,  # non donné par le manuel → on n'invente pas
        speeds=Speeds(vne=308.0, vno=260.0, va=215.0, vfe=170.0, unite="km/h"),
        # PROVISOIRE — valeurs typiques DR400/160 à ~75 %, PAS tirées du manuel F-ZZZZ.
        cruise=Cruise(tas=130.0, conso_horaire_l=32.0, unite="kt"),
        # Masse & centrage — valeurs typiques, PAS un appareil réel (F-ZZZZ fictif).
        weight_balance=WeightBalance(
            empty_mass_kg=620.0,
            empty_arm_m=0.32,
            stations=(
                Station(name="Sièges avant", arm_m=0.41, max_kg=220.0, default_kg=150.0),
                Station(name="Sièges arrière", arm_m=1.19, max_kg=200.0),
                Station(name="Bagages", arm_m=1.90, max_kg=60.0),
            ),
            fuels=(FuelTank(arm_m=1.12, capacity_l=110.0, density_kg_per_l=0.72, default_l=80.0),),
            envelope=(
                (0.205, 500.0),
                (0.205, 1050.0),
                (0.430, 1050.0),
                (0.520, 800.0),
                (0.520, 500.0),
            ),
            max_mass_kg=1050.0,
        ),
        performances=Performances(
            decollage=PerfSurfaces(
                dur=_table((1050.0, 850.0), _TAKEOFF_PAVED),
                herbe=_table((1050.0, 850.0), _TAKEOFF_GRASS),
            ),
            atterrissage=PerfSurfaces(
                dur=_table((1045.0, 845.0), _LANDING_PAVED),
                herbe=_table((1045.0, 845.0), _LANDING_GRASS),
            ),
        ),
        # Correction de vent de face (manuel p.5.2/5.5), identique déco/atterro.
        corrections=Corrections(
            vent_face={0.0: 1.0, 10.0: 0.8, 20.0: 0.66, 30.0: 0.55}, pente_pct_par_pct=0.0
        ),
    )
