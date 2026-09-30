"""Several conversations open at once, one of them on screen.

A second tab costs no room: the conversation in front keeps the whole width,
and the strip above it says which rooms are open and which one holds the
keyboard. What it must not cost is the guarantees the single-room screen
already had: every row still fits the terminal, one poll still answers for
every room, and the keyboard still belongs to exactly one conversation.
"""

from __future__ import annotations

import unittest

from tests.platform import require_posix_console

require_posix_console()

from kamado.model import Message
from kamado.tui.app import App
from kamado.tui.console import Key
from kamado.tui.layout import TAB_SEPARATOR, display_width, strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM, PEER_ID, build_reader

WIDE = 150
NARROW = 60
CRAMPED = 40
HEIGHT = 18

DIRECT_TITLE = "Peer Renamed"
GROUP_TITLE = "Peer Renamed, Friend Nick"
NAMED_TITLE = "Named Group"
GROUP_LABEL = "Peer Renamed, Friend Ni…"
"""The group's title as the strip draws it: the cap cuts it before the badge would."""


class TabCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()

    def open(self, *room_ids: int) -> None:
        for room_id in room_ids:
            self.app.open_room(next(r for r in self.app.rooms if r.id == room_id))

    def frame(self, width: int = WIDE, height: int = HEIGHT) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(width, height)]

    def raw_strip(self, width: int = WIDE) -> str:
        """The strip as drawn, escapes included, for what colour has to say."""
        return self.app.render(width, HEIGHT)[0]

    def lit_title(self, width: int = WIDE) -> str:
        """The title the strip lit, read back out of the escape run that lit it."""
        _before, _mark, after = self.raw_strip(width).partition("\x1b[1m")
        return strip_ansi(after).split(TAB_SEPARATOR)[0].strip()

    def arrival(self, room_id: int, text: str = "새 메시지") -> None:
        """Hand one conversation a message the way a poll would."""
        chat = next(one for one in self.app.chats if one.room.id == room_id)
        message = Message(
            id=910000 + len(chat.messages),
            room_id=room_id,
            sender_id=PEER_ID,
            sender="tester",
            text=text,
            sent_at=None,
            from_me=False,
            type=1,
        )
        self.app._absorb(chat, [message], WIDE)


class OpeningTests(TabCase):
    def test_a_second_room_joins_the_first_rather_than_replacing_it(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM, GROUP_ROOM])

    def test_only_the_room_in_front_is_drawn(self) -> None:
        """The width is the whole point: a tab is not half a screen."""
        self.open(DIRECT_ROOM, GROUP_ROOM)
        body = "\n".join(self.frame())
        self.assertIn("group hello", body)
        self.assertNotIn("first", body, "the room behind the tab was drawn as well")

    def test_opening_an_open_room_moves_the_keyboard_to_it(self) -> None:
        """Twice on screen would be two viewports over one conversation."""
        self.open(DIRECT_ROOM, GROUP_ROOM, DIRECT_ROOM)
        self.assertEqual(len(self.app.chats), 2)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)

    def test_the_newest_room_takes_the_keyboard(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)


