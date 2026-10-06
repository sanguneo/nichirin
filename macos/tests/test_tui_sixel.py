"""Sixel encoding and PNG decoding, checked against an independent parser.

Every picture here is built in the test from bytes, so nothing real is read.
"""

from __future__ import annotations

import struct
import unittest
import zlib

from kakaocommon.sixel import encode_sixel, png_to_rgb


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_png(
    width: int,
    height: int,
    color_type: int,
    scanlines: bytes,
    *,
    palette: bytes | None = None,
    transparency: bytes | None = None,
    interlace: int = 0,
    depth: int = 8,
) -> bytes:
    """Write a minimal PNG, so the decoder is tested against real containers."""
    header = struct.pack(
        ">IIBBBBB", width, height, depth, color_type, 0, 0, interlace
    )
    result = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    result += chunk(b"tEXt", b"test\x00synthetic")
    if palette is not None:
        result += chunk(b"PLTE", palette)
    if transparency is not None:
        result += chunk(b"tRNS", transparency)
    compressed = zlib.compress(scanlines)
    split = len(compressed) // 2
    return (
        result
        + chunk(b"IDAT", compressed[:split])
        + chunk(b"IDAT", compressed[split:])
        + chunk(b"IEND", b"")
    )


def read_sixel(sequence: str):
    """Parse the emitted sixel back into pixels, independently of the encoder."""
    assert sequence.startswith("\x1bPq") and sequence.endswith("\x1b\\")
    text = sequence[3:-2]
    pos = 0
    registers: dict[int, tuple[int, ...]] = {}
    pixels: dict[tuple[int, int], tuple[int, ...]] = {}
    x = y = selected = 0
    width = height = None

    def number() -> int:
        nonlocal pos
        start = pos
        while pos < len(text) and text[pos].isdigit():
            pos += 1
        assert pos > start
        return int(text[start:pos])

    def paint(char: str, count: int) -> None:
        nonlocal x
        assert 63 <= ord(char) <= 126
        bits = ord(char) - 63
        for column in range(x, x + count):
            for bit in range(6):
                if bits & (1 << bit):
                    assert (column, y + bit) not in pixels
                    pixels[column, y + bit] = registers[selected]
        x += count

    while pos < len(text):
        char = text[pos]
        pos += 1
        if char == '"':
            values: list[int] = [number()]
            while pos < len(text) and text[pos] == ";":
                pos += 1
                values.append(number())
            assert values[:2] == [1, 1] and len(values) == 4
            width, height = values[2:]
        elif char == "#":
            selected = number()
            if pos < len(text) and text[pos] == ";":
                values = []
                for _ in range(4):
                    assert text[pos] == ";"
                    pos += 1
                    values.append(number())
                assert values[0] == 2 and selected not in registers
                assert all(0 <= v <= 100 for v in values[1:])
                registers[selected] = tuple(values[1:])
        elif char == "$":
            x = 0
        elif char == "-":
            x = 0
            y += 6
        elif char == "!":
            count = number()
            assert count >= 4
            paint(text[pos], count)
            pos += 1
        else:
            paint(char, 1)
    assert width is not None and height is not None
    assert len(pixels) == width * height
    raster = [pixels[x, y] for y in range(height) for x in range(width)]
    return width, height, registers, raster


