"""Opening a photo from the transcript, and closing it again.

The store is a stand-in so no test reaches the network, and the terminal is
told it supports images so the toggle is exercised rather than skipped. What is
asserted is the transcript and the escapes the interface would write, because
that is the whole of what the terminal ever sees.
"""

from __future__ import annotations

import re
import unittest

from kamado.tui.app import App
from kamado.tui import images
from kamado.tui.console import Key
from kamado.tui.layout import display_width, strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import GROUP_ROOM, build_reader

WIDTH = 80
HEIGHT = 24

PHOTO_MESSAGE = 6006
MULTI_PHOTO_MESSAGE = 6007
VIDEO_MESSAGE = 6008


class FakeStore:
    """Stands in for PhotoStore, without a network or a cache directory."""

    def __init__(self, blob: bytes | None = b"\x89PNG\r\n\x1a\nsynthetic") -> None:
        self.blob = blob
        self.asked: list[str] = []

    def fetch(self, url: str) -> bytes | None:
        self.asked.append(url)
        return self.blob


class PhotoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.store = FakeStore()
        self.app._photos = self.store
        self.app._protocol = images.KITTY
        self.app.load_rooms()
        self.app.open_room(next(r for r in self.app.rooms if r.id == GROUP_ROOM))
        self.app.render(WIDTH, HEIGHT)

    def transcript(self) -> list[str]:
        chat = self.app.chat
        assert chat is not None
        return [strip_ansi(line) for line in self.app._transcript_of(chat, WIDTH)]

    def photo_row(self, message_id: int = PHOTO_MESSAGE) -> int:
        chat = self.app.chat
        assert chat is not None
        rows = [row for row, owner in chat.photo_rows.items() if owner == message_id]
        self.assertTrue(rows, f"no transcript row belongs to message {message_id}")
        return min(rows)


class ClosedStateTests(PhotoCase):
    def test_a_photo_starts_as_a_label(self) -> None:
        self.assertTrue(any("[사진]" in line for line in self.transcript()))

    def test_the_clients_own_word_for_a_photo_is_replaced_by_the_label(self) -> None:
        # A live client stores "사진" as the message text, which carries no
        # marker and reads as if somebody typed it.
        line = next(line for line in self.transcript() if "[사진]" in line)
        self.assertNotRegex(line, r"(?<!\[)사진(?!\s*\d*장?\])")

    def test_the_label_says_it_can_be_opened(self) -> None:
        line = next(line for line in self.transcript() if "[사진]" in line)
        self.assertIn("▸", line)

    def test_several_photos_say_how_many_there_are(self) -> None:
        self.assertTrue(any("[사진 2장]" in line for line in self.transcript()))

    def test_nothing_is_fetched_before_anything_is_clicked(self) -> None:
        self.assertEqual(self.store.asked, [])

    def test_a_terminal_without_image_support_offers_no_marker(self) -> None:
        self.app._protocol = images.NONE
        chat = self.app.chat
        assert chat is not None
        chat.rendered = None
        line = next(line for line in self.transcript() if "[사진]" in line)
        self.assertNotIn("▸", line)


