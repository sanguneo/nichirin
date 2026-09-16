"""Editing in the composer: a caret, a draft that survives, and newlines.

The composer used to be append-only. A typo three words back meant deleting
everything after it, escape threw the whole draft away with no undo, and a
pasted newline sent the first line on its own because the console delivers a
paste as ordinary keystrokes.
"""

from __future__ import annotations

import unittest

from kakaoread.tui.console import Key
from kakaoread.tui.layout import display_width, strip_ansi, window_around

from tests.test_tui_send import HEIGHT, WIDTH, ComposerCase


class WindowAroundTests(unittest.TestCase):
    """The slice of a draft that is on screen, and where its caret lands."""

    def test_a_draft_that_fits_is_shown_whole(self) -> None:
        self.assertEqual(window_around("abc", 3, 20), ("abc", 3))

    def test_the_caret_column_counts_columns_not_characters(self) -> None:
        _shown, column = window_around("가나다", 2, 20)
        self.assertEqual(column, 4)

    def test_a_long_draft_scrolls_to_keep_the_caret_visible(self) -> None:
        shown, column = window_around("abcdefghij", 10, 5)
        self.assertLessEqual(display_width(shown), 5)
        self.assertLessEqual(column, 5)
        self.assertTrue("j" in shown, "the caret's own end of the text fell off")

    def test_a_caret_at_the_head_shows_the_head(self) -> None:
        shown, column = window_around("abcdefghij", 0, 5)
        self.assertTrue(shown.startswith("a"))
        self.assertEqual(column, 0)

    def test_a_width_of_nothing_is_survivable(self) -> None:
        self.assertEqual(window_around("abc", 1, 0), ("", 0))


class CaretTests(ComposerCase):
    def test_typing_inserts_at_the_caret_rather_than_the_end(self) -> None:
        self.type("네시에")
        self.app.handle(Key.LEFT, "")
        self.type("다")
        self.assertEqual(self.chat.draft, "네시다에")

    def test_the_caret_walks_both_ways(self) -> None:
        self.type("abc")
        self.app.handle(Key.LEFT, "")
        self.app.handle(Key.LEFT, "")
        self.assertEqual(self.chat.caret, 1)
        self.app.handle(Key.RIGHT, "")
        self.assertEqual(self.chat.caret, 2)

    def test_the_caret_stops_at_both_ends(self) -> None:
        self.type("ab")
        for _ in range(5):
            self.app.handle(Key.LEFT, "")
        self.assertEqual(self.chat.caret, 0)
        for _ in range(5):
            self.app.handle(Key.RIGHT, "")
        self.assertEqual(self.chat.caret, 2)

    def test_backspace_takes_the_character_before_the_caret(self) -> None:
        self.type("abc")
        self.app.handle(Key.LEFT, "")
        self.app.handle(Key.BACKSPACE, "")
        self.assertEqual(self.chat.draft, "ac")
        self.assertEqual(self.chat.caret, 1)

    def test_backspace_at_the_head_removes_nothing(self) -> None:
        self.type("ab")
        self.app.handle(Key.HOME, "")
        self.app.handle(Key.BACKSPACE, "")
        self.assertEqual(self.chat.draft, "ab")

    def test_delete_takes_the_character_under_the_caret(self) -> None:
        self.type("abc")
        self.app.handle(Key.LEFT, "")
        self.app.handle(Key.DELETE, "")
        self.assertEqual(self.chat.draft, "ab")
        self.assertEqual(self.chat.caret, 2)

    def test_home_and_end_move_the_caret_while_writing(self) -> None:
        self.type("abc")
        self.app.handle(Key.HOME, "")
        self.assertEqual(self.chat.caret, 0)
        self.app.handle(Key.END, "")
        self.assertEqual(self.chat.caret, 3)

    def test_an_empty_box_leaves_home_and_end_to_the_conversation(self) -> None:
        self.app.handle(Key.HOME, "")
        self.assertTrue(self.chat.anchor_top)
        self.chat.offset = 9
        self.app.handle(Key.END, "")
        self.assertEqual(self.chat.offset, 0)

    def test_clearing_the_box_takes_the_caret_with_it(self) -> None:
        self.type("abc")
        self.app.handle("char", "\x15")  # ctrl+u
        self.assertEqual(self.chat.draft, "")
        self.assertEqual(self.chat.caret, 0)

    def test_sending_resets_the_caret(self) -> None:
        self.type("안녕")
        self.press_enter()
        self.assertEqual(self.chat.caret, 0)

    def test_the_terminal_caret_follows_the_text_caret(self) -> None:
        self.type("가나다")
        self.app.render(WIDTH, HEIGHT)
        at_end = self.app.cursor
        self.app.handle(Key.LEFT, "")
        self.app.render(WIDTH, HEIGHT)
        assert at_end is not None and self.app.cursor is not None
        self.assertEqual(at_end[1] - self.app.cursor[1], 2, "the caret ignored a wide character")

    def test_a_draft_wider_than_the_box_keeps_the_caret_on_screen(self) -> None:
        self.type("가" * 200)
        for _ in range(30):
            self.app.handle(Key.LEFT, "")
        frame = self.app.render(WIDTH, HEIGHT)
        assert self.app.cursor is not None
        self.assertLess(self.app.cursor[1], WIDTH)
        for line in frame:
            self.assertLessEqual(display_width(strip_ansi(line)), WIDTH)


class DraftSurvivalTests(ComposerCase):
    def test_escape_keeps_the_draft_for_the_next_visit(self) -> None:
        self.type("길게 쓰던 글")
        room = self.chat.room
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)

        self.app.open_room(room)
        self.assertEqual(self.chat.draft, "길게 쓰던 글", "escape threw the draft away")
        self.assertEqual(self.chat.caret, len("길게 쓰던 글"))

    def test_a_sent_draft_is_not_waiting_the_next_time(self) -> None:
        self.type("안녕")
        room = self.chat.room
        self.press_enter()
        self.app.handle(Key.ESCAPE, "")
        self.app.open_room(room)
        self.assertEqual(self.chat.draft, "")

    def test_escape_closes_a_digest_before_the_room(self) -> None:
        self.app._run_command("/sum 30m")
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.chat.slices, [])
        self.assertIsNotNone(self.app.chat)


class NewlineTests(ComposerCase):
    def test_a_newline_is_written_into_the_draft(self) -> None:
        self.type("첫 줄")
        self.app.handle("char", "\n")
        self.type("둘째 줄")
        self.assertEqual(self.chat.draft, "첫 줄\n둘째 줄")

    def test_the_box_expands_to_display_multiple_lines(self) -> None:
        self.type("첫 줄")
        self.app.handle("char", "\n")
        self.type("둘째 줄")
        frame = self.frame()
        self.assertIn("첫 줄", "\n".join(frame))
        self.assertIn("둘째 줄", "\n".join(frame))
        # Open omo-style layout: top divider, content lines, bottom divider, footer
        self.assertIn("─", frame[-5])
        self.assertIn("첫 줄", frame[-4])
        self.assertIn("둘째 줄", frame[-3])
        self.assertIn("─", frame[-2])

    def test_what_is_sent_carries_the_real_newline(self) -> None:
        self.type("첫 줄")
        self.app.handle("char", "\n")
        self.type("둘째 줄")
        self.press_enter()
        self.assertEqual(self.sender.sent[0][1], "첫 줄\n둘째 줄")


if __name__ == "__main__":
    unittest.main()
