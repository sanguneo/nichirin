"""The frame must fit the screen, whatever the caller hands over.

A single line wider than the console wraps, and every row after it lands one
row late and at the wrong column: the screen keeps fragments of the previous
frame until the interface is restarted. Driving a real console showed 23 rows
still holding the old frame where two were expected, so the width is enforced
here rather than trusted.
"""

from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import patch

from kakaoread.tui import console as console_module
from kakaoread.tui.app import App
from kakaoread.tui.console import (
    CLEAR_BELOW,
    CLEAR_LINE,
    HIDE_CURSOR,
    HOME,
    SHOW_CURSOR,
    Console,
)
from kakaoread.tui.layout import display_width, strip_ansi
from kakaoread.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, build_reader

COLUMNS = 40
ROWS = 8


def painted(lines: list[str]) -> list[str]:
    """Return the body of each row a draw wrote, without its escapes."""
    console = Console()
    buffer = io.StringIO()
    original = sys.stdout
    sys.stdout = buffer
    try:
        with patch.object(console_module, "_visible_size", lambda: (COLUMNS, ROWS)):
            console.draw(lines)
    finally:
        sys.stdout = original
    written = buffer.getvalue()
    # The cursor escapes carry a "?" that strip_ansi leaves alone, and they
    # ride on the last row rather than inside any line.
    for escape in (HOME, CLEAR_BELOW, HIDE_CURSOR, SHOW_CURSOR):
        written = written.replace(escape, "")
    return [strip_ansi(row).replace(CLEAR_LINE, "") for row in written.split("\r\n")]


def drawn_width() -> int:
    """The width the console reports, which is the screen minus its margin."""
    with patch.object(console_module, "_visible_size", lambda: (COLUMNS, ROWS)):
        return Console().size[0]


class FrameWidthTests(unittest.TestCase):
    def test_every_row_is_written_to_the_full_width(self) -> None:
        rows = painted(["짧은 줄"] + [""] * (ROWS - 1))
        for index, row in enumerate(rows):
            self.assertEqual(display_width(row), drawn_width(), f"row {index}: {row!r}")

    def test_a_row_wider_than_the_screen_is_cut(self) -> None:
        rows = painted(["가" * 200] + [""] * (ROWS - 1))
        self.assertEqual(display_width(rows[0]), drawn_width())

    def test_a_row_that_cannot_be_split_evenly_still_fits(self) -> None:
        # A wide character straddling the last column must not spill over it.
        rows = painted(["a" + "가" * 200] + [""] * (ROWS - 1))
        self.assertLessEqual(display_width(rows[0]), drawn_width())

    def test_the_whole_screen_is_covered(self) -> None:
        rows = painted(["one", "two"])
        self.assertEqual(len(rows), ROWS)


class WrapModeTests(unittest.TestCase):
    """Turning wrapping off has to reach the console, not just the terminal.

    \x1b[?7l is ignored here: a 140-column line still wrapped on a 120-column
    screen, which is what smeared the frame in the first place.
    """

    def test_the_wrap_flag_is_cleared_and_the_others_kept(self) -> None:
        wanted = console_module._wrap_free_mode(0x0003)
        self.assertFalse(wanted & 0x0002, "the console may still wrap at the last column")
        self.assertTrue(wanted & 0x0004, "escape sequences stopped being processed")
        self.assertTrue(wanted & 0x0001)


class EveryViewFitsTests(unittest.TestCase):
    """Producers must not hand the frame a line the screen cannot hold."""

    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()

    def assert_fits(self, width: int, height: int) -> None:
        for index, line in enumerate(self.app.render(width, height)):
            self.assertLessEqual(
                display_width(line), width, f"line {index}: {strip_ansi(line)!r}"
            )

    def test_the_room_list_fits(self) -> None:
        self.assert_fits(40, 12)

    def test_a_conversation_fits(self) -> None:
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        self.app.open_room(room)
        self.assert_fits(40, 12)

    def test_a_digest_fits(self) -> None:
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        self.app.open_room(room)
        self.app._run_command("/sum 30m")
        self.assert_fits(40, 12)

    def test_the_friends_list_fits(self) -> None:
        self.app.show_friends = True
        self.assert_fits(40, 12)

    def test_a_narrow_screen_still_fits(self) -> None:
        self.assert_fits(24, 10)


if __name__ == "__main__":
    unittest.main()
