"""Éphémérides solaires : lever, coucher, et nuit aéronautique.

Pur, sans I/O ni dépendance. Implémente l'« équation du lever du soleil »
(algorithme standard, précision ~1 min, suffisante pour la planification VFR).

La **nuit aéronautique** en France commence 30 min après le coucher du soleil et
se termine 30 min avant le lever (règle française). C'est la limite du VFR de
jour : il faut s'être posé avant. On expose le coucher, et le début de nuit aéro.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date as _date  # noqa: TID251 - une date calendaire, pas un instant
from datetime import timedelta  # noqa: TID251 - durée

from .window import UtcDateTime

#: Hauteur du centre solaire au lever/coucher apparent (réfraction + demi-diamètre).
_SUNSET_ALTITUDE_DEG = -0.833
#: Décalage France entre coucher et début de nuit aéronautique.
AERONAUTICAL_NIGHT_OFFSET = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class SunTimes:
    sunrise: UtcDateTime | None
    sunset: UtcDateTime | None

    @property
    def aeronautical_night_start(self) -> UtcDateTime | None:
        """Début de nuit aéronautique = coucher + 30 min (règle France)."""
        if self.sunset is None:
            return None
        return UtcDateTime.of(self.sunset + AERONAUTICAL_NIGHT_OFFSET, "nuit aéronautique")

    @property
    def aeronautical_night_end(self) -> UtcDateTime | None:
        """Fin de nuit aéronautique = lever − 30 min."""
        if self.sunrise is None:
            return None
        return UtcDateTime.of(self.sunrise - AERONAUTICAL_NIGHT_OFFSET, "fin de nuit aéronautique")


def sun_times(day: _date, lat: float, lon: float) -> SunTimes:
    """Lever et coucher du soleil (UTC) pour une date et une position.

    `None` si le soleil ne se lève/couche pas ce jour-là (latitudes polaires)."""
    transit, hour_angle = _transit_and_hour_angle(day, lat, lon, _SUNSET_ALTITUDE_DEG)
    if hour_angle is None:
        return SunTimes(sunrise=None, sunset=None)
    return SunTimes(
        sunrise=_from_julian(transit - hour_angle / 360.0),
        sunset=_from_julian(transit + hour_angle / 360.0),
    )


def _transit_and_hour_angle(
    day: _date, lat: float, lon: float, altitude_deg: float
) -> tuple[float, float | None]:
    """(Julian du transit solaire, demi-angle horaire en degrés) — cœur du calcul."""
    jd = _julian_day(day)
    n = round(jd - 2451545.0 + 0.0008)
    j_star = n + 0.0009 + lon / 360.0
    mean_anom = (357.5291 + 0.98560028 * j_star) % 360.0
    m_rad = math.radians(mean_anom)
    center = 1.9148 * math.sin(m_rad) + 0.0200 * math.sin(2 * m_rad) + 0.0003 * math.sin(3 * m_rad)
    ecl_lon = math.radians((mean_anom + center + 180.0 + 102.9372) % 360.0)
    transit = 2451545.0 + j_star + 0.0053 * math.sin(m_rad) - 0.0069 * math.sin(2 * ecl_lon)

    declination = math.asin(math.sin(ecl_lon) * math.sin(math.radians(23.44)))
    phi = math.radians(lat)
    cos_omega = (math.sin(math.radians(altitude_deg)) - math.sin(phi) * math.sin(declination)) / (
        math.cos(phi) * math.cos(declination)
    )
    if not -1.0 <= cos_omega <= 1.0:
        return transit, None
    return transit, math.degrees(math.acos(cos_omega))


def _julian_day(day: _date) -> float:
    """Jour julien à 00:00 UTC de la date (calendrier grégorien)."""
    year, month = day.year, day.month
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + day.day
        + b
        - 1524.5
    )


def _from_julian(jd: float) -> UtcDateTime:
    """Jour julien → instant UTC."""
    jd += 0.5
    z = math.floor(jd)
    frac = jd - z
    if z < 2299161:
        a = z
    else:
        alpha = math.floor((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - math.floor(alpha / 4)
    b = a + 1524
    c = math.floor((b - 122.1) / 365.25)
    d = math.floor(365.25 * c)
    e = math.floor((b - d) / 30.6001)
    day = b - d - math.floor(30.6001 * e)
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715
    seconds_total = round(frac * 86400.0)
    hours = seconds_total // 3600
    minutes = (seconds_total % 3600) // 60
    seconds = seconds_total % 60
    from datetime import UTC

    return UtcDateTime(year, month, int(day), hours, minutes, seconds, tzinfo=UTC)
