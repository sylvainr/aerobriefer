"""Tests du provider met.no.

La fixture `RESPONSE_JSON` est une VRAIE réponse de
``api.met.no/weatherapi/locationforecast/2.0/compact?lat=45.628&lon=-0.9725``,
capturée le 2026-07-20 à 12:00Z et réduite à neuf échéances : l'entourage de la
fenêtre cible LFCY du 2026-07-21 08:00-11:00Z, plus deux échéances de J+3 qui
documentent la bascule du pas horaire au pas 6-horaire (et la disparition
corrélée du bloc ``next_1_hours``).

Elle est figée : ces tests tournent hors ligne. Le seul test qui sort sur le
réseau porte la marque ``network``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from aerobriefer.domain.context import BriefingContext
from aerobriefer.domain.geo import Corridor, Position
from aerobriefer.domain.models import ForecastPoint
from aerobriefer.domain.window import TimeWindow, UtcDateTime
from aerobriefer.providers.base import ProviderError
from aerobriefer.providers.metno import (
    DEFAULT_USER_AGENT,
    MS_TO_KT,
    MetNoProvider,
    dewpoint_c,
    estimate_cloud_base_ft,
    ms_to_knots,
)

# LFCY Royan-Medis.
LFCY = Position(45.628101, -0.9725)

# En-tetes de cache reellement renvoyes par met.no sur la capture.
RESPONSE_HEADERS = {
    "Content-Type": "application/json",
    "Last-Modified": "Mon, 20 Jul 2026 12:00:54 GMT",
    "Expires": "Mon, 20 Jul 2026 12:31:21 GMT",
}

RESPONSE_JSON = r"""
{
  "type": "Feature",
  "geometry": {
    "type": "Point",
    "coordinates": [
      -0.9725,
      45.628,
      4
    ]
  },
  "properties": {
    "meta": {
      "updated_at": "2026-07-20T11:19:06Z",
      "units": {
        "air_pressure_at_sea_level": "hPa",
        "air_temperature": "celsius",
        "cloud_area_fraction": "%",
        "precipitation_amount": "mm",
        "relative_humidity": "%",
        "wind_from_direction": "degrees",
        "wind_speed": "m/s"
      }
    },
    "timeseries": [
      {
        "time": "2026-07-21T06:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.3,
              "air_temperature": 18.4,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 50.6,
              "wind_from_direction": 33.3,
              "wind_speed": 6.4
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-21T07:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.6,
              "air_temperature": 19.6,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 48.8,
              "wind_from_direction": 35.6,
              "wind_speed": 6.9
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-21T08:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.7,
              "air_temperature": 21.2,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 45.1,
              "wind_from_direction": 38.4,
              "wind_speed": 7.0
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-21T09:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.8,
              "air_temperature": 23.3,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 39.9,
              "wind_from_direction": 39.9,
              "wind_speed": 6.7
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-21T10:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.8,
              "air_temperature": 25.2,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 34.6,
              "wind_from_direction": 44.8,
              "wind_speed": 6.7
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-21T11:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.9,
              "air_temperature": 26.4,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 30.5,
              "wind_from_direction": 47.0,
              "wind_speed": 6.7
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-21T12:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1021.7,
              "air_temperature": 27.4,
              "cloud_area_fraction": 0.0,
              "relative_humidity": 29.1,
              "wind_from_direction": 44.3,
              "wind_speed": 6.4
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {}
          },
          "next_1_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "clearsky_day"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-23T00:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1018.6,
              "air_temperature": 23.4,
              "cloud_area_fraction": 77.3,
              "relative_humidity": 40.7,
              "wind_from_direction": 36.7,
              "wind_speed": 4.8
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "partlycloudy_day"
            },
            "details": {}
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "partlycloudy_night"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      },
      {
        "time": "2026-07-23T06:00:00Z",
        "data": {
          "instant": {
            "details": {
              "air_pressure_at_sea_level": 1018.5,
              "air_temperature": 21.0,
              "cloud_area_fraction": 89.1,
              "relative_humidity": 51.8,
              "wind_from_direction": 53.7,
              "wind_speed": 4.3
            }
          },
          "next_12_hours": {
            "summary": {
              "symbol_code": "fair_day"
            },
            "details": {}
          },
          "next_6_hours": {
            "summary": {
              "symbol_code": "cloudy"
            },
            "details": {
              "precipitation_amount": 0.0
            }
          }
        }
      }
    ]
  }
}
"""


def response_payload() -> dict:
    return json.loads(RESPONSE_JSON)


def window(start: str, end: str) -> TimeWindow:
    return TimeWindow(UtcDateTime.parse(start), UtcDateTime.parse(end))


def context_lfcy(start: str = "2026-07-21T08:00:00Z", end: str = "2026-07-21T11:00:00Z"):
    """Le cas cible : vol local a LFCY, demain matin."""
    return BriefingContext.local(
        center=LFCY, radius_nm=25.0, window=window(start, end), icao="LFCY"
    )


def make_provider(
    handler=None,
    *,
    payload: dict | None = None,
    status_code: int = 200,
    headers: dict | None = None,
    **kwargs,
) -> MetNoProvider:
    """Provider branche sur un transport simule : aucun octet ne sort."""
    if handler is None:

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                json=payload if payload is not None else response_payload(),
                headers={**RESPONSE_HEADERS, **(headers or {})},
            )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetNoProvider(client=client, **kwargs)


# --- Conversions d'unites -------------------------------------------------


def test_ms_to_knots_uses_the_exact_nautical_ratio():
    assert ms_to_knots(1.0) == pytest.approx(1.943844)
    assert ms_to_knots(0.0) == 0.0
    # 10 m/s, valeur ronde facile a verifier de tete : ~19.4 kt.
    assert ms_to_knots(10.0) == pytest.approx(19.43844)
    # 1 kt vaut 1852 m/h exactement : la constante doit refermer la boucle.
    assert MS_TO_KT == pytest.approx(3600.0 / 1852.0)


def test_wind_speed_is_converted_from_ms_to_knots_end_to_end():
    """La conversion doit survivre au trajet complet, pas seulement en unitaire.

    Une source en m/s rendue telle quelle dans un champ nomme `_kt` serait une
    sous-estimation d'un facteur deux du vent annonce au pilote.
    """
    provider = make_provider()
    points = [s.value for s in provider.fetch(context_lfcy())]

    # 2026-07-21T08:00Z porte wind_speed = 7.0 m/s dans la fixture.
    at_0800 = next(p for p in points if p.valid_at == UtcDateTime.parse("2026-07-21T08:00:00Z"))
    assert at_0800.wind_speed_kt == pytest.approx(13.6, abs=0.05)
    assert at_0800.wind_speed_kt == pytest.approx(round(7.0 * 1.943844, 1))

    # Et surtout : jamais la valeur brute en m/s.
    assert at_0800.wind_speed_kt != pytest.approx(7.0)


def test_direction_pressure_and_cloud_cover_pass_through_unconverted():
    """Ces trois-la sont deja dans l'unite du domaine : toute 'conversion'
    serait une corruption."""
    provider = make_provider()
    at_0900 = next(
        s.value
        for s in provider.fetch(context_lfcy())
        if s.value.valid_at == UtcDateTime.parse("2026-07-21T09:00:00Z")
    )
    assert at_0900.wind_dir_deg == pytest.approx(39.9)  # degres -> degres
    assert at_0900.qnh_hpa == pytest.approx(1021.8)  # hPa -> hPa
    assert at_0900.cloud_cover_pct == pytest.approx(0.0)  # % -> %
    assert at_0900.temperature_c == pytest.approx(23.3)  # celsius -> celsius


def test_precipitation_comes_from_next_1_hours_while_hourly():
    provider = make_provider()
    points = [s.value for s in provider.fetch(context_lfcy())]
    assert all(p.precipitation_mm == pytest.approx(0.0) for p in points)


def test_precipitation_falls_back_to_next_6_hours_past_the_step_change():
    """Au-dela de ~2,5 jours met.no passe au pas 6-horaire et `next_1_hours`
    disparait ; le cumul doit alors venir de `next_6_hours`."""
    provider = make_provider()
    points = [
        s.value
        for s in provider.fetch(context_lfcy("2026-07-23T00:00:00Z", "2026-07-23T06:00:00Z"))
    ]
    assert [p.valid_at.hour for p in points] == [0, 6]
    assert points[0].precipitation_mm is not None


def test_missing_field_becomes_none_and_never_zero():
    """Un vent inconnu et un vent nul ne se pilotent pas pareil."""
    payload = response_payload()
    del payload["properties"]["timeseries"][2]["data"]["instant"]["details"]["wind_speed"]
    provider = make_provider(payload=payload)

    at_0800 = next(
        s.value
        for s in provider.fetch(context_lfcy())
        if s.value.valid_at == UtcDateTime.parse("2026-07-21T08:00:00Z")
    )
    assert at_0800.wind_speed_kt is None
    assert at_0800.wind_dir_deg is not None  # les voisins survivent


def test_compact_endpoint_never_claims_a_gust():
    """`compact` ne porte pas `wind_speed_of_gust`. Annoncer une rafale egale au
    vent moyen se lirait comme une absence de rafale AVEREE."""
    provider = make_provider()
    assert all(s.value.wind_gust_kt is None for s in provider.fetch(context_lfcy()))


# --- Filtrage sur la fenetre ---------------------------------------------


def test_only_forecasts_inside_the_window_are_returned():
    """La fixture couvre 06:00Z a 12:00Z ; la fenetre 08:00-11:00Z ne doit en
    retenir que quatre."""
    provider = make_provider()
    points = [s.value for s in provider.fetch(context_lfcy())]

    assert [p.valid_at.hour for p in points] == [8, 9, 10, 11]
    # Les echeances hors fenetre sont bien presentes dans la source...
    times = [e["time"] for e in response_payload()["properties"]["timeseries"]]
    assert "2026-07-21T07:00:00Z" in times and "2026-07-21T12:00:00Z" in times
    # ... et bien ecartees a la sortie.
    assert all(context_lfcy().window.contains(p.valid_at) for p in points)


def test_padding_hours_ajoute_des_echeances_de_contexte():
    """Avec padding_hours=2, on rend aussi ±2 h autour de la fenêtre.

    La fenêtre 08–11Z devient 06–13Z ; la fixture couvre 06–12Z, donc on gagne
    07Z avant et 12Z après en plus des quatre échéances du vol."""
    strict = [s.value.valid_at.hour for s in make_provider().fetch(context_lfcy())]
    padded = [s.value.valid_at.hour for s in make_provider(padding_hours=2.0).fetch(context_lfcy())]
    assert strict == [8, 9, 10, 11]
    assert 7 in padded and 12 in padded
    assert set(strict) <= set(padded)


def test_window_bounds_are_inclusive():
    """Convention du domaine : une donnee valide a l'heure exacte du decollage
    nous concerne."""
    provider = make_provider()
    points = [s.value for s in provider.fetch(context_lfcy())]
    assert points[0].valid_at == UtcDateTime.parse("2026-07-21T08:00:00Z")
    assert points[-1].valid_at == UtcDateTime.parse("2026-07-21T11:00:00Z")


def test_single_instant_window_keeps_exactly_that_instant():
    provider = make_provider()
    points = [
        s.value
        for s in provider.fetch(context_lfcy("2026-07-21T09:00:00Z", "2026-07-21T09:00:00Z"))
    ]
    assert [p.valid_at.hour for p in points] == [9]


def test_window_narrower_than_the_model_step_falls_back_to_bracketing_points():
    """Un vol de 40 min entre deux echeances horaires : `contains` ne retient
    rien, et rendre du vide serait mentir. On encadre."""
    provider = make_provider()
    points = [
        s.value
        for s in provider.fetch(context_lfcy("2026-07-21T09:10:00Z", "2026-07-21T09:50:00Z"))
    ]
    assert [p.valid_at.hour for p in points] == [9]


def test_window_outside_the_model_range_raises_rather_than_returning_empty():
    """Regle cardinale : un provider en echec leve. Une liste vide se lirait
    comme 'pas de meteo' et non 'pas de donnee'."""
    provider = make_provider()
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch(context_lfcy("2027-01-01T00:00:00Z", "2027-01-01T03:00:00Z"))
    assert "met.no" in str(excinfo.value)
    assert "aucune echeance" in str(excinfo.value).replace("é", "e")


# --- Geometrie et contrat du domaine --------------------------------------


def test_position_is_the_bounding_circle_center():
    provider = make_provider()
    for sourced in provider.fetch(context_lfcy()):
        assert sourced.value.position == LFCY


def test_corridor_geometry_is_queried_at_its_bounding_circle_center():
    """Un couloir de nav n'a pas de 'point' : le provider doit passer par
    `bounding_circle`, comme le prevoit le Protocol Geometry."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["lat"] = float(request.url.params["lat"])
        seen["lon"] = float(request.url.params["lon"])
        return httpx.Response(200, json=response_payload(), headers=RESPONSE_HEADERS)

    corridor = Corridor([LFCY, Position(46.628101, -0.9725)], half_width_nm=10.0)
    context = BriefingContext(
        geometry=corridor, window=window("2026-07-21T08:00:00Z", "2026-07-21T11:00:00Z")
    )
    points = [s.value for s in make_provider(handler).fetch(context)]

    expected = corridor.bounding_circle().center
    assert seen["lat"] == pytest.approx(expected.lat, abs=1e-4)
    assert points[0].position == expected


