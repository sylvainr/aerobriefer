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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..domain.freshness import describe as freshness_label
from ..domain.freshness import max_age_minutes
from ..domain.models import Aerodrome, Notam
from ..domain.package import BriefingPackage, ProviderFailure
from ..domain.sourced import Sourced
from ..domain.window import TimeWindow, UtcDateTime, utcnow

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
    area: str | None
    flight_level: str | None
    issued_dual: str | None
    valid_dual: str | None
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


@dataclass(frozen=True, slots=True)
class NotamView:
    identifier: str
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
    ) -> None:
        self.display_timezone = display_timezone
        self.stale_after_minutes = stale_after_minutes
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
            is_stale=item.is_stale(max_age_minutes(item.value), now),
            freshness_limit=freshness_label(item.value),
            issued_dual=self._dual(prov.issued_at, with_date=True) if prov.issued_at else None,
            retrieved_dual=self._dual(prov.retrieved_at, with_date=True),
            url=prov.url,
        )

    def _chart_view(
        self, item: Sourced[Any], now: UtcDateTime, window: TimeWindow | None = None
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
            area=chart.area,
            flight_level=chart.flight_level,
            issued_dual=self._dual(chart.issued_at, with_date=True) if chart.issued_at else None,
            valid_dual=self._dual(chart.valid_at, with_date=True) if chart.valid_at else None,
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
            source=self._source_info(item, now),
        )

    def _stale_count(self, items: Iterable[Sourced[Any]], now: UtcDateTime) -> int:
        return sum(1 for i in items if i.is_stale(max_age_minutes(i.value), now))

    # -- API ---------------------------------------------------------------

    def build_view(
        self, package: BriefingPackage, *, now: UtcDateTime | None = None
    ) -> dict[str, Any]:
        """Prépare le modèle de vue. Séparé du rendu pour être testable seul."""
        moment = now if now is not None else utcnow()
        ctx = package.context
        window = ctx.window
        self._window = window

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
        charts = [self._chart_view(c, moment, package.context.window) for c in package.charts]
        chart_groups = _group_charts(charts)
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
            "generated_dual": self._dual(moment, with_date=True),
            "is_complete": package.is_complete,
            "critical_failures": critical_failures,
            "other_failures": other_failures,
            "failures": list(package.failures),
            "metars": metars,
            "tafs": tafs,
            "forecasts": forecasts,
            "notams": notams,
            "charts": charts,
            "chart_groups": chart_groups,
            "sigmets": sigmets,
            "missing_chart_kinds": list(package.missing_chart_kinds()),
            "notam_count": len(package.notams),
            "category_counts": category_counts,
            "stale_count": self._stale_count(everything, moment),
            "stale_after_minutes": None,  # remplacé par un seuil PAR TYPE (cf. domain.freshness)
            "unembedded_charts": sum(1 for c in charts if c.data_uri is None),
            "display_timezone": self.display_timezone,
        }

    def render(self, package: BriefingPackage, *, now: UtcDateTime | None = None) -> str:
        """Rend le dossier en HTML autonome (CSS inline, images en data: URI)."""
        template = self._env.get_template(TEMPLATE_NAME)
        return template.render(**self.build_view(package, now=now))


def render_html(
    package: BriefingPackage,
    *,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
    now: UtcDateTime | None = None,
) -> str:
    """Raccourci fonctionnel pour le cas courant."""
    renderer = HtmlRenderer(
        display_timezone=display_timezone, stale_after_minutes=stale_after_minutes
    )
    return renderer.render(package, now=now)


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


def _group_charts(charts: list[ChartView]) -> list[dict[str, Any]]:
    """Regroupe les cartes par type, chaque groupe trié par échéance.

    Un groupe de plusieurs images devient un LECTEUR animé (slider + play) en
    HTML ; un groupe d'une seule reste une image simple. L'ordre des groupes
    suit l'ordre d'apparition des types, pour rester stable entre deux rendus.
    """
    order: list[str] = []
    by_kind: dict[str, list] = {}
    for chart in charts:
        if chart.kind not in by_kind:
            by_kind[chart.kind] = []
            order.append(chart.kind)
        by_kind[chart.kind].append(chart)

    groups = []
    for kind in order:
        items = sorted(by_kind[kind], key=lambda c: c.valid_label)
        head = items[0]
        groups.append(
            {
                "kind": kind,
                "kind_label": head.kind_label,
                "area": head.area,
                "flight_level": head.flight_level,
                "frames": items,
                "animated": len(items) > 1,
                "any_covers_window": any(c.covers_window for c in items),
            }
        )
    return groups


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
