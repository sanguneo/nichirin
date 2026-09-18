"""Choosing a protocol, sizing a picture, and the escapes that draw it.

No network and no real photo: the addresses are made up, the fetch is driven
through a stand-in opener, and the pictures are written from bytes here.
"""

from __future__ import annotations

import base64
import struct
import unittest
import zlib

from kanao.tui import images
from tests.platform import require_windows_imaging


def make_png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    height, width = len(rows), len(rows[0])
    scanlines = b"".join(
        b"\x00" + bytes(value for pixel in row for value in pixel) for row in rows
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class ProtocolDetectionTests(unittest.TestCase):
    def blank(self, **overrides: object) -> str:
        arguments: dict = dict(
            term_program="", term="", override="", lc_terminal="", wt_session=""
        )
        arguments.update(overrides)
        return images.detect(**arguments)

    def test_windows_terminal_is_recognised_without_being_asked(self) -> None:
        # It sets this itself and draws sixel from 1.22, so on the platform this
        # tool runs on the answer is already in the environment.
        self.assertEqual(self.blank(wt_session="abc-123"), images.SIXEL)

    def test_the_kitty_family_still_wins_where_it_is_drawn(self) -> None:
        for program in ("kitty", "ghostty", "WezTerm"):
            with self.subTest(program=program):
                self.assertEqual(self.blank(term_program=program), images.KITTY)

    def test_iterm2_is_recognised_by_either_name(self) -> None:
        self.assertEqual(self.blank(term_program="iTerm.app"), images.ITERM2)
        self.assertEqual(self.blank(lc_terminal="iTerm2"), images.ITERM2)

    def test_a_terminal_that_says_nothing_draws_nothing(self) -> None:
        self.assertEqual(self.blank(term="xterm-256color"), images.NONE)

    def test_asking_is_what_finds_sixel_over_ssh(self) -> None:
        # ssh forwards TERM and nothing else, so WT_SESSION cannot reach us.
        self.assertEqual(self.blank(query_sixel=lambda: True), images.SIXEL)
        self.assertEqual(self.blank(query_sixel=lambda: False), images.NONE)

    def test_the_override_wins_over_everything(self) -> None:
        for value, expected in (
            ("kitty", images.KITTY),
            ("sixel", images.SIXEL),
            ("none", images.NONE),
            ("off", images.NONE),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    images.detect(override=value, wt_session="abc", term_program="kitty"),
                    expected,
                )

    def test_an_explicit_off_is_not_overturned_by_a_query(self) -> None:
        self.assertEqual(
            images.detect(override="none", query_sixel=lambda: True), images.NONE
        )


class DrawableTests(unittest.TestCase):
    def test_sixel_takes_the_photo_in_whatever_format_it_arrived(self) -> None:
        # Windows decodes with its own codecs, so nothing has to become a PNG.
        for blob in (b"\xff\xd8\xff\xdb jpeg", b"RIFF....WEBPVP8 ", make_png([[(1, 2, 3)]])):
            with self.subTest(blob=blob[:4]):
                self.assertEqual(images.drawable(blob, images.SIXEL), blob)

    def test_the_file_protocols_take_only_png(self) -> None:
        png = make_png([[(1, 2, 3)]])
        for protocol in (images.KITTY, images.ITERM2):
            with self.subTest(protocol=protocol):
                self.assertEqual(images.drawable(png, protocol), png)
                self.assertIsNone(images.drawable(b"\xff\xd8\xff\xdb jpeg", protocol))

    def test_nothing_is_not_drawable(self) -> None:
        self.assertIsNone(images.drawable(b"", images.SIXEL))


class AddressTests(unittest.TestCase):
    def test_the_full_size_address_is_preferred(self) -> None:
        chosen = images.photo_url(
            {"url": "https://talk.kakaocdn.net/a", "thumbnailUrl": "https://talk.kakaocdn.net/b"}
        )
        self.assertEqual(chosen, "https://talk.kakaocdn.net/a")

    def test_an_album_falls_back_to_its_list(self) -> None:
        chosen = images.photo_url({"imageUrls": ["https://talk.kakaocdn.net/first"]})
        self.assertEqual(chosen, "https://talk.kakaocdn.net/first")

    def test_a_row_with_no_address_has_no_photo(self) -> None:
        self.assertIsNone(images.photo_url({"w": 10, "h": 10}))


class InlineEscapeTests(unittest.TestCase):
    def test_iterm2_carries_the_file_sized_in_cells(self) -> None:
        png = make_png([[(9, 9, 9)]])
        escape = images.inline_escape(images.ITERM2, png, 4, 2)
        assert escape is not None
        self.assertIn("\x1b]1337;File=inline=1;", escape)
        self.assertIn("width=4;height=2", escape)
        self.assertIn(base64.standard_b64encode(png).decode("ascii"), escape)

    def test_iterm2_declines_what_it_cannot_crop(self) -> None:
        self.assertIsNone(
            images.inline_escape(images.ITERM2, make_png([[(0, 0, 0)]]), 4, 2, (1, 1))
        )

    def test_sixel_draws_through_the_systems_own_decoder(self) -> None:
        require_windows_imaging()
        picture = make_png([[(200, 30, 30)] * 4] * 4)
        escape = images.inline_escape(images.SIXEL, picture, 2, 1, None, (8, 16))
        assert escape is not None
        self.assertTrue(escape.startswith("\x1bP"))
        self.assertTrue(escape.endswith("\x1b\\"))
        self.assertIn('"1;1;16;16', escape)

    def test_sixel_assumes_a_cell_the_terminal_would_not_report(self) -> None:
        require_windows_imaging()
        escape = images.inline_escape(
            images.SIXEL, make_png([[(5, 5, 5)] * 2] * 2), 2, 1, None, None
        )
        assert escape is not None
        width = 2 * images.DEFAULT_CELL[0]
        height = 1 * images.DEFAULT_CELL[1]
        self.assertIn(f'"1;1;{width};{height}', escape)

    def test_sixel_refuses_bytes_that_are_not_a_picture(self) -> None:
        require_windows_imaging()
        self.assertIsNone(images.inline_escape(images.SIXEL, b"nope", 2, 1, None, (8, 16)))

    def test_the_overlay_protocols_have_no_inline_escape(self) -> None:
        for protocol in (images.KITTY, images.NONE):
            with self.subTest(protocol=protocol):
                self.assertIsNone(
                    images.inline_escape(protocol, make_png([[(0, 0, 0)]]), 2, 1)
                )


class LetterboxTests(unittest.TestCase):
    def test_letterboxing_refuses_rather_than_skewing(self) -> None:
        # The macOS side pads through sips; here the sixel path resamples to the
        # box itself, so this is a named refusal rather than a silent skew.
        self.assertIsNone(images.letterbox(make_png([[(0, 0, 0)]]), (10, 10)))


if __name__ == "__main__":
    unittest.main()