class ToggleTests(PhotoCase):
    def test_opening_a_photo_fetches_it_once(self) -> None:
        self.assertTrue(self.app.toggle_photo(PHOTO_MESSAGE))
        self.assertEqual(len(self.store.asked), 1)

    def test_an_opened_photo_reserves_blank_rows_for_itself(self) -> None:
        before = len(self.transcript())
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.assertGreater(len(self.transcript()), before)

    def test_an_opened_photo_says_it_can_be_closed(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        line = next(line for line in self.transcript() if "[사진]" in line)
        self.assertIn("▾", line)

    def test_closing_it_puts_the_transcript_back(self) -> None:
        before = self.transcript()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.assertEqual(self.transcript(), before)

    def test_a_photo_that_cannot_be_fetched_stays_closed_and_says_so(self) -> None:
        self.app._photos = FakeStore(blob=None)
        self.app.toggle_photo(PHOTO_MESSAGE)
        chat = self.app.chat
        assert chat is not None
        self.assertNotIn(PHOTO_MESSAGE, chat.opened_photos)
        self.assertIn("사진을 가져오지 못했습니다", chat.notice)

    def test_a_message_without_a_photo_is_not_toggled(self) -> None:
        self.assertFalse(self.app.toggle_photo(6001))

    def test_a_terminal_without_image_support_refuses_to_open(self) -> None:
        self.app._protocol = images.NONE
        self.assertFalse(self.app.toggle_photo(PHOTO_MESSAGE))
        self.assertEqual(self.store.asked, [])


class ClickTests(PhotoCase):
    def click(self, screen_row: int, column: int = 10) -> None:
        self.app.handle(Key.CLICK, f"{screen_row},{column}")

    def screen_row_of(self, message_id: int = PHOTO_MESSAGE) -> int:
        """Find the screen row the app itself maps to this photo's label."""
        self.app.render(WIDTH, HEIGHT)
        row = self.photo_row(message_id)
        for candidate in range(HEIGHT):
            found = self.app._pane_at(candidate, 10)
            if found is not None and found[1] == row:
                return candidate
        raise AssertionError("no screen row maps to the photo label")

    def test_clicking_the_label_opens_the_photo(self) -> None:
        self.click(self.screen_row_of())
        chat = self.app.chat
        assert chat is not None
        self.assertIn(PHOTO_MESSAGE, chat.opened_photos)

    def test_clicking_it_again_closes_the_photo(self) -> None:
        self.click(self.screen_row_of())
        self.app.render(WIDTH, HEIGHT)
        self.click(self.screen_row_of())
        chat = self.app.chat
        assert chat is not None
        self.assertNotIn(PHOTO_MESSAGE, chat.opened_photos)

    def test_clicking_an_ordinary_line_opens_nothing(self) -> None:
        geometry = self.app._pane_geometry
        assert geometry is not None
        self.click(geometry[0])
        chat = self.app.chat
        assert chat is not None
        self.assertEqual(chat.opened_photos, set())

    def test_a_click_above_the_conversation_is_ignored(self) -> None:
        self.click(0)
        chat = self.app.chat
        assert chat is not None
        self.assertEqual(chat.opened_photos, set())

    def test_a_click_does_not_reach_the_draft(self) -> None:
        self.click(self.screen_row_of())
        chat = self.app.chat
        assert chat is not None
        self.assertEqual(chat.draft, "")


class ScrollTests(PhotoCase):
    """Opening a photo scrolls the view; closing it must hand the view back."""

    def scrollable(self) -> int:
        self.app.render(WIDTH, HEIGHT)
        geometry = self.app._pane_geometry
        assert geometry is not None
        _top, rows, cell, _panes = geometry
        chat = self.app.chat
        assert chat is not None
        return max(0, len(self.app._transcript_of(chat, cell)) - rows)

    def test_closing_a_photo_restores_the_scroll_position(self) -> None:
        chat = self.app.chat
        assert chat is not None
        before = chat.offset
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertEqual(chat.offset, before)

    def test_the_view_never_parks_past_what_can_be_scrolled(self) -> None:
        # An offset stranded beyond the end is what stopped scrolling working
        # after a photo was closed.
        chat = self.app.chat
        assert chat is not None
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertLessEqual(chat.offset, self.scrollable())

    def test_scrolling_still_moves_the_screen_after_a_photo_was_closed(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.toggle_photo(PHOTO_MESSAGE)
        settled = self.app.render(WIDTH, HEIGHT)
        self.app.handle(Key.UP, "")
        self.assertNotEqual(self.app.render(WIDTH, HEIGHT), settled)

    def test_an_opened_photo_keeps_its_label_on_screen(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        geometry = self.app._pane_geometry
        assert geometry is not None
        _top, rows, cell, _panes = geometry
        body = self.app._transcript_of(chat, cell)
        scrollable = len(body) - rows
        offset = min(chat.offset, scrollable) if scrollable > 0 else chat.offset
        end = max(1, len(body) - max(0, offset))
        start = max(0, end - rows)
        self.assertTrue(start <= self.photo_row() < end)


    def test_the_wheel_scrolls_while_a_photo_is_open(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        settled = self.app.render(WIDTH, HEIGHT)
        self.app.handle(Key.WHEEL_UP, "")
        self.assertNotEqual(self.app.render(WIDTH, HEIGHT), settled)

    def test_the_wheel_moves_further_than_one_line(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        before = chat.offset
        self.app.handle(Key.WHEEL_UP, "")
        self.assertGreater(chat.offset - before, 1)

    def test_the_wheel_comes_back_down_to_the_newest_line(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app.handle(Key.WHEEL_UP, "")
        self.app.handle(Key.WHEEL_UP, "")
        self.assertGreater(chat.offset, 0)
        for _ in range(6):
            self.app.handle(Key.WHEEL_DOWN, "")
        self.assertEqual(chat.offset, 0)

    def test_the_wheel_does_not_type_into_the_draft(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app.handle(Key.WHEEL_UP, "")
        self.app.handle(Key.WHEEL_DOWN, "")
        self.assertEqual(chat.draft, "")


class LeavingTests(PhotoCase):
    """A placement outlives the text under it, so leaving must withdraw it."""

    def test_leaving_a_room_withdraws_the_picture(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)
        self.app.render(WIDTH, HEIGHT)
        self.assertIn("a=d", self.app.photo_escapes())

    def test_the_room_list_draws_no_leftover_picture(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.app.handle(Key.ESCAPE, "")
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.assertEqual(self.app.placements, [])
        self.assertEqual(self.app.photo_escapes(), "")

    def test_reopening_the_room_starts_with_the_photo_closed(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.handle(Key.ESCAPE, "")
        self.app.open_room(next(r for r in self.app.rooms if r.id == GROUP_ROOM))
        self.app.render(WIDTH, HEIGHT)
        chat = self.app.chat
        assert chat is not None
        self.assertEqual(chat.opened_photos, set())


class OpeningPositionTests(PhotoCase):
    """Opening a picture must not move a conversation that already shows it."""

    def test_a_photo_already_on_screen_opens_without_scrolling(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app.render(WIDTH, 40)
        before = chat.offset
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, 40)
        self.assertEqual(chat.offset, before)

    def test_an_opened_photo_is_not_pinned_to_the_bottom(self) -> None:
        self.app.render(WIDTH, 40)
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, 40)
        body = self.app._transcript_lines(WIDTH)
        owned = sorted(
            row for row, owner in (self.app.chat.photo_rows.items() if self.app.chat else [])
            if owner == PHOTO_MESSAGE
        )
        chat = self.app.chat
        assert chat is not None
        end = len(body) - chat.offset
        # Pinned to the bottom would put the picture's last row exactly at the
        # window's end; it should keep its natural place instead.
        self.assertNotEqual(owned[-1] + 1, end)


class VideoTests(PhotoCase):
    """A video's attachment looks like a photo's, but there is nothing to open."""

    def test_a_video_is_named_rather_than_offered(self) -> None:
        line = next(line for line in self.transcript() if "[영상]" in line)
        self.assertNotIn("▸", line)
        self.assertNotIn("▾", line)

    def test_the_clients_own_word_for_a_video_is_replaced(self) -> None:
        # A video arrives as the plain text "동상열", which reads as if it were typed.
        self.assertFalse(
            any("동상열" in line and "[영상]" not in line for line in self.transcript())
        )

    def test_a_video_carries_no_photo(self) -> None:
        chat = self.app.chat
        assert chat is not None
        video = next(one for one in chat.messages if one.id == VIDEO_MESSAGE)
        self.assertIsNone(video.photo)

    def test_a_video_refuses_to_toggle(self) -> None:
        self.assertFalse(self.app.toggle_photo(VIDEO_MESSAGE))

    def test_clicking_a_video_opens_nothing(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.assertNotIn(VIDEO_MESSAGE, chat.photo_rows.values())

    def test_a_video_is_never_fetched(self) -> None:
        self.app.toggle_photo(VIDEO_MESSAGE)
        self.assertEqual(self.store.asked, [])


class PaneTests(PhotoCase):
    """A picture belongs to one pane's column, not to the screen."""

    WIDE = 120
    """Wide enough for two panes; at WIDTH only one fits."""

    NARROW = 60
    """Narrow enough that only the focused pane is painted."""

    def second_pane(self) -> None:
        other = next(r for r in self.app.rooms if r.id != GROUP_ROOM)
        self.app.open_room(other)
        self.app.render(self.WIDE, HEIGHT)

    def group_pane(self):
        geometry = self.app._pane_geometry
        assert geometry is not None
        return geometry, next(p for p in geometry[3] if p.room.id == GROUP_ROOM)

    def scroll_photo_into_view(self) -> None:
        (_top, _rows, cell, _panes), pane = self.group_pane()
        body = self.app._transcript_of(pane, cell)
        owned = sorted(r for r, o in pane.photo_rows.items() if o == PHOTO_MESSAGE)
        pane.offset = max(0, len(body) - (owned[-1] + 1))
        self.app.render(self.WIDE, HEIGHT)

    def test_two_panes_are_visible_at_this_width(self) -> None:
        self.second_pane()
        geometry = self.app._pane_geometry
        assert geometry is not None
        self.assertEqual(len(geometry[3]), 2)

    def test_a_photo_stays_inside_its_own_column(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.second_pane()
        self.scroll_photo_into_view()
        (_top, _rows, cell, _panes), _pane = self.group_pane()
        self.assertTrue(self.app.placements)
        for _id, _row, column, _blob, cols, _rows_, _crop in self.app.placements:
            self.assertLessEqual(column + cols, cell)

    def test_a_photo_keeps_drawing_when_another_pane_takes_focus(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.second_pane()
        self.scroll_photo_into_view()
        before = len(self.app.placements)
        self.assertTrue(before)
        self.app.focus = 1
        self.app.render(self.WIDE, HEIGHT)
        self.assertEqual(len(self.app.placements), before)

    def test_a_click_reaches_a_photo_in_an_unfocused_pane(self) -> None:
        self.second_pane()
        (top, _rows, _cell, _panes), pane = self.group_pane()
        row = min(r for r, o in pane.photo_rows.items() if o == PHOTO_MESSAGE)
        screen = next(
            (s for s in range(HEIGHT)
             if (found := self.app._pane_at(s, 10)) is not None
             and found[0] is pane and found[1] == row),
            None,
        )
        if screen is None:
            self.skipTest("the photo's label is not on screen in this layout")
        self.app.handle(Key.CLICK, f"{screen},10")
        self.assertIn(PHOTO_MESSAGE, pane.opened_photos)

    def test_a_hidden_pane_has_its_photo_withdrawn(self) -> None:
        """A pane the window is too narrow to show must not leave an image behind.

        `_visible_panes` slides rather than closing anything, so the conversation
        is still open when its column stops being painted -- and a placement
        that outlives its column floats over whichever pane took the space.
        """
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.second_pane()
        self.scroll_photo_into_view()
        self.assertTrue(self.app.placements)
        self.app.photo_escapes()
        self.app.render(self.NARROW, HEIGHT)
        geometry = self.app._pane_geometry
        assert geometry is not None
        self.assertNotIn(GROUP_ROOM, [p.room.id for p in geometry[3]])
        self.assertEqual(self.app.placements, [])
        # The frame is painted and the escapes written together, the way the
        # loop does it, so the withdrawal rides out with the narrowed frame.
        self.assertIn("a=d", self.app.photo_escapes())

    def test_moving_between_panes_does_not_redraw_a_photo(self) -> None:
        """Tab changes which pane the keyboard is in, and nothing about the image."""
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.second_pane()
        self.scroll_photo_into_view()
        self.app.photo_escapes()
        before = list(self.app.placements)
        self.app.handle(Key.TAB, "")
        self.app.render(self.WIDE, HEIGHT)
        self.assertEqual(len(self.app.placements), len(before))
        self.assertEqual(self.app.photo_escapes(), "")

    def test_closing_every_pane_leaves_nothing_drawn(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.second_pane()
        self.scroll_photo_into_view()
        self.app.photo_escapes()
        while self.app.chats:
            self.app.handle(Key.ESCAPE, "")
            self.app.render(self.WIDE, HEIGHT)
            self.app.photo_escapes()
        self.assertEqual(self.app.placements, [])
        self.assertEqual(self.app._drawn, {})
        self.assertEqual(self.app._transmitted, set())

    def test_a_click_on_the_rule_between_panes_does_nothing(self) -> None:
        self.second_pane()
        (top, _rows, cell, _panes), _pane = self.group_pane()
        self.assertIsNone(self.app._pane_at(top, cell))


class PlacementTests(PhotoCase):
    def test_a_closed_room_draws_no_image(self) -> None:
        self.assertEqual(self.app.photo_escapes(), "")

    def test_an_opened_photo_is_transmitted_and_positioned(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        escapes = self.app.photo_escapes()
        self.assertIn("\x1b_G", escapes)
        self.assertIn("a=T", escapes)
        self.assertRegex(escapes, r"\x1b\[\d+;\d+H")

    def test_the_placement_uses_an_id_the_protocol_can_carry(self) -> None:
        # A message id is a ~63-bit snowflake and the protocol's field is 32
        # bits; a terminal handed the raw id draws nothing and reports nothing.
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        found = re.search(r"i=(\d+)", self.app.photo_escapes())
        assert found is not None
        self.assertLessEqual(int(found.group(1)), 0xFFFFFFFF)
        self.assertNotIn(f"i={PHOTO_MESSAGE}", self.app.photo_escapes())

    def test_a_still_screen_redraws_nothing(self) -> None:
        # Redrawing an image that has not moved is what made it flicker several
        # times a second, because the interface repaints on every key wait.
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertIn("a=T", self.app.photo_escapes())
        for _ in range(5):
            self.app.render(WIDTH, HEIGHT)
            self.assertEqual(self.app.photo_escapes(), "")

    def test_a_photo_that_moved_is_replaced_without_being_sent_again(self) -> None:
        # An original measured 294-362 KB; re-sending it per scrolled line is
        # the same flicker in a slower form.
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        first = self.app.photo_escapes()
        self.assertIn("a=T", first)
        chat = self.app.chat
        assert chat is not None
        chat.offset += 1
        self.app.render(WIDTH, HEIGHT)
        moved = self.app.photo_escapes()
        self.assertIn("a=p", moved)
        self.assertNotIn("a=T", moved)
        self.assertLess(len(moved), len(first))

    def test_a_move_keeps_the_bytes_the_terminal_already_holds(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        chat = self.app.chat
        assert chat is not None
        chat.offset += 1
        self.app.render(WIDTH, HEIGHT)
        self.assertIn("d=i", self.app.photo_escapes())

    def test_closing_a_photo_frees_it(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertIn("d=I", self.app.photo_escapes())

    def test_the_next_frame_withdraws_what_it_drew_before(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertIn("a=d", self.app.photo_escapes())

    def test_closing_a_photo_erases_it_and_draws_nothing_new(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.app.photo_escapes()
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        escapes = self.app.photo_escapes()
        self.assertIn("a=d", escapes)
        self.assertNotIn("a=T", escapes)

    def test_a_photo_outside_the_window_is_not_drawn(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.render(WIDTH, HEIGHT)
        self.assertIn("a=T", self.app.photo_escapes())
        # A screen with almost no room for the conversation leaves the photo's
        # rows outside the window, which is the case a placement must skip.
        self.app.render(WIDTH, 10)
        self.assertNotIn("a=T", self.app.photo_escapes())

    def test_an_open_photo_never_widens_the_frame(self) -> None:
        self.app.toggle_photo(PHOTO_MESSAGE)
        self.app.toggle_photo(MULTI_PHOTO_MESSAGE)
        for width in (24, 40, 80):
            with self.subTest(width=width):
                for line in self.app.render(width, HEIGHT):
                    self.assertLessEqual(display_width(line), width)


if __name__ == "__main__":
    unittest.main()
