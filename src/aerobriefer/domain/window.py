"""Le temps du domaine. Tout est en UTC — le temps local n'existe pas en aéro.

`UtcDateTime` porte l'invariant dans le TYPE et non dans des validateurs répétés :
une instance existe, donc elle est localisée et normalisée en UTC. Il n'y a pas
de chemin de construction qui produise un naïf.

Le naïf est un piège particulièrement vicieux ici : il ne lève rien à la
construction, mais des heures plus tard au premier comparatif avec un aware —
typiquement au milieu d'un calcul d'âge, en plein rendu du briefing. On ferme la
porte à l'entrée.

Elle hérite de `datetime` à dessein : `isinstance(x, datetime)`, strftime, les
comparaisons et l'arithmétique continuent de fonctionner, et les providers
peuvent passer n'importe quel datetime aware sans cérémonie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, overload


class UtcDateTime(datetime):
    """Un datetime dont l'existence prouve qu'il est localisé et en UTC."""

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> UtcDateTime:
        # Chemin de désérialisation pickle : datetime(bytes, tzinfo).
        if args and isinstance(args[0], (bytes, str)):
            return super().__new__(cls, *args, **kwargs)

        if kwargs.get("tzinfo", args[7] if len(args) > 7 else None) is None:
            raise ValueError(
                "UtcDateTime refuse un datetime naïf : fournir tzinfo (ex. tzinfo=timezone.utc)"
            )

        instance = super().__new__(cls, *args, **kwargs)
        offset = instance.utcoffset()
        if offset is None:
            raise ValueError("UtcDateTime refuse un tzinfo dont l'offset est indéfini")
        if offset == timedelta(0):
            return instance
        naive = datetime(*instance.timetuple()[:6], instance.microsecond)
        return cls._from_naive_utc(naive - offset)

    @classmethod
    def _from_naive_utc(cls, naive: datetime) -> UtcDateTime:
        return super().__new__(
            cls,
            naive.year,
            naive.month,
            naive.day,
            naive.hour,
            naive.minute,
            naive.second,
            naive.microsecond,
            UTC,
        )

    @classmethod
    def of(cls, value: datetime, field: str = "datetime") -> UtcDateTime:
        """Porte d'entrée depuis un `datetime` quelconque. Rejette les naïfs."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, datetime):
            raise TypeError(f"{field} doit être un datetime, reçu {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"{field} est un datetime naïf ; fournir un datetime localisé "
                f"(ex. datetime(..., tzinfo=timezone.utc))"
            )
        as_utc = value.astimezone(UTC)
        return cls._from_naive_utc(as_utc.replace(tzinfo=None))

    @classmethod
    def optional(cls, value: datetime | None, field: str = "datetime") -> UtcDateTime | None:
        return None if value is None else cls.of(value, field)

    @classmethod
    def now(cls) -> UtcDateTime:  # type: ignore[override]
        return cls._from_naive_utc(datetime.now(UTC).replace(tzinfo=None))

    @classmethod
    def parse(cls, text: str, field: str = "datetime") -> UtcDateTime:
        """Depuis une chaîne ISO 8601. Le suffixe Z est accepté.

        Un ISO sans offset est refusé — c'est le cas le plus courant de naïf qui
        s'infiltre depuis une API.
        """
        return cls.of(datetime.fromisoformat(text.replace("Z", "+00:00")), field)

    def _as_plain(self) -> datetime:
        """Copie en `datetime` nu, toujours en UTC."""
        return datetime(
            self.year,
            self.month,
            self.day,
            self.hour,
            self.minute,
            self.second,
            self.microsecond,
            UTC,
        )

    # L'arithmétique reste dans le type : sans cela, `instant + timedelta`
    # retomberait en `datetime` nu et l'invariant fuirait au premier calcul.
    #
    # On calcule sur une copie nue plutôt que de déléguer à `super()` : la
    # construction C-level d'une sous-classe de datetime ne repasse pas `tzinfo`
    # en argument, et se ferait donc rejeter par notre propre `__new__`.
    def __add__(self, other: timedelta) -> UtcDateTime:
        if not isinstance(other, timedelta):
            return NotImplemented
        return self._from_naive_utc((self._as_plain() + other).replace(tzinfo=None))

    __radd__ = __add__

    # `datetime.__sub__` est surchargé (− timedelta → datetime, − datetime →
    # timedelta) ; on reproduit ces deux signatures pour que mypy sache lequel
    # sort selon l'argument, au lieu d'un union qui contaminerait tout appel.
    @overload  # type: ignore[override]
    def __sub__(self, other: timedelta) -> UtcDateTime: ...
    @overload
    def __sub__(self, other: datetime) -> timedelta: ...
    def __sub__(self, other: timedelta | datetime) -> UtcDateTime | timedelta:
        if isinstance(other, timedelta):
            return self._from_naive_utc((self._as_plain() - other).replace(tzinfo=None))
        if isinstance(other, datetime):
            return self._as_plain() - UtcDateTime.of(other)._as_plain()
        return NotImplemented

    def __rsub__(self, other: datetime) -> timedelta:
        if isinstance(other, datetime):
            return UtcDateTime.of(other)._as_plain() - self._as_plain()
        return NotImplemented

    def astimezone(self, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        """Sortie DÉLIBÉRÉE du domaine UTC : rend un `datetime` nu et honnête.

        Ne surtout pas rendre un `UtcDateTime` ici. L'implémentation C de
        `datetime.astimezone` reconstruit via `__new__`, qui renormalise en UTC :
        on obtiendrait un objet aux champs justes (10:00) mais étiqueté UTC —
        faux de deux heures dès la première soustraction, et d'autant plus
        piégeux qu'il s'affiche correctement.

        Convertir vers un fuseau local, c'est quitter l'invariant. On le fait
        explicitement, et uniquement à la couche de présentation.
        """
        return self._as_plain().astimezone(tz)


def utcnow() -> UtcDateTime:
    return UtcDateTime.now()


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: UtcDateTime
    end: UtcDateTime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", UtcDateTime.of(self.start, "start"))
        object.__setattr__(self, "end", UtcDateTime.of(self.end, "end"))
        if self.end < self.start:
            raise ValueError("end antérieur à start")

    def overlaps(self, other: TimeWindow) -> bool:
        """Chevauchement, bornes incluses.

        Second prédicat du filtrage : une donnée nous concerne si sa validité
        chevauche notre fenêtre de vol. Inclusif à dessein — un NOTAM prenant
        effet à l'heure exacte du décollage nous concerne.
        """
        return self.start <= other.end and other.start <= self.end

    def contains(self, instant: datetime) -> bool:
        return self.start <= UtcDateTime.of(instant, "instant") <= self.end

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0