def test_produces_forecast_points_carrying_their_provenance():
    provider = make_provider()
    results = provider.fetch(context_lfcy())

    assert results, "jamais de liste vide"
    for sourced in results:
        assert isinstance(sourced.value, ForecastPoint)
        assert sourced.provenance.source == "met.no"
        assert sourced.provenance.url is not None
        # `meta.updated_at` de la fixture : le dernier tour de modele, distinct
        # de l'instant de telechargement.
        assert sourced.provenance.issued_at == UtcDateTime.parse("2026-07-20T11:19:06Z")
        assert sourced.provenance.retrieved_at >= sourced.provenance.issued_at


def test_valid_at_is_utc_and_never_naive():
    provider = make_provider()
    for sourced in provider.fetch(context_lfcy()):
        assert isinstance(sourced.value.valid_at, UtcDateTime)
        assert sourced.value.valid_at.utcoffset().total_seconds() == 0


def test_provider_satisfies_the_protocol_metadata():
    provider = make_provider()
    assert provider.name == "met.no"
    assert provider.is_critical is False


# --- Base des nuages : une ESTIMATION, jamais une observation --------------


def test_dewpoint_matches_magnus_reference_values():
    # Saturation : le point de rosee rejoint la temperature.
    assert dewpoint_c(20.0, 100.0) == pytest.approx(20.0, abs=0.05)
    # Reference usuelle : 20 C / 50 % -> ~9.3 C.
    assert dewpoint_c(20.0, 50.0) == pytest.approx(9.3, abs=0.3)
    assert dewpoint_c(20.0, 0.0) is None


