"""Sondage des couches AROME AU DROIT des terrains.

Regarder une carte AROME et décider « est-ce que ça touche Royan ? » est un
exercice d'appréciation visuelle : on estime une position à l'œil, sur un aplat
sans contour, à côté d'un fond de carte. C'est précisément le genre de jugement
qu'un dossier de vol ne doit pas demander.

Ce module répond à la même question par la mesure : pour chaque terrain, on
calcule sa position EXACTE dans l'image (l'emprise EPSG:3857 est celle du
GetMap qui l'a produite), on lit la couleur de CE pixel, et on la rapproche des
classes de légende relevées dans `AROME_LEGENDS`. Aucune reconnaissance de
forme, aucune interprétation : une projection et une comparaison de couleurs.

Les limites sont dites, pas contournées :

- **Pixel transparent = la couche NE PEINT PAS cette maille.** Cela signifie
  « sous le seuil que la couche représente », pas « rien à signaler » et surtout
  pas « garanti clair ». Le sondage rend `painted=False`, et le rendu l'écrit
  ainsi.
- **Couleur absente de la légende** → `class_label=None` (« hors légende »). On
  ne rattache jamais une teinte inconnue à la classe la plus proche : les
  classes sont relevées, pas publiées, et deviner ferait dire au dossier quelque
  chose que la source ne dit pas.
- **Aucune valeur en km ni en pieds.** Météo-France ne publie pas l'échelle de
  ces couches (cf. `AROME_LEGENDS`). Le sondage rend un NOM de classe, jamais un
  seuil chiffré.
- Le résultat est un **point de grille de modèle**, pas une observation.
"""

from __future__ import annotations

import math
import struct
import urllib.parse
import zlib
from dataclasses import dataclass
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Seuil d'opacité au-delà duquel on considère que la couche a PEINT le pixel.
#: Les bords d'aplat sont anti-aliasés ; en deçà, la couleur n'est pas une
#: classe mais un mélange avec le fond, et on ne la lit pas.
_OPAQUE_MIN = 200

#: Tolérance d'appariement à une classe de légende, en distance de Manhattan sur
#: (R,G,B). Elle absorbe la recompression JPEG/PNG du service, RIEN de plus :
#: deux classes voisines de la même légende sont bien plus éloignées que ça.
_COLOUR_TOLERANCE = 12


@dataclass(frozen=True)
class Probe:
    """Ce que la couche dit d'un terrain à une échéance."""

    painted: bool
    """La couche peint-elle cette maille ? Faux = sous le seuil représenté."""
    class_label: str | None = None
    """Nom de la classe de légende, ou `None` si la teinte n'y figure pas."""
    class_index: int | None = None
    """Rang de la classe dans la légende (0 = la moins marquée), si ordonnée."""
    colour: str | None = None
    """La teinte lue, en #rrggbb — la donnée brute, toujours montrable."""


def _bbox_of(url: str) -> tuple[float, float, float, float] | None:
    """Emprise EPSG:3857 relue dans l'URL du GetMap qui a produit l'image.

    On la RELIT plutôt que de la recalculer : l'image a été demandée pour cette
    emprise-là. Un second calcul finirait par diverger, et le sondage
    pointerait à côté sans que rien ne le signale.
    """
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if (params.get("CRS") or params.get("SRS") or [""])[0].upper() != "EPSG:3857":
        return None
    raw = (params.get("BBOX") or [""])[0].split(",")
    if len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except ValueError:
        return None
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _mercator(lat: float, lon: float) -> tuple[float, float]:
    x = math.radians(lon) * 6378137.0
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * 6378137.0
    return x, y


