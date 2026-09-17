"""Stubs and HTTP plumbing shared by the serve tests.

A stub sender, watcher and room stand in for the accessibility backend so the
envelope, auth, rate limit, query resolution and SSE streaming behaviour are
proven on any platform. The real ``Sender``, ``Reader`` and ``Watcher`` satisfy the same
protocol the tests pin through these.
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import HTTPServer

from kamado import serve
from kamado.errors import ErrorCode, KamadoError


TOKEN = "synthetic-test-token-0123456789"


class StubSender:
    """Mimics macauto.Sender.send without touching a desktop."""

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
            raise KamadoError(
                ErrorCode.AMBIGUOUS_CHAT,
                "multiple rooms match",
                candidates=["Ambiguous room", "Ambiguous room 2"],
            )
        if room == "Missing room":
            raise KamadoError(ErrorCode.WINDOW_NOT_FOUND, "no open window")
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
        self.primed = False
        self.resumed_from = None
        """The cursor `resume` was given, so a test can prove it was honoured."""
        self.shut = threading.Event()
        """Set by close(), so a test waits on the event, not on a clock."""

    def prime(self) -> None:
        self.primed = True

    def resume(self, cursor) -> None:
        self.resumed_from = dict(cursor)

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
        self.shut.set()


class QuietHTTPServer(serve.QuietDisconnects, HTTPServer):
    """One request at a time, and a client hanging up is not an error.

    Single-threaded on purpose: these tests are about one request's answer, and
    a thread per connection would only add a way for them to be flaky. The
    threaded server the command actually runs is exercised in
    ``test_server.py``. The quiet half is production's, not the tests' — the
    suite drops SSE connections deliberately, and stock ``socketserver`` prints
    a traceback for each one.
    """


class ServedCase(unittest.TestCase):
    """Starts one server per test and tears it down without leaking sockets."""

    server: HTTPServer
    thread: threading.Thread
    base: str

    def serve(self, handler) -> str:
        self.server = QuietHTTPServer(("127.0.0.1", 0), handler)
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


class StubRoom:
    """A room the way `reader.rooms()` hands one over: it serializes itself.

    The handler takes a provider of these rather than a ready-made list, so a
    test that wants to name the rooms wraps them here. The seam belongs in the
    tests; a parameter that only tests pass is one the production signature
    has to keep explaining.
    """

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def as_json(self) -> dict:
        return dict(self._payload)


def _start_server(sender, *, rooms=None, watcher=None, token=TOKEN, min_interval=0.0,
                  rooms_provider=None, context_provider=None, digest_provider=None,
                  poll_interval=0.05, clock=None, request_timeout=None,
                  max_streams=None, watcher_factory=None, max_queued_sends=None):
    if rooms_provider is None:
        listed = [StubRoom(room) for room in (rooms or [])]
        rooms_provider = lambda: listed  # noqa: E731 - one expression, read once
    if watcher_factory is None and watcher is not None:
        watcher_factory = lambda: watcher  # noqa: E731 - one expression, read once
    kwargs: dict = {
        "token": token,
        "min_interval": min_interval,
        "rooms_provider": rooms_provider,
        "context_provider": context_provider,
        "digest_provider": digest_provider,
        "watcher_factory": watcher_factory,
        "poll_interval": poll_interval,
    }
    if request_timeout is not None:
        kwargs["request_timeout"] = request_timeout
    if max_streams is not None:
        kwargs["max_streams"] = max_streams
    if max_queued_sends is not None:
        kwargs["max_queued_sends"] = max_queued_sends
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
