"""What ``make_live_send`` accepts as a room.

``openapi.json`` calls ``SendRequest.room`` "the room's title, or its id as a
string", and an HTTP body carries an id as a string like everything else. The
reader and the sender are stubbed here: what is under test is which room the
send path picks, not the client automation it hands the text to.
"""

from __future__ import annotations

import unittest
from typing import Any, Optional

import kanao.reader
import kanao.winauto
from kanao.errors import ErrorCode, KanaoError
from kanao.serve.backends import make_live_send

DIRECT = 1001
GROUP = 1002
NUMERIC_TITLE = 2002
SELF = 1003


class _Room:
    def __init__(self, room_id: int, title: str, room_type: str = "DirectChat") -> None:
        self.id = room_id
        self.title = title
        self.type = room_type
        self.readable = True

    def as_json(self) -> dict[str, Any]:
        return {"id": str(self.id), "title": self.title}


class _StubReader:
    """The queries the send path makes, over a fixed catalog."""

    rooms_listed: list[_Room] = []

    def __init__(self, **_: Any) -> None:
        self.keyring = object()

    def __enter__(self) -> "_StubReader":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def room(self, room_id: int) -> Optional[_Room]:
        return next((r for r in self.rooms_listed if r.id == room_id), None)

    def rooms(self) -> list[_Room]:
        return list(self.rooms_listed)

    def resolve(self, target: str) -> list[_Room]:
        return [r for r in self.rooms_listed if r.title == target]

    def stored_title(self, _room_id: int) -> Optional[str]:
        return None

    def search_position(self, _room_id: int, query=None, rooms=None, listed=None) -> int:
        return 0


class _StubSender:
    """Records the title it was asked to type into and reports a dry run."""

    last_title: Optional[str] = None
    last_where: dict[str, Any] = {}

    def send(self, title: str, text: str, **where: Any) -> dict[str, Any]:
        _StubSender.last_title = title
        _StubSender.last_where = where
        return {"ok": True, "submitted": False, "dryRun": True, "room": title,
                "length": len(text)}


class SendRoomArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        _StubReader.rooms_listed = [
            _Room(DIRECT, "가상 관측소"),
            _Room(GROUP, "가상 합주단"),
            _Room(NUMERIC_TITLE, "2002"),
            _Room(SELF, "나와의 채팅", "MemoChat"),
        ]
        _StubSender.last_title = None
        self._reader = kanao.reader.Reader
        self._sender = kanao.winauto.make_sender
        kanao.reader.Reader = _StubReader  # type: ignore[assignment]
        kanao.winauto.make_sender = lambda: _StubSender()  # type: ignore[assignment]
        self.send = make_live_send()

    def tearDown(self) -> None:
        kanao.reader.Reader = self._reader  # type: ignore[assignment]
        kanao.winauto.make_sender = self._sender  # type: ignore[assignment]

    def test_an_id_as_a_string_names_the_room_it_is_the_id_of(self) -> None:
        result = self.send(str(DIRECT), "안녕", dry_run=True)

        self.assertEqual(result["roomId"], str(DIRECT))
        self.assertEqual(_StubSender.last_title, "가상 관측소")

    def test_a_title_still_names_its_room(self) -> None:
        result = self.send("가상 합주단", "안녕", dry_run=True)

        self.assertEqual(result["roomId"], str(GROUP))
        self.assertEqual(_StubSender.last_title, "가상 합주단")

    def test_a_room_whose_title_is_a_number_is_still_found_by_that_title(self) -> None:
        result = self.send("2002", "안녕", dry_run=True)

        self.assertEqual(result["roomId"], str(NUMERIC_TITLE))
        self.assertEqual(_StubSender.last_title, "2002")

    def test_digits_naming_neither_an_id_nor_a_title_are_refused(self) -> None:
        with self.assertRaises(KanaoError) as raised:
            self.send("9999", "안녕", dry_run=True)

        self.assertEqual(raised.exception.code, ErrorCode.CHAT_NOT_FOUND)

    def test_only_the_self_chat_is_opened_from_the_friends_list(self) -> None:
        # The account's own profile opens the self-chat and nothing else (#87).
        self.send("나와의 채팅", "안녕", dry_run=True)
        self.assertIs(_StubSender.last_where.get("self_chat"), True)

        self.send("가상 합주단", "안녕", dry_run=True)
        self.assertIs(_StubSender.last_where.get("self_chat"), False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