def test_cloud_base_is_estimated_only_when_there_is_actually_a_cloud_layer():
    """La formule decrit une base de cumulus. Sans couche, il n'y a pas de
    plafond a annoncer — et en inventer un serait une erreur de securite."""
    # Ciel clair : la fixture reelle est a 0 % de nebulosite.
    assert estimate_cloud_base_ft(21.2, 45.1, 0.0) is None
    # FEW : toujours rien a annoncer.
    assert estimate_cloud_base_ft(21.2, 45.1, 10.0) is None
    # SCT et au-dela : estimation possible.
    assert estimate_cloud_base_ft(20.0, 70.0, 60.0) is not None


def test_cloud_base_estimate_follows_the_400ft_per_degree_rule():
    base = estimate_cloud_base_ft(20.0, 70.0, 60.0)
    spread = 20.0 - dewpoint_c(20.0, 70.0)
    assert base == pytest.approx(spread * 400.0, abs=1.0)
    # 20 C a 70 % -> point de rosee ~14.4 C, soit ~5.6 C d'ecart -> ~2250 ft.
    assert spread == pytest.approx(5.6, abs=0.2)
    assert 2000 < base < 2500


def test_cloud_base_is_none_when_the_spread_leaves_the_formula_domain():
    """Air tres sec : la regle du pouce extrapole hors de son domaine. On omet
    plutot que de deviner — une base absente se voit, une base fausse se croit."""
    assert estimate_cloud_base_ft(30.0, 10.0, 90.0) is None
    assert estimate_cloud_base_ft(None, 70.0, 90.0) is None
    assert estimate_cloud_base_ft(20.0, None, 90.0) is None


