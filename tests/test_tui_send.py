"""Composer tests: the box, what typing does, and what sending refuses.

No desktop is touched. The sender is a stand-in that records calls and fails
the way the real one does, so a test cannot pass against an error type
production never raises.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.tui.app import REFRESH_SECONDS, App, _next_poll_at, _poll_due, _window, ChatState
from kakaoread.tui import console as console_module
from kakaoread.tui.console import (
    AUTOWRAP_OFF,
    AUTOWRAP_ON,
    HIDE_CURSOR,
    SHOW_CURSOR,
    Console,
    Key,
)
from kakaoread.tui.layout import display_width, strip_ansi
from kakaoread.tui.theme import Theme
from kakaoread.winauto import SendResult

from tests.fixtures import DIRECT_ROOM, build_reader

WIDTH = 80
HEIGHT = 24


class FakeSender:
    """Stands in for winauto.Sender, recording what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.calls: list[dict] = []
        self.failure: Exception | None = None

    def send(
        self,
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ) -> SendResult:
        self.calls.append(
            {
                "room": room,
                "open_if_needed": open_if_needed,
                "aliases": list(aliases),
                "hint": hint,
            }
        )
        if self.failure is not None:
            raise self.failure
        self.sent.append((room, text))
        return {"submitted": True, "dryRun": False, "room": room, "length": len(text)}


class ComposerCase(unittest.TestCase):
    """Shared fixture: one open room and a sender that never touches a desktop."""

    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.sender = FakeSender()
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False), sender=self.sender)
        self.app.load_rooms()
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        self.app.open_room(room)

    @property
    def chat(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def frame(self) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT)]

    def composer(self) -> str:
        return "\n".join(self.frame()[-5:-2])

    def type(self, written: str) -> None:
        for character in written:
            self.app.handle("char", character)

    def press_enter(self) -> None:
        """Press enter the way the loop does.

        A send is deferred so the screen can say it is happening before the
        window work blocks; the loop paints that frame and then flushes, and
        a test that only handled the key would assert against a send that had
        not been asked to run yet.
        """
        self.app.handle(Key.ENTER, "")
        self.app.flush_pending()

