"""Sondage AROME au droit des terrains : mesuré, jamais apprécié à l'œil.

Les images sont FABRIQUÉES ici, pixel par pixel, avec une emprise connue : on
sait donc exactement quelle couleur doit sortir à quelle coordonnée. C'est la
seule façon de tester un sondage — le comparer à une carte réelle reviendrait à
la regarder, c'est-à-dire à faire ce que ce module existe pour éviter.
"""

from __future__ import annotations

import math
import struct
import zlib

from aerobriefer.render.arome_probe import probe_chart

# Emprise EPSG:3857 d'un carré autour de la Charente-Maritime, choisie pour que
# les terrains du test tombent dedans avec de la marge.
_BBOX = (-302467.9, 5575293.9, 85951.5, 5866608.4)
_W = _H = 64

_LEGEND = {
    "classes": [("#fefe00", "réduction modérée"), ("#fea400", "réduction marquée")],
    "ordered": True,
}

LFCY = (45.628101, -0.9725)
LFBH = (46.179167, -1.195278)


def _url(bbox: tuple[float, float, float, float] = _BBOX, crs: str = "EPSG:3857") -> str:
    return (
        "https://example.invalid/wms?SERVICE=WMS&REQUEST=GetMap"
        f"&CRS={crs}&BBOX={','.join(str(v) for v in bbox)}&WIDTH={_W}&HEIGHT={_H}"
    )


def _pixel_of(lat: float, lon: float) -> tuple[int, int]:
    x = math.radians(lon) * 6378137.0
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * 6378137.0
    x0, y0, x1, y1 = _BBOX
    return int((x - x0) / (x1 - x0) * _W), int((y1 - y) / (y1 - y0) * _H)


def _png(paint: dict[tuple[int, int], tuple[int, int, int, int]], filter_type: int = 0) -> bytes:
    """PNG RGBA 8 bits, transparent sauf aux pixels demandés.

    `filter_type` couvre les prédicteurs que le décodeur doit savoir défaire :
    le service n'annonce pas lequel il emploie, et un PNG filtré lu comme un PNG
    brut rend des couleurs plausibles mais fausses — le pire des résultats.
    """
    rows = []
    for y in range(_H):
        row = bytearray()
        for x in range(_W):
            row += bytes(paint.get((x, y), (0, 0, 0, 0)))
        rows.append(bytes(row))

    stride = _W * 4
    raw = bytearray()
    previous = bytes(stride)
    for row in rows:
        raw.append(filter_type)
        if filter_type == 0:
            raw += row
        elif filter_type == 2:  # Up
            raw += bytes((row[i] - previous[i]) & 0xFF for i in range(stride))
        elif filter_type == 1:  # Sub
            raw += bytes((row[i] - (row[i - 4] if i >= 4 else 0)) & 0xFF for i in range(stride))
        else:
            raise AssertionError(filter_type)
        previous = row

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", _W, _H, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def test_paints_the_right_aerodrome_and_only_it() -> None:
    """La classe est lue AU DROIT du terrain, pas « quelque part par là »."""
    content = _png({_pixel_of(*LFBH): (0xFE, 0xA4, 0x00, 255)})
    probes = probe_chart(content, _url(), _LEGEND, [("LFCY", *LFCY), ("LFBH", *LFBH)])

    assert probes["LFBH"].painted
    assert probes["LFBH"].class_label == "réduction marquée"
    assert probes["LFBH"].class_index == 1
    assert probes["LFBH"].colour == "#fea400"
    # Royan est à 34 NM : il ne doit RIEN attraper de ce qui touche La Rochelle.
    assert probes["LFCY"].painted is False
    assert probes["LFCY"].class_label is None


def test_unpainted_is_not_reported_as_a_class() -> None:
    """Pixel transparent = la couche ne peint pas. Ce n'est pas une classe."""
    probes = probe_chart(_png({}), _url(), _LEGEND, [("LFCY", *LFCY)])
    assert probes["LFCY"] == probe_chart(_png({}), _url(), _LEGEND, [("LFCY", *LFCY)])["LFCY"]
    assert probes["LFCY"].painted is False
    assert probes["LFCY"].colour is None


def test_colour_outside_the_legend_is_never_guessed() -> None:
    """Une teinte inconnue reste inconnue.

    Les classes sont RELEVÉES sur les images, pas publiées par Météo-France :
    rattacher une teinte inattendue à la classe la plus proche ferait dire au
    dossier quelque chose que la source ne dit pas.
    """
    probes = probe_chart(
        _png({_pixel_of(*LFCY): (0x11, 0x22, 0x33, 255)}), _url(), _LEGEND, [("LFCY", *LFCY)]
    )
    assert probes["LFCY"].painted is True
    assert probes["LFCY"].class_label is None
    assert probes["LFCY"].class_index is None
    assert probes["LFCY"].colour == "#112233"  # la donnée brute reste montrable


def test_every_png_filter_is_undone() -> None:
    """Un PNG filtré lu comme un PNG brut rendrait des couleurs plausibles et fausses."""
    for filter_type in (0, 1, 2):
        content = _png({_pixel_of(*LFBH): (0xFE, 0xFE, 0x00, 255)}, filter_type=filter_type)
        probes = probe_chart(content, _url(), _LEGEND, [("LFBH", *LFBH)])
        assert probes["LFBH"].colour == "#fefe00", f"filtre {filter_type}"
        assert probes["LFBH"].class_label == "réduction modérée"


def test_site_outside_the_image_is_absent_not_clear() -> None:
    """Hors emprise → absent du résultat. Le dire « clair » serait un mensonge."""
    hors_zone = ("LFPG", 49.0128, 2.55)
    probes = probe_chart(_png({}), _url(), _LEGEND, [("LFCY", *LFCY), hors_zone])
    assert "LFCY" in probes
    assert "LFPG" not in probes


def test_nothing_is_probed_without_a_legend_or_a_projected_bbox() -> None:
    content = _png({_pixel_of(*LFCY): (0xFE, 0xFE, 0x00, 255)})
    sites = [("LFCY", *LFCY)]
    assert probe_chart(content, _url(), None, sites) == {}
    assert probe_chart(content, _url(crs="EPSG:4326"), _LEGEND, sites) == {}
    assert probe_chart(None, _url(), _LEGEND, sites) == {}
    assert probe_chart(b"pas un png", _url(), _LEGEND, sites) == {}


def test_truncated_image_yields_nothing_rather_than_half_a_table() -> None:
    """Un décodage partiel ne doit pas passer pour « ces terrains sont clairs »."""
    content = _png({_pixel_of(*LFBH): (0xFE, 0xFE, 0x00, 255)})
    # On coupe le flux compressé : l'IDAT ne peut plus produire toutes les lignes.
    cut = content[: len(content) // 2]
    assert probe_chart(cut, _url(), _LEGEND, [("LFCY", *LFCY), ("LFBH", *LFBH)]) == {}
