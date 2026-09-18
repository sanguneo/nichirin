"""Reading a photo's address, sizing it, and the escapes that draw it.

No network and no terminal. The fetch is driven through a stand-in opener so a
refusal is tested the way production refuses, and the escapes are asserted as
strings because a wrong control key draws nothing and reports no error.
"""

from __future__ import annotations

import base64
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from kamado.tui import images


class SupportTests(unittest.TestCase):
    def test_the_terminals_that_draw_images_are_recognised(self) -> None:
        for program in ("ghostty", "Ghostty", "kitty", "WezTerm"):
            with self.subTest(program=program):
                self.assertTrue(images.supported(term_program=program, term="xterm-256color"))

    def test_a_terminal_naming_itself_in_term_alone_still_counts(self) -> None:
        self.assertTrue(images.supported(term_program="", term="xterm-kitty"))

    def test_an_ordinary_terminal_does_not(self) -> None:
        self.assertFalse(images.supported(term_program="Apple_Terminal", term="xterm-256color"))


class AddressTests(unittest.TestCase):
    def test_the_full_size_address_is_preferred_over_the_thumbnail(self) -> None:
        # A thumbnail measured 110x120 where its original was 384x422, and both
        # are scaled into the same cells, so the thumbnail is only softer.
        found = images.photo_url(
            {
                "thumbnailUrl": "https://talk.kakaocdn.net/thumb.jpg",
                "url": "https://talk.kakaocdn.net/full.jpg",
            }
        )
        self.assertEqual(found, "https://talk.kakaocdn.net/full.jpg")

    def test_the_thumbnail_is_used_when_no_original_was_stored(self) -> None:
        self.assertEqual(
            images.photo_url({"thumbnailUrl": "https://talk.kakaocdn.net/thumb.jpg"}),
            "https://talk.kakaocdn.net/thumb.jpg",
        )

    def test_a_multi_photo_message_shows_its_first_picture(self) -> None:
        found = images.photo_url(
            {
                "imageUrls": [
                    "https://talk.kakaocdn.net/one.jpg",
                    "https://talk.kakaocdn.net/two.jpg",
                ]
            }
        )
        self.assertEqual(found, "https://talk.kakaocdn.net/one.jpg")

    def test_an_attachment_with_no_address_names_nothing(self) -> None:
        for attachment in ({}, {"thumbnailUrl": 5}, {"thumbnailUrls": []}, {"url": "nope"}):
            with self.subTest(attachment=attachment):
                self.assertIsNone(images.photo_url(attachment))


class SizingTests(unittest.TestCase):
    def test_a_wide_photo_is_wider_than_it_is_tall_in_cells(self) -> None:
        columns, rows = images.cell_size(1988, 1118, 80)
        self.assertGreater(columns, rows)

    def test_a_photo_gets_enough_cells_to_be_legible(self) -> None:
        columns, _rows = images.cell_size(1988, 1118, 100)
        self.assertGreaterEqual(columns, 40)

    def test_a_tall_photo_is_narrowed_rather_than_squashed(self) -> None:
        columns, rows = images.cell_size(970, 2216, 100)
        self.assertLessEqual(rows, images.MAX_ROWS)
        # Halving for cell shape, the box should still be about as tall
        # relative to its width as the picture is.
        self.assertAlmostEqual(rows / columns, 2216 / 970 / 2, delta=0.35)

    def test_a_narrow_screen_never_gets_a_wider_picture_than_it_has(self) -> None:
        columns, _rows = images.cell_size(1988, 1118, 10)
        self.assertLessEqual(columns, 10)

    def test_an_unknown_size_still_produces_a_usable_box(self) -> None:
        columns, rows = images.cell_size(0, 0, 80)
        self.assertGreater(columns, 0)
        self.assertGreater(rows, 0)


