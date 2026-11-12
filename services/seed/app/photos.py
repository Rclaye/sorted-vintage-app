"""Placeholder listing photos, drawn with the standard library (decided 2026-11-12: no image
files in Git, no new dependency). Each photo is a PNG of colour bands, the same for the same
number, so a re-run uploads identical bytes."""

import struct
import zlib

PALETTE = [
    (0x8C, 0x5A, 0x3C),  # tan
    (0x2F, 0x4F, 0x6F),  # denim
    (0xC8, 0xB8, 0x9A),  # oatmeal
    (0x6B, 0x2E, 0x3A),  # burgundy
    (0x3E, 0x5C, 0x3A),  # olive
    (0xD9, 0xA4, 0x41),  # mustard
    (0x1F, 0x1F, 0x24),  # black
    (0xE8, 0xE2, 0xD6),  # cream
]


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(number: int, width: int = 480, height: int = 600) -> bytes:
    """A width x height RGB PNG: diagonal bands in two colours picked by `number`."""
    a = PALETTE[number % len(PALETTE)]
    b = PALETTE[(number * 3 + 1) % len(PALETTE)]
    if a == b:
        b = PALETTE[(number + 1) % len(PALETTE)]
    band = 24 + (number % 5) * 8
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")  # filter type: none
        for x in range(width):
            row += bytes(a if ((x + y) // band) % 2 == 0 else b)
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _chunk(b"IEND", b"")
    )
