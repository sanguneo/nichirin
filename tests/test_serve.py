"""Serve-mode tests: remote HTTP access to the send + read + stream paths.

A stub sender and a stub reader stand in for the Win32 backend so the
envelope, auth, rate limit, context resolution, and SSE streaming behaviour
are proven on Linux too. The real ``Sender`` and ``Reader`` satisfy the same
protocol the tests pin here.
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import HTTPServer

from kanao.errors import ErrorCode, KanaoError


TOKEN = "synthetic-test-token-0123456789"


class StubSender:
    """Mimics winauto.Sender.send without touching a desktop."""

    def __init__(self, fail_with=None) -> None:
        self.calls: list[dict] = []
        self.fail_with = fail_with
        self.entered = threading.Event()
        self.release = threading.Event()

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
        if room == "Block me":
            self.entered.set()
            self.release.wait(timeout=5)
        if self.fail_with is not None:
            raise self.fail_with
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

    def __init__(self, batches: list[list[StubMessage]], fail_on_poll: int = -1) -> None:
        self._batches = [list(batch) for batch in batches]
        self._polls = 0
        self._fail_on_poll = fail_on_poll
        self.closed = False

    def prime(self) -> None:
        pass

    def poll(self) -> list[StubMessage]:
        if self._polls == self._fail_on_poll:
            raise OSError("synthetic poll failure")
        if self._polls < len(self._batches):
            batch = self._batches[self._polls]
        else:
            batch = []
        self._polls += 1
        return batch

    def close(self) -> None:
        self.closed = True


class ServedCase(unittest.TestCase):
    """Starts one server per test and tears it down without leaking sockets."""

    server: HTTPServer
    thread: threading.Thread
    base: str

    def serve(self, handler) -> str:
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        return self.base

    def tearDown(self) -> None:
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            self.thread.join(timeout=5)
            server.server_close()


def _start_server(sender, *, rooms=None, watcher=None, token=TOKEN, min_interval=0.0,
                  rooms_provider=None, context_provider=None, poll_interval=0.05,
                  clock=None):
    from kanao import serve

    kwargs: dict = {
        "token": token,
        "min_interval": min_interval,
        "rooms": rooms if rooms_provider is None else None,
        "rooms_provider": rooms_provider,
        "context_provider": context_provider,
        "watcher_factory": (lambda: watcher) if watcher is not None else None,
        "poll_interval": poll_interval,
    }
    if rooms is None and rooms_provider is None:
        kwargs["rooms"] = []
    if clock is not None:
        kwargs["clock"] = clock
    return serve.build_handler(sender.send, **kwargs)


def _request(base, method, path, body=None, token=TOKEN, raw_headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if raw_headers:
        headers.update(raw_headers)
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
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


def _raw_request(base, method, path, raw_body: bytes, headers: dict):
    """Bypass urllib to control framing (missing length, chunked, truncation)."""
    host, _, port = base.partition("://")[2].partition(":")
    connection = HTTPConnection(host, int(port), timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{host}:{port}")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        if raw_body:
            connection.send(raw_body)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, payload
    finally:
        connection.close()


def _read_sse(base, path, *, token=TOKEN, timeout=1.5, max_events=10,
              extra_headers=None):
    """Stream SSE events from *path* until *max_events* or *timeout* elapses."""
    headers = {"Accept": "text/event-stream"}
    if extra_headers:
        headers.update(extra_headers)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, headers=headers)
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


class ServeTests(ServedCase):
    # ---- health + auth -------------------------------------------------

    def test_health_needs_no_token(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_send_without_token_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "r", "text": "t"}, token=None
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_send_with_wrong_token_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "r", "text": "t"}, token="wrong"
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_non_ascii_authorization_never_crashes(self) -> None:
        self.serve(_start_server(StubSender()))
        import socket

        host, _, port = self.base.partition("://")[2].partition(":")
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            sock.sendall(
                b"POST /send HTTP/1.1\r\n"
                b"Host: x\r\n"
                b"Authorization: Bearer caf\xe9-token-value-123456\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 26\r\n"
                b"Connection: close\r\n\r\n"
                b'{"room": "r", "text": "t"}'
            )
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        finally:
            sock.close()
        head, _, response_body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        body = json.loads(response_body.decode("utf-8"))
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_bearer_scheme_is_case_insensitive_and_spacing_tolerant(self) -> None:
        self.serve(_start_server(StubSender()))
        for header in (f"bearer {TOKEN}", f"BEARER   {TOKEN}  "):
            status, body, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": "hi"},
                raw_headers={"Authorization": header},
            )
            self.assertEqual(status, 200, header)
            self.assertTrue(body["ok"])

    def test_every_protected_route_requires_auth(self) -> None:
        self.serve(_start_server(StubSender(), rooms=[{"id": "1"}],
                                 watcher=StubWatcher([[]])))
        for method, path in [
            ("GET", "/rooms"), ("GET", "/context?roomId=1"),
            ("GET", "/stream"), ("POST", "/send"), ("PUT", "/send"),
            ("GET", "/nope"),
        ]:
            status, body, _ = _request(
                self.base, method, path,
                {"room": "r", "text": "t"} if method == "POST" else None,
                token=None,
            )
            self.assertEqual(status, 401, f"{method} {path}")
            if body is not None:
                self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_bad_tokens_are_rejected_at_build_time(self) -> None:
        from kanao import serve

        for bad in ("short", "has space in it 123456", "한글토큰열여섯글자이상입니다", "", None):
            with self.assertRaises(KanaoError, msg=repr(bad)):
                serve.build_handler(StubSender().send, token=bad)

    def test_zero_poll_interval_is_rejected_at_build_time(self) -> None:
        from kanao import serve

        for bad in (0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(KanaoError, msg=repr(bad)):
                serve.build_handler(StubSender().send, token=TOKEN, poll_interval=bad)

    def test_negative_min_interval_is_rejected_at_build_time(self) -> None:
        from kanao import serve

        for bad in (-1.0, float("inf"), float("nan"), True):
            with self.assertRaises(KanaoError, msg=repr(bad)):
                serve.build_handler(StubSender().send, token=TOKEN, min_interval=bad)

    # ---- body framing --------------------------------------------------

    def test_post_without_content_length_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        host, _, port = self.base.partition("://")[2].partition(":")
        connection = HTTPConnection(host, int(port), timeout=5)
        try:
            connection.putrequest("POST", "/send", skip_host=True,
                                  skip_accept_encoding=True)
            connection.putheader("Host", f"{host}:{port}")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.endheaders()
            connection.send(b'{"room": "r", "text": "t"}')
            response = connection.getresponse()
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_negative_content_length_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body = _raw_request(
            self.base, "POST", "/send", b"",
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "-5",
             "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_chunked_body_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body = _raw_request(
            self.base, "POST", "/send", b'{"room": "r", "text": "t"}',
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "24",
             "Transfer-Encoding": "chunked", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_invalid_utf8_body_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body = _raw_request(
            self.base, "POST", "/send", b'{"room": "\xff\xfe", "text": "t"}',
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "26",
             "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_truncated_body_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        import socket

        host, _, port = self.base.partition("://")[2].partition(":")
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            sock.sendall(
                f"POST /send HTTP/1.1\r\n"
                f"Host: x\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: 100\r\n"
                f"Connection: close\r\n\r\n".encode("ascii") + b'{"room":'
            )
            # Half-close: the client will send no more, so the server's
            # Content-Length wait must end in a 400, not a silent hang.
            sock.shutdown(socket.SHUT_WR)
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        finally:
            sock.close()
        head, _, response_body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        body = json.loads(response_body.decode("utf-8"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    # ---- send ----------------------------------------------------------

    def test_send_happy_path_returns_the_sender_envelope(self) -> None:
        sender = StubSender()
        self.serve(_start_server(sender))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "hello"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["submitted"])
        self.assertEqual(body["room"], "Synthetic")
        self.assertEqual(body["length"], 5)
        self.assertEqual(sender.calls[0]["room"], "Synthetic")

    def test_send_missing_text_is_a_bad_request(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(self.base, "POST", "/send", {"room": "Synthetic"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_send_string_flags_are_rejected_not_coerced(self) -> None:
        self.serve(_start_server(StubSender()))
        for flag in ("dryRun", "openRoom"):
            status, body, _ = _request(
                self.base, "POST", "/send",
                {"room": "Synthetic", "text": "hi", flag: "false"},
            )
            self.assertEqual(status, 400, flag)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_ambiguous_room_surfaces_candidates(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Ambiguous room", "text": "hello"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "AMBIGUOUS_CHAT")
        self.assertEqual(len(body["candidates"]), 2)

    def test_back_to_back_sends_are_rate_limited(self) -> None:
        self.serve(_start_server(StubSender(), min_interval=60.0))
        status, _, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "one"}
        )
        self.assertEqual(status, 200)
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "two"}
        )
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "RATE_LIMITED")

    def test_zero_min_interval_disables_the_limiter(self) -> None:
        sender = StubSender()
        self.serve(_start_server(sender, min_interval=0))
        for word in ("one", "two", "three"):
            status, _, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": word}
            )
            self.assertEqual(status, 200, word)
        self.assertEqual(len(sender.calls), 3)

    def test_dry_run_never_consumes_quota(self) -> None:
        self.serve(_start_server(StubSender(), min_interval=3600.0))
        status, _, _ = _request(
            self.base, "POST", "/send",
            {"room": "Synthetic", "text": "probe", "dryRun": True},
        )
        self.assertEqual(status, 200)
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "real"}
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["submitted"])

    def test_concurrent_sends_are_serialized_and_quota_holds(self) -> None:
        sender = StubSender()
        entered_holder: list = []
        barrier = threading.Barrier(2)
        results: list = []

        self.serve(_start_server(sender, min_interval=3600.0))

        def one(word: str) -> None:
            barrier.wait(timeout=5)
            results.append(
                _request(self.base, "POST", "/send", {"room": "Synthetic", "text": word})
            )
            entered_holder.append(word)

        first = threading.Thread(target=one, args=("aaa",), daemon=True)
        second = threading.Thread(target=one, args=("bbb",), daemon=True)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        statuses = sorted(status for status, _, _ in results)
        # One wins the lock (200), the other is quota-refused (429) — never two
        # real sends through the single input box, and never a 500.
        self.assertEqual(statuses, [200, 429])
        self.assertEqual(len(sender.calls), 1)

    def test_lock_is_released_after_a_sender_error(self) -> None:
        sender = StubSender(fail_with=KanaoError(ErrorCode.SEND_FAILED, "boom"))
        self.serve(_start_server(sender))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "x"}
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "SEND_FAILED")
        sender.fail_with = None
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "y"}
        )
        self.assertEqual(status, 200, body)

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

    def test_context_unknown_room_is_not_found(self) -> None:
        def context_provider(room_id: int, limit: int):
            raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"No conversation has id {room_id}")

        self.serve(_start_server(StubSender(), rooms=[{"id": "1", "title": "Family"}],
                                 context_provider=context_provider))
        status, body, _ = _request(self.base, "GET", "/context?roomId=99")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "CHAT_NOT_FOUND")

    # ---- SSE streaming -------------------------------------------------

    def test_stream_emits_new_messages_then_heartbeats(self) -> None:
        watcher = StubWatcher(
            [
                [StubMessage(1, 1, "alice", "first")],
                [StubMessage(2, 1, "alice", "second")],
                [],
            ]
        )
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=4)
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual(events[0]["event"], "ready")
        self.assertTrue(json.loads(events[0]["data"])["ok"])
        payloads = [
            json.loads(event["data"])["message"]
            for event in events if event["event"] == "message"
        ]
        self.assertEqual(payloads[0]["text"], "first")
        self.assertEqual(payloads[1]["text"], "second")
        pings = [event for event in events if event["event"] == "ping"]
        self.assertTrue(pings)
        self.assertTrue(json.loads(pings[0]["data"])["ok"])

    def test_stream_requires_token(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(self.base, "GET", "/stream", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_stream_without_watcher_is_a_service_error(self) -> None:
        from kanao import serve

        self.serve(serve.build_handler(StubSender().send, token=TOKEN,
                                       rooms=[], poll_interval=0.05))
        status, body, _ = _request(self.base, "GET", "/stream")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "READ_FAILED")

    def test_stream_poll_failure_uses_the_envelope(self) -> None:
        watcher = StubWatcher([[]], fail_on_poll=1)
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=3)
        kinds = [event["event"] for event in events]
        self.assertIn("ready", kinds)
        self.assertIn("error", kinds)
        error_event = next(event for event in events if event["event"] == "error")
        payload = json.loads(error_event["data"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "READ_FAILED")
        # No raw backend text leaks into the envelope.
        self.assertNotIn("synthetic", json.dumps(payload))

    def test_stream_disconnect_closes_the_watcher(self) -> None:
        watcher = StubWatcher([[StubMessage(1, 1, "alice", "x")]] * 50)
        self.serve(_start_server(StubSender(), watcher=watcher, poll_interval=0.01))
        events = _read_sse(self.base, "/stream", timeout=1.0, max_events=2)
        self.assertTrue(events)
        deadline = time.monotonic() + 5
        while not watcher.closed and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(watcher.closed)

    # ---- misc ----------------------------------------------------------

    def test_unknown_path_is_not_found(self) -> None:
        self.serve(_start_server(StubSender()))
        status, _, _ = _request(self.base, "GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
