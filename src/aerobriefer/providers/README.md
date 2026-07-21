# Providers

Un **provider** transforme une source externe (API, portail) en objets du
domaine (`Metar`, `Taf`, `Notam`, `Sigmet`, `ForecastPoint`, `Chart`). Il ne
décide jamais de ce qui est pertinent : il collecte pour un `BriefingContext` et
rend des `Sourced[...]` horodatés. Le contrat commun est dans [`base.py`](base.py).

## Règles communes à tous les providers

1. **Un échec LÈVE `ProviderError`, jamais une liste vide silencieuse.** C'est
   l'agrégateur (`assemble.py`) qui capture l'exception et la convertit en
   `ProviderFailure` visible dans le dossier. Un « 0 NOTAM » dû à un parseur
   cassé est plus dangereux que pas de briefing du tout.
2. **Le texte brut est toujours conservé** à côté du décodé. Si un parseur
   échoue, la donnée remonte quand même avec son brut.
3. **UTC uniquement** via `UtcDateTime` — le lint bannit `datetime` nu.
4. **Politesse réseau** : client HTTP via `cache.make_client()`, timeouts
   explicites, User-Agent identifiable, aucune boucle de polling.

## Tableau des providers

| Provider | Classe(s) | Donnée | Source | Auth | Coût | Licence / CGU | `is_critical` |
|---|---|---|---|---|---|---|---|
| **NOAA — METAR/TAF** | `NoaaMetarProvider`, `NoaaTafProvider` | Observations & prévisions de terrain | [aviationweather.gov](https://aviationweather.gov) (NOAA/AWC) | aucune | gratuit | domaine public (US Gov) | METAR ✅ / TAF ❌ |
| **SIGMET** | `SigmetProvider` | Phénomènes dangereux en route (orage, turbulence, givrage…) | aviationweather.gov `isigmet` | aucune | gratuit | domaine public (US Gov) | ❌ |
| **SOFIA** | `SofiaProvider` | NOTAM (zone + fenêtre) | [SOFIA-Briefing](https://sofia-briefing.aviation-civile.gouv.fr) (SIA / DGAC) | session anonyme (JSESSIONID, sans compte) | gratuit | Licence Ouverte Etalab 2.0 — **aucune restriction sur l'accès automatisé** | ✅ |
| **met.no** | `MetNoProvider` | Prévisions ponctuelles (vent, T°, nuages, QNH…) | [api.met.no](https://api.met.no) (Institut météo norvégien) | User-Agent identifiable obligatoire | gratuit | CC-BY 4.0 | ❌ |
| **Aeroweb** | `AerowebProvider` | Cartes (fronts, TEMSI, WINTEM, satellite, radar) | [aviation.meteo.fr](https://aviation.meteo.fr) (Météo-France) | **login requis** (`AEROWEB_LOGIN`/`AEROWEB_PASSWORD`, MD5 côté client) | compte gratuit (ayant droit) | **© Météo-France — usage strictement personnel et local, rediffusion INTERDITE** | ❌ |

### Détails par provider

- **NOAA** (`noaa.py`) — endpoint `/api/data/metar` et `/api/data/taf`, JSON.
  Décodage via `avwx` en confort ; les champs NOAA (`fltCat`, epochs) priment
  quand ils sont plus fiables. Une station sans données répond `204` (pas une
  erreur). Gère les stations de repli quand le terrain de départ n'observe pas.

- **SIGMET** (`sigmet.py`) — même hôte que NOAA. Zone POLYGONALE + plancher/
  plafond. Filtré par intersection avec la géométrie du vol. Les enregistrements
  vides renvoyés par l'API sont écartés.

- **SOFIA** (`sofia.py`) — dispatcher RPC form-encoded (Adobe AEM), un seul
  endpoint `POST /sofia` avec `:operation`. **La réponse est du JSON HTML-échappé
  dans une `<div id="Message">`, à double encodage** — la source la plus fragile,
  d'où les `sanity_check` abondants. Q-codes déjà éclatés, texte FR + EN. La
  catégorie affichée est la **rubrique métier fournie par SOFIA**, pas une
  sévérité inventée. `JSESSIONID` rafraîchi sur `cause=refresh`.

- **met.no** (`metno.py`) — respecte les en-têtes de cache (`Expires`,
  `Last-Modified`) imposés par les CGU. Convertit m/s → nœuds. Le plafond nuages
  est une **estimation dérivée** (règle 400 ft/°C), bridée hors de son domaine de
  validité — jamais présentée comme observée. Padding ±2 h optionnel.

- **Aeroweb** (`aeroweb.py`) — ⚠️ **cadre juridique en tête de module, à lire.**
  Les CGU Météo-France interdisent « toute extraction répétée et systématique »,
  « l'insertion d'une image dans une page ne lui appartenant pas », l'usage
  commercial et la mise en réseau. Le code les INCARNE : cache disque obligatoire
  (une échéance téléchargée n'est jamais re-téléchargée), TTL = cadence de
  production, une seule session. **Les images ne doivent jamais être committées
  ni republiées.**

## Infrastructure (pas des providers)

- [`base.py`](base.py) — `Provider` (Protocol), `ProviderError`, `sanity_check`.
- [`cache.py`](cache.py) — cache HTTP de **développement**, désactivé par défaut,
  activé par `AEROBRIEFER_CACHE=<secondes>`. Bruyant quand il sert : chaque
  lecture depuis le cache devient une anomalie critique dans le dossier
  (« NE PAS UTILISER EN VOL »). Ne cache jamais une réponse qui pose un cookie.
