"""Several conversations open, one in front: tabs rather than side-by-side panes.

Two conversations beside each other gave each half the width, and half a
width is where Hangul sentences break into fragments. Now the conversation in
front has the whole screen and the others are a strip of tabs above it. The
composer, the status line and the footer belong to the one in front - there
is a single caret and a single box - so what the strip has to make obvious is
which one that is, and what arrived in the others.
"""

from __future__ import annotations

import unittest
from typing import cast

from kanao.model import Message
from kanao.watch import Watcher
from tests.platform import require_windows_console

require_windows_console()

from kanao.tui import images
from kanao.tui.app import FOCUS_MARK, App, ChatState
from kanao.tui.console import Key
from kanao.tui.layout import ELLIPSIS, display_width, strip_ansi
from kanao.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, SELF_ROOM, build_reader

WIDTH = 140
HEIGHT = 30


class TabCase(unittest.TestCase):
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

    def focused(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def arrive(self, chat: ChatState, text: str) -> None:
        """Hand one room an arrival, the way its own watcher would."""
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
        # A tab only ever asks its watcher to poll, which is all this stub
        # does; the annotation names the real class because production has no
        # other kind.
        chat.watcher = cast(Watcher, _Arriving(fresh))
        self.app.refresh(WIDTH)


class OpeningTests(TabCase):
    """A second room joins the first rather than replacing it."""

    def test_opening_beside_keeps_what_was_open(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.assertEqual(len(self.app.chats), 2)
        self.assertIn("Peer Renamed", self.titles())

    def test_the_room_just_opened_comes_to_the_front(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.assertEqual(self.focused().room.id, GROUP_ROOM)

    def test_opening_without_beside_still_replaces(self) -> None:
        """The room list opens one conversation, the way it always did."""
        self.open(GROUP_ROOM)
        self.assertEqual(len(self.app.chats), 1)

    def test_a_room_already_open_is_brought_forward_rather_than_doubled(self) -> None:
        """Two tabs of one room would each hold a draft of the same message."""
        self.open(GROUP_ROOM, beside=True)
        self.open(DIRECT_ROOM, beside=True)
        self.assertEqual(len(self.app.chats), 2)
        self.assertEqual(self.focused().room.id, DIRECT_ROOM)

    def test_open_by_name_adds_a_tab(self) -> None:
        self.app._run_command("/open Peer Renamed, Friend Nick")
        self.assertEqual(len(self.app.chats), 2)

    def test_an_ambiguous_name_is_refused_with_the_count(self) -> None:
        """Refused rather than resolved by taking the first match."""
        chat = self.focused()
        self.app._run_command("/open Peer")
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("2개", chat.notice)

    def test_a_name_that_matches_nothing_says_so(self) -> None:
        chat = self.focused()
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

    def test_choosing_from_that_list_adds_a_tab(self) -> None:
        self.app._run_command("/open")
        self.app.selected = next(
            index for index, room in enumerate(self.app.visible_rooms())
            if room.id == GROUP_ROOM
        )
        self.app.handle(Key.ENTER, "")
        self.assertEqual(len(self.app.chats), 2)
        self.assertFalse(self.app.adding)


class SwitchingTests(TabCase):
    """One box, one caret, so which room it types into must be movable."""

    def setUp(self) -> None:
        super().setUp()
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)

    def test_tab_moves_to_the_next_tab_and_wraps_round(self) -> None:
        self.app.handle(Key.TAB, "")
        self.assertEqual(self.focused().room.id, DIRECT_ROOM)
        self.app.handle(Key.TAB, "")
        self.assertEqual(self.focused().room.id, GROUP_ROOM)

    def test_shift_tab_walks_the_other_way_and_wraps_round(self) -> None:
        self.app.handle(Key.BACKTAB, "")
        self.assertEqual(self.focused().room.id, GROUP_ROOM)
        self.app.handle(Key.BACKTAB, "")
        self.app.handle(Key.BACKTAB, "")
        self.assertEqual(self.focused().room.id, SELF_ROOM)

    def test_each_tab_keeps_its_own_draft(self) -> None:
        self.app.handle("char", "셋")
        self.app.handle(Key.TAB, "")
        self.app.handle("char", "하나")
        self.app.handle(Key.BACKTAB, "")
        self.assertEqual(self.focused().room.id, SELF_ROOM)
        self.assertEqual(self.focused().draft, "셋")
        self.app.handle(Key.TAB, "")
        self.assertEqual(self.focused().draft, "하나")
        self.assertEqual(self.app.chats[1].draft, "")

    def test_the_composer_follows_the_tab_in_front(self) -> None:
        self.app.handle("char", "안")
        self.app.render(WIDTH, HEIGHT)
        self.app.handle(Key.TAB, "")
        frame = self.frame()
        composer = next(line for line in frame if "❯" in line)
        self.assertNotIn("안", composer)


class LoneRoomTests(unittest.TestCase):
    def test_a_lone_conversation_ignores_tab_and_still_draws_its_tab(self) -> None:
        reader = build_reader()
        self.addCleanup(reader.close)
        app = App(reader, theme=Theme(truecolor=True, enabled=False))
        app.load_rooms()
        app.open_room(next(room for room in app.rooms if room.id == DIRECT_ROOM))
        app.handle(Key.TAB, "")
        app.handle(Key.BACKTAB, "")
        chat = app.chat
        assert chat is not None
        self.assertEqual(chat.draft, "", "tab must not be swallowed as text")
        frame = [strip_ansi(line) for line in app.render(WIDTH, HEIGHT)]
        self.assertTrue(frame[0].startswith(FOCUS_MARK + "Peer Renamed"), frame[0])
        self.assertTrue(frame[1].startswith("enter"), "the hints sit under the strip")
        self.assertNotIn("Peer Renamed", frame[-1], "the strip names it; the footer need not")


class StripTests(TabCase):
    def test_a_second_tab_moves_nothing_below_the_strip(self) -> None:
        """The strip was added with the second room, pushing all of it down a row."""
        before = self.frame()
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.TAB, "")
        self.assertEqual(self.focused().room.id, DIRECT_ROOM)
        self.assertEqual(self.frame()[1:], before[1:])

    def test_the_strip_names_every_room_and_marks_one(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        strip = self.frame()[0]
        self.assertEqual(strip.count(FOCUS_MARK), 1)
        for title in self.titles():
            self.assertIn(title[:6], strip)
        self.assertLess(strip.index("Peer Renamed"), strip.index(FOCUS_MARK))

    def test_the_mark_moves_with_the_keyboard(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.TAB, "")
        strip = self.frame()[0]
        self.assertTrue(strip.startswith(FOCUS_MARK + "Peer Renamed"))

    def test_with_several_tabs_the_footer_does_not_repeat_the_title(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        footer = self.frame()[-1]
        self.assertNotIn(self.focused().room.title, footer)
        self.assertIn("loaded", footer)


class WidthTests(TabCase):
    """The conversation in front has the whole screen; nothing exceeds it."""

    def test_the_conversation_is_as_wide_as_the_screen(self) -> None:
        long = "가나다라마바사 " * 30
        self.arrive(self.focused(), long)
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.TAB, "")
        widest = max(display_width(line.rstrip()) for line in self.frame(WIDTH))
        self.assertGreater(widest, WIDTH // 2 + 10, "the body was drawn in half the screen")

    def test_no_line_is_wider_than_the_screen(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        for width in (24, 40, 60, 80, 89, 134, 200):
            with self.subTest(width=width):
                for index, line in enumerate(self.app.render(width, HEIGHT)):
                    self.assertLessEqual(
                        display_width(line), width, f"line {index}: {strip_ansi(line)!r}"
                    )

    def test_a_narrow_strip_keeps_the_tab_in_front_and_counts_the_rest(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        strip = self.frame(24)[0]
        self.assertIn(FOCUS_MARK, strip)
        self.assertIn(ELLIPSIS, strip)
        self.assertEqual(len(self.app.chats), 3, "a tab that does not fit stays open")


class ArrivalTests(TabCase):
    """A message belongs to the room it arrived in, not to the one in front."""

    def test_an_arrival_lands_in_its_own_tab(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        background = self.app.chats[0]
        self.arrive(background, "도착한 줄")
        self.assertIn("도착한 줄", [message.text for message in background.messages])
        self.assertNotIn(
            "도착한 줄", [message.text for message in self.app.chats[1].messages]
        )

    def test_a_background_tab_counts_what_arrived_and_says_so(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        background = self.app.chats[0]
        self.arrive(background, "하나")
        self.arrive(background, "둘")
        self.assertEqual(background.unseen, 2)
        self.assertIn("+2", self.frame()[0])

    def test_coming_to_the_tab_clears_its_count(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        background = self.app.chats[0]
        self.arrive(background, "하나")
        self.app.handle(Key.TAB, "")
        self.assertEqual(background.unseen, 0)
        self.assertNotIn("+1", self.frame()[0])

    def test_the_tab_in_front_at_its_newest_line_counts_nothing(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.arrive(self.focused(), "보이는 줄")
        self.assertEqual(self.focused().unseen, 0)

    def test_arrivals_in_a_tab_off_the_strip_reach_its_marker(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        self.arrive(self.app.chats[0], "멀리서")
        strip = self.frame(24)[0]
        self.assertIn(ELLIPSIS, strip)
        self.assertEqual(self.app.chats[0].unseen, 1)

    def test_a_delivery_tick_belongs_to_the_room_that_sent_it(self) -> None:
        """With two rooms open, the wrong tab must not claim the message."""
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


class ClosingTests(TabCase):
    """Escape closes the tab in front, not the screen."""

    def test_escape_closes_only_the_tab_in_front(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM])

    def test_closing_the_last_tab_goes_back_to_the_list(self) -> None:
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)

    def test_a_closed_tab_keeps_its_draft_for_next_time(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app.handle("char", "쓰")
        self.app.handle(Key.ESCAPE, "")
        self.open(GROUP_ROOM, beside=True)
        self.assertEqual(self.focused().draft, "쓰")

    def test_close_command_closes_the_tab_in_front(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.app._run_command("/close")
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM])

    def test_the_keyboard_lands_on_the_tab_to_the_left(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        self.open(SELF_ROOM, beside=True)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.focused().room.id, GROUP_ROOM)

    def test_only_the_closed_tab_stops_being_followed(self) -> None:
        self.open(GROUP_ROOM, beside=True)
        kept, closed = _Counting(), _Counting()
        self.app.chats[0].watcher = cast(Watcher, kept)
        self.app.chats[1].watcher = cast(Watcher, closed)
        self.app.handle(Key.ESCAPE, "")
        self.app.refresh(WIDTH)
        self.app.refresh(WIDTH)
        self.assertEqual((kept.polls, closed.polls), (2, 0))


class PhotoTests(unittest.TestCase):
    """A picture belongs to its conversation and goes with it when it goes back."""

    def setUp(self) -> None:
        from tests.test_tui_photos import PHOTO_MESSAGE, FakeStore

        self.photo = PHOTO_MESSAGE
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app._photos = FakeStore()
        self.app._protocol = images.SIXEL
        self.app._cell = (8, 16)
        self.app.load_rooms()
        self.app.open_room(next(room for room in self.app.rooms if room.id == GROUP_ROOM))
        self.app.render(80, 24)
        self.assertTrue(self.app.toggle_photo(self.photo))
        self.app.render(80, 24)

    def test_switching_tabs_withdraws_the_picture_and_gives_the_frame_up(self) -> None:
        self.assertTrue(self.app.placements)
        self.app.photo_escapes()
        self.app._images_moved = False
        self.app.open_room(
            next(room for room in self.app.rooms if room.id == DIRECT_ROOM), beside=True
        )
        self.app.render(80, 24)
        self.assertEqual(self.app.placements, [])
        self.app.photo_escapes()
        self.assertTrue(self.app._images_moved, "the old pixels would stay on screen")

    def test_coming_back_puts_it_where_the_conversation_starts(self) -> None:
        self.app.open_room(
            next(room for room in self.app.rooms if room.id == DIRECT_ROOM), beside=True
        )
        self.app.render(80, 24)
        self.app.handle(Key.TAB, "")
        self.app.render(80, 24)
        placed = [entry for entry in self.app.placements if entry[0] == self.photo]
        self.assertEqual(len(placed), 1)
        _id, row, column, _blob, columns, _rows, _crop = placed[0]
        self.assertGreaterEqual(row, 1, "the strip row is not the conversation's")
        self.assertLessEqual(column + columns, 80)


class _Arriving:
    """A watcher that reports one batch and then nothing, like a quiet room."""

    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages

    def poll(self) -> list[Message]:
        found, self._messages = self._messages, []
        return found


class _Counting:
    """A watcher that finds nothing and remembers being asked."""

    def __init__(self) -> None:
        self.polls = 0

    def poll(self) -> list[Message]:
        self.polls += 1
        return []


if __name__ == "__main__":
    unittest.main()
