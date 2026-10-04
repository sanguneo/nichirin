"""The read routes: ``/rooms``, ``/context`` and ``/digest``."""

import unittest

from kamado.errors import ErrorCode, KamadoError
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

        def context_provider(room_id: int, limit: int, *, show_covered=False):
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

        def digest_provider(room_id: int, span: str, limit: int, highlights: int, *, show_covered=False):
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

        def digest_provider(room_id: int, span: str, limit: int, highlights: int, *, show_covered=False):
            seen.update(span=span, limit=limit, highlights=highlights)
            return {"slices": []}

        self.serve(_start_server(StubSender(), digest_provider=digest_provider))
        _request(self.base, "GET", "/digest?roomId=1")
        self.assertEqual(seen, {"span": "30m", "limit": 400, "highlights": 3})

    def test_digest_without_a_room_is_refused(self) -> None:
        self.serve(_start_server(StubSender(), digest_provider=lambda *_, **__: {}))
        status, body, _ = _request(self.base, "GET", "/digest")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_digest_relays_a_refusal_from_the_slicer(self) -> None:
        """A span the slicer cannot take is the CLI's answer, not a crash."""

        def digest_provider(room_id: int, span: str, limit: int, highlights: int, *, show_covered=False):
            raise KamadoError(ErrorCode.INVALID_REQUEST, "a slice cannot exceed 12h")

        self.serve(_start_server(StubSender(), digest_provider=digest_provider))
        status, body, _ = _request(self.base, "GET", "/digest?roomId=1&span=13h")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_digest_needs_a_token_like_every_other_read(self) -> None:
        self.serve(_start_server(StubSender(), digest_provider=lambda *_, **__: {}))
        status, _, _ = _request(self.base, "GET", "/digest?roomId=1", token=None)
        self.assertEqual(status, 401)

    def test_context_unknown_room_is_not_found(self) -> None:
        def context_provider(room_id: int, limit: int, *, show_covered=False):
            raise KamadoError(ErrorCode.CHAT_NOT_FOUND, f"No conversation has id {room_id}")

        self.serve(_start_server(StubSender(), rooms=[{"id": "1", "title": "Family"}],
                                 context_provider=context_provider))
        status, body, _ = _request(self.base, "GET", "/context?roomId=99")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "CHAT_NOT_FOUND")

    # ---- how big a read a caller may ask for ---------------------------

    def _counting_providers(self):
        """Providers that record being called, so a refusal can be proven."""
        calls = []

        def context_provider(room_id: int, limit: int, *, show_covered=False):
            calls.append(("context", room_id, limit))
            return {"room": {"id": str(room_id)}, "messages": []}

        def digest_provider(room_id: int, span: str, limit: int, highlights: int, *, show_covered=False):
            calls.append(("digest", room_id, limit, highlights))
            return {"room": {"id": str(room_id)}, "span": span, "slices": [], "note": "…"}

        return calls, context_provider, digest_provider

    def test_a_negative_limit_never_reaches_the_reader(self) -> None:
        """`LIMIT -1` is SQLite for *no limit*, and a room can hold 445,714 rows.

        The CLI caps nothing because a person typing the command is the cap.
        A remote caller is not, so the refusal has to happen here — and before
        the reader is opened, or the damage is already done.
        """
        calls, context_provider, digest_provider = self._counting_providers()
        self.serve(_start_server(StubSender(), context_provider=context_provider,
                                 digest_provider=digest_provider))
        for path in ("/context?roomId=1&limit=-1", "/digest?roomId=1&limit=-1"):
            with self.subTest(path=path):
                status, body, _ = _request(self.base, "GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(calls, [])

    def test_counts_outside_their_range_are_refused_untouched(self) -> None:
        calls, context_provider, digest_provider = self._counting_providers()
        self.serve(_start_server(StubSender(), context_provider=context_provider,
                                 digest_provider=digest_provider))
        refused = (
            "/context?roomId=1&limit=0",
            "/context?roomId=1&limit=1001",
            "/context?roomId=1&limit=many",
            "/digest?roomId=1&limit=0",
            "/digest?roomId=1&limit=1001",
            "/digest?roomId=1&highlights=0",
            "/digest?roomId=1&highlights=21",
            "/digest?roomId=1&highlights=lots",
        )
        for path in refused:
            with self.subTest(path=path):
                status, body, _ = _request(self.base, "GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(calls, [])

    def test_the_largest_allowed_counts_still_go_through(self) -> None:
        calls, context_provider, digest_provider = self._counting_providers()
        self.serve(_start_server(StubSender(), context_provider=context_provider,
                                 digest_provider=digest_provider))
        self.assertEqual(_request(self.base, "GET", "/context?roomId=1&limit=1000")[0], 200)
        self.assertEqual(
            _request(self.base, "GET", "/digest?roomId=1&limit=1000&highlights=20")[0], 200
        )
        self.assertEqual(calls, [("context", 1, 1000), ("digest", 1, 1000, 20)])

    def test_a_room_id_sqlite_could_not_hold_is_a_bad_request(self) -> None:
        """Out of range is a bad request, not an OverflowError out of the driver."""
        calls, context_provider, digest_provider = self._counting_providers()
        self.serve(_start_server(StubSender(), context_provider=context_provider,
                                 digest_provider=digest_provider))
        huge = 2 ** 63
        for path in (f"/context?roomId={huge}", f"/digest?roomId={huge}"):
            with self.subTest(path=path):
                status, body, _ = _request(self.base, "GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(calls, [])
        # The largest id that fits is still an ordinary lookup.
        self.assertEqual(
            _request(self.base, "GET", f"/context?roomId={2 ** 63 - 1}")[0], 200
        )


if __name__ == "__main__":
    unittest.main()