class ComposerTests(ComposerCase):
    def test_an_open_room_shows_a_bordered_message_box(self) -> None:
        box = self.composer()
        self.assertIn("❯", box)
        for corner in ("╭", "╮", "╰", "╯"):
            self.assertIn(corner, box)

    def test_the_box_is_closed_on_both_sides(self) -> None:
        middle = self.composer().split("\n")[1]
        self.assertTrue(middle.startswith("│"), middle[:12])
        self.assertTrue(middle.endswith("│"), middle[-12:])

    def test_the_room_list_has_no_message_box(self) -> None:
        self.app.chat = None
        self.assertNotIn("❯", "\n".join(self.frame()))

    def test_typing_reaches_the_box_rather_than_a_shortcut(self) -> None:
        self.type("q")
        self.assertTrue(self.app.running, "a letter quit the browser instead of being typed")
        self.assertEqual(self.chat.draft, "q")
        self.assertIn("q", self.composer())

    def test_backspace_removes_the_last_character(self) -> None:
        self.type("안녕")
        self.app.handle(Key.BACKSPACE, "")
        self.assertEqual(self.chat.draft, "안")

    def test_the_box_never_overflows_the_width(self) -> None:
        self.type("가" * 200)
        for line in self.app.render(WIDTH, HEIGHT):
            self.assertLessEqual(display_width(line), WIDTH)

    def test_enter_hands_the_text_to_the_client_and_clears_the_box(self) -> None:
        self.type("보낼 말")
        self.press_enter()
        self.assertEqual(self.sender.sent, [(self.chat.room.title, "보낼 말")])
        self.assertEqual(self.chat.draft, "")

    def test_nothing_is_sent_and_nothing_is_echoed_before_the_client_writes_it(self) -> None:
        loaded = len(self.chat.messages)
        self.type("보낼 말")
        self.press_enter()
        self.assertEqual(len(self.chat.messages), loaded)

    def test_an_empty_box_sends_nothing(self) -> None:
        self.press_enter()
        self.type("   ")
        self.press_enter()
        self.assertEqual(self.sender.sent, [])

    def test_a_slash_line_runs_a_command_instead_of_sending(self) -> None:
        self.type("/sum 30m")
        self.press_enter()
        self.assertEqual(self.sender.sent, [])

    def test_older_moved_from_a_letter_to_a_command(self) -> None:
        self.type("/older")
        self.press_enter()
        self.assertEqual(self.sender.sent, [])
        self.assertNotEqual(self.chat.notice, "")

    def test_a_refused_send_is_reported_and_keeps_the_room_open(self) -> None:
        self.sender.failure = KakaoReadError(ErrorCode.WINDOW_NOT_FOUND, "창이 없습니다")
        self.type("안녕")
        self.press_enter()
        self.assertIn("창이 없습니다", self.chat.notice)
        self.assertIsNotNone(self.app.chat)

    def test_a_native_failure_is_reported_rather_than_raised(self) -> None:
        self.sender.failure = OSError("PostMessage failed")
        self.type("안녕")
        self.press_enter()
        self.assertIn("PostMessage failed", self.chat.notice)

    def test_sending_never_re_reads_the_catalog(self) -> None:
        # Re-reading it cost up to 3.0 s per send once the snapshot was stale,
        # and sending is exactly what makes it stale.
        original = self.reader.rooms
        reads: list[int] = []

        def counted(limit: int | None = None) -> list:
            reads.append(1)
            return original(limit)

        self.reader.rooms = counted  # type: ignore[method-assign]
        self.type("안녕")
        self.press_enter()

        self.assertEqual(reads, [], "a send decrypted the catalog again")
        self.assertEqual(len(self.sender.sent), 1)

    def test_two_rooms_sharing_a_title_refuse_to_send(self) -> None:
        self.app.titles = [self.chat.room.title, self.chat.room.title]
        self.type("안녕")
        self.press_enter()
        self.assertEqual(self.sender.sent, [])
        self.assertIn("특정할 수 없습니다", self.chat.notice)


class CostTests(ComposerCase):
    """Typing must not pay for the conversation it is typed into."""

    def test_typing_does_not_rebuild_the_transcript(self) -> None:
        lines = self.app._transcript_lines(WIDTH)
        self.type("가")
        self.assertIs(self.app._transcript_lines(WIDTH), lines)

    def test_a_new_message_rebuilds_it(self) -> None:
        lines = self.app._transcript_lines(WIDTH)
        self.chat.messages.append(self.chat.messages[-1])
        self.assertIsNot(self.app._transcript_lines(WIDTH), lines)

    def test_a_narrower_screen_rebuilds_it(self) -> None:
        lines = self.app._transcript_lines(WIDTH)
        self.assertIsNot(self.app._transcript_lines(WIDTH - 10), lines)


class CaretTests(ComposerCase):
    """The caret is where an IME draws half-typed Hangul, so it must sit in the box."""

    def caret(self) -> tuple[int, int]:
        frame = self.app.render(WIDTH, HEIGHT)
        cursor = self.app.cursor
        self.assertIsNotNone(cursor)
        assert cursor is not None
        self.assertIn("❯", strip_ansi(frame[cursor[0]]))
        return cursor

    def test_the_caret_sits_on_the_prompt_row(self) -> None:
        row, column = self.caret()
        self.assertGreater(column, 0)
        self.assertLess(row, HEIGHT)

    def test_the_caret_follows_double_width_text(self) -> None:
        _row, before = self.caret()
        self.type("가")
        _row, after = self.caret()
        self.assertEqual(after - before, 2, "the caret ignored Hangul's second column")

    def test_the_room_list_parks_no_caret(self) -> None:
        self.app.chat = None
        self.app.render(WIDTH, HEIGHT)
        self.assertIsNone(self.app.cursor)


