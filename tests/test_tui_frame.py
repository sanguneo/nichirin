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

from kamado.tui import console as console_module
from kamado.tui.app import App
from kamado.tui.console import (
    CLEAR_BELOW,
    CLEAR_LINE,
    HIDE_CURSOR,
    HOME,
    SHOW_CURSOR,
    Console,
)
from kamado.tui.layout import display_width, strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, PEER_ID, build_reader

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


class RepaintTests(unittest.TestCase):
    """A redraw must cost the rows that changed, not the screen.

    The interface redraws after every keystroke and again whenever the key
    wait expires, so a whole-screen frame was rewritten eight times a second
    whether or not anything moved. Each of those ends in a cursor move, and a
    Hangul IME draws its composition over that cursor: typing Korean lagged
    behind the keyboard while ASCII, which composes nothing, did not.
    """

    def captured(self, action, size: tuple[int, int] = (COLUMNS, ROWS)) -> str:
        """Run *action* with a known screen and return what it wrote."""
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            with patch.object(console_module, "_visible_size", lambda: size):
                action()
        finally:
            sys.stdout = original
        return buffer.getvalue()

    def test_a_frame_the_screen_already_holds_is_not_written_again(self) -> None:
        console = Console()
        lines = ["첫 줄", "둘째 줄"]
        self.assertNotEqual(self.captured(lambda: console.draw(lines, (1, 2))), "")
        self.assertEqual(self.captured(lambda: console.draw(lines, (1, 2))), "")

    def test_only_the_row_that_changed_is_written(self) -> None:
        console = Console()
        self.captured(lambda: console.draw(["one", "two", "three"], (0, 0)))
        written = self.captured(lambda: console.draw(["one", "둘", "three"], (0, 0)))
        self.assertIn("둘", written)
        self.assertNotIn("three", written)
        self.assertIn("\x1b[2;1H", written)  # Addressed, not reached by wrapping.

    def test_a_caret_that_moved_costs_only_its_own_escape(self) -> None:
        console = Console()
        lines = ["안녕하세요"]
        self.captured(lambda: console.draw(lines, (0, 0)))
        written = self.captured(lambda: console.draw(lines, (0, 4)))
        self.assertEqual(written, "\x1b[1;5H" + SHOW_CURSOR)

    def test_a_resized_screen_is_painted_in_full_again(self) -> None:
        """Rows cut to the old width describe a screen that no longer exists."""
        console = Console()
        lines = ["one", "two"]
        self.captured(lambda: console.draw(lines))
        written = self.captured(lambda: console.draw(lines), size=(COLUMNS + 10, ROWS))
        self.assertTrue(written.startswith(HOME), written[:20])
        self.assertEqual(written.count("\r\n"), ROWS - 1)

    def test_entering_the_alternate_screen_forgets_what_was_painted(self) -> None:
        """The screen a fresh alternate buffer shows is blank, not the last frame."""
        console = Console()
        lines = ["one", "two"]
        self.captured(lambda: console.draw(lines))

        def cycle() -> None:
            with console:  # Not a tty here, so the terminal keeps its mode.
                pass

        with patch.object(sys, "stdin", io.StringIO()):
            self.captured(cycle)
        self.assertTrue(self.captured(lambda: console.draw(lines)).startswith(HOME))


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


class AddressedLinesTests(unittest.TestCase):
    """A quote above a reply, and a mark that costs the head no columns.

    Anything drawn left of a message moves the whole conversation sideways, so
    the mention mark lives in the gap the head already had between the time
    and the name. A mark with a column of its own would be invisible in a
    screenshot and obvious only as text landing one column late.
    """

    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()
        self.app.open_room(next(r for r in self.app.rooms if r.id == GROUP_ROOM))

    def transcript(self, width: int = 60) -> list[str]:
        return [strip_ansi(line) for line in self.app._transcript_lines(width)]

    def test_a_reply_shows_what_it_answers_on_the_line_above_it(self) -> None:
        lines = self.transcript()
        answer = next(index for index, line in enumerate(lines) if "그거 맞아요" in line)
        self.assertIn("나: 내가 남긴 줄", lines[answer - 1])

    def test_the_replier_is_named_beside_what_they_answered(self) -> None:
        """One head, the way the client stacks it: who, then what, then their words."""
        lines = self.transcript()
        quoted = next(line for line in lines if "나: 내가 남긴 줄" in line)
        ordinary = next(line for line in lines if "group hello" in line)
        self.assertIn("Friend", quoted)
        self.assertEqual(quoted.index("Friend"), ordinary.index("Friend"))
        self.assertEqual(quoted.index("나:"), ordinary.index("group hello"))

    def test_the_mark_belongs_to_the_answer_and_not_to_the_quotation(self) -> None:
        lines = self.transcript()
        answer = next(index for index, line in enumerate(lines) if "그거 맞아요" in line)
        self.assertNotIn("↳", lines[answer - 1])
        self.assertTrue(lines[answer].endswith("↳그거 맞아요"), lines[answer])

    def test_a_mention_of_the_user_is_marked(self) -> None:
        marked = next(line for line in self.transcript() if "이것 좀 봐줘" in line)
        self.assertIn("@", marked.split("Peer")[0])

    def test_neither_mark_moves_the_conversation_sideways(self) -> None:
        lines = self.transcript()
        ordinary = next(line for line in lines if "group hello" in line)
        mentioned = next(line for line in lines if "이것 좀 봐줘" in line)
        answered = next(line for line in lines if "그거 맞아요" in line)
        self.assertEqual(mentioned.index("Peer"), ordinary.index("Friend"))
        self.assertEqual(answered.index("그거 맞아요"), ordinary.index("group hello"))

    def test_the_quoted_line_is_cut_to_the_screen_like_any_other(self) -> None:
        for width in (24, 40, 60):
            with self.subTest(width=width):
                for line in self.app._transcript_lines(width):
                    self.assertLessEqual(display_width(line), width)


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

    def test_a_long_name_history_fits(self) -> None:
        """A `/real` answer is user data: any length, and emoji in it.

        Nicknames in an open chat routinely carry emoji, and a chain of five
        of them is far wider than a narrow screen. The notice has to be cut by
        the frame like anything else rather than pushing the border off.
        """
        from tests.test_names import feed

        self.reader.close()
        self.reader = build_reader(
            messages=[
                feed(10, 1_700_000_000, (PEER_ID, "🦞 아주아주긴합성닉네임을가진사람")),
                feed(11, 1_700_000_100, (PEER_ID, "🦞합성닉🐱")),
                feed(12, 1_700_000_200, (PEER_ID, "합성 닉네임")),
            ]
        )
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()
        self.app.open_room(next(room for room in self.app.rooms if room.id == DIRECT_ROOM))
        self.app._run_command("/real Peer Renamed")
        assert self.app.chat is not None
        self.assertIn("🦞", self.app.chat.notice)
        self.assert_fits(40, 12)
        self.assert_fits(24, 10)


if __name__ == "__main__":
    unittest.main()