def test_cloud_base_estimate_reaches_the_forecast_point():
    """Meme trajet, mais depuis une reponse mutee : la fixture reelle etant a
    ciel clair, elle ne peut pas exercer ce chemin."""
    payload = response_payload()
    details = payload["properties"]["timeseries"][2]["data"]["instant"]["details"]
    details["cloud_area_fraction"] = 75.0
    details["relative_humidity"] = 80.0
    details["air_temperature"] = 18.0

    at_0800 = next(
        s.value
        for s in make_provider(payload=payload).fetch(context_lfcy())
        if s.value.valid_at == UtcDateTime.parse("2026-07-21T08:00:00Z")
    )
    assert at_0800.cloud_base_ft is not None
    assert at_0800.cloud_base_ft == estimate_cloud_base_ft(18.0, 80.0, 75.0)


# --- Transport, en-tetes, erreurs -----------------------------------------


def test_identifiable_user_agent_is_sent():
    """met.no repond 403 sans User-Agent joignable : c'est la contrepartie de
    la gratuite."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json=response_payload(), headers=RESPONSE_HEADERS)

    make_provider(handler).fetch(context_lfcy())
    assert seen["ua"] == DEFAULT_USER_AGENT
    assert "aerobriefer" in seen["ua"]
    assert "http" in seen["ua"]  # un moyen de contact, pas juste un nom


def test_anonymous_user_agent_is_refused_at_construction():
    with pytest.raises(ProviderError):
        MetNoProvider(user_agent="curl/8.0")


def test_cache_headers_are_recorded_for_the_caller():
    """met.no demande de respecter Expires et Last-Modified ; encore faut-il
    les avoir lus."""
    provider = make_provider()
    provider.fetch(context_lfcy())
    assert provider.last_modified == UtcDateTime.parse("2026-07-20T12:00:54Z")
    assert provider.expires == UtcDateTime.parse("2026-07-20T12:31:21Z")


def test_a_second_fetch_revalidates_conditionally_rather_than_refetching():
    """Une fois Expires depasse, on revalide en conditionnel avec
    If-Modified-Since, et un 304 reutilise le payload memorise.

    Expires est force dans le passe : sans cela le test dependrait de l'horloge
    (la capture est du 2026-07-20 12:00Z et resterait fraiche 31 minutes).
    """
    calls = []
    stale = {**RESPONSE_HEADERS, "Expires": "Mon, 20 Jul 2020 12:31:21 GMT"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("If-Modified-Since"))
        if len(calls) == 1:
            return httpx.Response(200, json=response_payload(), headers=stale)
        return httpx.Response(304, headers=stale)

    provider = make_provider(handler)
    first = [s.value for s in provider.fetch(context_lfcy())]
    second = [s.value for s in provider.fetch(context_lfcy())]

    assert calls[0] is None
    assert calls[1] == "Mon, 20 Jul 2026 12:00:54 GMT"
    assert [p.valid_at for p in first] == [p.valid_at for p in second]
    assert [p.wind_speed_kt for p in first] == [p.wind_speed_kt for p in second]


def test_unexpired_response_is_served_from_cache_without_a_second_request():
    """Tant que Expires n'est pas atteint, met.no INTERDIT de redemander."""
    calls = []
    far_future = {"Expires": "Mon, 20 Jul 2099 12:31:21 GMT"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            200, json=response_payload(), headers={**RESPONSE_HEADERS, **far_future}
        )

    provider = make_provider(handler)
    provider.fetch(context_lfcy())
    provider.fetch(context_lfcy())
    assert len(calls) == 1


