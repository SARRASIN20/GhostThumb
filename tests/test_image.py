"""Capture brute → PNG réduit : pixels choisis, en-têtes 12/16 octets, ordre BGRA, PNG valide."""

import random
import struct
import unittest
import zlib

from ghostthumb.adbtools import PhoneError
from ghostthumb.image import fit, looks_black, parse_screencap, to_png, up_filter


def raw_frame(width, height, fmt=1, header=16, pixel=lambda x, y: (x * 10 % 256, y * 20 % 256, 7)):
    head = struct.pack("<III", width, height, fmt) + (b"\x00" * 4 if header == 16 else b"")
    body = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b = pixel(x, y)
            body += bytes((b, g, r, 255) if fmt == 5 else (r, g, b, 255))
    return head + bytes(body)


def decode_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        tag, payload = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, tag
        chunks[tag] = chunks.get(tag, b"") + payload
        pos += 12 + n
    width, height, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color) == (8, 2)
    raw = zlib.decompress(chunks[b"IDAT"])
    rows, prior = [], bytes(width * 3)
    for y in range(height):
        line = raw[y * (1 + width * 3):(y + 1) * (1 + width * 3)]
        kind, data = line[0], line[1:]
        assert kind in (0, 2), kind
        if kind == 2:  # Up
            data = bytes((d + p) & 0xFF for d, p in zip(data, prior))
        prior = data
        rows.append([tuple(data[3 * x:3 * x + 3]) for x in range(width)])
    return width, height, rows


class ImageTest(unittest.TestCase):
    def test_downscale_picks_centre_pixels(self):
        frame = parse_screencap(raw_frame(8, 4))
        w, h, rows = decode_png(to_png(frame, 4, 2))
        self.assertEqual((w, h), (4, 2))
        self.assertEqual(rows[0], [(10, 20, 7), (30, 20, 7), (50, 20, 7), (70, 20, 7)])
        self.assertEqual(rows[1][0], (10, 60, 7))

    def test_twelve_byte_header_and_bgra(self):
        frame = parse_screencap(raw_frame(4, 2, fmt=5, header=12))
        self.assertEqual(frame.offset, 12)
        _, _, rows = decode_png(to_png(frame, 4, 2))
        self.assertEqual(rows[1][3], (30, 20, 7))

    def test_no_scaling_keeps_every_pixel(self):
        frame = parse_screencap(raw_frame(3, 3))
        _, _, rows = decode_png(to_png(frame, 3, 3))
        self.assertEqual(rows[2], [(0, 40, 7), (10, 40, 7), (20, 40, 7)])

    def test_rejects_truncated_and_unknown_formats(self):
        with self.assertRaises(PhoneError):
            parse_screencap(raw_frame(4, 4)[:-5])
        with self.assertRaises(PhoneError):
            parse_screencap(raw_frame(4, 4, fmt=4))
        with self.assertRaises(PhoneError):
            parse_screencap(b"")

    def test_black_detection(self):
        self.assertTrue(looks_black(parse_screencap(raw_frame(32, 32, pixel=lambda x, y: (0, 0, 0)))))
        self.assertFalse(looks_black(parse_screencap(raw_frame(32, 32))))

    def test_color_bits_clear_low_bits_only(self):
        frame = parse_screencap(raw_frame(2, 1, pixel=lambda x, y: (0xFF, 0x87, 0x07)))
        _, _, rows = decode_png(to_png(frame, 2, 1, color_bits=5))
        self.assertEqual(rows[0][0], (0xF8, 0x80, 0x00))
        _, _, rows = decode_png(to_png(frame, 2, 1))
        self.assertEqual(rows[0][0], (0xFF, 0x87, 0x07))

    def test_up_filter_matches_the_png_definition(self):
        rng = random.Random(7)
        for n in (1, 3, 1716):
            row = bytes(rng.randrange(256) for _ in range(n))
            prior = bytes(rng.randrange(256) for _ in range(n))
            self.assertEqual(up_filter(row, prior), bytes((a - b) & 0xFF for a, b in zip(row, prior)))
        self.assertEqual(up_filter(b"\x00\xff", b"\xff\x00"), b"\x01\xff")

    def test_fit(self):
        self.assertEqual(fit(720, 1612, 1280), (572, 1280))
        self.assertEqual(fit(1080, 2400, 1280), (576, 1280))
        self.assertEqual(fit(600, 800, 1280), (600, 800))


if __name__ == "__main__":
    unittest.main()
