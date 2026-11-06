"""Capture d'écran brute (adb screencap) → PNG réduit, en Python pur (aucune dépendance).

La réduction est faite ici, à une taille connue, pour que les coordonnées que l'IA lit sur
l'image se convertissent exactement en pixels de l'écran.
"""

from __future__ import annotations

import operator
import struct
import zlib
from dataclasses import dataclass

from .adbtools import PhoneError

# android.graphics.PixelFormat → (octets par pixel, positions de R, G, B)
PIXEL_FORMATS = {
    1: (4, (0, 1, 2)),  # RGBA_8888
    2: (4, (0, 1, 2)),  # RGBX_8888
    3: (3, (0, 1, 2)),  # RGB_888
    5: (4, (2, 1, 0)),  # BGRA_8888
}


@dataclass
class RawFrame:
    width: int
    height: int
    bpp: int
    channels: tuple[int, int, int]
    data: bytes
    offset: int


def parse_screencap(data: bytes) -> RawFrame:
    if len(data) < 12:
        raise PhoneError("Capture d'écran vide (téléphone en veille profonde ou déconnecté ?).")
    width, height, fmt = struct.unpack_from("<III", data, 0)
    if fmt not in PIXEL_FORMATS:
        raise PhoneError(f"Format de capture non géré ({fmt}).")
    bpp, channels = PIXEL_FORMATS[fmt]
    header = len(data) - width * height * bpp
    # En-tête : largeur, hauteur, format (+ espace colorimétrique depuis Android 9).
    if header not in (12, 16):
        raise PhoneError("Capture d'écran tronquée ou de format inattendu.")
    return RawFrame(width, height, bpp, channels, data, header)


def scale_for(width: int, height: int, max_side: int) -> float:
    """Facteur unique (mêmes proportions sur les deux axes) entre l'écran et l'image envoyée."""
    return min(1.0, max_side / max(width, height))


def fit(width: int, height: int, max_side: int) -> tuple[int, int]:
    scale = scale_for(width, height, max_side)
    return max(1, round(width * scale)), max(1, round(height * scale))


def up_filter(row: bytes, prior: bytes) -> bytes:
    """Filtre PNG « Up » (octet − octet du dessus, modulo 256) calculé d'un bloc, sans boucle Python.

    Chaque octet est placé dans un couloir de 16 bits d'un grand entier : a + 256 − b tient entre
    1 et 511, donc aucune retenue ne déborde d'un couloir ; l'octet bas de chaque couloir est le résultat.
    """
    n = len(row)
    a, b = bytearray(2 * n), bytearray(2 * n)
    a[0::2], b[0::2] = row, prior
    lanes = (int.from_bytes(a, "little") + int.from_bytes(b"\x00\x01" * n, "little")
             - int.from_bytes(b, "little"))
    return lanes.to_bytes(2 * n + 1, "little")[0:2 * n:2]


def to_png(frame: RawFrame, out_w: int, out_h: int, color_bits: int = 8) -> bytes:
    """Réduction au plus proche voisin, puis PNG RGB (filtre « Up »).

    color_bits < 8 efface les bits faibles de chaque couleur : sur une capture réelle avec fond
    d'écran photo, 5 bits font passer le PNG de 923 à 325 Ko sans gêner la lecture du texte.
    """
    stride = frame.width * frame.bpp
    r, g, b = frame.channels
    mask = (0xFF << (8 - min(max(color_bits, 1), 8))) & 0xFF
    table = bytes(v & mask for v in range(256))
    indexes = []
    for i in range(out_w):
        base = min(frame.width - 1, int((i + 0.5) * frame.width / out_w)) * frame.bpp
        indexes.extend((base + r, base + g, base + b))
    pick = operator.itemgetter(*indexes)
    raw = bytearray()
    prior = bytes(out_w * 3)
    for j in range(out_h):
        sy = min(frame.height - 1, int((j + 0.5) * frame.height / out_h))
        start = frame.offset + sy * stride
        row = bytes(pick(frame.data[start:start + stride])).translate(table)
        raw.append(2)
        raw += up_filter(row, prior)
        prior = row
    return encode_png(out_w, out_h, bytes(raw))


def encode_png(width: int, height: int, filtered_rows: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(filtered_rows, 6)) + chunk(b"IEND", b""))


def looks_black(frame: RawFrame, grid: int = 16) -> bool:
    """Image entièrement noire : app protégée contre les captures (FLAG_SECURE) ou écran éteint."""
    stride = frame.width * frame.bpp
    for j in range(grid):
        y = min(frame.height - 1, (2 * j + 1) * frame.height // (2 * grid))
        for i in range(grid):
            x = min(frame.width - 1, (2 * i + 1) * frame.width // (2 * grid))
            p = frame.offset + y * stride + x * frame.bpp
            if max(frame.data[p + c] for c in frame.channels) > 8:
                return False
    return True