class PngDecodingTests(unittest.TestCase):
    def test_every_scanline_filter_is_reversed(self) -> None:
        first = bytes((10, 20, 30, 40, 50, 60))
        second = bytes((15, 18, 40, 45, 55, 65))
        filtered = (
            second,
            bytes((15, 18, 40, 30, 37, 25)),
            bytes((5, 254, 10, 5, 5, 5)),
            bytes((10, 8, 25, 18, 21, 15)),
            bytes((5, 254, 10, 5, 5, 5)),
        )
        for filter_type, encoded in enumerate(filtered):
            with self.subTest(filter_type=filter_type):
                blob = make_png(
                    2, 2, 2, b"\x00" + first + bytes((filter_type,)) + encoded
                )
                self.assertEqual(png_to_rgb(blob), (2, 2, first + second))

    def test_paeth_picks_the_nearest_neighbour(self) -> None:
        blob = make_png(3, 2, 0, bytes((0, 100, 0, 10, 4, 60, 166, 10)))
        self.assertEqual(
            png_to_rgb(blob),
            (
                3,
                2,
                bytes(
                    (
                        100, 100, 100, 0, 0, 0, 10, 10, 10,
                        160, 160, 160, 10, 10, 10, 20, 20, 20,
                    )
                ),
            ),
        )

    def test_palette_transparency_composites_over_white(self) -> None:
        blob = make_png(
            3,
            1,
            3,
            b"\x00\x00\x01\x02",
            palette=bytes((255, 0, 0, 0, 0, 255, 0, 255, 0)),
            transparency=bytes((255, 128)),
        )
        self.assertEqual(
            png_to_rgb(blob), (3, 1, bytes((255, 0, 0, 127, 127, 255, 0, 255, 0)))
        )

    def test_rgba_composites_over_white(self) -> None:
        blob = make_png(
            3, 1, 6, bytes((0, 10, 20, 30, 0, 255, 0, 0, 128, 3, 4, 5, 255))
        )
        self.assertEqual(
            png_to_rgb(blob), (3, 1, bytes((255, 255, 255, 255, 127, 127, 3, 4, 5)))
        )

    def test_grayscale_alpha(self) -> None:
        blob = make_png(2, 1, 4, bytes((0, 0, 128, 80, 255)))
        self.assertEqual(png_to_rgb(blob), (2, 1, bytes((127,) * 3 + (80,) * 3)))

    def test_a_transparent_sample_becomes_the_background(self) -> None:
        gray = make_png(2, 1, 0, bytes((0, 12, 13)), transparency=struct.pack(">H", 12))
        self.assertEqual(png_to_rgb(gray)[2], bytes((255,) * 3 + (13,) * 3))
        rgb = make_png(
            2,
            1,
            2,
            bytes((0, 1, 2, 3, 4, 5, 6)),
            transparency=struct.pack(">HHH", 1, 2, 3),
        )
        self.assertEqual(png_to_rgb(rgb)[2], bytes((255, 255, 255, 4, 5, 6)))

    def test_what_it_cannot_read_is_refused(self) -> None:
        valid = make_png(1, 1, 2, bytes((0, 1, 2, 3)))
        invalid = [
            b"not PNG",
            valid[:-1],
            make_png(1, 1, 2, b"\x00\x00\x00\x00", interlace=1),
            make_png(1, 1, 2, b"\x00\x00\x00\x00", depth=16),
            make_png(1, 1, 2, b"\x05\x00\x00\x00"),
            make_png(1, 1, 2, b"\x00\x00"),
            make_png(1, 1, 3, b"\x00\x01", palette=b"\x00\x00\x00"),
        ]
        for blob in invalid:
            with self.subTest(blob=blob), self.assertRaises(ValueError):
                png_to_rgb(blob)


class SixelEncodingTests(unittest.TestCase):
    def test_a_solid_picture_round_trips_and_is_run_length_encoded(self) -> None:
        for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)):
            with self.subTest(color=color):
                sequence = encode_sixel(bytes(color) * 16, 4, 4)
                self.assertTrue(sequence.startswith("\x1bP"))
                self.assertTrue(sequence.endswith("\x1b\\"))
                self.assertIn("#0;2;", sequence)
                self.assertIn("!4N", sequence)
                width, height, registers, pixels = read_sixel(sequence)
                self.assertEqual((width, height), (4, 4))
                self.assertEqual(len(registers), 1)
                self.assertEqual(
                    pixels, [tuple(round(v * 100 / 255) for v in color)] * 16
                )

    def test_bands_and_a_short_final_band(self) -> None:
        colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255))
        for height in (1, 5, 6, 7, 12, 13):
            with self.subTest(height=height):
                original = [
                    colors[(x + y) % 3] for y in range(height) for x in range(5)
                ]
                rgb = bytes(v for color in original for v in color)
                sequence = encode_sixel(rgb, 5, height)
                width, got_height, registers, pixels = read_sixel(sequence)
                self.assertEqual((width, got_height), (5, height))
                self.assertEqual(len(registers), 3)
                self.assertEqual(
                    pixels, [tuple(v * 100 // 255 for v in c) for c in original]
                )
                self.assertEqual(sequence.count("-"), (height - 1) // 6)
                self.assertIn("$", sequence)
                self.assertNotEqual(sequence[-3], "-")

    def test_the_palette_is_capped_and_the_output_is_deterministic(self) -> None:
        rgb = bytes(
            v for i in range(300) for v in (i % 256, i // 256, (i * 37) % 256)
        )
        for limit in (1, 7, 16, 256):
            with self.subTest(limit=limit):
                sequence = encode_sixel(rgb, 30, 10, max_colors=limit)
                self.assertEqual(sequence, encode_sixel(rgb, 30, 10, max_colors=limit))
                width, height, registers, pixels = read_sixel(sequence)
                self.assertEqual((width, height, len(pixels)), (30, 10, 300))
                self.assertLessEqual(len(registers), limit)

    def test_one_colour_is_the_frequency_weighted_average(self) -> None:
        sequence = encode_sixel(
            bytes((0, 0, 0)) * 3 + bytes((255, 255, 255)), 4, 1, max_colors=1
        )
        self.assertEqual(read_sixel(sequence)[3], [(25, 25, 25)] * 4)

    def test_impossible_dimensions_are_refused(self) -> None:
        for rgb, width, height, limit in (
            (b"", 0, 1, 256),
            (b"", 1, -1, 256),
            (b"", 1, 1, 256),
            (b"\x00" * 3, 1, 1, 0),
        ):
            with self.subTest(width=width, height=height, limit=limit):
                with self.assertRaises(ValueError):
                    encode_sixel(rgb, width, height, max_colors=limit)


if __name__ == "__main__":
    unittest.main()
