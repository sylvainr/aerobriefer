"""Rendu déterministe d'un `BriefingPackage` en feuille de briefing A4.

Aucun LLM, aucun appel réseau, aucun rappel de provider : tout ce qui est
affiché vient du `BriefingPackage`. À `now` fixé, deux rendus du même dossier
produisent deux chaînes identiques — c'est ce qui rend le rendu testable et
auditable après le vol.

Trois règles de présentation portées par ce module, et qui sont des exigences de
sécurité, pas du style :

1. Le texte BRUT aéro (METAR, TAF, NOTAM) n'est jamais converti en heure locale.
   Il reste en Z, parce que c'est ce que le pilote compare à la radio. Seules les
   synthèses dérivées et les en-têtes s'affichent en double « 10:00L / 08:00Z ».
2. Rien de périmé n'est masqué : c'est signalé, jamais retiré.
3. Un dossier incomplet le dit en tête de première page. Un briefing amputé qui
   a l'air complet est le pire résultat possible.
"""

from __future__ import annotations

import base64
import math
import re
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..data import supaip
from ..data import tiles as tiles_mod
from ..domain.context import BriefingContext
from ..domain.freshness import describe as freshness_label
from ..domain.freshness import max_age_minutes
from ..domain.geo import Circle, Corridor
from ..domain.geo import Union as GeoUnion
from ..domain.models import CLOUD_BASE_FT_PER_C, Aerodrome, ForecastPoint, Notam
from ..domain.package import BriefingPackage, ProviderFailure
from ..domain.route import Route
from ..domain.sourced import Sourced
from ..domain.window import TimeWindow, UtcDateTime, utcnow
from .arome_probe import Probe, probe_chart

DEFAULT_DISPLAY_TIMEZONE = "Europe/Paris"
"""Le fuseau d'affichage est un PARAMÈTRE du renderer, jamais une constante
enfouie : un briefing rendu pour un vol aux Antilles ou en Corse ne doit pas
hériter du fuseau de l'auteur du code."""

DEFAULT_STALE_AFTER_MINUTES = 90.0

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "briefing.html.j2"

PURPOSE_LABELS: dict[str, str] = {
    "local": "Vol local",
    "navigation": "Navigation",
    "diversion": "Déroutement",
}

CHART_LABELS: dict[str, str] = {
    "temsi": "TEMSI — temps significatif",
    "wintem": "WINTEM — vent et température en altitude",
    "front": "Carte de fronts",
    "satellite": "Image satellite",
    "radar": "Image radar",
    "arome_visi": "AROME — visibilité (maille fine)",
    "arome_plafond": "AROME — plafond (maille fine)",
    "arome_nebul_bas": "AROME — nébulosité basse (maille fine)",
}

#: Ordre de lecture des cartes : DU PLUS GRAND AU PLUS PETIT. On part de la
#: situation générale (fronts, échelle continentale), on descend à l'échelle
#: régionale prévue (TEMSI, WINTEM), puis à l'imagerie observée (radar,
#: satellite) — après quoi le briefing enchaîne sur le terrain (TAF, METAR).
#: Un type absent de cette liste passe en fin, dans son ordre d'apparition.
CHART_ORDER: tuple[str, ...] = (
    "front",
    "temsi",
    "wintem",
    # AROME vient APRÈS la TEMSI : même échelle régionale, mais résolution plus
    # fine et champ continu. On lit d'abord le zonage du prévisionniste, puis le
    # détail du modèle — jamais l'inverse, le modèle n'est pas expertisé.
    "arome_visi",
    "arome_plafond",
    "arome_nebul_bas",
    "radar",
    "satellite",
)

# Guide aviation Météo-France : page de LÉGENDE par type de carte. Le lien vit
# SOUS la carte concernée, à portée de clic (PDF, nouvel onglet).
GUIDE_URL = "https://aviation.meteo.fr/documentation/guide_aviation.pdf"
GUIDE_PAGES: dict[str, int] = {
    "temsi": 9,
    "wintem": 15,
    "front": 15,
    "satellite": 16,
    "radar": 17,
}
GUIDE_METAR_PAGE = 18  # déchiffrer un message METAR

#: CADENCE DE MISE À DISPOSITION, par rubrique. Texte STATIQUE — c'est une
#: propriété de la source, pas de ce dossier.
#:
#: Pourquoi l'afficher : un âge tout seul ne dit pas s'il faut rebriefer. « TAF
#: vieux de 5 h » n'a pas le même sens pour un TAF long (renouvelé toutes les
#: 6 h : normal) et pour un METAR (toutes les 30 min : anormal). Savoir quand
#: tombe la prochaine fournée évite de relancer trop tôt — rien de neuf — ou
#: trop tard, après avoir raté la mise à jour.
#:
#: Source : guide aviation Météo-France, page PDF citée en commentaire. Les
#: cadences NON documentées par le guide sont marquées « constaté » : elles
#: viennent des produits réellement reçus, et peuvent changer sans préavis.
RELEASE_CADENCE: dict[str, str] = {
    # Guide p.18 : « toutes les demi-heures en AUTO en France ».
    "metar": "Toutes les 30 min en station automatique (France). "
    "SPECI émis hors cadence dès qu'un seuil est franchi.",
    # Guide p.22 : « Le TAF court est renouvelé toutes les 3 heures, le long,
    # toutes les 6 heures. » Un seul type par aérodrome.
    "taf": "TAF court : toutes les 3 h (validité 9 h). "
    "TAF long : toutes les 6 h (validité 24 ou 30 h). Un seul type par terrain.",
    # Guide p.25 : établi au plus 4 h avant le début de validité, validité < 4 h
    # (6 h pour cendres volcaniques et cyclones tropicaux).
    "sigmet": "Aucune cadence : émis à l'occurrence, au plus 4 h avant le début de "
    "validité. Validité inférieure à 4 h (6 h : cendres volcaniques, cyclones).",
    "notam": "Aucune cadence : publiés en continu. Un NOTAM peut paraître à tout moment.",
    "forecast": "Prévision de modèle, réactualisée à chaque tour de modèle.",
    # Guide p.9.
    "temsi": "Toutes les 3 h de 06 à 00 UTC, mise à disposition 2 h avant l'échéance.",
    # Guide p.15.
    "wintem": "Toutes les 3 h à partir de 00 UTC.",
    # Guide p.15 : donne les échéances (12 h et 24 h), pas la cadence d'émission.
    "front": "Analyse, puis échéances à 12 h et 24 h.",
    # Runs exposés par le visualiseur maille fine (cf. providers/arome.py).
    "arome_visi": "Runs 03, 06, 12 et 18 UTC ; échéances horaires.",
    "arome_plafond": "Runs 03, 06, 12 et 18 UTC ; échéances horaires.",
    "arome_nebul_bas": "Runs 03, 06, 12 et 18 UTC ; échéances horaires.",
    "radar": "Nouvelle image toutes les 15 min (constaté).",
    "satellite": "Nouvelle image toutes les 15 min (constaté).",
}


#: La planche WINTEM FRANCE n'est pas une carte : elle empile TROIS panneaux —
#: FL020 (950 hPa) en grand, puis FL050 (850 hPa) et FL100 (700 hPa) en petit.
#: Pour un VFR à 2500 ft, seul le premier compte ; les deux autres mangent les
#: deux tiers de la hauteur.
#:
#: Fraction MESURÉE sur la planche réelle (1024×1800) : le cadre du panneau
#: FL020 se termine au pixel 1010, en-tête compris, soit 0.567 de la hauteur.
WINTEM_TOP_PANEL_FRACTION = 0.567

#: Rapport largeur/hauteur de la planche à trois panneaux. On ne découpe QUE si
#: l'image a cette forme : si Météo-France remanie la planche, mieux vaut ne pas
#: proposer le bouton que couper au mauvais endroit.
_WINTEM_SHEET_RATIO = 1024 / 1800
_SHEET_RATIO_TOLERANCE = 0.03

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes | None) -> tuple[int, int] | None:
    """Dimensions d'un PNG, lues dans son IHDR. Stdlib seule — le rendu ne tire
    aucune bibliothèque d'image."""
    if data is None or len(data) < 24:
        return None
    if not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def top_panel_ratio(kind: str, content: bytes | None) -> float | None:
    """Rapport largeur/hauteur du SEUL panneau haut, ou `None` si non applicable.

    Sert à recadrer en CSS (`aspect-ratio` + `object-fit: cover`) sans jamais
    toucher à l'image : le brut reste le brut, on ne fait que masquer le bas.
    """
    if kind != "wintem":
        return None
    size = png_size(content)
    if size is None:
        return None
    width, height = size
    if height <= 0 or abs(width / height - _WINTEM_SHEET_RATIO) > _SHEET_RATIO_TOLERANCE:
        return None
    return width / (height * WINTEM_TOP_PANEL_FRACTION)


