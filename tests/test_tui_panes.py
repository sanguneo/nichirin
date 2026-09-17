"""Several conversations on one screen.

What a pane costs is width, and what it must never cost is the guarantees the
single-room screen already had: every row still fits the terminal, one poll
still answers for every room, and the keyboard still belongs to exactly one
conversation.
"""

from __future__ import annotations

import unittest

from kamado.tui.app import MIN_PANE_COLUMNS, App
from kamado.tui.console import Key
from kamado.tui.layout import display_width, strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM, build_reader

WIDE = 150
NARROW = 60
HEIGHT = 18


class PaneCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app.load_rooms()

    def open(self, *room_ids: int) -> None:
        for room_id in room_ids:
            self.app.open_room(next(r for r in self.app.rooms if r.id == room_id))

    def frame(self, width: int = WIDE, height: int = HEIGHT) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(width, height)]


class OpeningTests(PaneCase):
    def test_a_second_room_joins_the_first_rather_than_replacing_it(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM, GROUP_ROOM])

    def test_both_conversations_are_on_the_screen_at_once(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        body = "\n".join(self.frame())
        self.assertIn("first", body, "the room opened first stopped being drawn")
        self.assertIn("group hello", body)

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


class WidthTests(PaneCase):
    def test_every_row_still_fits_the_screen(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        for width in (WIDE, 120, 100, NARROW):
            with self.subTest(width=width):
                for index, line in enumerate(self.frame(width)):
                    self.assertLessEqual(display_width(line), width, f"row {index}: {line!r}")

    def test_a_screen_too_narrow_for_two_shows_one(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.assertEqual(len(self.app._visible_panes(NARROW)), 1)

    def test_a_pane_is_never_narrower_than_a_sentence(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        for width in (WIDE, 120, 100, NARROW):
            with self.subTest(width=width):
                panes = self.app._visible_panes(width)
                self.assertGreaterEqual(
                    self.app._pane_width(width, len(panes)), min(MIN_PANE_COLUMNS, width)
                )

    def test_the_conversation_holding_the_keyboard_is_always_shown(self) -> None:
        """A pane the width cannot fit is hidden, never the focused one."""
        self.open(DIRECT_ROOM, GROUP_ROOM, NAMED_ROOM)
        for focus in range(3):
            with self.subTest(focus=focus):
                self.app.focus = focus
                self.assertIn(self.app.chats[focus], self.app._visible_panes(NARROW))

    def test_one_room_draws_the_screen_it_always_did(self) -> None:
        """A single conversation spends no row naming itself; the footer does."""
        self.open(DIRECT_ROOM)
        self.assertNotIn("▌", "\n".join(self.frame()))

    def test_a_room_open_but_off_screen_is_announced(self) -> None:
        """Otherwise nothing on screen says there is another room to tab to."""
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.assertIn("▌", "\n".join(self.frame(NARROW)))


class FocusTests(PaneCase):
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

    def test_what_is_typed_goes_to_the_room_holding_the_keyboard(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.TAB, "")
        for character in "안녕":
            self.app.handle("char", character)
        drafts = {chat.room.id: chat.draft for chat in self.app.chats}
        self.assertEqual(drafts, {DIRECT_ROOM: "안녕", GROUP_ROOM: ""})

    def test_tab_is_not_a_character_when_only_one_room_is_open(self) -> None:
        self.open(DIRECT_ROOM)
        self.app.handle(Key.TAB, "")
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.draft, "")


class TargetTests(PaneCase):
    """One box, several conversations: it has to say where the message goes.

    Which pane looks focused is a question about a highlight. Who receives
    what is typed is not, and it is answered right above the box.
    """

    def prompt_row(self, width: int = WIDE) -> int:
        frame = self.frame(width)
        return next(index for index, line in enumerate(frame) if line.startswith("❯"))

    def test_the_box_names_the_room_it_will_send_to(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        above = self.frame()[self.prompt_row() - 1]
        self.assertIn("Peer Renamed, Friend Nick", above)

    def test_tab_moves_that_name_with_the_keyboard(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.TAB, "")
        above = self.frame()[self.prompt_row() - 1]
        self.assertNotIn("Friend Nick", above)
        self.assertIn("Peer Renamed", above)

    def test_one_room_spends_no_ink_naming_itself(self) -> None:
        self.open(DIRECT_ROOM)
        self.assertNotIn("Peer Renamed", self.frame()[self.prompt_row() - 1])

    def test_something_worth_saying_still_wins_the_line(self) -> None:
        """A refusal or a delivery mark matters more than the target's name."""
        self.open(DIRECT_ROOM, GROUP_ROOM)
        assert self.app.chat is not None
        self.app.chat.notice = "뭔가 잘못됐습니다"
        self.assertIn("뭔가 잘못됐습니다", self.frame()[self.prompt_row() - 1])


class ClosingTests(PaneCase):
    def test_escape_closes_one_pane_and_keeps_the_rest(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM])

    def test_closing_the_last_pane_returns_to_the_room_list(self) -> None:
        self.open(DIRECT_ROOM)
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)

    def test_a_closed_pane_keeps_what_was_half_written_in_it(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        for character in "쓰다 만 말":
            self.app.handle("char", character)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.drafts[GROUP_ROOM], "쓰다 만 말")

    def test_closing_a_pane_stops_following_only_that_room(self) -> None:
        self.open(DIRECT_ROOM, GROUP_ROOM)
        self.app.handle(Key.ESCAPE, "")
        assert self.app.watcher is not None
        self.assertEqual(self.app.watcher.watched_rooms(), [DIRECT_ROOM])


class OneWatcherTests(PaneCase):
    """A second conversation must not cost a second decryption.

    A poll that finds the database moved is the most expensive thing the loop
    does, and the fingerprint it checks covers the whole account — so every
    open room is followed by one watcher or the cost multiplies by the number
    of panes.
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


class OpenCommandTests(PaneCase):
    def test_open_adds_a_pane_by_name(self) -> None:
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
        """Adding a pane has to start somewhere, and every other way back to
        the list closes the conversation standing in front of it."""
        self.open(DIRECT_ROOM)
        self.app._run_command("/open")
        self.assertTrue(self.app.picking)
        self.assertEqual(len(self.app.chats), 1, "the open conversation was closed")
        body = "\n".join(self.frame())
        self.assertIn("Named Group", body, "the list is not on screen")
        self.assertNotIn("first", body, "the conversation is still drawn behind it")

    def test_picking_a_room_from_that_list_adds_a_pane(self) -> None:
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
            "Named Group", [room.title for room in self.app.visible_rooms()]
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

    def test_an_ambiguous_name_answers_with_its_candidates(self) -> None:
        self.open(DIRECT_ROOM)
        # Both the direct room and the group are titled after this person.
        self.app._run_command("/open Peer")
        assert self.app.chat is not None
        self.assertEqual(len(self.app.chats), 1)
        self.assertIn("개입니다", self.app.chat.notice)


if __name__ == "__main__":
    unittest.main()