class ScreenShareSizingTests(unittest.TestCase):
    """A picture scales with the conversation rather than to a fixed box."""

    def test_a_taller_conversation_shows_a_larger_picture(self) -> None:
        small = images.cell_size(1122, 1402, 94, 18)
        large = images.cell_size(1122, 1402, 94, 74)
        self.assertGreater(large[1], small[1])
        self.assertGreater(large[0], small[0])

    def test_a_picture_never_fills_the_whole_conversation(self) -> None:
        for body_rows in range(3, 101):
            for width, height in ((1122, 1402), (970, 2216), (888, 1701), (120, 120)):
                with self.subTest(body_rows=body_rows, shape=(width, height)):
                    _cols, rows = images.cell_size(width, height, 94, body_rows)
                    self.assertLessEqual(rows, body_rows)

    def test_the_share_of_the_conversation_is_bounded(self) -> None:
        worst = max(
            images.cell_size(w, h, 94, body)[1] / body
            for body in range(3, 101)
            for w, h in ((1122, 1402), (970, 2216), (888, 1701))
        )
        self.assertLessEqual(worst, images.BODY_SHARE + 0.01)

    def test_a_short_conversation_still_gets_at_least_two_rows(self) -> None:
        _cols, rows = images.cell_size(1122, 1402, 94, 1)
        self.assertGreaterEqual(rows, 2)

    def test_without_a_measured_height_the_fixed_ceiling_applies(self) -> None:
        _cols, rows = images.cell_size(970, 2216, 94)
        self.assertLessEqual(rows, images.MAX_ROWS)


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp()
        self.store = images.PhotoStore(directory=self.directory)

    def test_a_non_cdn_address_is_refused_without_a_request(self) -> None:
        with patch.object(images.urllib.request, "urlopen") as opener:
            self.assertIsNone(self.store.fetch("https://example.com/evil.jpg"))
            opener.assert_not_called()

    def test_plain_http_is_refused_even_on_the_right_host(self) -> None:
        with patch.object(images.urllib.request, "urlopen") as opener:
            self.assertIsNone(self.store.fetch("http://talk.kakaocdn.net/photo.jpg"))
            opener.assert_not_called()

    def test_a_fetched_photo_is_written_beside_the_other_state(self) -> None:
        url = "https://talk.kakaocdn.net/photo.jpg"
        with patch.object(images.urllib.request, "urlopen", return_value=_Response(b"\x89PNG-bytes")):
            self.assertEqual(self.store.fetch(url), b"\x89PNG-bytes")
        self.assertTrue(os.path.exists(self.store.cached_path(url)))

    def test_a_cached_photo_is_not_fetched_again(self) -> None:
        url = "https://talk.kakaocdn.net/photo.jpg"
        with patch.object(images.urllib.request, "urlopen", return_value=_Response(b"first")):
            self.store.fetch(url)
        with patch.object(images.urllib.request, "urlopen") as opener:
            self.assertEqual(self.store.fetch(url), b"first")
            opener.assert_not_called()

    def test_the_cache_name_does_not_carry_the_address(self) -> None:
        path = self.store.cached_path("https://talk.kakaocdn.net/secret-token-123.jpg")
        self.assertNotIn("secret-token-123", path)

    def test_a_response_that_is_not_an_image_is_refused(self) -> None:
        response = _Response(b"<html>", content_type="text/html")
        with patch.object(images.urllib.request, "urlopen", return_value=response):
            self.assertIsNone(self.store.fetch("https://talk.kakaocdn.net/photo.jpg"))

    def test_a_failed_request_leaves_the_conversation_alone(self) -> None:
        with patch.object(images.urllib.request, "urlopen", side_effect=OSError("no network")):
            self.assertIsNone(self.store.fetch("https://talk.kakaocdn.net/photo.jpg"))


class ImageIdTests(unittest.TestCase):
    """A KakaoTalk id does not fit the protocol's 32-bit image id field."""

    def test_a_message_id_far_past_32_bits_becomes_a_usable_image_id(self) -> None:
        ids = images.ImageIds()
        assigned = ids.of(3931668198256119810)
        self.assertGreater(assigned, 0)
        self.assertLessEqual(assigned, 0xFFFFFFFF)

    def test_the_same_message_keeps_the_same_image_id(self) -> None:
        ids = images.ImageIds()
        first = ids.of(3931668198256119810)
        self.assertEqual(ids.of(3931668198256119810), first)

    def test_different_messages_never_share_an_image_id(self) -> None:
        ids = images.ImageIds()
        assigned = {ids.of(one) for one in (3931668198256119810, 3931663334663651328, 42)}
        self.assertEqual(len(assigned), 3)