class WidthTests(TabCase):
    def test_every_row_still_fits_the_screen(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        for width in (WIDE, 120, 100, NARROW, CRAMPED):
            with self.subTest(width=width):
                for index, line in enumerate(self.frame(width)):
                    self.assertLessEqual(display_width(line), width, f"row {index}: {line!r}")


class StripTests(TabCase):
    def test_the_strip_appears_once_there_is_something_to_switch_to(self) -> None:
        self.open(DIRECT_ROOM)
        self.assertNotIn(DIRECT_TITLE, self.frame()[0], "a strip with one tab says nothing")
        self.open(GROUP_ROOM)
        first = self.frame()[0]
        self.assertIn(DIRECT_TITLE, first)
        self.assertIn(GROUP_LABEL, first)

    def test_one_room_draws_the_screen_it_always_did(self) -> None:
        """A single conversation still names itself in the footer, not above it."""
        self.open(DIRECT_ROOM)
        frame = self.frame()
        self.assertTrue(frame[0].startswith("enter"), "the hints row lost its place")
        self.assertIn(DIRECT_TITLE, frame[-1])

    def test_only_the_focused_tab_is_lit(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        self.assertEqual(self.raw_strip().count("\x1b[1m"), 1, "more than one tab is bold")

    def test_the_lit_tab_is_the_one_holding_the_keyboard(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.assertEqual(self.lit_title(), GROUP_LABEL)
        self.app.handle(Key.TAB, "")
        self.assertEqual(self.lit_title(), DIRECT_TITLE)

    def test_the_strip_counts_arrivals_waiting_in_another_tab(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.arrival(DIRECT_ROOM)
        self.assertIn(f"{DIRECT_TITLE} +1", self.frame()[0])

    def test_arrivals_in_the_room_in_front_are_not_counted(self) -> None:
        """Its viewport follows the newest line, so there is nothing to count."""
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.arrival(GROUP_ROOM)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.unseen, 0)
        self.assertNotIn("+1", self.frame()[0])

    def test_switching_to_a_tab_clears_what_it_was_holding(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.arrival(DIRECT_ROOM)
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)
        self.assertNotIn("+1", self.frame()[0])

    def test_a_tab_off_the_edge_is_counted_in_a_marker(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        self.assertEqual(self.app.focus, 2)
        self.app._focus_tab(1)
        strip = self.frame(CRAMPED)[0]
        self.assertIn("…1", strip, "the tab hidden on the left was not counted")
        self.assertIn("Named G…", strip, "the tab beside the focused one was dropped")


class FocusTests(TabCase):
    def test_tab_moves_the_keyboard_to_the_next_room(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)

    def test_tab_comes_back_round(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.TAB, "")
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)

    def test_shift_tab_walks_the_other_way(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        self.app.handle(Key.BACKTAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)

    def test_shift_tab_comes_back_round_the_other_way(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)
        self.app.handle(Key.BACKTAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM, "the first tab did not wrap")

    def test_what_is_typed_goes_to_the_room_holding_the_keyboard(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.TAB, "")
        for character in "안녕":
            self.app.handle("char", character)
        drafts = {chat.room.id: chat.draft for chat in self.app.chats}
        self.assertEqual(drafts, {DIRECT_ROOM: "안녕", GROUP_ROOM: ""})

    def test_a_draft_waits_in_its_own_tab(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        for character in "가족":
            self.app.handle("char", character)
        self.app.handle(Key.TAB, "")
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.draft, "가족")

    def test_tab_is_not_a_character_when_only_one_room_is_open(self) -> None:
        self.open(DIRECT_ROOM)
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.draft, "")

    def test_shift_tab_is_not_a_character_when_only_one_room_is_open(self) -> None:
        self.open(DIRECT_ROOM)
        self.app.handle(Key.BACKTAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.draft, "")
        self.assertEqual(len(self.app.chats), 1)


class NamingTests(TabCase):
    """One title on one screen, and the strip is where it belongs."""

    def prompt_row(self, width: int = WIDE) -> int:
        frame = self.frame(width)
        return next(index for index, line in enumerate(frame) if line.startswith("❯"))

    def test_the_composer_divider_no_longer_names_the_room(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        above = self.frame()[self.prompt_row() - 1]
        self.assertEqual(set(above), {"─"}, "the divider is no longer a plain rule")

    def test_the_footer_stops_naming_a_room_the_strip_already_names(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        footer = self.frame()[-1]
        self.assertNotIn("Peer Renamed", footer)
        self.assertIn("group", footer, "the footer lost what the strip cannot say")

    def test_something_worth_saying_still_wins_the_line(self) -> None:
        """A refusal or a delivery mark matters more than anything the strip says."""
        self.open(DIRECT_ROOM, GROUP_ROOM)
        assert self.app.chat is not None
        self.app.chat.notice = "뭔가 잘못됐습니다"
        self.assertIn("뭔가 잘못됐습니다", self.frame()[self.prompt_row() - 1])


class ClickTests(TabCase):
    def tab_columns(self, index: int) -> int:
        """A column inside the tab at *index*, measured on the painted frame."""
        self.app.render(WIDE, HEIGHT)
        return next(start for one, start, _columns in self.app._tab_geometry if one == index)

    def test_clicking_a_tab_switches_to_it(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        self.app.handle(Key.CLICK, f"0,{self.tab_columns(0)}")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)

    def test_clicking_the_rule_between_two_tabs_does_nothing(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        rule = self.tab_columns(1) - 2
        self.app.handle(Key.CLICK, f"0,{rule}")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, NAMED_ROOM, "a near miss chose a tab")

    def test_clicking_a_marker_does_nothing(self) -> None:
        """A marker counts tabs the width could not show; it is not one of them."""
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        self.app._focus_tab(1)
        self.app.render(CRAMPED, HEIGHT)
        self.app.handle(Key.CLICK, "0,0")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)

    def test_a_click_in_the_body_leaves_the_tab_alone(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.render(WIDE, HEIGHT)
        top = self.app._body_geometry[0] if self.app._body_geometry else 1
        self.app.handle(Key.CLICK, f"{top},{WIDE // 2}")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)

    def test_dragging_tab_reorders_tabs(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        self.assertEqual([c.room.id for c in self.app.chats], [DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM])
        col0 = self.tab_columns(0)
        col2 = self.tab_columns(2)

        # Drag from Tab 0 to Tab 2
        self.app.handle(Key.CLICK, f"0,{col0}")
        self.app.handle(Key.RELEASE, f"0,{col2}")

        self.assertEqual([c.room.id for c in self.app.chats], [GROUP_ROOM, NAMED_ROOM, DIRECT_ROOM])
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, DIRECT_ROOM)
        self.assertEqual(self.app.focus, 2)

    def test_dragging_tab_to_earlier_position(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        col0 = self.tab_columns(0)
        col2 = self.tab_columns(2)

        # Drag from Tab 2 to Tab 0
        self.app.handle(Key.CLICK, f"0,{col2}")
        self.app.handle(Key.RELEASE, f"0,{col0}")

        self.assertEqual([c.room.id for c in self.app.chats], [NAMED_ROOM, DIRECT_ROOM, GROUP_ROOM])
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, NAMED_ROOM)
        self.assertEqual(self.app.focus, 0)

    def test_drag_release_outside_tab_strip_does_not_reorder(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        col0 = self.tab_columns(0)
        col2 = self.tab_columns(2)

        # Press on Tab 0, release in body row 5
        self.app.handle(Key.CLICK, f"0,{col0}")
        self.app.handle(Key.RELEASE, f"5,{col2}")

        self.assertEqual([c.room.id for c in self.app.chats], [DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM])


class ClosingTests(TabCase):
    def test_escape_closes_one_tab_and_keeps_the_rest(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM])

    def test_closing_the_last_tab_returns_to_the_room_list(self) -> None:
        self.open(DIRECT_ROOM)
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)

    def test_a_closed_tab_keeps_what_was_half_written_in_it(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        for character in "쓰다 만 말":
            self.app.handle("char", character)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.drafts[GROUP_ROOM], "쓰다 만 말")

    def test_closing_a_tab_stops_following_only_that_room(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.ESCAPE, "")
        assert self.app.watcher is not None
        self.assertEqual(self.app.watcher.watched_rooms(), [DIRECT_ROOM])


class OneWatcherTests(TabCase):
    """A second conversation must not cost a second decryption.

    A poll that finds the database moved is the most expensive thing the loop
    does, and the fingerprint it checks covers the whole account — so every
    open room is followed by one watcher or the cost multiplies by the number
    of tabs.
    """

    def test_one_watcher_follows_every_open_room(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        assert self.app.watcher is not None
        self.assertEqual(
            sorted(self.app.watcher.watched_rooms()),
            sorted([DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM]),
        )

    def test_a_second_room_does_not_build_a_second_watcher(self) -> None:
        self.open(DIRECT_ROOM)
        first = self.app.watcher
        self.open(GROUP_ROOM)
        self.assertIs(self.app.watcher, first)


class OpenCommandTests(TabCase):
    def test_open_adds_a_tab_by_name(self) -> None:
        self.open(DIRECT_ROOM)
        self.app._run_command("/open Named Group")
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM, NAMED_ROOM])

    def test_a_name_matching_nothing_is_reported_rather_than_guessed(self) -> None:
        self.open(DIRECT_ROOM)
        self.app._run_command("/open 없는방")
        assert self.app.chat is not None
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("없습니다", self.app.chat.notice)

    def test_open_without_a_name_puts_the_room_list_up(self) -> None:
        """Adding a tab has to start somewhere, and every other way back to
        the list closes the conversation standing in front of it."""
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.assertTrue(self.app.picking)
        self.assertEqual(len(self.app.chats), 1, "the open conversation was closed")
        body = "\n".join(self.frame())
        self.assertIn(NAMED_TITLE, body, "the list is not on screen")
        self.assertNotIn("first", body, "the conversation is still drawn behind it")

    def test_picking_a_room_from_that_list_adds_a_tab(self) -> None:
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.app.selected = next(
            index for index, room in enumerate(self.app.visible_rooms())
            if room.id == NAMED_ROOM
        )
        self.app.handle(Key.ENTER, "")
        self.assertFalse(self.app.picking)
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM, NAMED_ROOM])

    def test_the_list_can_be_searched_before_picking(self) -> None:
        """Six hundred rooms is too many to arrow through."""
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.app.handle("char", "/")
        for character in "Named":
            self.app.handle("char", character)
        self.assertTrue(self.app.filtering)
        self.assertIn(
            NAMED_TITLE, [room.title for room in self.app.visible_rooms()]
        )

    def test_quitting_that_list_leaves_the_list_not_the_interface(self) -> None:
        """The list stands in front of conversations that are still open."""
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.app.handle("char", "q")
        self.assertTrue(self.app.running, "q closed the interface over an open room")
        self.assertFalse(self.app.picking)
        self.assertEqual(len(self.app.chats), 1)

    def test_q_still_quits_from_the_ordinary_room_list(self) -> None:
        self.app.handle("char", "q")
        self.assertFalse(self.app.running)

    def test_a_letter_typed_into_the_filter_is_not_a_shortcut(self) -> None:
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.app.handle("char", "/")
        self.app.handle("char", "q")
        self.assertTrue(self.app.picking, "the filter's own letter left the list")
        self.assertEqual(self.app.filter, "q")

    def test_leaving_that_list_goes_back_to_the_conversations(self) -> None:
        """Escape out of the list must not be read as escape out of the tool."""
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.app.handle(Key.ESCAPE, "")
        self.assertFalse(self.app.picking)
        self.assertTrue(self.app.running)
        self.assertEqual(len(self.app.chats), 1)

    def test_shift_tab_also_leaves_that_list(self) -> None:
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.app.handle(Key.BACKTAB, "")
        self.assertFalse(self.app.picking)
        self.assertTrue(self.app.running)

    def test_an_ambiguous_name_answers_with_its_candidates(self) -> None:
        self.open(DIRECT_ROOM)
        # Both the direct room and the group are titled after this person.
        self.app._run_command("/open Peer")
        assert self.app.chat is not None
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("개입니다", self.app.chat.notice)


if __name__ == "__main__":
    unittest.main()