def test_403_is_reported_as_a_user_agent_problem():
    provider = make_provider(status_code=403, payload={})
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch(context_lfcy())
    assert "403" in str(excinfo.value)
    assert "User-Agent" in str(excinfo.value)


def test_429_mentions_the_expires_discipline():
    provider = make_provider(status_code=429, payload={})
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch(context_lfcy())
    assert "Expires" in str(excinfo.value)


def test_server_error_raises_provider_error():
    provider = make_provider(status_code=503, payload={})
    with pytest.raises(ProviderError):
        provider.fetch(context_lfcy())


def test_timeout_raises_provider_error_and_names_the_source():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ProviderError) as excinfo:
        make_provider(handler).fetch(context_lfcy())
    assert "[met.no]" in str(excinfo.value)


def test_network_error_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=request)

    with pytest.raises(ProviderError):
        make_provider(handler).fetch(context_lfcy())


def test_malformed_json_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>", headers=RESPONSE_HEADERS)

    with pytest.raises(ProviderError):
        make_provider(handler).fetch(context_lfcy())


def test_empty_timeseries_raises_rather_than_returning_empty():
    payload = response_payload()
    payload["properties"]["timeseries"] = []
    with pytest.raises(ProviderError):
        make_provider(payload=payload).fetch(context_lfcy())