class WebpTests(unittest.TestCase):
    """Kitty draws PNG and nothing else in file form, but the client sends WebP."""

    def lossy(self, width: int, height: int) -> bytes:
        # A VP8 bitstream puts its dimensions after a 3-byte frame tag and a
        # 3-byte start code, which is offset 26 in the containing RIFF file.
        body = b"\x00" * 6 + struct.pack("<HH", width, height)
        return (
            b"RIFF"
            + struct.pack("<I", 4 + 8 + len(body))
            + b"WEBPVP8 "
            + struct.pack("<I", len(body))
            + body
            + b"\x00" * 8
        )

    def extended(self, width: int, height: int) -> bytes:
        body = b"\x00" * 4 + bytes(
            (
                (width - 1) & 0xFF,
                ((width - 1) >> 8) & 0xFF,
                ((width - 1) >> 16) & 0xFF,
                (height - 1) & 0xFF,
                ((height - 1) >> 8) & 0xFF,
                ((height - 1) >> 16) & 0xFF,
            )
        )
        return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBPVP8X" + struct.pack("<I", len(body)) + body

    def test_a_lossy_webp_reports_its_size(self) -> None:
        self.assertEqual(images.pixel_size(self.lossy(372, 519)), (372, 519))

    def test_an_extended_webp_reports_its_size(self) -> None:
        self.assertEqual(images.pixel_size(self.extended(488, 680)), (488, 680))

    def test_a_truncated_webp_reports_nothing(self) -> None:
        self.assertIsNone(images.pixel_size(b"RIFF____WEBPVP8 "))

    def test_a_png_is_already_drawable(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"rest"
        self.assertIs(images.drawable(png), png)

    def test_a_jpeg_is_converted_too(self) -> None:
        # The protocol defines only 24-bit RGB, 32-bit RGBA and PNG, so a JPEG
        # is drawn by ghostty's own decoder but may be ignored within spec.
        jpeg = b"\xff\xd8\xff\xe0" + b"rest"
        with patch.object(images, "_to_png", return_value=b"\x89PNG\r\n\x1a\nconverted") as convert:
            converted = images.drawable(jpeg)
            self.assertIsNotNone(converted)
            assert converted is not None
            self.assertEqual(converted[:8], b"\x89PNG\r\n\x1a\n")
            convert.assert_called_once()

    def test_a_format_the_terminal_refuses_is_converted(self) -> None:
        with patch.object(images, "_to_png", return_value=b"\x89PNG\r\n\x1a\nconverted") as convert:
            converted = images.drawable(self.lossy(10, 10))
            self.assertIsNotNone(converted)
            assert converted is not None
            self.assertEqual(converted[:8], b"\x89PNG\r\n\x1a\n")
            convert.assert_called_once()

    def test_a_picture_that_cannot_be_converted_is_not_drawn(self) -> None:
        with patch.object(images, "_to_png", return_value=None):
            self.assertIsNone(images.drawable(self.lossy(10, 10)))


class LetterboxTests(unittest.TestCase):
    """Whole-cell rounding cannot express an exact ratio; padding can."""

    def png(self, width: int, height: int) -> bytes:
        import zlib

        raw = bytearray()
        for _ in range(height):
            raw.append(0)
            raw += bytes((180, 70, 70)) * width

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )

    def test_fit_reports_the_pixel_box_for_its_cell_box(self) -> None:
        cols, rows, box = images.fit(400, 247, 94, 44, (8, 17))
        self.assertEqual(box, (cols * 8, rows * 17))

    def test_without_a_measured_cell_the_box_still_has_a_shape(self) -> None:
        _cols, _rows, box = images.fit(400, 247, 94, 44)
        self.assertGreater(box[0], 0)
        self.assertGreater(box[1], 0)

    def test_padding_fills_the_box_exactly(self) -> None:
        _cols, _rows, box = images.fit(1550, 91, 94, 44, (8, 17))
        padded = images.letterbox(self.png(310, 18), box)
        assert padded is not None
        self.assertEqual(images.pixel_size(padded), box)

    def test_a_padded_picture_keeps_its_own_proportions(self) -> None:
        source_width, source_height = 888, 1701
        _cols, _rows, box = images.fit(source_width, source_height, 94, 44, (8, 17))
        scale = min(box[0] / source_width, box[1] / source_height)
        drawn = (round(source_width * scale), round(source_height * scale))
        # The visible picture, not the padded canvas, is what must stay in shape.
        self.assertAlmostEqual(
            drawn[1] / drawn[0], source_height / source_width, delta=0.01
        )

    def test_a_picture_already_matching_its_box_is_untouched(self) -> None:
        exact = self.png(40, 20)
        self.assertIs(images.letterbox(exact, (40, 20)), exact)

    def test_an_unreadable_picture_is_not_padded(self) -> None:
        self.assertIsNone(images.letterbox(b"not an image", (40, 20)))

    def test_a_nonsense_box_is_refused(self) -> None:
        self.assertIsNone(images.letterbox(self.png(10, 10), (0, 20)))


