"""Several conversations at once: how many fit, which one the keyboard is in.

The composer, the status line and the footer belong to one conversation —
there is a single caret and a single box — so what the panes have to make
obvious is which one that is, and what a key does to the others.
"""

from __future__ import annotations

import unittest

from kanao.model import Message
from tests.platform import require_windows_console

require_windows_console()

from kanao.tui.app import MIN_PANE_COLUMNS, App, ChatState
from kanao.tui.console import Key
from kanao.tui.layout import display_width, strip_ansi
from kanao.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, SELF_ROOM, build_reader

WIDTH = 140
HEIGHT = 30


class PaneCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app.load_rooms()
        self.open(DIRECT_ROOM)

    def room(self, room_id: int):
        return next(room for room in self.app.rooms if room.id == room_id)

    def open(self, room_id: int, beside: bool = False) -> None:
        self.app.open_room(self.room(room_id), beside=beside)

    def frame(self, width: int = WIDTH, height: int = HEIGHT) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(width, height)]

    def titles(self) -> list[str]:
        return [chat.room.title for chat in self.app.chats]


class OpeningTests(PaneCase):
    """A second room joins the first rather than replacing it."""

    def test_opening_beside_keeps_what_was_open(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.assertEqual(len(self.app.chats), 2)
        self.assertIn("Peer Renamed", self.titles())

    def test_the_room_just_opened_takes_the_keyboard(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)

    def test_opening_without_beside_still_replaces(self) -> None:
        """The room list opens one conversation, the way it always did."""
        self.open(GROUP_ROOM)
        self.assertEqual(len(self.app.chats), 1)

    def test_a_room_already_open_is_focused_rather_than_doubled(self) -> None:
        """Two panes of one room would each hold a draft of the same message."""
        self.open(GROUP_ROOM, beside=True)
        self.open(DIRECT_ROOM, beside=True)
        self.assertEqual(len(self.app.chats), 2)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)

    def test_open_by_name_adds_a_pane(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app._run_command("/open Peer Renamed, Friend Nick")
        self.assertEqual(len(self.app.chats), 2)

    def test_an_ambiguous_name_is_refused_with_the_count(self) -> None:
        """Refused rather than resolved by taking the first match."""
        chat = self.app.chat
        assert chat is not None
        self.app._run_command("/open Peer")
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("2개", chat.notice)

    def test_a_name_that_matches_nothing_says_so(self) -> None:
        chat = self.app.chat
        assert chat is not None
        self.app._run_command("/open 없는 방 이름")
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("없습니다", chat.notice)

    def test_open_with_no_name_brings_the_list_in_front(self) -> None:
        """The conversations stay open behind it, and escape goes back."""
        self.app._run_command("/open")
        self.assertTrue(self.app.adding)
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("rooms", "\n".join(self.frame()))
        self.app.handle(Key.ESCAPE, "")
        self.assertFalse(self.app.adding)
        self.assertTrue(self.app.running, "escape out of /open must not quit")

    def test_choosing_from_that_list_adds_a_pane(self) -> None:
        self.app._run_command("/open")
        self.app.selected = next(
            index for index, room in enumerate(self.app.visible_rooms())
            if room.id == GROUP_ROOM
        )
        self.app.handle(Key.ENTER, "")
        self.assertEqual(len(self.app.chats), 2)
        self.assertFalse(self.app.adding)


class FocusTests(PaneCase):
    """One box, one caret, so which room it types into must be movable."""

    def test_tab_moves_the_keyboard_along(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)

    def test_tab_wraps_round(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.TAB, "")
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)

    def test_a_lone_conversation_ignores_tab(self) -> None:
        """Nothing to switch to, and tab must not be swallowed as text."""
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.draft, "")

    def test_typing_reaches_only_the_focused_room(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle("char", "안")
        self.assertEqual(self.app.chats[1].draft, "안")
        self.assertEqual(self.app.chats[0].draft, "")

    def test_the_focused_pane_is_marked_and_the_others_are_not(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        header = next(line for line in self.frame() if "▌" in line)
        self.assertEqual(header.count("▌"), 1)


class ClosingTests(PaneCase):
    """Escape closes the pane the keyboard is in, not the screen."""

    def test_escape_closes_only_the_focused_pane(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(len(self.app.chats), 1)
        self.assertEqual(self.app.chats[0].room.id, DIRECT_ROOM)

    def test_closing_the_last_pane_goes_back_to_the_list(self) -> None:
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)

    def test_a_closed_pane_keeps_its_draft_for_next_time(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle("char", "쓰")
        self.app.handle(Key.ESCAPE, "")
        self.open(GROUP_ROOM, beside=True)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.draft, "쓰")

    def test_close_command_closes_the_focused_pane(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app._run_command("/close")
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM])

    def test_the_keyboard_lands_on_the_pane_to_the_left(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        self.app.handle(Key.ESCAPE, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)


class WidthTests(PaneCase):
    """The window decides how many fit, and nothing may exceed it."""

    def test_a_narrow_screen_shows_one_of_two(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.assertEqual(len(self.app._visible_panes(80)), 1)

    def test_two_fit_once_there_is_room_for_two(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.assertEqual(len(self.app._visible_panes(MIN_PANE_COLUMNS * 2 + 1)), 2)

    def test_three_fit_on_a_wide_screen(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        self.assertEqual(len(self.app._visible_panes(MIN_PANE_COLUMNS * 3 + 2)), 3)

    def test_a_pane_that_does_not_fit_is_hidden_not_closed(self) -> None:
        """The reader opened it; a window getting smaller is not a decision."""
        self.open(GROUP_ROOM, beside=True)
        self.app.render(80, HEIGHT)
        self.assertEqual(len(self.app.chats), 2)

    def test_the_focused_pane_is_always_among_those_shown(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        shown = self.app._visible_panes(80)
        self.assertIn(self.app.chat, shown)

    def test_no_line_is_wider_than_the_screen(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        for width in (80, 89, 100, 134, 200):
            with self.subTest(width=width):
                for index, line in enumerate(self.app.render(width, HEIGHT)):
                    self.assertLessEqual(
                        display_width(line), width, f"line {index}: {strip_ansi(line)!r}"
                    )

    def test_one_conversation_is_drawn_without_a_header(self) -> None:
        """A room names itself above its transcript only to be told apart."""
        self.assertNotIn("▌", "\n".join(self.frame()))

    def test_a_room_open_but_off_screen_still_earns_the_header(self) -> None:
        """Otherwise nothing on screen says there is another room to tab to."""
        self.open(GROUP_ROOM, beside=True)
        narrow = [strip_ansi(line) for line in self.app.render(80, HEIGHT)]
        self.assertIn("▌", "\n".join(narrow))


class ArrivalTests(PaneCase):
    """A message belongs to the room it arrived in, not to the focused one."""

    def arrive(self, chat: ChatState, text: str) -> None:
        newest = chat.messages[-1]
        fresh = [
            Message(
                id=newest.id + 1,
                room_id=chat.room.id,
                sender_id=newest.sender_id,
                sender=newest.sender,
                text=text,
                type=1,
                sent_at=(newest.sent_at or 0) + 60,
                from_me=False,
            )
        ]
        chat.watcher = _Arriving(fresh)
        self.app.refresh(WIDTH)

    def test_an_arrival_lands_in_its_own_pane(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        background = self.app.chats[0]
        self.arrive(background, "도착한 줄")
        self.assertIn("도착한 줄", [message.text for message in background.messages])
        self.assertNotIn(
            "도착한 줄", [message.text for message in self.app.chats[1].messages]
        )

    def test_a_delivery_tick_belongs_to_the_room_that_sent_it(self) -> None:
        """With two rooms open, the wrong pane must not claim the message."""
        self.open(GROUP_ROOM, beside=True)
        focused = self.app.chats[1]
        focused.outbox = "보낸 말"
        background = self.app.chats[0]
        background.outbox = "보낸 말"
        self.app._confirm_delivery(
            background,
            [
                Message(
                    id=90_001,
                    room_id=background.room.id,
                    sender_id=1,
                    sender="me",
                    text="보낸 말",
                    type=1,
                    sent_at=1,
                    from_me=True,
                )
            ],
        )
        self.assertTrue(background.delivered)
        self.assertFalse(focused.delivered)


class _Arriving:
    """A watcher that reports one batch and then nothing, like a quiet room."""

    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages

    def poll(self) -> list[Message]:
        found, self._messages = self._messages, []
        return found


if __name__ == "__main__":
    unittest.main()
