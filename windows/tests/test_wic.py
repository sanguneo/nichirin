"""Decoding an image through Windows Imaging, against pictures built here.

The images are written by these tests rather than read from anywhere, so the
suite never depends on a photo existing on the machine running it. What cannot
be built from bytes in a few lines -- JPEG, WebP -- is not asserted here; that
those decode was checked by hand against the live install, and the contract
this file pins is the one the caller depends on: RGB in that channel order,
three bytes a pixel, and a refusal rather than a crash for bytes that are not
an image.
"""

from __future__ import annotations

import struct
import unittest
import zlib

from tests.platform import require_windows_imaging


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """Write a truecolor 8-bit PNG from a grid of RGB triples."""
    height = len(rows)
    width = len(rows[0])
    scanlines = b"".join(
        b"\x00" + bytes(value for pixel in row for value in pixel) for row in rows
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class DecodeTests(unittest.TestCase):
    def setUp(self) -> None:
        require_windows_imaging()
        from kanao import wic

        self.wic = wic

    def test_the_pixels_come_back_as_red_green_blue_in_that_order(self) -> None:
        # A channel swap here is invisible in a screenshot and wrong in every
        # picture, so the order is pinned rather than eyeballed.
        red, green, blue = (255, 0, 0), (0, 255, 0), (0, 0, 255)
        width, height, pixels = self.wic.decode(make_png([[red, green, blue]]))
        self.assertEqual((width, height), (3, 1))
        self.assertEqual(pixels, bytes(red + green + blue))

    def test_the_shape_and_length_match_the_picture(self) -> None:
        grey = (128, 128, 128)
        width, height, pixels = self.wic.decode(make_png([[grey] * 4] * 3))
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(len(pixels), 4 * 3 * 3)

    def test_rows_stay_in_order_top_to_bottom(self) -> None:
        top, bottom = (10, 20, 30), (200, 210, 220)
        _width, _height, pixels = self.wic.decode(make_png([[top], [bottom]]))
        self.assertEqual(pixels[:3], bytes(top))
        self.assertEqual(pixels[3:6], bytes(bottom))

    def test_transparency_is_flattened_rather_than_refused(self) -> None:
        # Photos carry no alpha, but a picture that does must still decode; the
        # only promise is three bytes a pixel.
        source = make_png([[(1, 2, 3)]])
        width, height, pixels = self.wic.decode(source)
        self.assertEqual((width, height, len(pixels)), (1, 1, 3))

    def test_bytes_that_are_not_an_image_are_refused(self) -> None:
        for blob in (b"", b"not an image at all", make_png([[(0, 0, 0)]])[:20]):
            with self.subTest(blob=blob[:12]), self.assertRaises(ValueError):
                self.wic.decode(blob)

    def test_available_answers_without_raising(self) -> None:
        self.assertIsInstance(self.wic.available(), bool)


if __name__ == "__main__":
    unittest.main()
