"""Point d'entrée en ligne de commande.

    python -m aerobriefer LFCY --date 2026-07-21 --heure 10:00 --duree 3 --rayon 20

L'heure saisie est LOCALE (c'est ainsi qu'on prépare un vol), convertie en UTC
dès l'entrée : au-delà de cette frontière, le domaine ne connaît plus que Z.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import timedelta  # noqa: TID251 - timedelta est une durée, pas un instant
from pathlib import Path
from zoneinfo import ZoneInfo

from .assemble import assemble_briefing
from .data import airports
from .domain.context import BriefingContext
from .domain.geo import Position
from .domain.models import Aerodrome
from .domain.route import Route, Waypoint
from .domain.window import TimeWindow, UtcDateTime
from .providers.base import Provider

DEFAULT_ZONE = "Europe/Paris"


def build_context(
    icao: str,
    *,
    date: str,
    heure: str,
    duree_h: float,
    rayon_nm: float,
    zone: str = DEFAULT_ZONE,
    aeronef: str | None = None,
    route: str | None = None,
    largeur_nm: float = 10.0,
) -> BriefingContext:
    aerodrome = airports.require(icao)
    hour, minute = (int(part) for part in heure.split(":"))
    year, month, day = (int(part) for part in date.split("-"))

    start = UtcDateTime.of(
        UtcDateTime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone)),
        "début de vol",
    )
    window = TimeWindow(start, start + _hours(duree_h))

    if route:
        # Nav : le terrain `icao` est le DÉPART, la route donne le reste.
        parsed = parse_route(route, default_first=aerodrome.icao)
        last = parsed.waypoints[-1].name
        context = BriefingContext.navigation(
            route=parsed,
            window=window,
            half_width_nm=largeur_nm,
            origin_icao=aerodrome.icao,
            destination_icao=last if airports.lookup(last) else None,
            aircraft_id=aeronef,
        )
        return replace(context, observation_stations=_fallback_stations(aerodrome))

    context = BriefingContext.local(
        center=aerodrome.position,
        radius_nm=rayon_nm,
        window=window,
        icao=aerodrome.icao,
        aircraft_id=aeronef,
    )
    return replace(context, observation_stations=_fallback_stations(aerodrome))


def parse_route(spec: str, *, default_first: str | None = None) -> Route:
    """Parse « LFCY@1500,LFBN@2500 » ou « 45.6,-0.9@1500,LFBN » en `Route`.

    Chaque point : un code OACI (résolu via la base) OU « lat,lon », suivi
    optionnellement de « @altitude_ft ». Si `default_first` est fourni et que le
    premier point de `spec` n'est pas le terrain de départ, on l'ajoute en tête.
    """
    waypoints: list[Waypoint] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        # Un « lat,lon » a été coupé par la virgule : on rattache au précédent.
        waypoints.append(_parse_waypoint(token, waypoints))

    points = _regroup_coord_pairs(waypoints)
    if default_first and (not points or points[0].name.upper() != default_first.upper()):
        head = airports.require(default_first)
        points.insert(0, Waypoint(name=head.icao, position=head.position))
    return Route(points)


def _parse_waypoint(token: str, _prev: list[Waypoint]) -> Waypoint:
    name_part, _, alt_part = token.partition("@")
    altitude = float(alt_part) if alt_part else None
    name_part = name_part.strip()
    aerodrome = airports.lookup(name_part)
    if aerodrome is not None:
        return Waypoint(name=aerodrome.icao, position=aerodrome.position, altitude_ft=altitude)
    # sinon on suppose une moitié de coordonnée « lat » ou « lat lon »
    return Waypoint(name=name_part, position=Position(0.0, 0.0), altitude_ft=altitude)


def _regroup_coord_pairs(waypoints: list[Waypoint]) -> list[Waypoint]:
    """Recolle les « lat,lon » que le split sur la virgule a séparés en deux."""
    out: list[Waypoint] = []
    i = 0
    while i < len(waypoints):
        wp = waypoints[i]
        if _is_number(wp.name) and i + 1 < len(waypoints) and _is_number(waypoints[i + 1].name):
            lat = float(wp.name)
            lon = float(waypoints[i + 1].name)
            alt = waypoints[i + 1].altitude_ft or wp.altitude_ft
            out.append(
                Waypoint(name=f"{lat:.3f},{lon:.3f}", position=Position(lat, lon), altitude_ft=alt)
            )
            i += 2
        else:
            out.append(wp)
            i += 1
    return out


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _fallback_stations(
    aerodrome: Aerodrome, *, within_nm: float = 70.0, limit: int = 20
) -> tuple[str, ...]:
    """Stations candidates au repli météo, par distance croissante.

    On ne sait pas hors ligne lesquelles observent réellement : NOAA omet
    silencieusement les stations sans données, donc on propose largement et la
    source tranche. Le filet doit être large : autour de Royan, les six terrains
    les plus proches sont des plateformes sans observation, et les premières
    stations exploitables (La Rochelle ~38 NM, Bordeaux ~48 NM) n'arrivent qu'au
    delà. Un filet trop serré ne ramènerait rien.

    NOAA groupe toutes les stations en une seule requête : élargir ne coûte rien.
    """
    neighbours = airports.nearest(aerodrome.position, within_nm=within_nm, limit=limit + 1)
    return tuple(found.icao for found, _ in neighbours if found.icao != aerodrome.icao)


def _hours(value: float) -> timedelta:
    return timedelta(hours=value)


def default_providers() -> list[Provider]:
    """Providers disponibles, chargés paresseusement.

    Un provider absent ou mal configuré (identifiants manquants) ne doit pas
    empêcher le briefing : il est simplement écarté, et son absence apparaîtra
    dans le dossier.
    """
    # Options d'instanciation par provider, quand le défaut ne suffit pas.
    kwargs_by_class = {
        # Prévisions : ±2 h autour de la fenêtre, pour voir la tendance juste
        # avant et après le vol.
        "MetNoProvider": {"padding_hours": 2.0},
    }
    found = []
    for module_name, class_names in [
        ("noaa", ("NoaaMetarProvider", "NoaaTafProvider")),
        ("sigmet", ("SigmetProvider",)),
        ("sofia", ("SofiaProvider",)),
        ("metno", ("MetNoProvider",)),
        ("aeroweb", ("AerowebProvider",)),
    ]:
        try:
            module = __import__(f"aerobriefer.providers.{module_name}", fromlist=["*"])
        except ImportError:
            continue
        for class_name in class_names:
            provider_class = getattr(module, class_name, None)
            if provider_class is None:
                continue
            try:
                found.append(provider_class(**kwargs_by_class.get(class_name, {})))
            except Exception:  # noqa: BLE001 - config absente : on écarte, sans bruit fatal
                continue
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aerobriefer", description="Briefing de vol VFR local")
    parser.add_argument("icao", help="code OACI du terrain, ex. LFCY")
    parser.add_argument("--date", required=True, help="AAAA-MM-JJ (locale)")
    parser.add_argument("--heure", default="10:00", help="HH:MM locale (défaut 10:00)")
    parser.add_argument("--duree", type=float, default=3.0, help="durée en heures (défaut 3)")
    parser.add_argument("--rayon", type=float, default=20.0, help="rayon en NM (défaut 20)")
    parser.add_argument(
        "--route",
        default=None,
        help="nav : points tournants « LFBN@2500,LFXX@... » (le terrain est le départ)",
    )
    parser.add_argument("--largeur", type=float, default=10.0, help="demi-couloir nav en NM")
    parser.add_argument("--zone", default=DEFAULT_ZONE, help=f"fuseau (défaut {DEFAULT_ZONE})")
    parser.add_argument("--aeronef", default=None, help="immatriculation ou modèle")
    parser.add_argument("--pdf", type=Path, default=None, help="chemin du PDF à produire")
    parser.add_argument(
        "--html", type=Path, default=None, help="chemin du HTML autonome à produire"
    )
    parser.add_argument(
        "--viewer",
        type=Path,
        default=None,
        help="chemin du viewer 3D d'espaces aériens à produire (HTML, three.js)",
    )
    args = parser.parse_args(argv)

    context = build_context(
        args.icao,
        date=args.date,
        heure=args.heure,
        duree_h=args.duree,
        rayon_nm=args.rayon,
        zone=args.zone,
        aeronef=args.aeronef,
        route=args.route,
        largeur_nm=args.largeur,
    )

    package = assemble_briefing(context, default_providers())

    print(
        f"Briefing {args.icao} — {context.window.start:%d/%m/%Y %H:%MZ}"
        f" → {context.window.end:%H:%MZ}"
    )
    print(
        f"  METAR {len(package.metars)} | TAF {len(package.tafs)}"
        f" | NOTAM {len(package.notams)} | SIGMET {len(package.sigmets)}"
        f" | prévisions {len(package.forecasts)} | cartes {len(package.charts)}"
    )
    if not package.is_complete:
        print("  ATTENTION : dossier INCOMPLET")
    for failure in package.failures:
        marker = "CRITIQUE" if failure.is_critical else "mineur"
        print(f"    [{marker}] {failure.source} : {failure.reason}")

    if args.html:
        from .render.html import render_html

        # Le HTML est AUTONOME : images embarquées en data URI, aucun lien
        # externe. Consultable et archivable tel quel, hors ligne.
        args.html.write_text(render_html(package), encoding="utf-8")
        print(f"  HTML : {args.html}")

    if args.pdf:
        from .render.pdf import render_pdf

        render_pdf(package, args.pdf)
        print(f"  PDF : {args.pdf}")

    if args.viewer:
        from .render.viewer import render_viewer

        # Outil de PRÉPARATION en ligne (three.js via CDN) : montre les volumes
        # d'espaces aériens en 3D autour du terrain.
        args.viewer.write_text(render_viewer(package), encoding="utf-8")
        print(f"  Viewer 3D : {args.viewer} ({len(package.airspaces)} espaces)")

    return 0 if package.is_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
