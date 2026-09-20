"""Point d'entrée en ligne de commande.

    python -m aerobriefer LFCY --date 2026-07-21 --heure 10:00 --duree 3 --rayon 20

L'heure saisie est LOCALE (c'est ainsi qu'on prépare un vol), convertie en UTC
dès l'entrée : au-delà de cette frontière, le domaine ne connaît plus que Z.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import timedelta  # noqa: TID251 - timedelta est une durée, pas un instant
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .assemble import assemble_briefing
from .data import airports
from .domain.context import BriefingContext
from .domain.geo import Circle, Position, Union, project_onto_segment
from .domain.models import Aerodrome
from .domain.route import Route, Waypoint
from .domain.window import TimeWindow, UtcDateTime
from .providers.base import Provider

if TYPE_CHECKING:
    from .aircraft.model import AircraftSpec
    from .data.tiles import TileStore
    from .diversion import DiversionStudy
    from .domain.package import BriefingPackage

DEFAULT_ZONE = "Europe/Paris"


def _aerodromes_in_zone(geometry: Union | Circle | Any, limit: int = 60) -> list[str]:
    """Terrains que la zone de recherche CONTIENT, du plus central au plus loin.

    Découverts ici, à l'étape 1, et non devinés par la source : SOFIA ne rend
    les NOTAM d'aérodrome que des terrains qu'on lui nomme. Sans cette liste,
    une route qui survole Marennes, Oléron et Rochefort ne rapportait aucun
    NOTAM de CES terrains — alors qu'ils sont dans la zone que le dossier
    déclare avoir interrogée.

    On balaie le cercle englobant puis on garde ce que la géométrie RÉELLE
    contient : un couloir n'est pas son cercle englobant, et nommer des terrains
    hors zone ne servirait à rien — leurs NOTAM seraient refiltrés en aval.
    """
    enclosing = geometry.bounding_circle()
    found: list[str] = []
    for aerodrome, _distance in airports.nearest(
        enclosing.center, within_nm=enclosing.radius_nm, limit=limit * 4
    ):
        if len(found) >= limit:
            break
        if geometry.contains(aerodrome.position):
            found.append(aerodrome.icao)
    return found


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
    degagements: Sequence[str] = (),
    degagement_rayon_nm: float = 10.0,
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
        dest = airports.lookup(last)
        alternates = [airports.require(code) for code in degagements]
        context = BriefingContext.navigation(
            route=parsed,
            window=window,
            half_width_nm=largeur_nm,
            origin_icao=aerodrome.icao,
            destination_icao=dest.icao if dest else None,
            alternates_icao=[a.icao for a in alternates],
            aircraft_id=aeronef,
        )
        if alternates:
            # Un dégagement est HORS du couloir par nature : on lui ajoute son
            # propre cercle plutôt que d'élargir le couloir, ce qui ramènerait
            # des NOTAM sans rapport avec le vol.
            context = replace(
                context,
                geometry=Union(
                    (
                        context.geometry,
                        *(Circle(a.position, degagement_rayon_nm) for a in alternates),
                    )
                ),
            )
        # La météo d'une nav doit couvrir TOUTE la trajectoire : on ancre sur
        # chaque terrain du vol (départ, points tournants qui sont des terrains,
        # arrivée) et on rabat le filet de stations d'observation autour de
        # chacun — plus le milieu du couloir, pour l'en-route. Les dégagements
        # sont des terrains du vol : on s'y pose peut-être, ils sont ancrés aussi.
        anchors = _flight_aerodromes(parsed, aerodrome, dest)
        known = {a.icao for a in anchors}
        anchors = anchors + [a for a in alternates if a.icao not in known]
        enroute = context.geometry.bounding_circle().center
        return replace(
            context,
            aerodromes_in_zone=tuple(_aerodromes_in_zone(context.geometry)),
            weather_points=tuple((a.icao, a.position) for a in anchors),
            observation_stations=_observation_stations(
                [a.position for a in anchors] + [enroute],
                exclude={a.icao for a in anchors},
            ),
        )

    alternates = [airports.require(code) for code in degagements]
    context = BriefingContext.local(
        center=aerodrome.position,
        radius_nm=rayon_nm,
        window=window,
        icao=aerodrome.icao,
        alternates_icao=[a.icao for a in alternates],
        aircraft_id=aeronef,
    )
    if alternates:
        # Un dégagement HORS du cercle du vol doit quand même être couvert.
        # Ceux qui sont déjà dedans n'élargissent rien — l'union se contente de
        # les rendre explicites dans les paramètres de recherche.
        context = replace(
            context,
            geometry=Union(
                (
                    context.geometry,
                    *(Circle(a.position, degagement_rayon_nm) for a in alternates),
                )
            ),
        )
    anchors = [aerodrome, *(a for a in alternates if a.icao != aerodrome.icao)]
    return replace(
        context,
        aerodromes_in_zone=tuple(_aerodromes_in_zone(context.geometry)),
        weather_points=tuple((a.icao, a.position) for a in anchors),
        observation_stations=_observation_stations(
            [a.position for a in anchors], exclude={a.icao for a in anchors}
        ),
    )


def parse_route(spec: str, *, default_first: str | None = None) -> Route:
    """Parse « LFCY@1500,LFBN@2500 » ou « 45.6,-0.9@1500,LFBN » en `Route`.

    Chaque point : un code OACI (résolu via la base) OU « lat,lon », suivi
    optionnellement de « @altitude_ft ». Si `default_first` est fourni et que le
    premier point de `spec` n'est pas le terrain de départ, on l'ajoute en tête.
    """
    waypoints: list[Waypoint] = []
    # On amorce par le départ pour que les points suivants (notamment un « travers »)
    # disposent des points PRÉCÉDENTS pendant le parsing.
    if default_first:
        head = airports.require(default_first)
        waypoints.append(Waypoint(name=head.icao, position=head.position))
    seeded = len(waypoints)
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        # Un « lat,lon » a été coupé par la virgule : on rattache au précédent.
        waypoints.append(_parse_waypoint(token, waypoints))

    # Si le premier point du spec EST déjà le départ, il double le point de tête.
    if (
        default_first
        and len(waypoints) > seeded
        and waypoints[seeded].name.upper() == default_first.upper()
    ):
        del waypoints[seeded]
    return Route(_regroup_coord_pairs(waypoints))


def _parse_waypoint(token: str, prev: list[Waypoint]) -> Waypoint:
    """Un jeton de route → point. Préfixe « ~ » = travers (projeté sur la branche
    des deux points précédents)."""
    if token.startswith("~"):
        return _travers_waypoint(token[1:], prev)
    return _resolve_point(token)


def _travers_waypoint(core: str, prev: list[Waypoint]) -> Waypoint:
    """Travers d'une référence : pied de sa perpendiculaire sur le segment des deux
    derniers points. Rejeté si la projection tombe hors du segment."""
    ref = _resolve_point(core)
    if len(prev) < 2:
        raise ValueError(f"travers « {ref.name} » : deux points précédents requis")
    foot, t = project_onto_segment(ref.position, prev[-2].position, prev[-1].position)
    if not (-1e-9 <= t <= 1.0 + 1e-9):
        raise ValueError(
            f"travers « {ref.name} » hors du segment précédent : la perpendiculaire "
            f"tombe en dehors (position relative {t:.2f} ∉ [0, 1])"
        )
    return Waypoint(name=f"T/{ref.name}", position=foot, altitude_ft=ref.altitude_ft)


def _resolve_point(token: str) -> Waypoint:
    name_part, _, alt_part = token.partition("@")
    altitude = float(alt_part) if alt_part else None
    name_part = name_part.strip()
    # Coordonnées NOMMÉES en un seul jeton : « NOM:lat/lon » (slash, pas de
    # virgule → pas coupé par le split). Ex. « MARENNES:45.82/-1.10@2000 ».
    if ":" in name_part:
        label, _, coords = name_part.partition(":")
        lat_s, _, lon_s = coords.partition("/")
        if _is_number(lat_s) and _is_number(lon_s):
            return Waypoint(
                name=label.strip() or f"{float(lat_s):.3f},{float(lon_s):.3f}",
                position=Position(float(lat_s), float(lon_s)),
                altitude_ft=altitude,
            )
    aerodrome = airports.lookup(name_part)
    if aerodrome is not None:
        return Waypoint(name=aerodrome.icao, position=aerodrome.position, altitude_ft=altitude)
    # Point de report VFR « OACI/IDENT » (ex. « LFBD/N » = point November de LFBD).
    if "/" in name_part:
        from .data import reporting_points

        point = reporting_points.resolve(name_part)
        if point is None:
            raise ValueError(f"point de report VFR inconnu : {name_part}")
        return Waypoint(
            name=f"{point.icao}/{point.ident}", position=point.position, altitude_ft=altitude
        )
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


def _flight_aerodromes(route: Route, origin: Aerodrome, dest: Aerodrome | None) -> list[Aerodrome]:
    """Terrains du vol résolus, dans l'ordre : départ, points tournants qui sont
    des terrains, arrivée. Dédoublonnés en gardant le premier vu.

    Sert d'ancres météo : c'est autour d'eux qu'on veut observations ET prévisions.
    """
    out: dict[str, Aerodrome] = {origin.icao: origin}
    for waypoint in route.waypoints:
        aerodrome = airports.lookup(waypoint.name)
        if aerodrome is not None:
            out.setdefault(aerodrome.icao, aerodrome)
    if dest is not None:
        out.setdefault(dest.icao, dest)
    return list(out.values())


def _observation_stations(
    anchors: list[Position],
    *,
    exclude: set[str],
    within_nm: float = 70.0,
    per_anchor: int = 20,
) -> tuple[str, ...]:
    """Stations candidates au repli météo autour de CHAQUE ancre, dédoublonnées.

    On ne sait pas hors ligne lesquelles observent réellement : NOAA omet
    silencieusement les stations sans données, donc on propose largement autour
    de chaque ancre et la source tranche. Le filet doit être large : autour de
    Royan, les six terrains les plus proches sont des plateformes sans
    observation, et les premières stations exploitables (La Rochelle ~38 NM,
    Bordeaux ~48 NM) n'arrivent qu'au-delà — un filet trop serré ne ramènerait
    rien. Sur une nav, on rabat ce filet autour du départ, de l'arrivée et des
    points tournants (et du milieu du couloir), pas seulement du départ.

    NOAA groupe toutes les stations en une seule requête : élargir ne coûte rien.
    """
    excluded = {icao.upper() for icao in exclude}
    seen: dict[str, None] = {}
    for anchor in anchors:
        for found, _ in airports.nearest(anchor, within_nm=within_nm, limit=per_anchor + 1):
            icao = found.icao.upper()
            if icao not in excluded:
                seen.setdefault(icao, None)
    return tuple(seen)


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
        ("arome", ("AromeProvider",)),
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
    parser.add_argument("icao", nargs="?", help="code OACI du terrain, ex. LFCY (ou --nav)")
    parser.add_argument(
        "--nav",
        type=Path,
        default=None,
        help="fichier de navigation JSON ; génère TOUT le dossier dans --out",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="répertoire de sortie du dossier (avec --nav)"
    )
    parser.add_argument("--date", help="AAAA-MM-JJ (locale)")
    parser.add_argument("--heure", default="10:00", help="HH:MM locale (défaut 10:00)")
    parser.add_argument("--duree", type=float, default=3.0, help="durée en heures (défaut 3)")
    parser.add_argument("--rayon", type=float, default=20.0, help="rayon en NM (défaut 20)")
    parser.add_argument(
        "--degagements-icao",
        default="",
        help="terrains de dégagement, codes OACI séparés par des virgules (ex. LFDK,LFXB,LFDN)",
    )
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
    parser.add_argument(
        "--navlog",
        type=Path,
        default=None,
        help="chemin du log de navigation PDF à produire (exige --route)",
    )
    parser.add_argument(
        "--gpx",
        type=Path,
        default=None,
        help="chemin du fichier GPX à produire (route ForeFlight/SkyDemon ; exige --route)",
    )
    parser.add_argument(
        "--declinaison",
        type=float,
        default=None,
        help="déclinaison magnétique (° Est positif) ; défaut : calculée (WMM, offline)",
    )
    parser.add_argument(
        "--masse-centrage",
        type=Path,
        default=None,
        help="chemin de la web-app masse & centrage à produire (HTML+JS local)",
    )
    parser.add_argument(
        "--checklist",
        type=Path,
        default=None,
        help="chemin de la checklist de préparation nav à produire (PDF, exige --route)",
    )
    parser.add_argument(
        "--degagement",
        type=Path,
        default=None,
        help="chemin du briefing de dégagement/déroutement à produire (HTML) : "
        "terrains voisins + perfs déco/atterro + météo",
    )
    parser.add_argument(
        "--rayon-degagement",
        type=float,
        default=30.0,
        dest="rayon_degagement",
        help="rayon de recherche des terrains de dégagement en NM (défaut 30)",
    )
    parser.add_argument(
        "--nombre-degagement",
        type=int,
        default=12,
        dest="nombre_degagement",
        help="nombre maximum de terrains DÉTAILLÉS dans la feuille de dégagement "
        "(défaut 12) ; ceux que le plafond écarte sont nommés dans la page",
    )
    args = parser.parse_args(argv)

    # Mode « fichier de navigation » : lit le JSON et génère TOUT le dossier.
    if args.nav:
        if args.out is None:
            parser.error("--nav exige --out (répertoire de sortie du dossier)")
        return _run_navplan(args.nav, args.out)

    if not args.icao or not args.date:
        parser.error("fournir un code OACI et --date, OU --nav fichier.json --out dossier/")

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
        degagements=[c.strip().upper() for c in args.degagements_icao.split(",") if c.strip()],
        # Volontairement PAS `--rayon-degagement` : celui-ci est le rayon de
        # RECHERCHE des terrains posables de la feuille de dégagement (30 NM),
        # une notion sans rapport avec le rayon NOTAM autour d'un dégagement
        # DÉCLARÉ. Les confondre demandait 30 NM de NOTAM autour de chacun.
    )

    package = assemble_briefing(context, default_providers())
    tiles = _tile_store()

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

        # Un vol LOCAL cite des SUP AIP comme n'importe quel autre : ses NOTAM
        # viennent de la même source. Sans cet appel, l'index n'était jamais
        # rafraîchi sur ce chemin et aucune copie locale n'était déposée — le
        # briefing affichait « référence non résolue » sur TOUS les renvois.
        supaip_local, supaip_index = _download_cited_supaip(package, args.html.parent)
        # Le HTML est AUTONOME : images embarquées en data URI, aucun lien
        # externe. Consultable et archivable tel quel, hors ligne.
        args.html.write_text(
            render_html(
                package,
                supaip_local=supaip_local,
                supaip_index=supaip_index,
                resolve_tile=tiles.data_uri,
            ),
            encoding="utf-8",
        )
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

    if args.navlog:
        if context.route is None:
            parser.error("--navlog exige une route : ajouter --route")
        variation = _declination(
            args.declinaison,
            context.geometry.bounding_circle().center,
            context.window.start.strftime("%Y-%m-%d"),
        )
        _render_navlog(context, args.navlog, magnetic_variation_deg=variation)
        print(f"  Navlog : {args.navlog}")

    if args.gpx:
        if context.route is None:
            parser.error("--gpx exige une route : ajouter --route")
        from .gpx import route_to_gpx

        title = f"{context.origin_icao or ''} → {context.destination_icao or 'nav'}".strip()
        args.gpx.write_text(route_to_gpx(context.route, title=title), encoding="utf-8")
        print(f"  GPX (ForeFlight) : {args.gpx}")

    if args.masse_centrage:
        from .aircraft.registry import resolve as resolve_aircraft
        from .render.massbalance import render_massbalance

        # Web-app par avion, résolu par immatriculation (--aeronef, défaut F-ZZZZ).
        aircraft = resolve_aircraft(args.aeronef)
        args.masse_centrage.write_text(render_massbalance(aircraft), encoding="utf-8")
        print(f"  Masse & centrage ({aircraft.name}) : {args.masse_centrage}")

    if args.checklist:
        if context.route is None:
            parser.error("--checklist exige une route : ajouter --route")
        _render_checklist(context, args.checklist, zone=args.zone)
        print(f"  Checklist : {args.checklist}")

    if args.degagement:
        aircraft, study = _render_diversion(
            context,
            package,
            args.degagement,
            aeronef=args.aeronef,
            radius_nm=args.rayon_degagement,
            limit=args.nombre_degagement,
            declinaison=args.declinaison,
            resolve_tile=tiles.data_uri,
        )
        print(f"  Dégagement ({aircraft.name}, {len(study.fields)} terrains) : {args.degagement}")
        if study.omitted:
            names = ", ".join(f"{icao} ({d:.0f} NM)" for icao, d in study.omitted)
            print(
                f"    plafond {args.nombre_degagement} atteint — NON détaillés : {names}"
                f" (--nombre-degagement pour les inclure)"
            )

    _report_tiles(tiles)
    return 0 if package.is_complete else 1


def _tile_store() -> TileStore:
    """Magasin de tuiles OSM du run, partagé par tous les documents produits.

    UN seul magasin : les cartes AROME et la carte de dégagement d'un même
    dossier regardent la même région, donc les mêmes tuiles. Le partager, c'est
    la différence entre télécharger neuf tuiles et les télécharger quinze fois —
    ce qu'OpenStreetMap appelle, à raison, ne pas respecter sa politique d'usage.
    """
    from .data.tiles import TileStore

    return TileStore()


def _report_tiles(store: TileStore) -> None:
    """Dit ce que le fond de carte a coûté, et surtout ce qui lui MANQUE.

    Un fond incomplet ne casse pas un dossier, mais il ne doit pas passer
    inaperçu : c'est une carte avec des trous.
    """
    if store.missing:
        print(
            f"  ATTENTION : fond de carte incomplet — {store.missing} tuile(s) OSM "
            f"non obtenue(s) ; les cartes concernées s'affichent sans fond"
        )
    elif store.downloaded:
        print(f"  Fond de carte : {store.downloaded} tuile(s) OSM téléchargée(s) et embarquée(s)")


def _download_cited_supaip(package: BriefingPackage, out_dir: Path) -> tuple[dict[str, str], Any]:
    """Télécharge, à côté du dossier, les SUP AIP que ses NOTAM citent.

    Un TRIGGER NOTAM ne porte pas l'information : elle est dans le supplément.
    Sans copie locale, la seule chose qu'on emporte en vol est un numéro. On ne
    prend QUE les suppléments cités — jamais la liste entière.

    C'est LE point où le réseau est autorisé pour les SUP AIP : l'index est
    chargé ici, une fois, puis passé au rendu — qui, lui, n'appelle jamais le
    réseau. Renvoie `(référence → chemin relatif, index)`. Un échec n'est
    jamais fatal : le lien retombe alors sur le site du SIA.
    """
    from .data import supaip

    references: set[str] = set()
    for item in package.notams:
        references.update(supaip.iter_references(item.value.raw_text, item.value.decoded_text))
    if not references:
        return {}, supaip.SupAipIndex()

    index = supaip.load_index()
    store = supaip.store_dir()
    directory = out_dir / "sup_aip"
    local: dict[str, str] = {}
    liens = copies = 0
    for reference in sorted(references):
        entry = index.lookup(reference)
        if entry is None:
            continue
        # Le PDF est téléchargé UNE fois dans le magasin de référence ; le
        # dossier n'en garde qu'un lien. Deux vols qui citent le même
        # supplément ne le stockent donc pas deux fois.
        pdf = supaip.download(entry, store)
        if pdf is None:
            continue
        path, is_link = supaip.link_into(pdf, directory)
        local[reference] = f"sup_aip/{path.name}"
        liens += is_link
        copies += not is_link
    if local:
        comment = f"{liens} lien(s)" + (f", {copies} copie(s)" if copies else "")
        print(f"  SUP AIP : {len(local)}/{len(references)} dans sup_aip/ ({comment})")
    elif references:
        print(f"  SUP AIP : {len(references)} cités, aucun résolu (liens vers le SIA)")
    return local, index


def _render_diversion(
    context: BriefingContext,
    package: BriefingPackage,
    output: Path,
    *,
    aeronef: str | None,
    radius_nm: float,
    declinaison: float | None,
    limit: int = 12,
    resolve_tile: Callable[[str], str | None] | None = None,
) -> tuple[AircraftSpec, DiversionStudy]:
    """Écrit le briefing de dégagement. Retourne (avion, étude) pour le log."""
    from .aircraft.registry import resolve as resolve_aircraft
    from .diversion import build_diversion_study
    from .render.diversion import render_diversion_html

    aircraft = resolve_aircraft(aeronef)
    variation = _declination(
        declinaison,
        context.geometry.bounding_circle().center,
        context.window.start.strftime("%Y-%m-%d"),
    )
    study = build_diversion_study(
        package,
        aircraft,
        radius_nm=radius_nm,
        limit=limit,
        variation_deg=variation,
    )
    output.write_text(render_diversion_html(study, resolve_tile=resolve_tile), encoding="utf-8")
    return aircraft, study


def _render_checklist(context: BriefingContext, output: Path, *, zone: str) -> None:
    """Checklist de prépa nav, indépendante du réseau : coucher du soleil, liste
    VAC et carburant mini ESTIMÉ (sans vent) pré-remplis en tête."""
    from .aircraft.registry import resolve as resolve_aircraft
    from .data import airports
    from .domain.fuel import fuel_plan
    from .domain.sun import sun_times
    from .render.checklist import render_checklist_html

    assert context.route is not None
    route = context.route
    aircraft = resolve_aircraft(context.aircraft_id)
    tz = ZoneInfo(zone)
    local_start = context.window.start.astimezone(tz)

    center = route.bounding_circle().center
    sun = sun_times(local_start.date(), center.lat, center.lon)

    fuel_min = None
    capacity = (
        sum(t.capacity_l for t in aircraft.weight_balance.fuels) if aircraft.weight_balance else 0.0
    )
    if aircraft.cruise_tas_kt and aircraft.cruise_fuel_lph and capacity:
        trip_l = route.total_distance_nm() / aircraft.cruise_tas_kt * aircraft.cruise_fuel_lph
        plan = fuel_plan(trip_l=trip_l, fuel_flow_lph=aircraft.cruise_fuel_lph, capacity_l=capacity)
        fuel_min = f"{plan.total_l:.0f}"

    vac_icaos: list[str] = []
    for waypoint in route.waypoints:
        aerodrome = airports.lookup(waypoint.name)
        if aerodrome is not None and aerodrome.icao not in vac_icaos:
            vac_icaos.append(aerodrome.icao)

    output.write_text(
        render_checklist_html(
            origin=context.origin_icao or route.waypoints[0].name,
            destination=context.destination_icao or route.waypoints[-1].name,
            date_label=f"{local_start:%d/%m/%Y}",
            aircraft_name=aircraft.name,
            sunset=f"{sun.sunset.astimezone(tz):%H:%M}" if sun.sunset else None,
            night=f"{sun.aeronautical_night_start.astimezone(tz):%H:%M}"
            if sun.aeronautical_night_start
            else None,
            fuel_min_l=fuel_min,
            vac_icaos=vac_icaos,
            display_timezone=zone,
        ),
        encoding="utf-8",
    )


def _declination(override: float | None, center: Position, date_iso: str) -> float:
    """Déclinaison magnétique : l'override s'il est donné, sinon le calcul WMM offline.

    Si le modèle est indisponible, on retombe sur 0° en le SIGNALANT (plutôt que de
    faire échouer tout le dossier) — mais pygeomag est offline/déterministe."""
    if override is not None:
        return override
    try:
        from .data import magnetic

        value = magnetic.declination_deg(center, year=magnetic.decimal_year(date_iso))
        print(f"  Déclinaison magnétique {value:+.1f}° (WMM_2025, automatique)")
        return value
    except Exception as exc:  # noqa: BLE001 - WMM indispo : 0° signalé, pas fatal
        print(f"  ATTENTION : déclinaison auto indisponible ({exc}) → 0°")
        return 0.0


def _render_navlog(
    context: BriefingContext,
    output: Path,
    *,
    magnetic_variation_deg: float,
    show_diversions: bool = True,
) -> None:
    """Produit le log de navigation PDF pour la route du contexte.

    L'avion est le DR400/160 d'exemple (seul modèle codé pour l'instant) ; ses
    perfs de croisière sont encore provisoires. Le vent en altitude vient
    d'Open-Meteo, échantillonné à l'heure de départ.
    """
    from .aircraft.registry import resolve as resolve_aircraft
    from .navlog_build import (
        annotate_navlog,
        build_navlog,
        runway_performance,
        safety_altitudes,
    )
    from .render.navlog import render_navlog_pdf

    assert context.route is not None
    aircraft = resolve_aircraft(context.aircraft_id)
    navlog = build_navlog(
        context.route,
        aircraft,
        departure_time=context.window.start,
        magnetic_variation_deg=magnetic_variation_deg,
    )
    from .data.winds import SurfaceWinds

    annotations, vac_links = annotate_navlog(
        navlog,
        aircraft,
        magnetic_variation_deg=magnetic_variation_deg,
        surface_winds=SurfaceWinds(),
    )
    try:
        safety_ft = safety_altitudes(navlog)
    except Exception:  # noqa: BLE001 - élévation indisponible : Zmin à la main, pas fatal
        safety_ft = None

    fuel_plan = None
    capacity_l = (
        sum(t.capacity_l for t in aircraft.weight_balance.fuels) if aircraft.weight_balance else 0.0
    )
    if navlog.total_fuel_l is not None and aircraft.cruise_fuel_lph and capacity_l:
        from .domain.fuel import fuel_plan as compute_fuel_plan

        fuel_plan = compute_fuel_plan(
            trip_l=navlog.total_fuel_l,
            fuel_flow_lph=aircraft.cruise_fuel_lph,
            capacity_l=capacity_l,
        )

    origin_icao = context.origin_icao or context.route.waypoints[0].name
    dest_icao = context.destination_icao or context.route.waypoints[-1].name
    perfs = runway_performance(aircraft, origin_icao, dest_icao)
    meta: dict[str, Any] = dict(
        origin=origin_icao,
        destination=dest_icao,
        aircraft_name=aircraft.name,
        tas_kt=aircraft.cruise_tas_kt,
        fuel_flow_lph=aircraft.cruise_fuel_lph or None,
        speed_unit=aircraft.speed_unit,
        magnetic_variation_deg=magnetic_variation_deg,
        annotations=annotations,
        vac_links=vac_links,
        safety_ft=safety_ft,
        fuel_plan=fuel_plan,
        perfs=perfs,
        show_diversions=show_diversions,
    )
    # Extension .html → page HTML (confort écran) ; sinon PDF imprimable.
    if output.suffix.lower() == ".html":
        from .render.navlog import render_navlog_html

        output.write_text(render_navlog_html(navlog, **meta), encoding="utf-8")
    else:
        render_navlog_pdf(navlog, output, **meta)


def _render_fuelbalance(
    context: BriefingContext, output: Path, *, magnetic_variation_deg: float
) -> None:
    """Produit le bilan carburant par branche (HTML+JS autonome) pour la route.

    Réutilise le MÊME `build_navlog` que le navlog (temps de branche identiques) ;
    l'avion est résolu par immatriculation (--aeronef, défaut F-ZZZZ)."""
    from .aircraft.registry import resolve as resolve_aircraft
    from .navlog_build import build_navlog
    from .render.fuelbalance import render_fuelbalance

    assert context.route is not None
    aircraft = resolve_aircraft(context.aircraft_id)
    navlog = build_navlog(
        context.route,
        aircraft,
        departure_time=context.window.start,
        magnetic_variation_deg=magnetic_variation_deg,
    )
    output.write_text(render_fuelbalance(navlog, aircraft), encoding="utf-8")


def _run_navplan(nav_path: Path, out_dir: Path) -> int:
    """Lit un fichier de navigation JSON et génère TOUT le dossier dans `out_dir`
    (briefing HTML+PDF, navlog HTML+PDF, viewer 3D, masse & centrage, checklist)."""
    from pydantic import ValidationError

    from .aircraft.registry import DEFAULT_REGISTRATION
    from .aircraft.registry import resolve as resolve_aircraft
    from .gpx import route_to_gpx
    from .navplan import load_navplan
    from .render.html import render_html
    from .render.massbalance import render_massbalance
    from .render.viewer import render_viewer

    try:
        plan = load_navplan(nav_path)
    except (ValidationError, ValueError) as exc:
        print(f"NAVIGATION INVALIDE — {nav_path}\n{exc}")
        return 2
    context = build_context(
        plan.depart,
        date=plan.date,
        heure=plan.heure,
        duree_h=plan.duree_h,
        rayon_nm=20.0,
        zone=plan.zone,
        aeronef=plan.aeronef,
        route=plan.route_spec or None,
        largeur_nm=plan.demi_couloir_nm,
        degagements=plan.degagements,
        degagement_rayon_nm=plan.degagement_rayon_nm,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    package = assemble_briefing(context, default_providers())
    tiles = _tile_store()
    origin = context.origin_icao or plan.depart
    dest = context.destination_icao or "nav"
    slug = f"{origin}_{dest}"
    # Nom court du dossier tiré du fichier de nav (« nav_aller.json » → « aller »),
    # pour des sorties lisibles (navlog_aller, brief_aller…) ; repli sur le slug OACI.
    stem = nav_path.stem
    label = stem[4:] if stem.startswith("nav_") and len(stem) > 4 else slug

    print(f"Dossier « {plan.title} » — {context.window.start:%d/%m/%Y %H:%MZ} → {out_dir}")
    print(
        f"  METAR {len(package.metars)} | TAF {len(package.tafs)}"
        f" | NOTAM {len(package.notams)} | prévisions {len(package.forecasts)}"
        f" | cartes {len(package.charts)}"
    )
    if not package.is_complete:
        print("  ATTENTION : dossier INCOMPLET")
    for failure in package.failures:
        marker = "CRITIQUE" if failure.is_critical else "mineur"
        print(f"    [{marker}] {failure.source} : {failure.reason}")

    # Dossier 100 % HTML (imprimable soi-même) : pas de PDF, trop de fichiers.
    # Deux dossiers distincts : météo (Météo-France) et NOTAM (SIA/SOFIA).
    supaip_local, supaip_index = _download_cited_supaip(package, out_dir)
    for kind in ("meteo", "notam"):
        (out_dir / f"brief_{kind}_{label}.html").write_text(
            render_html(
                package,
                kind=kind,
                supaip_local=supaip_local,
                supaip_index=supaip_index,
                resolve_tile=tiles.data_uri,
            ),
            encoding="utf-8",
        )
    (out_dir / f"viewer_{label}.html").write_text(render_viewer(package), encoding="utf-8")
    registration = plan.aeronef or DEFAULT_REGISTRATION
    (out_dir / f"masse_centrage_{registration}.html").write_text(
        render_massbalance(resolve_aircraft(plan.aeronef)), encoding="utf-8"
    )
    # Briefing de dégagement : terrains posables autour du vol + perfs déco/atterro.
    _render_diversion(
        context,
        package,
        out_dir / f"brief_degagement_{label}.html",
        aeronef=plan.aeronef,
        radius_nm=30.0,
        declinaison=plan.declinaison_deg,
        resolve_tile=tiles.data_uri,
    )
    if context.route is not None:
        variation = _declination(
            plan.declinaison_deg, context.geometry.bounding_circle().center, plan.date
        )
        _render_navlog(
            context,
            out_dir / f"navlog_{label}.html",
            magnetic_variation_deg=variation,
            show_diversions=False,  # masqué pour l'instant (place) — code conservé
        )
        _render_fuelbalance(
            context, out_dir / f"bilan_carburant_{label}.html", magnetic_variation_deg=variation
        )
        _render_checklist(context, out_dir / f"checklist_{label}.html", zone=plan.zone)
        # Route exportable dans ForeFlight / SkyDemon / Garmin Pilot.
        (out_dir / f"nav_{label}.gpx").write_text(
            route_to_gpx(context.route, title=plan.title or slug), encoding="utf-8"
        )
        print(f"  GPX (ForeFlight) : nav_{label}.gpx ({len(context.route.waypoints)} points)")

    _report_tiles(tiles)
    print(f"  → dossier complet écrit dans {out_dir}")
    return 0 if package.is_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
