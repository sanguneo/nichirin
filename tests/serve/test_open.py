"""``open=true``: a remote read may ask the client to open a room it never loaded.

A room's key is in memory only once the client has opened that room, so a
read of any other room answered 503 ``KEY_UNAVAILABLE`` — and a remote caller,
unlike the interface and ``messages --open``, had no way to ask for it to be
opened. Opening marks the room read in the client, so it happens only when the
caller says so. No desktop is touched here.
"""

import unittest

from kanao.errors import ErrorCode, KanaoError
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server


class LockedRoom:
    """A room whose key appears once somebody has opened it."""

    def __init__(self) -> None:
        self.opened: list[int] = []
        self.reads = 0

    def open(self, room_id: int) -> None:
        self.opened.append(room_id)

    def context(self, room_id: int, limit: int):
        self.reads += 1
        if not self.opened:
            raise KanaoError(ErrorCode.KEY_UNAVAILABLE, "KakaoTalk has not opened that room")
        return {"room": {"id": str(room_id)}, "messages": []}

    def digest(self, room_id: int, span: str, limit: int, highlights: int):
        self.context(room_id, limit)
        return {"room": {"id": str(room_id)}, "span": span, "slices": [], "note": "…"}


class OpenTests(ServedCase):
    def serve_locked(self, opener: bool = True) -> LockedRoom:
        room = LockedRoom()
        self.serve(
            _start_server(
                StubSender(),
                context_provider=room.context,
                digest_provider=room.digest,
                room_opener=room.open if opener else None,
            )
        )
        return room

    def test_without_the_flag_a_locked_room_is_still_refused(self) -> None:
        room = self.serve_locked()
        status, body, _ = _request(self.base, "GET", "/context?roomId=7")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "KEY_UNAVAILABLE")
        self.assertEqual(room.opened, [], "a read opened a room nobody asked to open")

    def test_the_flag_opens_the_room_and_answers(self) -> None:
        room = self.serve_locked()
        status, body, _ = _request(self.base, "GET", "/context?roomId=7&open=true")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(room.opened, [7])
        self.assertEqual(room.reads, 2)

    def test_the_digest_opens_the_same_way(self) -> None:
        room = self.serve_locked()
        status, _, _ = _request(self.base, "GET", "/digest?roomId=7&open=true")
        self.assertEqual(status, 200)
        self.assertEqual(room.opened, [7])

    def test_a_readable_room_is_never_opened(self) -> None:
        room = self.serve_locked()
        room.opened.append(0)  # its key is already there
        status, _, _ = _request(self.base, "GET", "/context?roomId=7&open=true")
        self.assertEqual(status, 200)
        self.assertEqual(room.opened, [0])

    def test_a_flag_that_is_not_true_or_false_opens_nothing(self) -> None:
        room = self.serve_locked()
        status, body, _ = _request(self.base, "GET", "/context?roomId=7&open=yes")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual((room.opened, room.reads), ([], 0))

    def test_a_server_that_cannot_open_says_the_key_is_missing(self) -> None:
        room = self.serve_locked(opener=False)
        status, body, _ = _request(self.base, "GET", "/context?roomId=7&open=true")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "KEY_UNAVAILABLE")
        self.assertEqual(room.reads, 1)

    def test_a_room_that_will_not_open_is_explained(self) -> None:
        def refuse(room_id: int) -> None:
            raise KanaoError(ErrorCode.WINDOW_NOT_FOUND, "could not open that room")

        self.serve(_start_server(StubSender(), context_provider=LockedRoom().context,
                                 room_opener=refuse))
        status, body, _ = _request(self.base, "GET", "/context?roomId=7&open=true")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "WINDOW_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