#: Demi-circonférence de la Terre en Mercator sphérique — borne de l'axe X et Y
#: d'EPSG:3857, le repère des tuiles OSM comme du WMS AROME.
_MERC_HALF = 20037508.342789244

#: Plafond du NOMBRE de tuiles du fond. On borne le compte, pas la largeur en
#: pixels : c'est le nombre de requêtes vers OSM qu'il faut tenir. Quinze cartes
#: AROME dans un dossier, c'est autant de grilles — à 45 tuiles pièce on tape
#: 675 fois le serveur de tuiles pour un gain de netteté invisible à l'impression.
_BASEMAP_MAX_TILES = 16


def _bbox_3857(url: str) -> tuple[float, float, float, float] | None:
    """Emprise EPSG:3857 relue dans l'URL du GetMap.

    On la RELIT au lieu de la recalculer : l'image a été demandée pour cette
    emprise-là, et un second calcul finirait par diverger du premier — le fond
    de carte glisserait sous les nuages sans que rien ne le signale.
    """
    query = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(query)
    if (params.get("CRS") or params.get("SRS") or [""])[0].upper() != "EPSG:3857":
        return None
    raw = (params.get("BBOX") or [""])[0].split(",")
    if len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except ValueError:
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _world_px(x_m: float, y_m: float, zoom: int) -> tuple[float, float]:
    """Mètres EPSG:3857 → « pixel monde » des tuiles (256 px/tuile).

    L'axe Y s'inverse : le Mercator monte vers le nord, les pixels descendent.
    """
    span = 256.0 * (2**zoom)
    return (
        (x_m + _MERC_HALF) / (2 * _MERC_HALF) * span,
        (_MERC_HALF - y_m) / (2 * _MERC_HALF) * span,
    )


class TileRegistry:
    """Tuiles du document : chaque tuile n'est encodée QU'UNE FOIS.

    Un dossier contient une quinzaine de cartes AROME qui regardent la même
    région : sans registre, les mêmes neuf tuiles étaient ré-encodées en base64
    quinze fois et le briefing passait de 4 à 10 Mo — pour les mêmes pixels.
    On émet donc une règle CSS par tuile distincte et chaque carte s'y réfère
    par sa classe.

    `background-image` plutôt qu'un `<img>` : c'est le seul moyen de partager un
    encodage entre plusieurs emplacements. La contrepartie — les fonds ne
    s'impriment pas par défaut — est levée explicitement par
    `print-color-adjust: exact` sur la tuile ; un dossier de vol s'imprime.
    """

    def __init__(self, resolve: Callable[[str], str | None] | None = None) -> None:
        self._resolve = resolve
        self._classes: dict[str, str] = {}  # data: URI → nom de classe

    def register(self, url: str) -> str | None:
        """Nom de classe CSS de la tuile, ou `None` si on ne l'a pas."""
        if self._resolve is None:
            return None
        data_uri = self._resolve(url)
        if data_uri is None:
            return None
        known = self._classes.get(data_uri)
        if known is None:
            known = f"t{len(self._classes)}"
            self._classes[data_uri] = known
        return known

    def css(self) -> list[tuple[str, str]]:
        """(classe, data: URI) à écrire dans la feuille de style du document."""
        return [(cls, uri) for uri, cls in self._classes.items()]