class ConsoleCaretTests(unittest.TestCase):
    def paint(self, cursor: tuple[int, int] | None) -> str:
        console = Console()
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            console.draw(["one line"], cursor)
        finally:
            sys.stdout = original
        return buffer.getvalue()

    def test_a_caret_is_positioned_and_shown(self) -> None:
        painted = self.paint((3, 7))
        self.assertIn("\x1b[4;8H", painted)
        self.assertIn(SHOW_CURSOR, painted)

    def test_without_a_caret_the_cursor_stays_hidden(self) -> None:
        painted = self.paint(None)
        self.assertIn(HIDE_CURSOR, painted)
        self.assertNotIn(SHOW_CURSOR, painted)


class ChatChromeTests(ComposerCase):
    """The room is named once, in the footer, not twice."""

    def test_the_top_row_does_not_repeat_the_room_name(self) -> None:
        self.assertNotIn(self.chat.room.title, self.frame()[0])

    def test_the_footer_still_names_the_room(self) -> None:
        self.assertIn(self.chat.room.title, self.frame()[-1])

    def test_the_member_count_survives_the_lost_title_row(self) -> None:
        self.assertIn("2명", self.frame()[-1])

    def test_the_reclaimed_row_goes_to_the_conversation(self) -> None:
        body = [line for line in self.frame()[:-5] if line.strip()]
        self.assertTrue(any("first" in line for line in body))


class PollSchedulingTests(unittest.TestCase):
    """Polling must never sit in front of the person typing."""

    def test_a_recent_keystroke_defers_the_poll(self) -> None:
        self.assertFalse(_poll_due(now=10.0, due_at=0.0, last_key_at=9.9))

    def test_a_pause_lets_the_poll_run(self) -> None:
        self.assertTrue(_poll_due(now=10.0, due_at=0.0, last_key_at=9.0))

    def test_a_poll_that_is_not_due_yet_waits(self) -> None:
        self.assertFalse(_poll_due(now=10.0, due_at=11.0, last_key_at=0.0))

    def test_a_cheap_poll_keeps_the_ordinary_interval(self) -> None:
        self.assertAlmostEqual(_next_poll_at(100.0, 0.0003), 100.0 + REFRESH_SECONDS)

    def test_an_expensive_poll_backs_off_behind_its_own_cost(self) -> None:
        self.assertAlmostEqual(_next_poll_at(100.0, 0.94), 100.94)


class ScrollWindowTests(unittest.TestCase):
    """The viewport must survive being scrolled past the top."""

    def test_scrolling_to_the_top_shows_a_full_screen(self) -> None:
        lines = [str(index) for index in range(100)]
        self.assertEqual(_window(lines, 10_000_000, 20), lines[:20])

    def test_an_ordinary_offset_still_counts_from_the_bottom(self) -> None:
        lines = [str(index) for index in range(100)]
        self.assertEqual(_window(lines, 5, 20), lines[75:95])

    def test_a_body_that_already_fits_still_scrolls_off_the_bottom(self) -> None:
        # This is what keeps arrivals below the fold in a quiet room.
        lines = ["a", "b", "c"]
        self.assertEqual(_window(lines, 1, 20), ["a", "b"])


class DigestAnchorTests(ComposerCase):
    def test_a_digest_opens_at_its_own_beginning(self) -> None:
        self.app._run_command("/sum 30m")
        body = "\n".join(self.frame())
        self.assertIn("[digest]", body, "the digest opened past its own header")


class FooterHonestyTests(ComposerCase):
    def test_the_room_list_no_longer_calls_itself_read_only(self) -> None:
        self.app.chat = None
        self.assertNotIn("read-only", self.frame()[-1])


