"""Finding a room in a long list, and not freezing while one is opened."""

from __future__ import annotations

import unittest

from tests.platform import require_windows_console

require_windows_console()

from kanao.tui.app import App
from kanao.tui.console import Key
from kanao.tui.layout import strip_ansi
from kanao.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, build_reader
from tests.test_tui_send import FakeSender

WIDTH = 80
HEIGHT = 24


class ListCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.sender = FakeSender()
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False), sender=self.sender)
        self.app.load_rooms()
        self.app.load_friends()

    def frame(self) -> str:
        return "\n".join(strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT))

    def type(self, written: str) -> None:
        for character in written:
            self.app.handle("char", character)


class RoomFilterTests(ListCase):
    """A list of rooms is scrolled through one arrow at a time without this."""

    def test_a_letter_is_still_a_shortcut_until_the_filter_is_asked_for(self) -> None:
        self.type("q")
        self.assertFalse(self.app.running, "q stopped quitting the list")

    def test_a_slash_turns_the_letters_that_follow_into_a_filter(self) -> None:
        self.type("/q")
        self.assertTrue(self.app.running, "a letter typed into the filter quit the browser")
        self.assertEqual(self.app.filter, "q")

    def test_the_list_narrows_to_what_matches(self) -> None:
        everything = len(self.app.visible_rooms())
        self.type("/Peer")
        narrowed = self.app.visible_rooms()
        self.assertLess(len(narrowed), everything)
        for room in narrowed:
            self.assertIn("peer", room.title.casefold())

    def test_the_match_is_case_insensitive(self) -> None:
        self.type("/peer")
        self.assertTrue(self.app.visible_rooms())

    def test_enter_opens_the_room_the_filter_left_selected(self) -> None:
        self.type("/Peer")
        wanted = self.app.visible_rooms()[0]
        self.app.handle(Key.ENTER, "")
        self.assertIsNotNone(self.app.chat)
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, wanted.id)

    def test_backspace_widens_the_filter_again(self) -> None:
        self.type("/Peer")
        for _ in range(4):
            self.app.handle(Key.BACKSPACE, "")
        self.assertEqual(self.app.filter, "")
        self.assertEqual(len(self.app.visible_rooms()), len(self.app.rooms))

    def test_escape_puts_the_whole_list_back(self) -> None:
        self.type("/Peer")
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.filter, "")
        self.assertTrue(self.app.running, "escape quit instead of clearing the filter")

    def test_escape_with_no_filter_still_leaves(self) -> None:
        self.app.handle(Key.ESCAPE, "")
        self.assertFalse(self.app.running)

    def test_the_filter_is_on_screen_while_it_is_on(self) -> None:
        self.type("/Peer")
        self.assertIn("Peer", self.frame())

    def test_a_filter_that_matches_nothing_selects_nothing_rather_than_crashing(self) -> None:
        self.type("/존재하지않는방")
        self.assertEqual(self.app.visible_rooms(), [])
        self.app.handle(Key.ENTER, "")
        self.assertIsNone(self.app.chat)


class FriendFilterTests(ListCase):
    """229 friends were reachable only by holding down an arrow key."""

    def setUp(self) -> None:
        super().setUp()
        self.app.handle("char", "f")

    def test_the_friend_list_narrows_too(self) -> None:
        everything = len(self.app.visible_friends())
        self.type("/Renamed")
        narrowed = self.app.visible_friends()
        self.assertLess(len(narrowed), everything)
        for friend in narrowed:
            self.assertIn("renamed", friend.name.casefold())

    def test_f_stops_being_a_shortcut_inside_the_filter(self) -> None:
        self.type("/f")
        self.assertTrue(self.app.show_friends, "a letter typed into the filter left the view")

    def test_leaving_the_view_forgets_the_filter(self) -> None:
        self.type("/Renamed")
        self.app.handle(Key.ESCAPE, "")   # clears the filter
        self.app.handle(Key.ESCAPE, "")   # leaves the friends view
        self.assertFalse(self.app.show_friends)
        self.assertEqual(self.app.filter, "")

    def test_enter_opens_the_friend_the_filter_left_selected(self) -> None:
        self.type("/Renamed")
        self.assertTrue(self.app.visible_friends())
        self.app.handle(Key.ENTER, "")
        self.assertIsNotNone(self.app.chat)


class PendingSendTests(unittest.TestCase):
    """Sending can open a window, which took 1.7 s of frozen screen."""

    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.sender = FakeSender()
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False), sender=self.sender)
        self.app.load_rooms()
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        self.app.open_room(room)

    def frame(self) -> str:
        return "\n".join(strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT))

    def type(self, written: str) -> None:
        for character in written:
            self.app.handle("char", character)

    def test_enter_does_not_send_before_the_screen_is_repainted(self) -> None:
        self.type("안녕")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.sender.sent, [], "the send ran before anything was drawn")
        self.assertEqual(self.app.pending_send, (DIRECT_ROOM, "안녕"))

    def test_the_screen_says_what_it_is_doing_first(self) -> None:
        self.type("안녕")
        self.app.handle(Key.ENTER, "")
        self.assertIn("보내는 중", self.frame())

    def test_flushing_performs_the_send_and_clears_the_notice(self) -> None:
        self.type("안녕")
        self.app.handle(Key.ENTER, "")
        self.app.flush_pending()
        chat = self.app.chat
        assert chat is not None, "the room under test is not open"
        self.assertEqual(self.sender.sent, [(chat.room.title, "안녕")])
        self.assertIsNone(self.app.pending_send)
        self.assertNotIn("보내는 중", self.frame())

    def test_a_command_still_runs_at_once(self) -> None:
        self.type("/older")
        self.app.handle(Key.ENTER, "")
        self.assertIsNone(self.app.pending_send)


if __name__ == "__main__":
    unittest.main()
