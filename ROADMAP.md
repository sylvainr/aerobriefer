# Roadmap aerobriefer

Document de réflexion — ce qu'on a, ce qu'on veut, et *comment* on veut le faire.
Vivant : on l'édite en avançant.

---

## Où on en est

**Le brief local marche de bout en bout** et il est bon. Un `BriefingContext`
(géométrie + fenêtre + terrain) produit un `BriefingPackage` autosuffisant,
rendu en HTML autonome (lecteurs de cartes animés) et en PDF déterministe.

Sources branchées : NOAA (METAR/TAF), SIGMET, SOFIA (NOTAM), met.no (prévisions),
Aeroweb (cartes). Qualité tenue par `make check` (ruff format + lint typé + mypy
strict + tests).

Ce qui reste à polir sur l'existant :
- **Pistes** : OurAirports est incomplet (rate les bandes herbe). → migrer vers
  le SIA (voir ci-dessous).

---

## Principes de design (décidés, à respecter)

1. **Données vs code.** Ce qui est *référence publique et stable* (aérodromes,
   pistes, espaces aériens) va dans le repo. Ce qui est *donnée utilisateur* (les
   avions de l'utilisateur, ses profils) N'EST PAS dans le repo — sauf UN exemple
   pour les tests et la démo.

2. **Les avions sont du CODE, pas du YAML.** Un avion, c'est un comportement
   (interpolations de tables POH, corrections, limites), pas un sac de clés. On
   code une classe par appareil contre un framework commun. Le repo fournit le
   framework + un exemple (DR400). L'utilisateur code ses propres avions ailleurs.

3. **Le domaine reste pur et déterministe.** Aucun LLM dans les produits
   déterministes (cf. la règle déjà en place pour le rendu).

4. **Tout passe par `make check`** avant commit.

---

## Chantiers

### 1. Pistes depuis le SIA  🟢 prêt (recherche faite)

OurAirports rate les bandes herbe. Le SIA (AIXM 4.5, data.gouv/Etalab) a TOUT :
LFCY y a bien ses deux pistes (10/28 revêtue **et** 10R/28L herbe), 781 pistes
France, parsable en stdlib (`xml.etree.iterparse`).

- Télécharger une fois l'export, parser → `runways_fr.csv` embarqué.
- Fusionner avec `runways_eu.csv`, priorité au FR pour les `LF*`.
- Le `runways_supplement.csv` manuel devient inutile pour la France (garder comme
  fine couche d'override).

*Court. Nettoie une vraie dette.*

### 2. Espaces aériens + viewer  🟡 recherche faite, à construire

Source : `france.geojson` (planeur-net, 1608 espaces, polygones + classe +
plancher/plafond). **Ne pas committer** (licence non déclarée + 7 Mo) → provider
qui télécharge + cache, comme Aeroweb. Filtrage spatial contre la géométrie du vol.

Autour de LFCY, données réelles : TMA Aquitaine (E), LF-R260 Royan, CTR Cognac,
RMZ Rochefort, P-zone du Blayais…

Deux livrables :
- **a) Carte 2D** — SVG autosuffisant dans le briefing (polygones colorés par
  classe + cercle du vol + terrains). Pas de fond de carte externe (offline).
- **b) Viewer 3D three.js** — page interactive : chaque espace = un prisme extrudé
  entre plancher et plafond, translucide, coloré par classe. Conversion altitude :
  FT/MSL → m ; FL/STD → FL×100×0.3048 ; FT/GND → au-dessus du sol (MVP sol=0).

*Le morceau « waouh ». Domaine `Airspace` déjà esquissé.*

### 3. Modèle avion (framework + exemple DR400)  🟡 données en main

**Framework (repo)** : classes de base + machinerie d'interpolation bilinéaire
(altitude-pression × température), corrections (vent, surface herbe, pente),
limites (vitesses, masses, centrage). Le tout en CODE.