class BrowserOpensTheRoomTests(ComposerCase):
    """Someone reading a room in the browser has already named their target."""

    def test_a_closed_room_is_opened_rather_than_refused(self) -> None:
        self.type("안녕")
        self.press_enter()
        self.assertTrue(self.sender.calls[0]["open_if_needed"])

    def test_the_title_the_window_carries_is_offered_as_an_alias(self) -> None:
        from tests.fixtures import SELF_ROOM, SELF_STORED_TITLE

        room = next(room for room in self.app.rooms if room.id == SELF_ROOM)
        self.app.open_room(room)
        self.type("안녕")
        self.press_enter()
        self.assertEqual(self.sender.calls[0]["aliases"], [SELF_STORED_TITLE])

    def test_the_rooms_place_in_the_list_is_offered_as_a_hint(self) -> None:
        # Every wrong row opens someone's conversation and marks it read.
        self.type("안녕")
        self.press_enter()
        self.assertEqual(
            self.sender.calls[0]["hint"], self.app.titles.index(self.chat.room.title)
        )

    def test_a_room_whose_titles_agree_needs_no_alias(self) -> None:
        self.type("안녕")
        self.press_enter()
        self.assertEqual(self.sender.calls[0]["aliases"], [])


class WrapTests(unittest.TestCase):
    """A frame as wide as the screen must not wrap into the next row.

    Writing the last cell of a line makes the terminal wrap, and the frame's
    own newline then lands a row late: the box drifted sideways and its right
    border fell off the screen.
    """

    def paint(self) -> str:
        console = Console()
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            console.__enter__()
            console.__exit__()
        finally:
            sys.stdout = original
        return buffer.getvalue()

    def test_wrapping_is_turned_off_while_the_interface_owns_the_screen(self) -> None:
        self.assertIn(AUTOWRAP_OFF, self.paint())

    def test_wrapping_is_given_back_on_the_way_out(self) -> None:
        painted = self.paint()
        self.assertIn(AUTOWRAP_ON, painted)
        self.assertLess(painted.index(AUTOWRAP_OFF), painted.index(AUTOWRAP_ON))


class SentMarkTests(ComposerCase):
    """What was sent is marked beside the prompt, not announced in words."""

    def send(self, text: str = "안녕") -> None:
        self.type(text)
        self.press_enter()

    def test_nothing_is_announced_in_the_status_line(self) -> None:
        self.send()
        self.assertEqual(self.chat.notice, "")

    def test_a_queued_message_is_marked_at_the_far_right(self) -> None:
        self.send()
        middle = self.composer().split("\n")[1]
        self.assertIn("*", middle)
        self.assertGreater(middle.index("*"), middle.index("❯"))
        # Columns, not string indexes: the placeholder is Hangul.
        self.assertEqual(display_width(middle[: middle.index("*")]), WIDTH - 2)

    def test_the_prompt_does_not_move_when_a_mark_appears(self) -> None:
        # The first attempt used emoji and shifted the prompt on every send.
        before = self.composer().index("❯")
        self.send()
        self.assertEqual(self.composer().index("❯"), before)

    def test_the_mark_becomes_a_tick_once_the_client_writes_it(self) -> None:
        self.send("보낸 말")
        self.chat.messages.append(
            replace(self.chat.messages[-1], id=99_999, text="보낸 말", from_me=True)
        )
        self.app._confirm_delivery(self.chat.messages[-1:])
        box = self.composer()
        self.assertIn("✓", box)
        self.assertNotIn("*", box)

    def test_the_box_keeps_its_width_with_a_mark(self) -> None:
        self.send()
        for line in self.app.render(WIDTH, HEIGHT):
            self.assertLessEqual(display_width(line), WIDTH)

    def test_typing_again_clears_a_stale_tick(self) -> None:
        self.send("보낸 말")
        self.chat.messages.append(
            replace(self.chat.messages[-1], id=99_998, text="보낸 말", from_me=True)
        )
        self.app._confirm_delivery(self.chat.messages[-1:])
        self.type("다")
        self.assertNotIn("✓", self.composer())

    def test_a_message_still_unseen_keeps_its_mark_while_the_next_is_typed(self) -> None:
        # Hiding it because a new line is being written would hide the one
        # fact worth knowing: that the last message has not come back yet.
        self.send()
        self.type("다")
        self.assertIn("*", self.composer())


