"""Opening a room the client has not loaded, from inside the interface.

A room's key is in memory only once the client has opened that room, so the
list used to leave such rooms out entirely: 6 of 271 were shown. They are
listed now, marked, and enter has the client open one — held for a frame like
a send, because it takes a second or more. No desktop is touched here.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from kanao import paths
from kanao.errors import ErrorCode, KanaoError
from tests.platform import require_windows_console

require_windows_console()

from kanao.tui.app import App
from kanao.tui.console import Key
from kanao.tui.layout import strip_ansi
from kanao.tui.theme import Theme

from tests.fixtures import LOG_SCHEMA, UNREADABLE_ROOM, OWN_ID, _connect, build_reader

WIDTH = 100
HEIGHT = 30


class FakeOpener:
    """Stands in for winauto.Sender.resolve, recording which room it opened."""

    def __init__(self) -> None:
        self.opened: list[dict] = []
        self.failure: Exception | None = None

    def resolve(
        self, room: str, open_if_needed: bool = False, aliases=(), hint=None, search_rows=None,
        self_chat=False,
    ):
        self.opened.append({"room": room, "open_if_needed": open_if_needed, "hint": hint})
        if self.failure is not None:
            raise self.failure
        return None


class UnlockCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.opener = FakeOpener()
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app._sender = self.opener  # type: ignore[assignment]
        self.app.load_rooms()
        self.unlocks: list[int] = []
        self.key_appears = True

        def unlock(room_id: int, attempts: int = 8, settle=None) -> bool:
            # What the client does when its window opens: it loads the log,
            # and the next scan pairs its key.
            self.unlocks.append(room_id)
            if not self.key_appears:
                return False
            name = paths.chat_log_name(room_id)
            self.reader._cache.databases[name] = _connect(  # type: ignore[attr-defined]
                LOG_SCHEMA,
                {"chatLogs": [(7001, OWN_ID, 1, "열어서 읽은 말", None, 1_700_000_000, 0, None)]},
            )
            ring = self.reader.keyring
            key = bytes(32)
            self.reader._keyring = replace(
                ring, keys={**ring.keys, name: key, name.rsplit("/", 1)[-1]: key}
            )
            return True

        self.reader.unlock = unlock  # type: ignore[method-assign]

    def select(self, room_id: int) -> None:
        self.app.selected = next(
            index for index, room in enumerate(self.app.visible_rooms()) if room.id == room_id
        )

    def screen(self) -> str:
        return "\n".join(strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT))


class ListingTests(UnlockCase):
    def test_a_room_without_a_key_is_listed_and_marked(self) -> None:
        self.assertIn(UNREADABLE_ROOM, [room.id for room in self.app.rooms])
        self.assertIn("잠김", self.screen())

    def test_the_header_counts_what_can_be_read(self) -> None:
        readable = sum(1 for room in self.app.rooms if room.readable)
        self.assertIn(f"◉ {readable} readable", self.screen())


class OpeningTests(UnlockCase):
    def test_enter_waits_a_frame_before_touching_the_client(self) -> None:
        self.select(UNREADABLE_ROOM)
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.opener.opened, [], "the client was driven inside the keystroke")
        self.assertIn("여는 중", self.screen())

    def test_the_room_is_opened_in_the_client_and_then_read(self) -> None:
        self.select(UNREADABLE_ROOM)
        self.app.handle(Key.ENTER, "")
        self.app.flush_pending()
        self.assertEqual([call["room"] for call in self.opener.opened], ["Named Group"])
        self.assertTrue(self.opener.opened[0]["open_if_needed"])
        self.assertEqual(self.unlocks, [UNREADABLE_ROOM])
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, UNREADABLE_ROOM)
        self.assertEqual([message.text for message in self.app.chat.messages], ["열어서 읽은 말"])

    def test_a_readable_room_never_reaches_the_client(self) -> None:
        readable = next(room for room in self.app.rooms if room.readable)
        self.select(readable.id)
        self.app.handle(Key.ENTER, "")
        self.app.flush_pending()
        self.assertEqual(self.opener.opened, [])
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, readable.id)

    def test_a_window_that_cannot_be_found_is_explained(self) -> None:
        self.opener.failure = KanaoError(ErrorCode.WINDOW_NOT_FOUND, "no such window")
        self.select(UNREADABLE_ROOM)
        self.app.handle(Key.ENTER, "")
        self.app.flush_pending()
        self.assertIsNone(self.app.chat)
        self.assertIn("열기 실패", self.app.status)
        self.assertEqual(self.unlocks, [])

    def test_a_key_that_never_appears_is_explained(self) -> None:
        self.key_appears = False
        self.select(UNREADABLE_ROOM)
        self.app.handle(Key.ENTER, "")
        self.app.flush_pending()
        self.assertIsNone(self.app.chat)
        self.assertIn("키가 아직", self.app.status)


if __name__ == "__main__":
    unittest.main()
