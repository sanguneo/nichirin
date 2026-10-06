"""What somebody else wrote reaches the screen as text, never as a command.

A message, a nickname and a room title are data. The only escapes a frame may
carry are the ones the renderer wrote itself - colour, the OSC 8 around a
link - and measuring past a foreign one is not the same as disarming it:
truncation carries escapes through on purpose, so a message holding
``ESC[2J`` used to reach the terminal whole.
"""

from __future__ import annotations

import sqlite3
import unittest

from tests.platform import require_windows_console

require_windows_console()

from kanao import paths
from kanao.tui.app import App
from kanao.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, PEER_ID, build_reader

CLEAR_SCREEN = "\x1b[2J"
SET_TITLE = "\x1b]0;"
BELL = "\x07"
CSI_C1 = "\x9b"
FOREIGN = (CLEAR_SCREEN, SET_TITLE, BELL, CSI_C1, "\x1b[31m")
GOOD_URL = "https://example.com/ok"


class ForeignControlTests(unittest.TestCase):
    def setUp(self) -> None:
        messages = [
            (5000, PEER_ID, 1, f"clear{CLEAR_SCREEN}then{BELL} {GOOD_URL}", None, 1_700_000_000, 0),
            (5001, PEER_ID, 1, f"title{SET_TITLE}pwned{BELL} and{CSI_C1}2J", None, 1_700_000_100, 0),
        ]
        self.reader = build_reader(messages=messages)
        self.addCleanup(self.reader.close)
        databases = self.reader._cache.databases
        users = databases[paths.USER_DATABASE]
        users.execute(
            "UPDATE talkUser SET friendNickName = ? WHERE userId = ?",
            (f"Evil\x1b[31mName{BELL}", PEER_ID),
        )
        users.commit()
        catalog: sqlite3.Connection = databases[paths.CATALOG_DATABASE]
        catalog.execute(
            "UPDATE chatRoomList SET chatRoomTitle = ?, useCustomChatRoomTitle = 1 WHERE chatId = ?",
            (f"Group{SET_TITLE}x{BELL}", GROUP_ROOM),
        )
        catalog.commit()
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()

    def assert_clean(self, frame: list[str]) -> None:
        joined = "".join(frame)
        for foreign in FOREIGN:
            with self.subTest(foreign=ascii(foreign)):
                self.assertNotIn(foreign, joined)

    def room(self, room_id: int):
        return next(room for room in self.app.rooms if room.id == room_id)

    def test_the_room_list_shows_titles_as_text(self) -> None:
        frame = self.app.render(120, 20)
        self.assert_clean(frame)
        self.assertIn("␛]0;", "".join(frame), "the escape is shown, not dropped")

    def test_a_conversation_shows_messages_and_names_as_text(self) -> None:
        self.app.open_room(self.room(DIRECT_ROOM))
        frame = self.app.render(120, 20)
        self.assert_clean(frame)
        joined = "".join(frame)
        self.assertIn("␛[2J", joined)
        self.assertIn("\x1b[38;2;", joined, "the renderer's own colour still gets through")
        self.assertIn(f"\x1b]8;;{GOOD_URL}\x1b\\", joined, "and so does its own link")

    def test_a_tab_strip_or_header_shows_titles_as_text(self) -> None:
        self.app.open_room(self.room(DIRECT_ROOM))
        self.app.open_room(self.room(GROUP_ROOM), beside=True)
        self.assert_clean(self.app.render(120, 20))

    def test_a_notice_quoting_somebody_else_is_shown_as_text(self) -> None:
        self.app.open_room(self.room(DIRECT_ROOM))
        assert self.app.chat is not None
        self.app.chat.notice = f"실패: {CLEAR_SCREEN}{BELL}"
        self.assert_clean(self.app.render(120, 20))

    def test_the_friend_list_shows_names_as_text(self) -> None:
        self.app.show_friends = True
        self.assert_clean(self.app.render(120, 20))


if __name__ == "__main__":
    unittest.main()