**Exemple (repo, marqué comme tel)** : le DR400/160 du F-GGJY, dont on a extrait
les vraies données du manuel de vol (voir annexe). Sert aux tests et à la démo.

**Avions utilisateur** : hors repo. L'utilisateur sous-classe le framework.

Ce que ça débloque :
- **Go/no-go longueur de piste** : pour chaque déroutement, distance requise (aux
  conditions METAR du moment) vs longueur dispo → vert/rouge automatique.
- **Vent traversier vs limite démontrée** de l'appareil.
- Devis masse & centrage.

### 4. Produits nav & déroutement  🟡 moteur déjà là

Le moteur géométrique existe (`Corridor` = couloir le long d'une route). Manque le
produit.

- **Nav** : une route = une suite de **points tournants** (waypoints), à la SOFIA.
  À définir : comment on les saisit (codes OACI, coordonnées, points VFR publiés,
  radiale/distance ?). Génère un couloir → NOTAM/météo le long de la route, + un
  log de nav (branches, caps, distances, temps estimés).
- **Déroutement** : `Circle` autour de chaque dégagement + go/no-go piste (chantier 3).

### 5. Serveur MCP  🔵 idée initiale

Exposer `assemble_briefing` et les providers comme outils MCP → interroger le
briefing en conversation depuis Claude. La « toolbox » du départ.

---

## Ordre proposé

`1 (pistes SIA)` → `3 (avion, débloque le go/no-go)` → `4 (nav/déroutement)` →
`2 (espaces + 3D, le gros bloc visuel)` → `5 (MCP)`.
À réordonner selon l'envie — le viewer 3D peut passer devant si c'est ça qui motive.

---

## Annexe — données F-GGJY (DR400/160 Chevalier)

Extraites du manuel de vol (aéroclub de Royan). Pour l'exemple avion.

**Vitesses limites (EAS, km/h, à masse max)** : Vne 308 · Vno 260 · Vc 260 ·
Va 215 · Vfe 170. Arc vert 105–260 · arc blanc (volets) 93–170.

**Masses (kg)** : max décollage 1050 · max atterrissage 1045 · évolutions « U » 950.
**Facteurs de charge** : lisse +3.8 / volets +2.

**Décollage** (vent nul, volets 1er cran) — distance pour passer 15 m, (roulement) :

| Alt | Temp | 1050 kg béton | 1050 kg herbe | 850 kg béton | 850 kg herbe |
|----:|:----:|:---:|:---:|:---:|:---:|
| 0 ft | Std−20 | 560 (280) | 660 (380) | 360 (175) | 405 (220) |
| 0 ft | Std | 620 (330) | 745 (435) | 395 (195) | 450 (250) |
| 0 ft | Std+20 | 690 (350) | 830 (490) | 435 (215) | 500 (280) |
| 4000 ft | Std | 840 (420) | 1055 (635) | 525 (260) | 615 (350) |
| 8000 ft | Std | 1165 (580) | 1565 (980) | 710 (355) | 870 (515) |

**Atterrissage** (vent nul, volets 2e cran) — distance depuis 15 m, (roulement) :

| Alt | Temp | 1045 kg frein modéré | 1045 kg sans frein herbe | 845 kg frein | 845 kg herbe |
|----:|:----:|:---:|:---:|:---:|:---:|
| 0 ft | Std | 545 (250) | 670 (375) | 460 (205) | 560 (305) |
| 4000 ft | Std | 600 (280) | 740 (420) | 505 (230) | 615 (340) |
| 8000 ft | Std | 660 (320) | 820 (480) | 555 (260) | 685 (390) |

**Correction vent de face** (déco ET atterro) : 10 kt ×0.8 · 20 kt ×0.66 · 30 kt ×0.55.

**Montée** : 1050 kg → Vz sol 3.8 m/s, −0.22/1000 ft, plafond 14 500 ft, Vopt 165→145.
850 kg → Vz sol 5.3 m/s, plafond 20 000 ft. Chaque +10 °C au-dessus du std : plafond
−1000 ft, Vz −0.22 m/s.