def arome_basemap(
    url: str,
    route: Route | None = None,
    tiles: TileRegistry | None = None,
) -> dict[str, Any] | None:
    """Fond OpenStreetMap et tracé de route sous une couche AROME.

    Les couches AROME sont des CALQUES transparents : sans fond, on voit la
    forme des nuages mais pas où ils sont. On pose donc dessous les tuiles OSM
    couvrant exactement l'emprise de l'image, plus la route, tout en pourcentages
    — le fond suit l'image quelle que soit sa taille d'affichage.

    Aucun appel réseau ICI : le rendu ne connaît pas le réseau. Le `TileRegistry`
    est alimenté par l'appelant (le CLI lui branche un `data.tiles.TileStore`) et
    rend le nom de classe CSS d'une tuile déjà encodée, ou `None` s'il ne l'a
    pas. Une tuile non résolue est simplement ABSENTE du fond : jamais une URL
    distante, que le navigateur irait chercher pour se faire servir « Access
    blocked ».

    Sans registre, le fond n'a aucune tuile — la couche AROME et la route
    restent, et le rendu le dit sous la carte.
    """
    bbox = _bbox_3857(url)
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox

    # Zoom le plus DÉTAILLÉ dont la grille tient sous le plafond de tuiles.
    # On descend depuis le plus fin : le premier qui tient est le meilleur.
    zoom = 1
    for candidate in range(14, 0, -1):
        left, top = _world_px(x0, y1, candidate)
        right, bottom = _world_px(x1, y0, candidate)
        across = int(right // 256) - int(left // 256) + 1
        down = int(bottom // 256) - int(top // 256) + 1
        if across * down <= _BASEMAP_MAX_TILES:
            zoom = candidate
            break

    left_px, top_px = _world_px(x0, y1, zoom)  # coin haut-gauche
    right_px, bottom_px = _world_px(x1, y0, zoom)
    width_px = right_px - left_px
    height_px = bottom_px - top_px
    if width_px <= 0 or height_px <= 0:
        return None

    span = 2**zoom
    found: list[dict[str, Any]] = []
    for tx in range(int(left_px // 256), int(right_px // 256) + 1):
        for ty in range(int(top_px // 256), int(bottom_px // 256) + 1):
            if not (0 <= tx < span and 0 <= ty < span):
                continue
            css_class = tiles.register(tiles_mod.tile_url(zoom, tx, ty)) if tiles else None
            if css_class is None:
                continue
            found.append(
                {
                    "cls": css_class,
                    "left": (tx * 256 - left_px) / width_px * 100.0,
                    "top": (ty * 256 - top_px) / height_px * 100.0,
                    "width": 256 / width_px * 100.0,
                    "height": 256 / height_px * 100.0,
                }
            )

    path: list[dict[str, float]] = []
    if route is not None:
        for waypoint in route.waypoints:
            wx, wy = _to_mercator_m(waypoint.position.lat, waypoint.position.lon)
            px, py = _world_px(wx, wy, zoom)
            path.append(
                {"x": (px - left_px) / width_px * 100.0, "y": (py - top_px) / height_px * 100.0}
            )

    return {
        "tiles": found,
        "route": path,
        "aspect": width_px / height_px,
    }


def _to_mercator_m(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 → EPSG:3857 (mètres)."""
    x = math.radians(lon) * 6378137.0
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * 6378137.0
    return x, y


#: LÉGENDE DES COUCHES AROME.
#:
#: Météo-France ne publie AUCUNE échelle pour ces couches : ni
#: `GetLegendGraphic`, ni `GetCapabilities` (le service n'est pas un WMS
#: complet, les deux répondent 400), ni le visualiseur — ses fichiers de
#: localisation ne portent que les noms des paramètres, et le guide aviation
#: ignore le produit. Sondé le 2026-08-30.
#:
#: Les classes ci-dessous sont donc RELEVÉES sur les images réelles, et leur
#: ORDRE est établi par emboîtement : on compte, pour chaque classe, la part de
#: ses voisins vides. Une classe entourée d'autres classes (0 % de voisins
#: vides) est la plus interne, donc la plus marquée. Mesuré sur le dossier du
#: 2026-08-31, repris et ÉLARGI le 2026-09-20 (mesure ci-dessous).
#:
#: Relevé du 2026-09-20 sur quinze images (histogramme des pixels opaques, puis
#: emboîtement) — la table de sondage `arome_probe` rendait « hors légende » une
#: teinte qui couvrait un TIERS de la surface peinte, ce qui a mis le trou en
#: évidence :
#:   visibilité — #fefe00 (17,7 %, 4,0 % de voisins vides)
#:              < #fea400 (41,8 %, 0,8 %)
#:              < #fe0000 (35,2 %, 0,3 %)   ← manquait
#:   plafond    — #3d7bb0 (0,2 %, 7,1 %) < #180545 (99,5 %, 0,5 %)
#: Les ~200 autres teintes sont des bords d'aplat anti-aliasés (< 0,2 % chacune).
#:
#: On n'affiche AUCUNE valeur en km ni en pieds : la source ne les donne pas, et
#: inventer un seuil sur une feuille de briefing serait pire que de se taire.
AROME_LEGENDS: dict[str, dict[str, Any]] = {
    "arome_visi": {
        "definition": "Visibilité horizontale prévue près du sol.",
        "behaviour": "La couche ne peint QUE les zones de visibilité réduite. "
        "Pas de couleur = aucune réduction prévue à cet endroit.",
        "classes": [
            ("#fefe00", "réduction modérée"),
            ("#fea400", "réduction marquée"),
            ("#fe0000", "réduction sévère"),
        ],
        "ordered": True,
    },
    "arome_plafond": {
        "definition": "Hauteur de la base de la première couche couvrant au moins "
        "5/8 du ciel (BKN ou OVC), au-dessus du sol.",
        "behaviour": "La couche ne peint QUE les zones de plafond bas. "
        "Pas de couleur = aucun plafond sous seuil prévu à cet endroit.",
        "classes": [("#3d7bb0", "plafond bas"), ("#180545", "plafond très bas")],
        # Les libellés affirmaient déjà un ordre ; l'emboîtement le MESURE
        # désormais (2026-09-20), la page peut donc le justifier au lieu de le
        # poser. Réserve honnête : la classe « plafond bas » est rare (0,2 % des
        # pixels ce jour-là), la mesure repose sur peu de contours.
        "ordered": True,
    },
    "arome_nebul_bas": {
        "definition": "Fraction du ciel couverte par les nuages de l'étage inférieur.",
        # Corrigé le 2026-09-20 : la couche était décrite comme un « champ continu
        # sur toute la zone », or la mesure montre qu'elle laisse transparent
        # partout où aucun nuage bas n'est prévu (90 % du domaine ce jour-là).
        # La table de sondage rendait « — » en face d'une description qui
        # promettait une valeur partout : l'une des deux mentait.
        "behaviour": "La couche peint les zones où des nuages bas sont prévus, "
        "du peu couvert au très couvert. Pas de couleur = aucun nuage bas prévu "
        "à cet endroit.",
        "classes": [
            ("#ebebeb", "peu couvert"),
            ("#ddd2c7", ""),
            ("#d5baa0", ""),
            ("#c7a381", "très couvert"),
        ],
        "ordered": True,
    },
}


# --------------------------------------------------------------------------
# Formatage des heures
# --------------------------------------------------------------------------


def _local_wallclock(instant: UtcDateTime, tz: ZoneInfo, fmt: str) -> str:
    """Formate `instant` dans le fuseau d'affichage.

    Subtilité vicieuse : `UtcDateTime.astimezone(tz)` renvoie une instance dont
    les CHAMPS portent bien l'heure locale (le calcul d'offset de CPython est
    correct, transitions d'heure d'été comprises) mais dont le `tzinfo` est
    réétiqueté UTC par `UtcDateTime.__new__`. L'objet est donc un mensonge
    ambulant : le laisser fuir hors de cette fonction, c'est offrir à quelqu'un
    de le soustraire à un vrai UTC et de se tromper de deux heures.

    On le confine ici et on n'en ressort qu'une CHAÎNE. Aucun datetime converti
    ne franchit cette frontière.
    """
    return instant.astimezone(tz).strftime(fmt)


def _validity_span(
    notam: Notam, start: UtcDateTime, end: UtcDateTime, now: UtcDateTime, tz: ZoneInfo
) -> str:
    """« depuis 11 jours → jusqu'au 22/07/2026 » — durée saisie d'un coup d'œil.

    Lire deux dates de validité pour estimer « ça dure combien de temps » est
    pénible ; cette ligne le donne directement : depuis quand c'est en vigueur
    (heures si < 1 j, jamais « 0 jour »), et jusqu'à quand. Les NOTAM permanents
    affichent « permanent » plutôt qu'une date sentinelle de 2099.
    """
    head = _relative_since(start, now)  # « depuis 11 jours » / « dans 2 heures »
    if notam.is_open_ended:
        return f"{head} → permanent"
    return f"{head} → jusqu'au {_local_wallclock(end, tz, '%d/%m/%Y')}"


def _zulu(instant: UtcDateTime, fmt: str = "%H:%M") -> str:
    return instant.strftime(fmt)


try:  # français si disponible ; sinon l'anglais de humanize, jamais une erreur
    import humanize as _humanize

    _humanize.i18n.activate("fr_FR")
except Exception:  # noqa: BLE001 - locale absente : on dégrade proprement
    try:
        import humanize as _humanize
    except Exception:  # noqa: BLE001
        _humanize = None  # type: ignore[assignment]


_ONE_DAY_S = 86400.0


def _human_span(delta_seconds: float) -> str:
    """Durée lisible : en HEURES sous un jour, en jours/mois au-delà.

    Sous 24 h on descend à l'heure — « depuis 3 heures » est bien plus parlant
    que « depuis 0 jour », qui n'informe pas. Au-delà, on reste en jours/mois
    (« 19 jours », « 3 mois et 18 jours ») : l'heure exacte d'entrée en vigueur
    ne change rien à la décision une fois qu'on parle de semaines.
    """
    seconds = abs(delta_seconds)
    from datetime import timedelta  # noqa: TID251 - une durée, pas un instant

    if _humanize is None:
        if seconds < _ONE_DAY_S:
            return f"{int(seconds // 3600)} h"
        return f"{int(seconds // _ONE_DAY_S)} j"
    unit = "hours" if seconds < _ONE_DAY_S else "days"
    return _humanize.precisedelta(timedelta(seconds=seconds), minimum_unit=unit, format="%d")


def _relative_since(instant: UtcDateTime, now: UtcDateTime) -> str:
    """« depuis 5 jours », « dans 2 heures » — relatif à `now`, déterministe."""
    delta_s = (now - instant).total_seconds()
    span = _human_span(delta_s)
    return f"depuis {span}" if delta_s >= 0 else f"dans {span}"


def format_dual(instant: UtcDateTime, tz: ZoneInfo, *, with_date: bool = False) -> str:
    """« 10:00L / 08:00Z » — la forme des synthèses dérivées et des en-têtes.

    Jamais appliquée au texte brut aéro, qui reste en Z tel quel.
    """
    if with_date:
        local = _local_wallclock(instant, tz, "%d/%m %H:%M")
        return f"{local}L / {_zulu(instant, '%d/%m %H:%M')}Z"
    return f"{_local_wallclock(instant, tz, '%H:%M')}L / {_zulu(instant)}Z"


def format_local_only(instant: UtcDateTime, tz: ZoneInfo) -> str:
    """« 20/07 10:00L » — pour les lignes où le Z figure déjà juste à côté et où
    répéter les deux formes nuit plus qu'elle n'aide."""
    return f"{_local_wallclock(instant, tz, '%d/%m %H:%M')}L"


def format_age(minutes: float) -> str:
    """Âge lisible d'un coup d'œil. Un âge négatif (donnée émise « dans le
    futur », horloge désynchronisée ou prévision) est affiché tel quel plutôt
    que masqué."""
    if minutes < 0:
        return f"dans {format_age(-minutes)}"
    if minutes < 1:
        return "< 1 min"
    if minutes < 90:
        return f"{int(round(minutes))} min"
    hours, rest = divmod(int(round(minutes)), 60)
    return f"{hours} h {rest:02d}"


# --------------------------------------------------------------------------
# Vue : ce que le template consomme
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """La cartouche de traçabilité affichée sur chaque bloc de données."""

    source: str
    age_label: str
    ref_iso: str
    """Instant (ISO UTC) depuis lequel l'âge est compté — pour l'âge VIVANT côté
    navigateur (data-age). = émission si connue, sinon récupération."""
    is_stale: bool
    freshness_limit: str
    """Seuil retenu, en clair. Un « périmé » sans seuil affiché est une
    affirmation invérifiable ; avec, le pilote juge lui-même."""
    issued_dual: str | None
    retrieved_dual: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class ChartView:
    kind_label: str
    kind: str
    is_observation: bool
    """Vrai pour radar/satellite (issued_at = valid_at → l'âge est RÉEL). Faux
    pour les prévisions (front/TEMSI/WINTEM) : issued_at inconnu, l'« âge »
    retomberait sur l'heure de téléchargement — trompeur, donc on ne l'affiche
    pas ; on montre la validité et l'heure de récupération à la place."""
    area: str | None
    flight_level: str | None
    issued_dual: str | None
    valid_dual: str | None
    basemap: dict[str, Any] | None
    """Fond OSM + route sous une couche AROME (calque transparent), sinon `None`."""
    panel_ratio: float | None
    """Rapport L/H du panneau FL020 seul (planche WINTEM), sinon `None`.
    Permet un recadrage CSS : l'image n'est jamais modifiée."""
    data_uri: str | None
    """`None` quand la carte n'est pas embarquée : elle est alors INUTILISABLE
    hors ligne, et le template le dit au lieu de laisser un cadre vide."""
    url: str
    source: SourceInfo
    covers_window: bool
    """Faux si l'échéance de la carte tombe hors de la fenêtre de vol.

    TEMSI et WINTEM ne portent que quelques heures : pour un vol préparé la
    veille, les seules cartes publiées sont celles du jour même. On les affiche
    — la tendance reste utile — mais jamais sans le dire."""
    valid_label: str
    """Échéance courte pour l'étiquette du lecteur animé (ex. « 21/07 12:00Z »)."""
    probes: dict[str, Probe]
    """Ce que la couche dit AU DROIT de chaque terrain (clé = OACI), mesuré et
    non apprécié à l'œil — cf. `arome_probe`. Vide hors couches AROME."""


#: Référence à un SUPPLÉMENT À L'AIP dans le texte d'un NOTAM.
#:
#: Pourquoi ça compte : un NOTAM qui cite un SUP AIP ne CONTIENT pas
#: l'information — il l'ANNONCE. Le détail (cartes, coordonnées des zones,
#: conditions) vit dans un document séparé qu'il faut aller lire. Le cas le plus
#: courant est le « TRIGGER NOTAM », dont c'est l'unique fonction. Lu vite, il
#: ressemble à un NOTAM anodin ; c'est en réalité un renvoi à respecter.
#:
#: Les deux ordres existent dans le même dossier : « SUP AIP 162/26 » côté
#: français, « AIP SUP 162/26 » côté OACI, avec un « AIRAC » parfois intercalé.
_AIP_SUP_REF = re.compile(
    r"\b(?:AIRAC\s+)?(?:AIP\s+SUP|SUP\s+AIP)(?:\s+AIRAC)?\s+(\d{1,4}/\d{2})\b",
    re.IGNORECASE,
)
_TRIGGER = re.compile(r"\bTRIGGER\b", re.IGNORECASE)


def _anchor(identifier: str) -> str:
    """« R1000/26 » → « notam-R1000-26 ». Un fragment d'URL n'accepte pas « / »."""
    return "notam-" + re.sub(r"[^0-9A-Za-z]+", "-", identifier).strip("-")


def aip_sup_references(*texts: str | None) -> list[str]:
    """Références de SUP AIP citées, dédoublonnées et triées (ex. `["162/26"]`)."""
    found: set[str] = set()
    for text in texts:
        if text:
            found.update(m.group(1) for m in _AIP_SUP_REF.finditer(text))
    return sorted(found)


@dataclass(frozen=True, slots=True)
class NotamView:
    identifier: str
    anchor: str
    """Identifiant utilisable en ancre HTML : « R1000/26 » → « notam-R1000-26 ».
    Le « / » d'un numéro de NOTAM n'est pas valide dans un fragment d'URL."""
    raw_text: str
    decoded_text: str | None
    category_label: str
    """Rubrique attribuée par la SOURCE (SOFIA), affichée telle quelle. Remplace
    l'ancienne « sévérité » maison, jugée trop subjective."""
    validity_zulu: str
    validity_local: str
    activation_label: str
    """Date d'entrée en vigueur en clair — c'est le repère chronologique honnête,
    à la place de l'« âge » de notre requête qui n'a pas de sens pour un NOTAM."""
    validity_span: str
    """« depuis 11 jours → jusqu'au 22/07/2026 », ou « → permanent ». Vue d'un
    coup d'œil de l'ancienneté ET de la fin, sans avoir à lire deux dates."""
    affected_icao: str | None
    q_code: str | None
    limits: str | None
    source: SourceInfo
    aip_sup_refs: list[str]
    """SUP AIP cités par ce NOTAM. Non vide = le texte ci-dessous ne suffit pas,
    il faut aller lire le supplément."""
    aip_sup_links: list[dict[str, str]]
    """Mêmes références, résolues vers le document du SIA : `ref`, `url`,
    `title`, `resolved`. Non résolu → `url` pointe la LISTE, jamais une URL
    fabriquée, et `resolved` est vide."""
    is_trigger: bool
    """« TRIGGER NOTAM » : son seul rôle est d'annoncer un SUP AIP."""


class HtmlRenderer:
    """`BriefingPackage` → HTML A4 imprimable.

    Ne rappelle jamais un provider. Si une donnée manque, c'est le dossier qui
    est sous-spécifié, pas au renderer d'aller la chercher.
    """

    def __init__(
        self,
        *,
        display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
        stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
        supaip_local: dict[str, str] | None = None,
        supaip_index: supaip.SupAipIndex | None = None,
        resolve_tile: Callable[[str], str | None] | None = None,
    ) -> None:
        self.supaip_local = dict(supaip_local or {})
        """SUP AIP téléchargés à côté du dossier : `référence → nom de fichier`.
        Quand une copie locale existe, c'est ELLE qu'on lie — un dossier de vol
        doit rester consultable sans réseau."""
        self.display_timezone = display_timezone
        self.stale_after_minutes = stale_after_minutes
        self._tiles = TileRegistry(resolve_tile)
        """Registre des tuiles du fond de carte. L'appelant fournit de quoi les
        obtenir (le CLI : un `data.tiles.TileStore`) ; le rendu, lui, n'appelle
        jamais le réseau, et n'émet jamais d'URL distante à charger à
        l'ouverture de la page."""
        # Index des SUP AIP. Le rendu NE FAIT PAS DE RÉSEAU : soit l'appelant
        # fournit l'index (c'est le cas du CLI, qui l'a déjà chargé pour
        # télécharger les suppléments), soit on relit le cache disque et
        # RIEN DE PLUS — `allow_network=False` n'est pas une option, c'est la
        # règle du module. Sans cache, l'index est vide et les liens retombent
        # sur la liste officielle du SIA.
        self._supaip = (
            supaip_index if supaip_index is not None else supaip.load_index(allow_network=False)
        )
        self._tz = ZoneInfo(display_timezone)
        self._window: TimeWindow | None = None  # posé par build_view avant _notam_view
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # -- helpers internes --------------------------------------------------

    def _dual(self, instant: UtcDateTime, *, with_date: bool = False) -> str:
        return format_dual(instant, self._tz, with_date=with_date)

    def _source_info(self, item: Sourced[Any], now: UtcDateTime) -> SourceInfo:
        prov = item.provenance
        return SourceInfo(
            source=prov.source,
            age_label=format_age(item.age_minutes(now)),
            ref_iso=(prov.issued_at or prov.retrieved_at).strftime("%Y-%m-%dT%H:%M:%SZ"),
            is_stale=item.is_stale(max_age_minutes(item.value), now),
            freshness_limit=freshness_label(item.value),
            issued_dual=self._dual(prov.issued_at, with_date=True) if prov.issued_at else None,
            retrieved_dual=self._dual(prov.retrieved_at, with_date=True),
            url=prov.url,
        )

    def _chart_view(
        self,
        item: Sourced[Any],
        now: UtcDateTime,
        window: TimeWindow | None = None,
        route: Route | None = None,
        sites: Sequence[tuple[str, str, float, float]] = (),
    ) -> ChartView:
        chart = item.value
        data_uri = None
        if chart.content is not None:
            media_type = chart.media_type or "image/png"
            payload = base64.b64encode(chart.content).decode("ascii")
            data_uri = f"data:{media_type};base64,{payload}"
        return ChartView(
            kind_label=CHART_LABELS.get(chart.kind, chart.kind.upper()),
            kind=chart.kind,
            is_observation=chart.issued_at is not None,
            area=chart.area,
            flight_level=chart.flight_level,
            issued_dual=self._dual(chart.issued_at, with_date=True) if chart.issued_at else None,
            valid_dual=self._dual(chart.valid_at, with_date=True) if chart.valid_at else None,
            basemap=(
                arome_basemap(chart.url, route, self._tiles)
                if chart.kind.startswith("arome_")
                else None
            ),
            # Sondé À LA COORDONNÉE de chaque terrain, pas lu sur l'image à
            # l'œil. Hors AROME, il n'y a pas de légende de classes : on ne
            # sonde pas (une carte de fronts ne se lit pas pixel par pixel).
            probes=(
                probe_chart(
                    chart.content,
                    chart.url,
                    AROME_LEGENDS.get(chart.kind),
                    [(icao, lat, lon) for icao, _name, lat, lon in sites],
                )
                if sites and chart.kind.startswith("arome_")
                else {}
            ),
            panel_ratio=top_panel_ratio(chart.kind, chart.content),
            data_uri=data_uri,
            url=chart.url,
            source=self._source_info(item, now),
            covers_window=(
                chart.valid_at is not None
                and window is not None
                and window.contains(chart.valid_at)
            ),
            valid_label=(format_local_only(chart.valid_at, self._tz) if chart.valid_at else "?"),
        )

    def _aip_sup_documents(self, notams: list[NotamView]) -> list[dict[str, Any]]:
        """Suppléments cités par le dossier, chacun avec son lien et les NOTAM
        qui l'annoncent. Regroupés en tête : dans la masse, un TRIGGER NOTAM
        ressemble à un NOTAM anodin alors que c'est un renvoi à respecter."""
        by_reference: dict[str, dict[str, Any]] = {}
        for notam in notams:
            for link in notam.aip_sup_links:
                doc = by_reference.setdefault(link["ref"], {**link, "notams": []})
                doc["notams"].append({"identifier": notam.identifier, "anchor": notam.anchor})
        return [by_reference[ref] for ref in sorted(by_reference)]

    def _aip_sup_link(self, reference: str) -> dict[str, str]:
        """Une référence de SUP AIP → le document, ou à défaut la liste.

        Jamais d'URL fabriquée : un lien inventé qui tombe sur le mauvais
        supplément est pire qu'un lien vers la liste officielle.
        """
        entry = self._supaip.lookup(reference)
        local = self.supaip_local.get(supaip.normalize_reference(reference))
        if entry is None:
            return {
                "ref": reference,
                "url": local or supaip.FALLBACK_URL,
                "title": "",
                "validity": "",
                "rules": "",
                "tooltip": f"SUP AIP {reference} — non résolu. Ouvre la liste du SIA.",
                "resolved": "",
                "local": "1" if local else "",
            }
        validity = (
            f"{entry.valid_from} → {entry.valid_to}" if entry.valid_from and entry.valid_to else ""
        )
        rules = " · ".join(entry.rules)
        # L'infobulle porte TOUT : en relisant les NOTAM un par un, on ne veut
        # pas remonter au tableau de tête pour savoir de quoi parle un renvoi.
        tooltip = " — ".join(
            part for part in (f"SUP AIP {reference}", entry.title, validity, rules) if part
        )
        return {
            "ref": reference,
            # La copie locale d'abord : elle marche en vol, l'URL du SIA non.
            "url": local or entry.url,
            "title": entry.title,
            "validity": validity,
            "rules": rules,
            "tooltip": tooltip,
            "resolved": "1",
            "local": "1" if local else "",
        }

    def _notam_view(self, item: Sourced[Any], now: UtcDateTime) -> NotamView:
        notam = item.value
        limits = None
        if notam.lower_limit_ft is not None or notam.upper_limit_ft is not None:
            low = "SFC" if notam.lower_limit_ft in (None, 0) else f"{notam.lower_limit_ft} ft"
            high = "UNL" if notam.upper_limit_ft is None else f"{notam.upper_limit_ft} ft"
            limits = f"{low} — {high}"
        start, end = notam.validity.start, notam.validity.end
        return NotamView(
            identifier=notam.identifier,
            anchor=_anchor(notam.identifier),
            raw_text=notam.raw_text,
            decoded_text=notam.decoded_text,
            category_label=notam.source_category or "Non catégorisé",
            validity_zulu=(f"{_zulu(start, '%d/%m %H:%M')}Z — {_zulu(end, '%d/%m %H:%M')}Z"),
            validity_local=(
                f"{format_local_only(start, self._tz)} — {format_local_only(end, self._tz)}"
            ),
            activation_label=f"en vigueur depuis le {_zulu(start, '%d/%m/%Y %H:%M')}Z"
            if start <= now
            else f"entre en vigueur le {_zulu(start, '%d/%m/%Y %H:%M')}Z",
            validity_span=_validity_span(notam, start, end, now, self._tz),
            affected_icao=notam.affected_icao,
            q_code=notam.q_code,
            limits=limits,
            aip_sup_refs=aip_sup_references(notam.raw_text, notam.decoded_text),
            aip_sup_links=[
                self._aip_sup_link(ref)
                for ref in aip_sup_references(notam.raw_text, notam.decoded_text)
            ],
            is_trigger=bool(_TRIGGER.search(notam.raw_text or "")),
            source=self._source_info(item, now),
        )

    def _stale_count(self, items: Iterable[Sourced[Any]], now: UtcDateTime) -> int:
        return sum(1 for i in items if i.is_stale(max_age_minutes(i.value), now))

    def _forecast_groups(
        self, package: BriefingPackage, moment: UtcDateTime, window: TimeWindow
    ) -> list[dict[str, Any]]:
        """Prévisions met.no groupées par point d'échantillonnage (départ,
        arrivée, dégagements). Pour un point qui est un terrain avec des pistes,
        on ajoute le QFU favorable et les composantes face/arrière + traversier,
        heure par heure — même lecture que le tableau METAR, mais prévue."""
        by_label: dict[str, list[Sourced[Any]]] = {}
        for forecast in sorted(package.forecasts, key=lambda f: f.value.valid_at):
            by_label.setdefault(forecast.value.label, []).append(forecast)

        names = _name_index(package)
        groups: list[dict[str, Any]] = []
        for label, items in by_label.items():
            aerodrome = airports_lookup(label) if label else None
            has_runways = aerodrome is not None and bool(aerodrome.runways)
            rows: list[dict[str, Any]] = []
            for forecast in items:
                value = forecast.value
                components = None
                if (
                    has_runways
                    and value.wind_dir_deg is not None
                    and value.wind_speed_kt is not None
                ):
                    assert aerodrome is not None
                    components = aerodrome.favoured_wind_components(
                        value.wind_dir_deg, value.wind_speed_kt
                    )
                rows.append(
                    {
                        "value": value,
                        "valid_local": format_local_only(value.valid_at, self._tz),
                        "in_window": window.contains(value.valid_at),
                        "source": self._source_info(forecast, moment),
                        "qfu": components.runway_ident if components else None,
                        "headwind": round(components.headwind_kt) if components else None,
                        "crosswind": round(components.crosswind_kt) if components else None,
                        "arrow": components.arrow if components else None,
                        "from_right": components.from_right if components else None,
                        "tailwind": components.is_tailwind if components else None,
                        "base_detail": _cloud_base_detail(value),
                    }
                )
            groups.append(
                {
                    "label": label,
                    "name": names.get(label.upper()) if label else None,
                    "runways": (
                        ", ".join(r.ident for r in aerodrome.runways)
                        if has_runways and aerodrome is not None
                        else None
                    ),
                    "source": rows[0]["source"] if rows else None,
                    "rows": rows,
                }
            )
        return groups

    def _sun_view(self, ctx: BriefingContext) -> dict[str, Any] | None:
        """Coucher du soleil et début de nuit aéronautique pour le vol.

        C'est une CONDITION du vol (limite VFR jour) : sa place est dans le
        briefing. On avertit si la fenêtre de vol se termine APRÈS le début de
        nuit aéronautique."""
        from ..domain.sun import sun_times

        center = ctx.geometry.bounding_circle().center
        local_start = ctx.window.start.astimezone(self._tz)
        times = sun_times(local_start.date(), center.lat, center.lon)
        if times.sunset is None and times.sunrise is None:
            return None
        night = times.aeronautical_night_start
        day_start = times.aeronautical_night_end
        # On affiche les QUATRE bornes, toujours. Le document ne montrait que la
        # fin de journée : pour un vol de 08:00, c'est le mauvais bout — la
        # limite qui l'encadre est le DÉBUT du jour aéronautique. Et même quand
        # aucune borne ne mord, les avoir sous les yeux entretient la conscience
        # de la situation : le retard qu'on peut absorber se lit d'un coup d'œil.
        return {
            "sunrise": self._dual(times.sunrise) if times.sunrise is not None else None,
            "day_start": self._dual(day_start) if day_start is not None else None,
            "sunset": self._dual(times.sunset) if times.sunset is not None else None,
            "night": self._dual(night) if night is not None else None,
            "lands_after_night": night is not None and ctx.window.end > night,
            # Symétrique de l'alerte du soir, jamais posée jusqu'ici : décoller
            # avant la fin de la nuit aéronautique est tout aussi interdit en
            # VFR de jour que se poser après son début.
            "departs_before_day": day_start is not None and ctx.window.start < day_start,
        }

    # -- API ---------------------------------------------------------------

    def build_view(
        self, package: BriefingPackage, *, now: UtcDateTime | None = None
    ) -> dict[str, Any]:
        """Prépare le modèle de vue. Séparé du rendu pour être testable seul."""
        moment = now if now is not None else utcnow()
        ctx = package.context
        window = ctx.window
        self._window = window
        sun = self._sun_view(ctx)

        # Tri CHRONOLOGIQUE : par date d'entrée en vigueur, la plus récente
        # d'abord. Le tri repose sur la date d'entrée en vigueur — une donnée
        # officielle du NOTAM — et non sur un jugement de gravité maison.
        notams = [self._notam_view(item, moment) for item in package.notams_by_activation()]

        # Répartition par RUBRIQUE de la source (SOFIA), pas par sévérité inventée.
        category_counter: dict[str, int] = {}
        for item in package.notams:
            label = item.value.source_category or "Non catégorisé"
            category_counter[label] = category_counter.get(label, 0) + 1
        category_counts = sorted(category_counter.items(), key=lambda kv: (-kv[1], kv[0]))

        metars = [
            {"value": m.value, "source": self._source_info(m, moment)} for m in package.metars
        ]
        window = package.context.window
        tafs = [
            {
                "value": t.value,
                "source": self._source_info(t, moment),
                "periods": [
                    {
                        "p": period,
                        # Un groupe qui recouvre la fenêtre de vol est mis en
                        # avant : c'est celui que le pilote doit lire en premier.
                        "in_window": period.validity.overlaps(window),
                        # Tableau décodé = heure LOCALE (le brut au-dessus garde
                        # le Z). C'est la vue de confort, on la lit en local.
                        "from_local": _local_wallclock(period.validity.start, self._tz, "%d %H:%M"),
                        "to_local": _local_wallclock(period.validity.end, self._tz, "%d %H:%M"),
                    }
                    for period in t.value.periods
                ],
            }
            for t in package.tafs
        ]
        # Ordre « dans le sens du vol » : sur une nav, on projette chaque station
        # sur l'axe de la route (départ = 0, arrivée = total) et on trie par cette
        # distance le long de la trajectoire. Le pilote lit les stations dans
        # l'ordre où il les survole, pas dans un ordre de collecte arbitraire.
        route = ctx.route
        if route is not None:
            flight_route: Route = route

            def _axis_position(value: Any) -> float:
                aerodrome = airports_lookup(value.station)
                return flight_route.along_track_nm(aerodrome.position) if aerodrome else math.inf

            metars.sort(key=lambda view: _axis_position(view["value"]))
            tafs.sort(key=lambda view: _axis_position(view["value"]))
        forecasts = [
            {
                "value": f.value,
                "valid_dual": self._dual(f.value.valid_at, with_date=True),
                "valid_local": format_local_only(f.value.valid_at, self._tz),
                # Hors de la fenêtre STRICTE de vol : échéance de contexte (±2 h),
                # grisée à l'affichage pour ne pas la confondre avec le vol.
                "in_window": window.contains(f.value.valid_at),
                "source": self._source_info(f, moment),
            }
            for f in sorted(package.forecasts, key=lambda f: f.value.valid_at)
        ]
        # Toutes les cartes, satellite (couverture nuageuse) compris.
        probe_sites, probe_omitted = _probe_sites(package.context)
        charts = [
            self._chart_view(c, moment, package.context.window, package.context.route, probe_sites)
            for c in package.charts
        ]
        chart_groups = _group_charts(charts, probe_sites, probe_omitted)
        sigmets = [
            {
                "value": s.value,
                "validity_local": (
                    f"{format_local_only(s.value.validity.start, self._tz)} — "
                    f"{format_local_only(s.value.validity.end, self._tz)}"
                ),
                "validity_zulu": (
                    f"{_zulu(s.value.validity.start, '%d/%m %H:%M')}Z — "
                    f"{_zulu(s.value.validity.end, '%d/%m %H:%M')}Z"
                ),
                "source": self._source_info(s, moment),
            }
            for s in package.sigmets
        ]

        everything: Sequence[Sourced[Any]] = (
            *package.metars,
            *package.tafs,
            *package.notams,
            *package.sigmets,
            *package.forecasts,
            *package.charts,
        )

        critical_failures = [f for f in package.failures if f.is_critical]
        other_failures = [f for f in package.failures if not f.is_critical]

        return {
            "package": package,
            "context": ctx,
            "purpose_label": PURPOSE_LABELS.get(ctx.purpose.value, ctx.purpose.value),
            # Le titre ne nomme QUE les terrains du vol. Les stations de repli
            # météo sont des voisines interrogées faute d'observation sur place :
            # les mêler ici donnerait à croire qu'on se pose sur les vingt.
            "stations": ctx.flight_aerodromes,
            "aerodrome_names": _name_index(package),
            "observation_stations": _observation_rows(package),
            "crosswind_estimate": _crosswind_estimate(package),
            "aerodromes": [a for a in package.aerodromes if a.icao in ctx.flight_aerodromes],
            "window_start_dual": self._dual(window.start, with_date=True),
            "window_end_dual": self._dual(window.end, with_date=True),
            "window_duration_h": f"{window.duration_hours:.1f}",
            # Résumé compact injecté dans le pied de CHAQUE page du PDF (marges
            # @page). Sans virgule ni guillemet parasite : c'est une valeur CSS.
            "page_footer": (
                f"{' / '.join(ctx.flight_aerodromes) or 'BRIEFING'} — "
                f"{_zulu(window.start, '%d/%m %H:%M')}Z → {_zulu(window.end, '%H:%M')}Z "
                f"({window.duration_hours:.1f} h) — assemblé {self._dual(package.assembled_at)}"
            ),
            "assembled_dual": self._dual(package.assembled_at, with_date=True),
            "assembled_iso": package.assembled_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_dual": self._dual(moment, with_date=True),
            "is_complete": package.is_complete,
            "critical_failures": critical_failures,
            "other_failures": other_failures,
            "failures": list(package.failures),
            "sun": sun,
            "metars": metars,
            "tafs": tafs,
            "forecasts": forecasts,
            "forecast_groups": self._forecast_groups(package, moment, window),
            # Le facteur de la règle du pouce est EXPOSÉ à la vue plutôt que
            # recopié dans le gabarit : l'avertissement affiché doit citer le
            # nombre réellement employé par le calcul.
            "cloud_base_ft_per_c": CLOUD_BASE_FT_PER_C,
            "notams": notams,
            "search_zones": _search_zones(package.context),
            "zone_aerodromes": list(package.context.aerodromes_in_zone),
            "aip_sup_refs": sorted({r for n in notams for r in n.aip_sup_refs}),
            "aip_sup_documents": self._aip_sup_documents(notams),
            "aip_sup_list_url": supaip.LIST_URL,
            "charts": charts,
            "chart_groups": chart_groups,
            "release_cadence": RELEASE_CADENCE,
            "sigmets": sigmets,
            "missing_chart_kinds": list(package.missing_chart_kinds()),
            "notam_count": len(package.notams),
            "category_counts": category_counts,
            "stale_count": self._stale_count(everything, moment),
            "stale_after_minutes": None,  # remplacé par un seuil PAR TYPE (cf. domain.freshness)
            "unembedded_charts": sum(1 for c in charts if c.data_uri is None),
            "display_timezone": self.display_timezone,
            "document_kind": "all",
            "doc_label": "BRIEFING VFR",
            "guide_url": GUIDE_URL,
            "guide_metar_page": GUIDE_METAR_PAGE,
        }

    def render(
        self, package: BriefingPackage, *, now: UtcDateTime | None = None, kind: str = "all"
    ) -> str:
        """Rend le dossier en HTML autonome (CSS inline, images en data: URI).

        `kind` sépare le dossier : « meteo » (météo + cartes, sans NOTAM),
        « notam » (NOTAM seuls) ou « all » (tout, comportement historique). La
        météo et les NOTAM viennent de sources distinctes (Météo-France vs
        SIA/SOFIA) — deux dossiers focalisés valent mieux qu'un fourre-tout."""
        template = self._env.get_template(TEMPLATE_NAME)
        view = self.build_view(package, now=now)
        view["document_kind"] = kind
        # Le mot DISCRIMINANT en tête du titre d'onglet : Chrome tronque par la
        # droite, et « BRIEFING MÉTÉO » / « BRIEFING NOTAM » se ressemblaient
        # jusqu'au dernier caractère visible. La favicon distingue en plus.
        view["tab_label"] = {"meteo": "MÉTÉO", "notam": "NOTAM"}.get(kind, "VFR")
        view["favicon"] = {"meteo": "🌦", "notam": "⚠"}.get(kind, "📋")
        if kind == "meteo":
            view["notams"] = []
            view["notam_count"] = 0
            view["aip_sup_refs"] = []
            view["aip_sup_documents"] = []
            view["doc_label"] = "BRIEFING MÉTÉO"
        elif kind == "notam":
            for empty in (
                "metars",
                "tafs",
                "forecasts",
                "forecast_groups",
                "charts",
                "chart_groups",
                "sigmets",
            ):
                view[empty] = []
            view["sun"] = None
            view["doc_label"] = "BRIEFING NOTAM"
        # Les règles de tuiles sont écrites APRÈS le filtrage par `kind` : le
        # dossier NOTAM n'a plus de carte, il n'a donc pas à porter des mégaoctets
        # de fond de carte que rien n'affiche.
        view["tile_styles"] = [] if kind == "notam" else self._tiles.css()
        return template.render(**view)


def render_html(
    package: BriefingPackage,
    *,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
    now: UtcDateTime | None = None,
    kind: str = "all",
    supaip_local: dict[str, str] | None = None,
    supaip_index: supaip.SupAipIndex | None = None,
    resolve_tile: Callable[[str], str | None] | None = None,
) -> str:
    """Raccourci fonctionnel pour le cas courant. `kind` : all/meteo/notam."""
    renderer = HtmlRenderer(
        display_timezone=display_timezone,
        stale_after_minutes=stale_after_minutes,
        supaip_local=supaip_local,
        supaip_index=supaip_index,
        resolve_tile=resolve_tile,
    )
    return renderer.render(package, now=now, kind=kind)


__all__ = [
    "DEFAULT_DISPLAY_TIMEZONE",
    "DEFAULT_STALE_AFTER_MINUTES",
    "ChartView",
    "HtmlRenderer",
    "NotamView",
    "ProviderFailure",
    "SourceInfo",
    "format_age",
    "format_dual",
    "format_local_only",
    "render_html",
]


def _group_charts(
    charts: list[ChartView],
    probe_sites: Sequence[tuple[str, str, float, float]] = (),
    probe_omitted: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Regroupe les cartes par type, chaque groupe trié par échéance.

    Un groupe de plusieurs images devient un LECTEUR animé (slider + play) en
    HTML ; un groupe d'une seule reste une image simple. L'ordre des groupes
    suit `CHART_ORDER` (du plus grand au plus petit) ; un type inconnu tombe en
    fin de liste dans son ordre d'apparition, pour rester stable entre rendus.
    """
    order: list[str] = []
    by_kind: dict[str, list] = {}
    for chart in charts:
        if chart.kind not in by_kind:
            by_kind[chart.kind] = []
            order.append(chart.kind)
        by_kind[chart.kind].append(chart)

    # Le rang est figé AVANT le tri : `order` est trié en place, l'index
    # d'apparition d'un type inconnu ne doit pas bouger sous les pieds du tri.
    seen = {kind: i for i, kind in enumerate(order)}
    order.sort(key=lambda k: (0, CHART_ORDER.index(k)) if k in CHART_ORDER else (1, seen[k]))

    groups = []
    for kind in order:
        items = sorted(by_kind[kind], key=lambda c: c.valid_label)
        head = items[0]
        groups.append(
            {
                "kind": kind,
                "kind_label": head.kind_label,
                "is_observation": head.is_observation,
                "area": head.area,
                "flight_level": head.flight_level,
                "frames": items,
                "animated": len(items) > 1,
                "any_covers_window": any(c.covers_window for c in items),
                "guide_page": GUIDE_PAGES.get(kind),
                "cadence": RELEASE_CADENCE.get(kind),
                "panel_ratio": head.panel_ratio,
                "basemap": head.basemap,
                "legend": AROME_LEGENDS.get(kind),
                "probe_table": _probe_table(items, probe_sites, probe_omitted),
                "is_arome": kind.startswith("arome_"),
                # Bandeau de sélection posé sur le PREMIER groupe AROME
                # seulement : les couches AROME sont contiguës dans
                # `CHART_ORDER`, elles forment donc un bloc, et un bandeau par
                # couche n'aurait aucun sens.
                "arome_bar": None,
            }
        )
    arome = [g for g in groups if g["is_arome"]]
    if len(arome) > 1:
        arome[0]["arome_bar"] = [(g["kind"], g["kind_label"]) for g in arome]
    return groups


def _probe_table(
    frames: list[ChartView],
    sites: Sequence[tuple[str, str, float, float]],
    omitted: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Table « couche au droit de chaque terrain », une colonne par échéance.

    Elle existe pour remplacer un geste : chercher un terrain à l'œil sur un
    aplat de couleur et décider si « ça le touche ». Ici la position est
    calculée, la couleur est lue, la classe est celle de la légende. Rien n'est
    estimé.

    Rend `None` quand aucune échéance n'a pu être sondée — une table vide
    laisserait croire que tout est clair, alors qu'elle ne saurait rien.
    """
    if not sites or not any(f.probes for f in frames):
        return None
    rows = []
    for icao, name, _lat, _lon in sites:
        cells = [f.probes.get(icao) for f in frames]
        if all(cell is None for cell in cells):
            continue  # terrain hors de l'emprise de la carte : absent, pas « clair »
        rows.append(
            {
                "icao": icao,
                "name": name,
                "cells": cells,
                # Un terrain que la couche peint au moins une fois passe en
                # tête de lecture : c'est celui qui demande une décision.
                "any_painted": any(c is not None and c.painted for c in cells),
            }
        )
    if not rows:
        return None
    return {
        "columns": [f.valid_label for f in frames],
        "rows": rows,
        "omitted": list(omitted),
    }


#: Nombre maximum de terrains dans la table de sondage. Au-delà, la table cesse
#: d'être lisible d'un coup d'œil et devient un listing — or elle sert à trancher
#: vite. Les terrains du VOL y sont toujours, quel que soit le plafond, et ce que
#: le plafond écarte est NOMMÉ sous la table (cf. `_probe_sites`).
#:
#: Seize et pas douze : un cercle de 40 NM sur la côte charentaise en contient
#: quatorze, et couper à douze laissait dehors La Rochelle — le terrain le plus
#: loin, donc le premier coupé, et souvent celui qu'on regarde.
_PROBE_SITES_MAX = 16


def _probe_sites(
    context: BriefingContext,
) -> tuple[list[tuple[str, str, float, float]], list[str]]:
    """Terrains à sonder : ceux du vol d'abord, puis les voisins de la zone.

    On ne sonde que des terrains — des endroits où l'on se pose. Sonder des
    points quelconques du couloir donnerait une table plus fournie et moins
    utile : la question que pose le pilote est « est-ce que je peux aller là ? ».

    Rend `(terrains, écartés)`. Le plafond de lisibilité ne doit JAMAIS couper en
    silence : sur un cercle de 40 NM autour de Royan, le treizième terrain est La
    Rochelle, et une table qui ne le montre pas sans le dire laisse croire qu'il
    n'y avait rien de plus à regarder.
    """
    from ..data import airports

    sites: list[tuple[str, str, float, float]] = []
    seen: set[str] = set()

    def add(aerodrome: Aerodrome) -> None:
        if aerodrome.icao in seen:
            return
        seen.add(aerodrome.icao)
        sites.append(
            (aerodrome.icao, aerodrome.name, aerodrome.position.lat, aerodrome.position.lon)
        )

    for icao in context.flight_aerodromes:
        aerodrome = airports.lookup(icao)
        if aerodrome is not None:
            add(aerodrome)

    # Puis le voisinage, par distance croissante au centre de la zone cherchée,
    # en ne gardant que ce que cette zone contient RÉELLEMENT (un couloir n'est
    # pas son cercle englobant).
    circle = context.geometry.bounding_circle()
    omitted: list[str] = []
    for aerodrome, _distance in airports.nearest(
        circle.center, within_nm=circle.radius_nm, limit=_PROBE_SITES_MAX * 8
    ):
        if not context.geometry.contains(aerodrome.position) or aerodrome.icao in seen:
            continue
        if len(sites) >= _PROBE_SITES_MAX:
            omitted.append(aerodrome.icao)  # dans la zone, hors de la table : on le DIT
            continue
        add(aerodrome)
    return sites, omitted


def _search_zones(context: BriefingContext) -> list[str]:
    """Zone(s) réellement interrogées, en clair.

    Un briefing NOTAM sans ses paramètres de recherche est inauditable : « 28
    NOTAM » ne veut rien dire si on ignore sur quelle surface ils ont été
    cherchés. On décrit donc la géométrie telle qu'elle est, pas telle qu'on
    l'imagine.
    """
    geometry = context.geometry
    parts = list(geometry.parts) if isinstance(geometry, GeoUnion) else [geometry]
    circles = [p for p in parts if isinstance(p, Circle)]
    alternates = list(context.alternates_icao)
    # Les cercles des dégagements sont AJOUTÉS EN DERNIER par `build_context`,
    # après la géométrie du vol. On les apparie donc PAR LA FIN : sur un vol
    # local, la géométrie du vol est elle-même un cercle, et compter depuis le
    # début décalait tout d'un rang — les trois dégagements sortaient étiquetés
    # du nom du terrain de départ. On ne nomme rien si le compte ne suffit pas.
    premier_degagement = len(circles) - len(alternates)
    names = alternates if premier_degagement >= 0 else []
    labels: list[str] = []
    seen_circles = 0
    for part in parts:
        if isinstance(part, Corridor):
            labels.append(
                f"Couloir de ±{part.half_width_nm:g} NM de part et d'autre de la route "
                f"({len(part.points)} points)"
            )
        elif isinstance(part, Circle):
            rang = seen_circles - premier_degagement
            target = (
                names[rang]
                if names and 0 <= rang < len(names)
                else (context.origin_icao or "le point de référence")
            )
            labels.append(f"Cercle de {part.radius_nm:g} NM autour de {target}")
            seen_circles += 1
        else:  # pragma: no cover - toute géométrie future reste nommée
            labels.append(type(part).__name__)
    return labels


def _crosswind_estimate(package: BriefingPackage) -> dict[str, Any] | None:
    """Croise le vent de CHAQUE station voisine contre les pistes du terrain de
    départ, pour donner une fourchette de traversier estimé.

    Cas d'usage : un vol local sur un terrain sans METAR propre (LFCY). Aucune
    station ne donne le vent EXACT sur place, mais en appliquant les vents des
    voisines aux pistes du terrain on voit la VARIÉTÉ des traversiers plausibles.
    C'est explicitement une estimation — le vent réel peut différer — et le rendu
    le dit. Vide si le terrain n'a pas de piste orientée connue.
    """
    context = package.context
    focus = next(
        (a for a in package.aerodromes if a.icao == context.origin_icao and a.runways), None
    )
    if focus is None:
        return None

    rows = []
    for item in package.metars:
        metar = item.value
        if metar.wind_dir_deg is None or metar.wind_speed_kt is None:
            continue
        components = focus.favoured_wind_components(metar.wind_dir_deg, metar.wind_speed_kt)
        if components is None:
            continue
        station = airports_lookup(metar.station)
        distance = focus.position.distance_nm(station.position) if station else None
        rows.append(
            {
                "station": metar.station,
                "distance_nm": round(distance) if distance is not None else None,
                "wind": f"{metar.wind_dir_deg:03d}° / {metar.wind_speed_kt} kt"
                + (f" raf. {metar.wind_gust_kt}" if metar.wind_gust_kt else ""),
                "qfu": components.runway_ident,
                "headwind": round(components.headwind_kt),
                "crosswind": round(components.crosswind_kt),
                "arrow": components.arrow,
                "from_right": components.from_right,
                "tailwind": components.is_tailwind,
            }
        )
    if not rows:
        return None
    runways = ", ".join(r.ident for r in focus.runways)
    return {"icao": focus.icao, "runways": runways, "rows": rows}


def _cloud_base_detail(value: ForecastPoint) -> str | None:
    """Détail du calcul derrière la base estimée, à afficher au survol.

    Le pilote doit pouvoir remonter le raisonnement d'un coup d'œil : c'est une
    grandeur DÉRIVÉE d'un écart T/Td par une règle du pouce, pas une hauteur
    mesurée ni fournie par la source. Sans ce détail, la colonne se lirait comme
    un plafond prévu — la confusion exacte qu'on veut rendre impossible.

    Renvoie None si l'un des ingrédients manque : on n'explique pas un calcul
    avec des chiffres qu'on n'a pas.
    """
    spread = value.spread_c
    if value.cloud_base_ft is None or spread is None:
        return None
    assert value.temperature_c is not None and value.dewpoint_c is not None
    return (
        f"ESTIMATION, pas une donnée de la source. "
        f"T {value.temperature_c:.1f} °C − Td {value.dewpoint_c:.1f} °C "
        f"= {spread:.1f} °C d'écart × {CLOUD_BASE_FT_PER_C:.0f} ft/°C "
        f"≈ {round(value.cloud_base_ft)} ft. "
        f"Règle du pouce de convection (base de cumulus) : ne vaut pas pour une "
        f"couche stratiforme, un plafond d'advection ou une inversion."
    )


def airports_lookup(icao: str) -> Aerodrome | None:
    from ..data import airports

    return airports.lookup(icao)


def _observation_rows(package: BriefingPackage) -> list[dict[str, Any]]:
    """Stations de repli ayant RÉELLEMENT fourni une observation, avec leur
    distance au terrain de départ.

    On n'affiche pas les candidates muettes : ce qui compte pour le pilote,
    c'est de savoir d'où vient la météo qu'il lit, et à quelle distance — une
    observation à 50 NM ne décrit pas forcément le régime local.
    """
    context = package.context
    origin = next((a for a in package.aerodromes if a.icao == context.origin_icao), None)
    if origin is None:
        return []

    with_data = {m.value.station.upper() for m in package.metars}
    with_data |= {t.value.station.upper() for t in package.tafs}

    rows: list[dict[str, Any]] = []
    for aerodrome in package.aerodromes:
        if aerodrome.icao in context.flight_aerodromes or aerodrome.icao not in with_data:
            continue
        rows.append(
            {
                "icao": aerodrome.icao,
                "name": aerodrome.name,
                "distance_nm": round(origin.position.distance_nm(aerodrome.position)),
            }
        )
    rows.sort(key=lambda row: row["distance_nm"])
    return rows


_NAME_SUFFIXES = (" Airport", " Airfield", " Air Base", " airport", " Aerodrome")


def _readable(name: str) -> str:
    """Nom d'usage, débarrassé du suffixe générique d'OurAirports.

    « Royan-Médis Airport » devient « Royan-Médis » : c'est ainsi qu'un pilote
    nomme le terrain, et la place gagnée compte sur une feuille A4.
    """
    cleaned = name.strip()
    for suffix in _NAME_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    return cleaned


def _name_index(package: BriefingPackage) -> dict[str, str]:
    """OACI → nom lisible.

    Les noms d'AÉRODROMES viennent du dossier : si un terrain manque ici, c'est
    l'agrégateur qui a sous-alimenté le paquet, et le rendu affiche le code seul
    plutôt que d'aller le chercher ailleurs.

    Les noms de FIR, eux, viennent d'une table de RÉFÉRENCE statique (LFBB → FIR
    Bordeaux). Un code FIR n'est pas de la donnée susceptible de dater — c'est
    une abréviation, au même titre que les libellés de cartes ou d'unités. On la
    résout donc directement, sans que ça viole le principe du dossier
    autosuffisant.
    """
    from ..data import fir

    index = {a.icao.upper(): _readable(a.name) for a in package.aerodromes}
    # Complète avec les FIR cités par les NOTAM et non déjà résolus en aérodrome.
    for item in package.notams:
        code = (item.value.affected_icao or "").upper()
        if code and code not in index:
            fir_name = fir.lookup(code)
            if fir_name:
                index[code] = fir_name
    return index
