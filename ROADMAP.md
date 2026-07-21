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

Trois livrables :
- **a) Carte 2D** — vue de dessus, polygones colorés par classe + cercle du vol +
  terrains. Autosuffisante, pas de fond de carte externe (offline).
- **b) Viewer 3D three.js** — chaque espace = un prisme extrudé entre plancher et
  plafond, translucide, coloré par classe. Conversion altitude : FT/MSL → m ;
  FL/STD → FL×100×0.3048 ; FT/GND → au-dessus du sol (MVP sol=0).
- **c) Le viewer construit la ROUTE** (voir chantier 4). Ce n'est pas que de la
  visualisation : on clique ses points tournants sur la 2D, on voit les espaces
  traversés, et ça définit la nav. Le viewer est l'ÉDITEUR de route.

*Le morceau « waouh ». Domaine `Airspace` déjà esquissé.*

**En ligne vs hors ligne — la distinction clé.** Le briefing (PDF/HTML emporté en
vol) reste AUTOSUFFISANT et hors ligne. Le viewer de création de route, lui, est
un outil de PRÉPARATION au bureau, en ligne : il PEUT charger un fond de carte
externe. Les deux ne suivent pas la même règle, et c'est assumé.

**Fond de carte du viewer.** Pour préparer une nav, voir le terrain réel
(satellite) compte. Options, de la plus pertinente à la plus générique :
- **IGN Géoportail (français, officiel, gratuit, WMTS)** — le bon choix pour la
  France. Deux couches en or : l'**ortho-photo** (satellite haute résolution) ET
  surtout la **carte OACI VFR 1:500 000** (la vraie carte aéro, qui montre déjà
  espaces, obstacles, points VFR). On pose nos couches par-dessus.
- **Esri World Imagery** — satellite mondial, sans clé, bon repli hors France.
- **Mapbox / MapLibre GL** — satellite + rendu vectoriel, clé API (offre gratuite).
  MapLibre (open-source) si on veut éviter la dépendance Mapbox.

Moteur de carte : **MapLibre GL JS** (open-source, gère raster WMTS + vecteur +
3D terrain), ou Leaflet pour un MVP 2D plus simple. Le 3D three.js reste pour la
vue volumétrique des espaces ; MapLibre peut aussi faire de la 3D terrain si on
veut tout au même endroit.

**Architecture du round-trip viewer ↔ moteur.** Le viewer est du JS navigateur,
le moteur est Python — il faut une frontière propre :
1. Le viewer charge les espaces + terrains (données servies par le moteur, ou
   fetchées/cachées).
2. L'utilisateur place ses points tournants → le viewer produit une **route JSON**
   (suite de {lat, lon, nom?, altitude?}).
3. Cette route alimente le moteur (`Corridor`) → briefing de nav.
4. *Plus tard* : le moteur renvoie le résultat (NOTAM/météo le long de la route)
   et le viewer l'affiche par-dessus la carte. Boucle complète.

MVP : viewer exporte la route → CLI génère le brief. Intégration serrée ensuite
(petit serveur local, ou MCP — cf. chantier 5).

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

### 5. Cartes VAC des terrains  🟡 source ouverte, URL à trouver

Pour une nav, il faut la **VAC** (carte d'atterrissage à vue) de chaque terrain
concerné : destination, dégagements, déroutements, terrains des points tournants.

- **Source : eAIP du SIA**, sous Licence Ouverte Etalab — donc librement
  récupérable ET **rediffusable** (on peut l'embarquer dans le dossier emporté,
  contrairement aux cartes Aeroweb). Gros avantage vs Aeroweb.
- **Provider `VacProvider`** indexé sur les terrains du contexte
  (`flight_aerodromes` + points tournants). Rend des PDF/images embarqués.
- **Travail** : rétro-ingénier le motif d'URL des VAC dans l'eAIP (comme SOFIA /
  Aeroweb). Attention au **cycle AIRAC** (change toutes les 4 semaines) — viser
  le cycle courant, pas une URL figée.

### 6. Serveur MCP  🔵 idée initiale

Exposer `assemble_briefing` et les providers comme outils MCP → interroger le
briefing en conversation depuis Claude. La « toolbox » du départ.

---

## Ordre proposé

`1 (pistes SIA)` → `3 (avion, débloque le go/no-go)` → `4 (nav/déroutement)` +
`5 (VAC)` → `2 (espaces + viewer, qui devient aussi l'éditeur de route)` →
`6 (MCP)`. À réordonner selon l'envie — le viewer peut passer devant si c'est ça
qui motive, d'autant qu'il porte la création de route.

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
