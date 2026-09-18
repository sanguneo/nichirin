"""Opening a picture in the conversation, and the escapes that draw it.

No network and no terminal: the fetch goes through a stand-in, and the picture
is written from bytes here. What is asserted is the interface's side — which
rows are reserved, what the label says, and that a still screen writes nothing.
"""

from __future__ import annotations

import struct
import unittest
import zlib
from typing import Optional

from kanao.tui import images
from kanao.tui.console import SHOW_PHOTO
from kanao.tui.layout import strip_ansi
from kanao.tui.theme import Theme
from tests.fixtures import GROUP_ROOM, build_reader
from tests.platform import import_app, require_windows_imaging

WIDTH = 80
HEIGHT = 24
PHOTO_MESSAGE = 6006
VIDEO_MESSAGE = 6007


def make_png(width: int = 4, height: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + bytes((20, 160, 90) * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class FakeStore:
    """Stands in for PhotoStore, without a network or a cache directory."""

    def __init__(self, blob: Optional[bytes] = None) -> None:
        self.blob = make_png() if blob is None else blob
        self.asked: list[str] = []

    def fetch(self, url: str) -> Optional[bytes]:
        self.asked.append(url)
        return self.blob


class PhotoCase(unittest.TestCase):
    def setUp(self) -> None:
        App = import_app()
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.store = FakeStore()
        self.app._photos = self.store
        self.app._protocol = images.SIXEL
        self.app._cell = (8, 16)
        self.app.load_rooms()
        self.app.open_room(next(r for r in self.app.rooms if r.id == GROUP_ROOM))
        self.app.render(WIDTH, HEIGHT)

    def transcript(self) -> list[str]:
        chat = self.app.chat
        assert chat is not None
        return [strip_ansi(line) for line in self.app._transcript_of(chat, WIDTH)]


class LabelTests(PhotoCase):
    def test_a_photo_offers_to_be_opened(self) -> None:
        self.assertTrue(any("[photo ▸]" in line for line in self.transcript()))

    def test_a_video_is_not_offered(self) -> None:
        # It stores the same shaped attachment, and opening it would produce
        # nothing, so it is never marked as openable.
        lines = " ".join(self.transcript())
        self.assertNotIn("video ▸", lines)
        self.assertNotIn("video ▾", lines)

    def test_the_clients_own_word_for_a_photo_is_replaced_by_the_label(self) -> None:
        # A live client stores "사진" as the message text, which carries no
        # marker and reads as if somebody typed it.
        line = next(line for line in self.transcript() if "[photo" in line)
        self.assertNotIn("사진", line)

    def test_a_terminal_that_draws_nothing_offers_nothing(self) -> None:
        self.app._protocol = images.NONE
        chat = self.app.chat
        assert chat is not None
        chat.rendered = None
        self.assertFalse(any("▸" in line for line in self.transcript()))

    def test_an_opened_photo_says_it_can_be_closed(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.assertTrue(any("[photo ▾]" in line for line in self.transcript()))


class OpeningTests(PhotoCase):
    def test_opening_reserves_blank_rows_for_the_picture(self) -> None:
        before = len(self.transcript())
        self.app.toggle_photo(PHOTO_MESSAGE)
        after = len(self.transcript())
        self.assertGreater(after, before)

    def test_opening_fetches_once_however_often_it_is_drawn(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        for _ in range(4):
            self.app.render(WIDTH, HEIGHT)
        self.assertEqual(len(self.store.asked), 1)

    def test_the_full_size_address_is_the_one_fetched(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.assertEqual(self.store.asked, ["https://talk.kakaocdn.net/synthetic/full.jpg"])

    def test_closing_gives_the_rows_back(self) -> None:
        plain = len(self.transcript())
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.assertGreater(len(self.transcript()), plain)
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.assertEqual(len(self.transcript()), plain)

    def test_a_picture_that_cannot_be_fetched_stays_closed(self) -> None:
        self.app._photos = FakeStore(blob=b"")
        self.app._photo_bytes.clear()
        self.assertFalse(self.app.toggle_photo(PHOTO_MESSAGE))
        chat = self.app.chat
        assert chat is not None
        self.assertNotIn(PHOTO_MESSAGE, chat.opened_photos)
        self.assertIn("사진", chat.notice)

    def test_a_video_cannot_be_opened(self) -> None:
        self.assertFalse(self.app.toggle_photo(VIDEO_MESSAGE))


class KeyAndCommandTests(PhotoCase):
    def chat(self):
        chat = self.app.chat
        assert chat is not None
        return chat

    def test_control_p_opens_the_newest_picture(self) -> None:
        self.app.handle("char", SHOW_PHOTO)
        self.assertEqual(self.chat().opened_photos, {PHOTO_MESSAGE})

    def test_control_p_again_closes_it(self) -> None:
        self.app.handle("char", SHOW_PHOTO)
        self.app.handle("char", SHOW_PHOTO)
        self.assertEqual(self.chat().opened_photos, set())

    def test_control_p_does_not_reach_the_draft(self) -> None:
        # Every other character typed in a room goes into the composer.
        self.app.handle("char", SHOW_PHOTO)
        self.assertEqual(self.chat().draft, "")

    def test_the_photo_command_does_the_same(self) -> None:
        for character in "/photo":
            self.app.handle("char", character)
        self.app.handle("enter", "")
        self.assertEqual(self.chat().opened_photos, {PHOTO_MESSAGE})

    def test_a_terminal_that_cannot_draw_says_so(self) -> None:
        self.app._protocol = images.NONE
        self.app.handle("char", SHOW_PHOTO)
        self.assertEqual(self.chat().opened_photos, set())
        self.assertIn("KANAO_IMAGE", self.chat().notice)


class PlacementTests(PhotoCase):
    def test_a_closed_conversation_draws_nothing(self) -> None:
        self.app.render(WIDTH, HEIGHT)
        self.assertEqual(self.app.photo_escapes(), "")

    def test_an_opened_picture_is_positioned_and_drawn(self) -> None:
        require_windows_imaging()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        escapes = self.app.photo_escapes()
        self.assertRegex(escapes, r"\x1b\[\d+;\d+H")
        self.assertIn("\x1bP", escapes)

    def test_a_still_screen_redraws_nothing(self) -> None:
        require_windows_imaging()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertTrue(self.app.photo_escapes())
        for _ in range(4):
            self.app.render(WIDTH, HEIGHT)
            self.assertEqual(self.app.photo_escapes(), "")

    def test_a_change_gives_the_frame_up_so_nothing_is_left_behind(self) -> None:
        require_windows_imaging()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.assertTrue(self.app._images_moved)
        self.app._images_moved = False
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.assertFalse(self.app._images_moved)

    def test_the_escape_is_built_once_and_then_reused(self) -> None:
        require_windows_imaging()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        built = len(self.app._inline_cache)
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.assertEqual(len(self.app._inline_cache), built)


if __name__ == "__main__":
    unittest.main()
