"""L'agrégateur : `BriefingContext` → `BriefingPackage`.

Couche 3.5 du modèle. Elle collecte auprès des providers, capture leurs échecs,
et rend un dossier autosuffisant. Elle ne rédige rien : la mise en forme est au
renderer, le jugement au modèle.

C'est ici, et seulement ici, qu'un échec de provider est converti en
`ProviderFailure`. Les providers lèvent, l'agrégateur encaisse.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor

from .domain.context import BriefingContext
from .domain.models import Aerodrome, Chart, ForecastPoint, Metar, Notam, Sigmet, Taf
from .domain.package import BriefingPackage, ProviderFailure
from .domain.sourced import Sourced
from .domain.window import utcnow
from .providers import cache
from .providers.base import Provider, ProviderError


def assemble_briefing(
    context: BriefingContext,
    providers: Sequence[Provider],
    *,
    parallel: bool = True,
) -> BriefingPackage:
    """Interroge tous les providers et assemble le dossier.

    Les providers sont indépendants : on les interroge en parallèle, et l'échec
    de l'un n'empêche jamais les autres de contribuer. Un briefing partiel
    correctement signalé vaut mieux qu'une absence de briefing.
    """
    collected: list[Sourced] = []
    failures: list[ProviderFailure] = []
    cache.reset_hits()

    def run(provider: Provider) -> None:
        try:
            collected.extend(provider.fetch(context))
        except ProviderError as error:
            failures.append(
                ProviderFailure(
                    source=error.source,
                    reason=error.message,
                    occurred_at=utcnow(),
                    category=getattr(provider, "category", None),
                    is_critical=getattr(provider, "is_critical", False),
                )
            )
        except Exception as error:  # noqa: BLE001 - un provider ne doit jamais tuer le briefing
            failures.append(
                ProviderFailure(
                    source=getattr(provider, "name", provider.__class__.__name__),
                    reason=f"exception inattendue : {error!r}",
                    occurred_at=utcnow(),
                    category=getattr(provider, "category", None),
                    is_critical=getattr(provider, "is_critical", False),
                )
            )

    if parallel and len(providers) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(providers))) as pool:
            list(pool.map(run, providers))
    else:
        for provider in providers:
            run(provider)

    if cache.is_enabled():
        # Le cache de développement est une DÉGRADATION du dossier : les données
        # peuvent dater. On le fait remonter comme anomalie visible plutôt que
        # de laisser croire à une collecte fraîche.
        served = sum(cache.hits().values())
        if served:
            failures.append(
                ProviderFailure(
                    source="cache",
                    reason=(
                        f"{served} réponse(s) servie(s) depuis le cache de développement "
                        f"(TTL {int(cache.ttl_seconds() or 0)} s) — données potentiellement "
                        f"périmées. NE PAS UTILISER EN VOL."
                    ),
                    occurred_at=utcnow(),
                    category="cache",
                    is_critical=True,
                )
            )

    return BriefingPackage(
        context=context,
        aerodromes=_aerodromes_for(context, collected),
        metars=_of_type(collected, Metar),
        tafs=_of_type(collected, Taf),
        notams=_relevant_notams(collected, context),
        sigmets=_relevant_sigmets(collected, context),
        forecasts=_of_type(collected, ForecastPoint),
        charts=_of_type(collected, Chart),
        failures=tuple(failures),
    )


def _of_type(items: Iterable[Sourced], wanted: type) -> tuple[Sourced, ...]:
    return tuple(item for item in items if isinstance(item.value, wanted))


def _relevant_notams(items: Iterable[Sourced], context: BriefingContext) -> tuple[Sourced, ...]:
    """Re-filtrage local des NOTAM.

    SOFIA filtre déjà côté serveur, mais on ne délègue pas un critère de
    sécurité à une source externe : on revérifie avec nos propres prédicats.
    Sans géométrie déclarée, `Notam.concerns` conserve — c'est voulu.
    """
    return tuple(
        item
        for item in items
        if isinstance(item.value, Notam) and item.value.concerns(context.geometry, context.window)
    )


def _relevant_sigmets(items: Iterable[Sourced], context: BriefingContext) -> tuple[Sourced, ...]:
    """Re-filtrage local des SIGMET : zone ET fenêtre. Sans polygone, on conserve
    (un SIGMET orage manquant est bien plus grave qu'un SIGMET de trop)."""
    return tuple(
        item
        for item in items
        if isinstance(item.value, Sigmet) and item.value.concerns(context.geometry, context.window)
    )


def _aerodromes_for(
    context: BriefingContext, collected: Iterable[Sourced] = ()
) -> tuple[Aerodrome, ...]:
    """Terrains à embarquer dans le dossier.

    Au-delà des terrains du vol et des stations interrogées, on inclut ceux
    cités par les NOTAM : sans quoi le rendu ne pourrait afficher que « LFDK »
    là où le lecteur attend « Soulac-sur-Mer ». Le dossier doit être
    autosuffisant — le renderer ne doit rien avoir à aller rechercher.
    """
    from .data import airports

    wanted = list(context.stations_of_interest)
    for item in collected:
        icao = getattr(item.value, "affected_icao", None) or getattr(item.value, "station", None)
        if icao:
            wanted.append(icao)

    found: dict[str, Aerodrome] = {}
    for icao in wanted:
        key = icao.strip().upper()
        if key in found:
            continue
        aerodrome = airports.lookup(key)
        if aerodrome is not None:
            found[key] = aerodrome
    return tuple(found.values())