class _Png:
    """Lecteur PNG minimal, stdlib seule (le rendu ne tire aucune dépendance).

    Ne décode QUE les lignes nécessaires : le désentrelacement d'un PNG est
    séquentiel (chaque ligne se reconstruit sur la précédente), mais on peut
    s'arrêter à la dernière ligne utile. Sonder cinq terrains dans le tiers haut
    d'une image n'en décode que le tiers haut.
    """

    def __init__(self, data: bytes, max_row: int) -> None:
        pos = 8
        self.width = self.height = self._depth = self._ctype = 0
        palette: bytes | None = None
        alphas: bytes | None = None
        chunks: list[bytes] = []
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos : pos + 4])[0]
            kind = data[pos + 4 : pos + 8]
            body = data[pos + 8 : pos + 8 + length]
            pos += 12 + length
            if kind == b"IHDR":
                self.width, self.height, self._depth, self._ctype = struct.unpack(
                    ">IIBB", body[:10]
                )
            elif kind == b"PLTE":
                palette = body
            elif kind == b"tRNS":
                alphas = body
            elif kind == b"IDAT":
                chunks.append(body)
            elif kind == b"IEND":
                break
        self._palette = palette
        self._alphas = alphas
        self._channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(self._ctype, 0)
        self._rows: list[bytes] = []
        self._wanted = min(max_row + 1, self.height) if self.height else 0
        if self._depth == 8 and self._channels and self._wanted:
            self._unfilter(b"".join(chunks), self._wanted)

    @property
    def usable(self) -> bool:
        """Toutes les lignes demandées ont-elles été reconstruites ?

        Exigeant à dessein : un décodage PARTIEL rendrait `None` sur les pixels
        manquants, que l'appelant ne saurait pas distinguer d'un terrain hors
        emprise. Le sondage doit échouer franchement, pas discrètement.
        """
        return len(self._rows) >= self._wanted > 0

    def _unfilter(self, compressed: bytes, rows: int) -> None:
        stride = self.width * self._channels
        step = self._channels
        # Décompression BORNÉE : on ne déroule que les lignes demandées. Sonder
        # cinq terrains dans le haut d'une image ne décompresse pas le reste.
        try:
            raw = zlib.decompressobj().decompress(compressed, rows * (stride + 1))
        except zlib.error:
            return
        previous = bytearray(stride)
        offset = 0
        for _ in range(rows):
            if offset + 1 + stride > len(raw):
                return
            method = raw[offset]
            line = bytearray(raw[offset + 1 : offset + 1 + stride])
            offset += 1 + stride
            if method == 1:
                for i in range(step, stride):
                    line[i] = (line[i] + line[i - step]) & 0xFF
            elif method == 2:
                for i in range(stride):
                    line[i] = (line[i] + previous[i]) & 0xFF
            elif method == 3:
                for i in range(stride):
                    left = line[i - step] if i >= step else 0
                    line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
            elif method == 4:
                for i in range(stride):
                    left = line[i - step] if i >= step else 0
                    up = previous[i]
                    upleft = previous[i - step] if i >= step else 0
                    estimate = left + up - upleft
                    da, db, dc = (
                        abs(estimate - left),
                        abs(estimate - up),
                        abs(estimate - upleft),
                    )
                    nearest = left if (da <= db and da <= dc) else (up if db <= dc else upleft)
                    line[i] = (line[i] + nearest) & 0xFF
            self._rows.append(bytes(line))
            previous = line

    def rgba(self, x: int, y: int) -> tuple[int, int, int, int] | None:
        if not (0 <= x < self.width and 0 <= y < len(self._rows)):
            return None
        line = self._rows[y]
        at = x * self._channels
        if self._ctype == 3:
            if self._palette is None:
                return None
            index = line[at]
            red, green, blue = self._palette[index * 3 : index * 3 + 3]
            alpha = self._alphas[index] if self._alphas and index < len(self._alphas) else 255
            return red, green, blue, alpha
        if self._ctype == 6:
            return (line[at], line[at + 1], line[at + 2], line[at + 3])
        if self._ctype == 2:
            return (line[at], line[at + 1], line[at + 2], 255)
        if self._ctype == 4:
            return (line[at], line[at], line[at], line[at + 1])
        return (line[at], line[at], line[at], 255)


def _match_class(
    rgb: tuple[int, int, int], legend: dict[str, Any]
) -> tuple[str | None, int | None]:
    """Teinte lue → (nom de classe, rang), ou (None, None) si hors légende."""
    best: tuple[int, int, str] | None = None
    for index, (colour, label) in enumerate(legend.get("classes") or []):
        reference = (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
        gap = sum(abs(a - b) for a, b in zip(rgb, reference, strict=True))
        if gap <= _COLOUR_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, index, label)
    if best is None:
        return None, None
    # Une classe sans libellé (légende à quatre paliers dont deux muets) est
    # désignée par son rang : mieux vaut « palier 2/4 » qu'une case vide.
    _, index, label = best
    return (label or None), index


def probe_chart(
    content: bytes | None,
    url: str,
    legend: dict[str, Any] | None,
    sites: list[tuple[str, float, float]],
) -> dict[str, Probe]:
    """Sonde une image AROME aux coordonnées données. `sites` : (clé, lat, lon).

    Rend `{clé: Probe}` pour les seuls terrains qui TOMBENT dans l'image — un
    terrain hors emprise est absent du résultat plutôt que rendu « rien à
    signaler », ce qui serait un mensonge par omission.
    """
    if not content or not content.startswith(_PNG_SIGNATURE) or legend is None:
        return {}
    bbox = _bbox_of(url)
    if bbox is None:
        return {}
    x0, y0, x1, y1 = bbox

    header = _Png(content, max_row=0)
    if not header.width or not header.height:
        return {}

    placed: list[tuple[str, int, int]] = []
    for key, lat, lon in sites:
        mx, my = _mercator(lat, lon)
        px = int((mx - x0) / (x1 - x0) * header.width)
        py = int((y1 - my) / (y1 - y0) * header.height)
        if 0 <= px < header.width and 0 <= py < header.height:
            placed.append((key, px, py))
    if not placed:
        return {}

    image = _Png(content, max_row=max(py for _, _, py in placed))
    if not image.usable:
        # Décodage incomplet : on ne rend RIEN. Rendre les terrains décodés
        # laisserait les autres passer pour « hors emprise », ce qu'ils ne sont
        # pas — une table incomplète qui a l'air complète est pire qu'aucune.
        return {}

    result: dict[str, Probe] = {}
    for key, px, py in placed:
        pixel = image.rgba(px, py)
        if pixel is None:
            continue
        red, green, blue, alpha = pixel
        if alpha < _OPAQUE_MIN:
            result[key] = Probe(painted=False)
            continue
        label, index = _match_class((red, green, blue), legend)
        result[key] = Probe(
            painted=True,
            class_label=label,
            class_index=index,
            colour=f"#{red:02x}{green:02x}{blue:02x}",
        )
    return result


__all__ = ["Probe", "probe_chart"]