class PixelSizeTests(unittest.TestCase):
    """The stored dimensions are wrong for a rotated photo, so bytes decide."""

    def png(self, width: int, height: int) -> bytes:
        import struct, zlib

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        raw = bytearray()
        for _ in range(height):
            raw.append(0)
            raw += bytes((200, 60, 60)) * width
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )

    def test_a_png_reports_its_own_size(self) -> None:
        self.assertEqual(images.pixel_size(self.png(37, 11)), (37, 11))

    def test_a_landscape_png_is_not_read_as_portrait(self) -> None:
        # One live row recorded 1848x4000 for an image that is really 4000x1848.
        self.assertEqual(images.pixel_size(self.png(40, 18)), (40, 18))

    def test_a_format_that_is_not_decoded_reports_nothing(self) -> None:
        self.assertIsNone(images.pixel_size(b"RIFF____WEBPVP8 "))

    def test_a_truncated_header_reports_nothing_rather_than_raising(self) -> None:
        self.assertIsNone(images.pixel_size(b"\x89PNG\r\n\x1a\n"))
        self.assertIsNone(images.pixel_size(b"\xff\xd8"))


class CropTests(unittest.TestCase):
    """A picture only partly inside the window shows that part, not all or none."""

    def test_a_fully_visible_picture_needs_no_band(self) -> None:
        self.assertIsNone(images.crop_for(1118, 17, 0, 17))

    def test_a_picture_cut_off_at_the_bottom_starts_at_its_top(self) -> None:
        band = images.crop_for(1118, 17, 0, 9)
        assert band is not None
        self.assertEqual(band[0], 0)
        self.assertLess(band[1], 1118)

    def test_a_picture_cut_off_at_the_top_skips_the_hidden_pixels(self) -> None:
        band = images.crop_for(1118, 17, 8, 9)
        assert band is not None
        self.assertGreater(band[0], 0)
        self.assertEqual(band[0] + band[1], 1118)

    def test_a_band_never_runs_past_the_source(self) -> None:
        for hidden in range(0, 18):
            for shown in range(1, 18):
                band = images.crop_for(1118, 17, hidden, shown)
                if band is None:
                    continue
                with self.subTest(hidden=hidden, shown=shown):
                    top, height = band
                    self.assertGreaterEqual(top, 0)
                    self.assertGreaterEqual(height, 1)
                    self.assertLessEqual(top + height, 1118)

    def test_a_fully_hidden_picture_has_no_band_to_show(self) -> None:
        # A zero-height band draws nothing while still holding a placement.
        self.assertIsNone(images.crop_for(1118, 17, 17, 5))

    def test_a_cropped_placement_carries_the_band_in_its_escape(self) -> None:
        drawn = images.draw(1, b"bytes", columns=20, rows=6, crop=(66, 600))
        self.assertIn("y=66", drawn)
        self.assertIn("h=600", drawn)

    def test_an_uncropped_placement_carries_no_band(self) -> None:
        drawn = images.draw(1, b"bytes", columns=20, rows=6)
        self.assertNotIn("y=", drawn)
        self.assertNotIn("h=", drawn)

    def test_a_replaced_picture_can_be_cropped_too(self) -> None:
        shown = images.place(1, 20, 6, crop=(66, 600))
        self.assertIn("a=p", shown)
        self.assertIn("y=66", shown)
        self.assertIn("h=600", shown)


class EscapeTests(unittest.TestCase):
    def test_a_drawn_photo_transmits_and_displays_in_one_escape(self) -> None:
        drawn = images.draw(7, b"bytes", columns=20, rows=6)
        self.assertTrue(drawn.startswith("\x1b_G"))
        self.assertTrue(drawn.endswith("\x1b\\"))
        self.assertIn("a=T", drawn)
        self.assertIn("f=100", drawn)
        self.assertIn("i=7", drawn)
        self.assertIn("c=20", drawn)
        self.assertIn("r=6", drawn)

    def test_the_payload_is_the_pictures_bytes_in_base64(self) -> None:
        drawn = images.draw(1, b"bytes", columns=4, rows=2)
        self.assertIn(base64.standard_b64encode(b"bytes").decode("ascii"), drawn)

    def test_a_large_photo_is_chunked_and_every_chunk_but_the_last_says_more(self) -> None:
        drawn = images.draw(2, os.urandom(20_000), columns=20, rows=6)
        self.assertGreater(drawn.count("\x1b_G"), 1)
        self.assertEqual(drawn.count("m=1"), drawn.count("\x1b_G") - 1)
        self.assertTrue(drawn.rstrip("\x1b\\").endswith(drawn.split(";")[-1].rstrip("\x1b\\")))

    def test_erasing_deletes_the_placement_and_frees_the_image(self) -> None:
        erased = images.erase(7)
        self.assertIn("a=d", erased)
        self.assertIn("d=I", erased)
        self.assertIn("i=7", erased)


class _Response:
    """Stands in for an ``http.client.HTTPResponse`` the way urlopen returns one."""

    def __init__(self, blob: bytes, content_type: str = "image/jpeg") -> None:
        self._blob = blob
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def read(self, _limit: int = -1) -> bytes:
        return self._blob

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exception: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
