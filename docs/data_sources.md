# Sources de données

Inventaire des sources qu'`aerobriefer` interroge, leur licence, et le rôle
d'**OpenAIP**. Principe transverse : **aucune IA** dans la chaîne de génération ;
toute donnée « de référence » est téléchargée puis mise en cache localement
(jamais committée, cf. `.gitignore`), et on **vérifie toujours** sur la carte VAC
officielle avant un vol.

## Configuration (variables d'environnement)

| Variable | Rôle | Requis ? |
|---|---|---|
| `OPENAIP_KEY` | Clé API OpenAIP (compte gratuit sur openaip.net) — points de report VFR + espaces aériens | Recommandé (sinon repli communautaire) |
| `AEROWEB_LOGIN` / `AEROWEB_PASSWORD` | Aeroweb Météo-France (METAR/TAF/TEMSI/WINTEM) | Pour la météo MF |
| `ign_scan_ws` | Clé WMTS IGN Géoplateforme (fonds SCAN-OACI / VAC du viewer) | Pour la carte OACI |
| `AEROBRIEFER_*_DIR` | Redirige un cache vers une fixture (tests, hors-ligne) | Non |

Les identifiants ne sont **jamais** committés : uniquement lus depuis l'env.

## Données aéronautiques de référence (fetch + cache)

| Domaine | Source actuelle | Licence | OpenAIP |
|---|---|---|---|
| Points de report VFR | **OpenAIP** (repli `vrp_france`) | ODbL | ✅ migré — couvre les petits terrains (ex. LFCY : S/N/NE/SE/W) |
| Espaces aériens | **OpenAIP** (repli planeur-net) | ODbL | ✅ migré — classe/type/limites **+ fréquences** de contrôle |
| Aérodromes / pistes | OurAirports (`airports.csv`, `runways.csv`) + `runways_supplement.csv` local | domaine public | 🔎 candidat (consolidation possible) |
| Fréquences terrain | OurAirports `airport-frequencies.csv` | domaine public | 🔎 candidat |
| VOR / NDB / DME | OurAirports `navaids.csv` | domaine public | 🔎 candidat |
| Structure SIA (FIR/AIXM) | `data.cquest.org` export BD SIA | © SIA | partiel |
| Obstacles | *(aucune aujourd'hui)* | — | 🆕 OpenAIP a une couche obstacles → fiabiliserait le Zmin (relief **+** obstacles) |

## Terrain & vent (calcul)

| Donnée | Source | Notes |
|---|---|---|
| Élévation (Zmin par branche) | Open-Meteo Elevation API | relief max du couloir + 1000 ft |
| Vent en altitude / surface | Open-Meteo Forecast API | triangle des vitesses, piste favorable |

*Hors périmètre OpenAIP.*

## Météo & NOTAM (temps réel)

| Donnée | Source | Licence |
|---|---|---|
| METAR / TAF / TEMSI / WINTEM | Aeroweb (Météo-France) | © Météo-France — images non redistribuées |
| Prévision ponctuelle | met.no | — |
| METAR / TAF / SIGMET (secours) | NOAA `aviationweather.gov` | domaine public |
| NOTAM | SOFIA (DGAC / SIA) | © |

*Hors périmètre OpenAIP.*

## Cartes (tuiles du viewer)

| Fond | Source | Licence |
|---|---|---|
| SCAN-OACI 500k / VAC | IGN Géoplateforme WMTS (`data.geopf.fr`) | © IGN — usage personnel |
| Satellite | Esri World Imagery + IGN ortho | © respectifs |
| Moteur 3D | three.js (CDN jsdelivr) | MIT |

*Hors périmètre OpenAIP.*

## Rôle d'OpenAIP — état & pistes

- **Fait** : points de report VFR, espaces aériens (licence ODbL claire, plus
  complet et à jour que les sources communautaires précédentes). Les deux
  retombent automatiquement sur l'ancienne source si `OPENAIP_KEY` est absente
  (et les tests forcent ce repli pour rester hors-ligne et déterministes).
- **Pistes** : couche **obstacles** (nouveau, pour le Zmin) ; consolidation
  possible des aérodromes/pistes/fréquences/navaids (aujourd'hui OurAirports).
- **Idée viewer** : superposer espaces (déjà en 3D) **et** obstacles.

### Note de licence OpenAIP (ODbL)

Usage personnel : OK. Cache local **gitignoré**, pas de redistribution du brut.
Si un jour une base dérivée est publiée : attribution OpenAIP + partage à
l'identique requis.

## Détails d'implémentation

- Client OpenAIP : `src/aerobriefer/data/openaip.py` (auth header
  `x-openaip-api-key`, requête `pos`+`dist` en mètres, cache disque par zone).
- Points de report : `src/aerobriefer/data/reporting_points.py`.
- Espaces (mapping enums → modèle domaine) : `src/aerobriefer/data/airspace.py`.