class ScreenSizeTests(unittest.TestCase):
    """What is visible, not what the buffer holds.

    A console buffer is often wider than the window showing it — a scrollbar
    alone takes a couple of columns — and shutil reports the buffer, so every
    frame was drawn a few columns wider than the screen and lost its right
    edge.
    """

    def setUp(self) -> None:
        self.original = console_module._visible_size
        self.addCleanup(setattr, console_module, "_visible_size", self.original)

    def test_the_visible_window_wins_over_the_buffer(self) -> None:
        console_module._visible_size = lambda: (117, 30)
        self.assertEqual(Console().size, (117 - console_module.right_margin(), 30))

    def test_the_right_edge_is_left_to_the_scrollbar(self) -> None:
        console_module._visible_size = lambda: (100, 30)
        with patch.dict(os.environ, {console_module.RIGHT_MARGIN_VARIABLE: "3"}):
            self.assertEqual(Console().size, (97, 30))

    def test_a_terminal_without_one_can_take_the_columns_back(self) -> None:
        console_module._visible_size = lambda: (100, 30)
        with patch.dict(os.environ, {console_module.RIGHT_MARGIN_VARIABLE: "0"}):
            self.assertEqual(Console().size, (100, 30))

    def test_nonsense_in_the_variable_is_ignored(self) -> None:
        console_module._visible_size = lambda: (100, 30)
        with patch.dict(os.environ, {console_module.RIGHT_MARGIN_VARIABLE: "wide"}):
            self.assertEqual(Console().size, (100 - console_module.DEFAULT_RIGHT_MARGIN, 30))

    def test_an_unknown_window_falls_back_to_the_library(self) -> None:
        console_module._visible_size = lambda: None
        columns, rows = Console().size
        self.assertGreaterEqual(columns, 20)
        self.assertGreaterEqual(rows, 8)

    def test_a_tiny_window_is_still_drawable(self) -> None:
        console_module._visible_size = lambda: (3, 1)
        self.assertEqual(Console().size, (20, 8))


class DeliveryMarkTests(ComposerCase):
    """The mark has to tell three states apart, and one of them is doubt."""

    def send(self, text: str) -> None:
        self.type(text)
        self.press_enter()

    def arrive(self, text: str) -> None:
        self.app._confirm_delivery(
            [replace(self.chat.messages[-1], id=90_001, text=text, from_me=True)]
        )

    def test_a_message_that_comes_back_respaced_is_still_recognised(self) -> None:
        # The client is free to tidy what it stores; only the words are ours.
        self.send("오늘  저녁에\n봐요")
        self.arrive("오늘 저녁에 봐요")
        self.assertIn("✓", self.composer())

    def test_a_different_message_is_not_mistaken_for_it(self) -> None:
        self.send("안녕")
        self.arrive("안녕하세요")
        self.assertNotIn("✓", self.composer())

    def test_a_send_that_never_comes_back_stops_claiming_to_be_waiting(self) -> None:
        now = [1_000.0]
        self.app._clock = lambda: now[0]
        self.send("안녕")
        self.assertIn("*", self.composer())
        now[0] += 60.0
        self.assertIn("?", self.composer(), "an unanswered send still looked queued")

    def test_a_tick_is_not_undone_by_the_clock(self) -> None:
        now = [1_000.0]
        self.app._clock = lambda: now[0]
        self.send("안녕")
        self.arrive("안녕")
        now[0] += 600.0
        self.assertIn("✓", self.composer())


if __name__ == "__main__":
    unittest.main()