def test_structurally_broken_response_raises():
    with pytest.raises(ProviderError):
        make_provider(payload={"nothing": "useful"}).fetch(context_lfcy())


def test_unparsable_timestamps_are_skipped_not_fatal():
    """Un horodatage casse ne doit pas emporter les echeances saines."""
    payload = response_payload()
    payload["properties"]["timeseries"][2]["time"] = "pas une date"
    points = [s.value for s in make_provider(payload=payload).fetch(context_lfcy())]
    assert [p.valid_at.hour for p in points] == [9, 10, 11]


def test_default_timeout_is_explicit_and_bounded():
    """Sans timeout explicite, une source non critique pourrait suspendre tout
    le briefing."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=response_payload(), headers=RESPONSE_HEADERS)

    make_provider(handler).fetch(context_lfcy())
    assert seen["timeout"]["connect"] == 10.0
    assert seen["timeout"]["read"] == 10.0


# --- Test reseau ----------------------------------------------------------


@pytest.mark.network
def test_live_api_still_matches_the_frozen_fixture_shape():
    """Contre la derive de contrat : la fixture ci-dessus a une date de
    peremption, ce test la detecte.

    Ne remplace pas les tests hors ligne : il sort sur Internet et depend de la
    disponibilite de met.no.
    """
    provider = MetNoProvider()
    start = UtcDateTime.now() + timedelta(hours=2)
    context = BriefingContext.local(
        center=LFCY,
        radius_nm=25.0,
        window=TimeWindow(start, start + timedelta(hours=3)),
        icao="LFCY",
    )

    results = provider.fetch(context)
    assert results, "met.no doit rendre au moins une echeance a deux heures"

    for sourced in results:
        point = sourced.value
        assert isinstance(point, ForecastPoint)
        assert context.window.contains(point.valid_at)
        assert point.position == LFCY
        assert sourced.provenance.source == "met.no"
        # Plages physiques : detecte une unite qui aurait change de cote.
        assert point.wind_speed_kt is None or 0.0 <= point.wind_speed_kt < 200.0
        assert point.wind_dir_deg is None or 0.0 <= point.wind_dir_deg <= 360.0
        assert point.temperature_c is None or -60.0 < point.temperature_c < 60.0
        assert point.cloud_cover_pct is None or 0.0 <= point.cloud_cover_pct <= 100.0
        assert point.qnh_hpa is None or 850.0 < point.qnh_hpa < 1100.0

    # Les en-tetes de cache doivent exister : ils conditionnent notre droit
    # d'usage de l'API.
    assert provider.expires is not None
    assert provider.last_modified is not None
    assert provider.expires > provider.last_modified
