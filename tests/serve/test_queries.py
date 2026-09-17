"""The read routes: ``/rooms``, ``/context`` and ``/digest``."""

import unittest

from kanao.errors import ErrorCode, KanaoError
from tests.serve.helpers import (
    ServedCase,
    StubMessage,
    StubSender,
    _request,
    _start_server,
)


class QueryTests(ServedCase):
    # ---- rooms + context ----------------------------------------------

    def test_rooms_lists_every_room_with_serialized_titles(self) -> None:
        rooms = [
            {"id": "1", "title": "Family", "type": "MultiChat",
             "activeMembers": 3, "lastLogId": "9"},
            {"id": "2", "title": "Self", "type": "MemoChat",
             "activeMembers": 1, "lastLogId": "4"},
        ]
        self.serve(_start_server(StubSender(), rooms=rooms))
        status, body, _ = _request(self.base, "GET", "/rooms")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["rooms"]), 2)
        self.assertEqual(body["rooms"][0]["title"], "Family")

    def test_context_by_room_id_returns_messages(self) -> None:
        rooms = [{"id": "1", "title": "Family", "type": "MultiChat", "activeMembers": 3}]
        messages = [
            StubMessage(101, 1, "alice", "hi", 1700000000),
            StubMessage(102, 1, "bob", "hello", 1700000010),
        ]

        def context_provider(room_id: int, limit: int):
            room = next(r for r in rooms if int(r["id"]) == room_id)
            return {"room": room, "messages": [m.as_json() for m in messages]}

        self.serve(_start_server(StubSender(), rooms=rooms,
                                 context_provider=context_provider))
        status, body, _ = _request(self.base, "GET", "/context?roomId=1&limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["text"], "hi")

    def test_digest_passes_the_slicing_arguments_through(self) -> None:
        """A remote caller asked to summarise should not have to page the room.

        Measured locally, the same newest 400 messages are 116,269 bytes
        through `messages` and 3,989 through `digest`; over a link that gap is
        the reason this endpoint exists.
        """
        seen = {}

        def digest_provider(room_id: int, span: str, limit: int, highlights: int):
            seen.update(roomId=room_id, span=span, limit=limit, highlights=highlights)
            return {"room": {"id": str(room_id)}, "span": span, "slices": [], "note": "…"}

        self.serve(_start_server(StubSender(), digest_provider=digest_provider))
        status, body, _ = _request(
            self.base, "GET", "/digest?roomId=1&span=2h&limit=200&highlights=5"
        )
        self.assertEqual(status, 200)
        self.assertEqual(seen, {"roomId": 1, "span": "2h", "limit": 200, "highlights": 5})
        self.assertIn("note", body)

    def test_digest_defaults_match_the_command(self) -> None:
        seen = {}

        def digest_provider(room_id: int, span: str, limit: int, highlights: int):
            seen.update(span=span, limit=limit, highlights=highlights)
            return {"slices": []}

        self.serve(_start_server(StubSender(), digest_provider=digest_provider))
        _request(self.base, "GET", "/digest?roomId=1")
        self.assertEqual(seen, {"span": "30m", "limit": 400, "highlights": 3})

    def test_digest_without_a_room_is_refused(self) -> None:
        self.serve(_start_server(StubSender(), digest_provider=lambda *_: {}))
        status, body, _ = _request(self.base, "GET", "/digest")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_digest_relays_a_refusal_from_the_slicer(self) -> None:
        """A span the slicer cannot take is the CLI's answer, not a crash."""

        def digest_provider(room_id: int, span: str, limit: int, highlights: int):
            raise KanaoError(ErrorCode.INVALID_REQUEST, "a slice cannot exceed 12h")

        self.serve(_start_server(StubSender(), digest_provider=digest_provider))
        status, body, _ = _request(self.base, "GET", "/digest?roomId=1&span=13h")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_digest_needs_a_token_like_every_other_read(self) -> None:
        self.serve(_start_server(StubSender(), digest_provider=lambda *_: {}))
        status, _, _ = _request(self.base, "GET", "/digest?roomId=1", token=None)
        self.assertEqual(status, 401)

    def test_context_unknown_room_is_not_found(self) -> None:
        def context_provider(room_id: int, limit: int):
            raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"No conversation has id {room_id}")

        self.serve(_start_server(StubSender(), rooms=[{"id": "1", "title": "Family"}],
                                 context_provider=context_provider))
        status, body, _ = _request(self.base, "GET", "/context?roomId=99")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "CHAT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
