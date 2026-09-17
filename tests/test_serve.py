"""Serve-mode tests: remote HTTP access to the send + read + stream paths.

A stub sender and a stub reader stand in for the Win32 backend so the
envelope, auth, rate limit, context resolution, and SSE streaming behaviour
are proven on Linux too. The real ``Sender`` and ``Reader`` satisfy the same
protocol the tests pin here.
"""

import json
import queue
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from kanao.errors import ErrorCode, KanaoError


TOKEN = "synthetic-test-token-0123456789"


class StubSender:
    """Mimics winauto.Sender.send without touching a desktop."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(
        self,
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ):
        if room == "Ambiguous room":
            raise KanaoError(
                ErrorCode.AMBIGUOUS_CHAT,
                "multiple rooms match",
                candidates=["Ambiguous room", "Ambiguous room 2"],
            )
        if room == "Missing room":
            raise KanaoError(ErrorCode.WINDOW_NOT_FOUND, "no open window")
        self.calls.append({"room": room, "text": text, "dry_run": dry_run})
        return {
            "submitted": not dry_run,
            "dryRun": dry_run,
            "room": room,
            "length": len(text),
            "clearedDraft": False,
        }


class StubMessage:
    """Mimics model.Message just enough for SSE serialization."""

    def __init__(self, id: int, room_id: int, sender: str, text: str, sent_at: int = 0) -> None:
        self.id = id
        self.room_id = room_id
        self.sender = sender
        self.text = text
        self.sent_at = sent_at

    def as_json(self) -> dict:
        return {
            "id": str(self.id),
            "roomId": str(self.room_id),
            "sender": self.sender,
            "text": self.text,
            "sentAt": self.sent_at,
        }


class StubWatcher:
    """Mimics watch.Watcher by draining a queue of new message batches."""

    def __init__(self, batches: list[list[StubMessage]]) -> None:
        self._batches = [list(batch) for batch in batches]
        self._polls = 0

    def prime(self) -> None:
        pass

    def poll(self) -> list[StubMessage]:
        if self._polls < len(self._batches):
            batch = self._batches[self._polls]
        else:
            batch = []
        self._polls += 1
        return batch


def _start_server(sender, *, rooms=None, watcher=None, token=TOKEN, min_interval=0.0):
    from kanao import serve

    handler = serve.build_handler(
        sender.send,
        token=token,
        min_interval=min_interval,
        rooms=rooms or [],
        watcher_factory=(lambda: watcher) if watcher is not None else None,
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _request(base, method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, payload, dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return error.code, payload, dict(error.headers)


def _read_sse(base, path, *, token=TOKEN, timeout=1.5, max_events=10):
    """Stream SSE events from *path* until *max_events* are collected or *timeout* elapses."""

    request = urllib.request.Request(base + path, headers={"Accept": "text/event-stream"})
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    events: list[dict[str, str]] = []
    response = urllib.request.urlopen(request, timeout=timeout)
    try:
        deadline = time.monotonic() + timeout
        kind = ""
        data: list[str] = []
        while time.monotonic() < deadline and len(events) < max_events:
            chunk = response.read(1).decode("utf-8")
            if not chunk:
                break
            if chunk == "\n":
                if kind and data:
                    events.append({"event": kind, "data": "\n".join(data)})
                    if len(events) >= max_events:
                        break
                    kind = ""
                    data = []
            elif chunk == ":":
                # comment line, drop the rest of the line
                response.readline()
                kind = ""
                data = []
            else:
                line = chunk + response.readline().decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    kind = line[len("event: ") :]
                elif line.startswith("data: "):
                    data.append(line[len("data: ") :])
                else:
                    kind = ""
                    data = []
    finally:
        response.close()
    return events


class ServeTests(unittest.TestCase):
    # ---- health + auth -------------------------------------------------

    def test_health_needs_no_token(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, body, _ = _request(base, "GET", "/health", token=None)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
        finally:
            server.shutdown()

    def test_send_without_token_is_rejected(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, body, _ = _request(base, "POST", "/send", {"room": "r", "text": "t"}, token=None)
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        finally:
            server.shutdown()

    def test_send_with_wrong_token_is_rejected(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, body, _ = _request(base, "POST", "/send", {"room": "r", "text": "t"}, token="wrong")
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        finally:
            server.shutdown()

    # ---- send ----------------------------------------------------------

    def test_send_happy_path_returns_the_sender_envelope(self) -> None:
        sender = StubSender()
        server, base = _start_server(sender)
        try:
            status, body, _ = _request(base, "POST", "/send", {"room": "Synthetic", "text": "hello"})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertTrue(body["submitted"])
            self.assertEqual(body["room"], "Synthetic")
            self.assertEqual(body["length"], 5)
            self.assertEqual(sender.calls[0]["room"], "Synthetic")
        finally:
            server.shutdown()

    def test_send_missing_text_is_a_bad_request(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, body, _ = _request(base, "POST", "/send", {"room": "Synthetic"})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        finally:
            server.shutdown()

    def test_ambiguous_room_surfaces_candidates(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, body, _ = _request(base, "POST", "/send", {"room": "Ambiguous room", "text": "hello"})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "AMBIGUOUS_CHAT")
            self.assertEqual(len(body["candidates"]), 2)
        finally:
            server.shutdown()

    def test_back_to_back_sends_are_rate_limited(self) -> None:
        server, base = _start_server(StubSender(), min_interval=60.0)
        try:
            status, _, _ = _request(base, "POST", "/send", {"room": "Synthetic", "text": "one"})
            self.assertEqual(status, 200)
            status, body, _ = _request(base, "POST", "/send", {"room": "Synthetic", "text": "two"})
            self.assertEqual(status, 429)
            self.assertEqual(body["error"]["code"], "RATE_LIMITED")
        finally:
            server.shutdown()

    # ---- rooms + context ----------------------------------------------

    def test_rooms_lists_every_room_with_serialized_titles(self) -> None:
        rooms = [
            {"id": "1", "title": "Family", "type": "MultiChat", "activeMembers": 3, "lastLogId": "9"},
            {"id": "2", "title": "Self", "type": "MemoChat", "activeMembers": 1, "lastLogId": "4"},
        ]
        server, base = _start_server(StubSender(), rooms=rooms)
        try:
            status, body, _ = _request(base, "GET", "/rooms")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["rooms"]), 2)
            self.assertEqual(body["rooms"][0]["title"], "Family")
        finally:
            server.shutdown()

    def test_context_by_room_id_returns_messages(self) -> None:
        rooms = [{"id": "1", "title": "Family", "type": "MultiChat", "activeMembers": 3}]
        messages = [
            StubMessage(101, 1, "alice", "hi", 1700000000),
            StubMessage(102, 1, "bob", "hello", 1700000010),
        ]
        # Build a small context provider closure that mirrors context.read_context
        # shape: returns ``{"room": {...}, "messages": [...]}`` keyed by room id.
        def context_provider(room_id: int, limit: int):
            room = next(r for r in rooms if int(r["id"]) == room_id)
            return {"room": room, "messages": [m.as_json() for m in messages]}

        from kanao import serve

        handler = serve.build_handler(
            StubSender().send,
            token=TOKEN,
            rooms=rooms,
            context_provider=context_provider,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            status, body, _ = _request(base, "GET", "/context?roomId=1&limit=20")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["messages"]), 2)
            self.assertEqual(body["messages"][0]["text"], "hi")
        finally:
            server.shutdown()

    def test_context_unknown_room_is_not_found(self) -> None:
        from kanao import serve

        def context_provider(room_id: int, limit: int):
            raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"No conversation has id {room_id}")

        handler = serve.build_handler(
            StubSender().send,
            token=TOKEN,
            rooms=[{"id": "1", "title": "Family"}],
            context_provider=context_provider,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            status, body, _ = _request(base, "GET", "/context?roomId=99")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "CHAT_NOT_FOUND")
        finally:
            server.shutdown()

    # ---- SSE streaming -------------------------------------------------

    def test_stream_emits_new_messages_then_heartbeats(self) -> None:
        watcher = StubWatcher(
            [
                [StubMessage(1, 1, "alice", "first")],
                [StubMessage(2, 1, "alice", "second")],
                [],
            ]
        )
        from kanao import serve

        handler = serve.build_handler(
            StubSender().send,
            token=TOKEN,
            watcher_factory=lambda: watcher,
            poll_interval=0.05,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            events = _read_sse(base, "/stream", timeout=2.0, max_events=4)
            self.assertGreaterEqual(len(events), 3)
            self.assertEqual(events[0]["event"], "ready")
            payloads = [
                json.loads(event["data"]) for event in events if event["event"] == "message"
            ]
            self.assertEqual(payloads[0]["text"], "first")
            self.assertEqual(payloads[1]["text"], "second")
        finally:
            server.shutdown()

    def test_stream_requires_token(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, body, _ = _request(base, "GET", "/stream", token=None)
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        finally:
            server.shutdown()

    # ---- misc ----------------------------------------------------------

    def test_unknown_path_is_not_found(self) -> None:
        server, base = _start_server(StubSender())
        try:
            status, _, _ = _request(base, "GET", "/nope")
            self.assertEqual(status, 404)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()