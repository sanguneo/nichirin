"""The server the command actually runs, rather than a stand-in for it.

Everything else in this directory drives a single-threaded ``HTTPServer``,
which answers one request at a time. That is the right harness for "what does
this route reply" and the wrong one for every question this file asks: a stream
that must not block the rest of the server can only fail to block it where
there is more than one thread, and two sends only race where they can actually
run at once.
"""

import contextlib
import io
import json
import socket
import threading
import unittest
import urllib.request

from kanao import serve
from kanao.serve.handler import _ShuttingDown

from tests.serve.helpers import (
    TOKEN,
    StubMessage,
    StubSender,
    StubWatcher,
    _request,
    _start_server,
)


class OverlappingSender(StubSender):
    """Notices if two sends are ever inside it at the same time."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.inside = 0
        self.most = 0
        self.first_inside = threading.Event()
        self.overlapped = threading.Event()
        self.may_finish = threading.Event()

    def send(
        self,
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
        search_row=None,
    ):
        with self._lock:
            self.inside += 1
            self.most = max(self.most, self.inside)
            if self.inside > 1:
                self.overlapped.set()
        self.first_inside.set()
        try:
            self.may_finish.wait(timeout=5)
            return super().send(
                room,
                text,
                dry_run=dry_run,
                open_if_needed=open_if_needed,
                aliases=aliases,
                hint=hint,
            )
        finally:
            with self._lock:
                self.inside -= 1


class ThreadedServerCase(unittest.TestCase):
    """Runs ``serve.ServeHTTPServer`` — the class ``serve.run`` binds."""

    def serve(self, handler) -> str:
        self.server = serve.ServeHTTPServer(("127.0.0.1", 0), handler)
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

    def open_stream(self, timeout=5.0):
        """Open /stream and hold it, having read its first event."""
        request = urllib.request.Request(
            self.base + "/stream",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"},
        )
        response = urllib.request.urlopen(request, timeout=timeout)
        self.addCleanup(response.close)
        self.assertEqual(response.fp.readline(), b"event: ready\n")
        return response


class ConcurrencyTests(ThreadedServerCase):
    def test_an_open_stream_does_not_stop_the_other_routes(self) -> None:
        """The reason the running server is threaded, stated as a test.

        On the single-threaded harness this request would never be answered:
        the stream owns the only thread until its reader leaves.
        """
        watcher = StubWatcher([[]] * 200)
        self.serve(_start_server(StubSender(), watcher=watcher, poll_interval=0.01,
                                 rooms=[{"id": "1", "title": "Family"}]))
        self.open_stream()
        status, body, _ = _request(self.base, "GET", "/rooms")
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"][0]["title"], "Family")
        self.assertEqual(_request(self.base, "GET", "/health", token=None)[0], 200)

    def test_two_overlapping_sends_still_reach_the_input_box_one_at_a_time(self) -> None:
        """Two sends that genuinely overlap are serialized by the lock.

        The same claim is made on the single-threaded harness, where the server
        could not have overlapped them anyway. Here the second request is
        already in the server while the first is inside the sender, so the lock
        is the only thing keeping the composer from being written twice.

        The waits are on events, not on a clock. The one bounded wait is the
        negative: with the lock removed the second send enters immediately and
        sets ``overlapped``, so half a second is generous rather than lucky.
        """
        sender = OverlappingSender()
        self.serve(_start_server(sender))
        results = []

        def send(text):
            results.append(
                _request(self.base, "POST", "/send", {"room": "Synthetic", "text": text})
            )

        first = threading.Thread(target=send, args=("one",), daemon=True)
        second = threading.Thread(target=send, args=("two",), daemon=True)
        first.start()
        self.assertTrue(sender.first_inside.wait(5), "the first send never arrived")
        second.start()
        self.assertFalse(
            sender.overlapped.wait(0.5), "two sends were inside the sender at once"
        )
        sender.may_finish.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual(sorted(status for status, _, _ in results), [200, 200])
        self.assertEqual(sender.most, 1)
        self.assertEqual({call["text"] for call in sender.calls}, {"one", "two"})


class StreamLimitTests(ThreadedServerCase):
    def test_one_stream_too_many_is_refused_so_the_others_survive(self) -> None:
        watchers = [StubWatcher([[]] * 200) for _ in range(3)]
        handed = iter(watchers)
        self.serve(_start_server(StubSender(), poll_interval=0.01, max_streams=2,
                                 watcher_factory=lambda: next(handed)))
        self.open_stream()
        self.open_stream()
        status, body, _ = _request(self.base, "GET", "/stream")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "BUSY")
        self.assertIn("2", body["error"]["message"])
        # The two that were let in are untouched, and ordinary routes answer.
        self.assertEqual(_request(self.base, "GET", "/health", token=None)[0], 200)

    def test_a_closed_stream_gives_its_slot_back(self) -> None:
        watchers = [
            StubWatcher([[StubMessage(1, 1, "alice", "x")]] * 200) for _ in range(3)
        ]
        handed = iter(watchers)
        self.serve(_start_server(StubSender(), poll_interval=0.01, max_streams=1,
                                 watcher_factory=lambda: next(handed)))
        first = self.open_stream()
        self.assertEqual(_request(self.base, "GET", "/stream")[0], 503)
        first.close()
        # The slot is released exactly where the watcher is closed, so waiting
        # on the watcher is waiting on the thing under test — no sleeping.
        self.assertTrue(watchers[0].shut.wait(5), "the watcher outlived its client")
        self.open_stream()


class SlowClientTests(ThreadedServerCase):
    def test_a_body_that_never_arrives_is_given_up_on(self) -> None:
        """A promised body that never comes must not hold a thread for ever.

        Time is the behaviour under test here, so the server is built with a
        short patience and the test waits on the answer rather than a clock.
        """
        self.serve(_start_server(StubSender(), request_timeout=0.2))
        host, _, port = self.base.partition("://")[2].partition(":")
        with socket.create_connection((host, int(port)), timeout=5) as raw:
            raw.sendall(
                b"POST /send HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                b"Content-Length: 120\r\n"
                b"\r\n"
                b'{"room": "Synthetic", "text": "'  # and then nothing, ever
            )
            answer = b""
            while True:
                head, separator, body = answer.partition(b"\r\n\r\n")
                if separator:
                    declared = next(
                        (
                            int(line.split(b":", 1)[1])
                            for line in head.split(b"\r\n")
                            if line.lower().startswith(b"content-length:")
                        ),
                        0,
                    )
                    if len(body) >= declared:
                        break
                chunk = raw.recv(4096)
                if not chunk:
                    break
                answer += chunk
        self.assertTrue(answer, "the server never answered the stalled request")
        head, _, body = answer.partition(b"\r\n\r\n")
        self.assertEqual(int(head.split(b" ", 2)[1]), 408)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_REQUEST")

    def test_a_complete_request_is_unaffected_by_the_timeout(self) -> None:
        self.serve(_start_server(StubSender(), request_timeout=0.2))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "hello"}
        )
        self.assertEqual(status, 200, body)


class ShutdownTests(ThreadedServerCase):
    """Queueing a send creates a state that shutting down could swallow."""

    def test_a_send_still_in_line_is_answered_when_the_queue_closes(self) -> None:
        """The waiting request gets 503, rather than a thread killed mid-wait.

        Request threads are daemons, so nothing else would ever answer them.
        Every wait here is on an event: the first send parks inside the stub
        sender, which is what puts the second one in line.
        """
        sender = StubSender()
        handler = _start_server(sender, min_interval=60.0)
        self.serve(handler)
        answers: dict = {}

        def request(name, body):
            answers[name] = _request(self.base, "POST", "/send", body)

        first = threading.Thread(
            target=request, args=("first", {"room": "Block me", "text": "one"}),
            daemon=True,
        )
        first.start()
        self.assertTrue(sender.entered.wait(5), "the first send never arrived")
        second = threading.Thread(
            target=request, args=("second", {"room": "Synthetic", "text": "two"}),
            daemon=True,
        )
        second.start()
        handler._send_queue.close()  # type: ignore[attr-defined]
        sender.release.set()
        first.join(timeout=10)
        second.join(timeout=10)

        self.assertEqual(answers["first"][0], 200)
        self.assertEqual(answers["second"][0], 503)
        self.assertEqual(answers["second"][1]["error"]["code"], "BUSY")
        self.assertEqual([call["text"] for call in sender.calls], ["one"])


class InterruptTests(unittest.TestCase):
    """What ``Ctrl-C`` reaches: the socket, and now the line behind it."""

    class _Interrupted:
        """A server that is interrupted the moment it starts serving."""

        built: list = []

        def __init__(self, address, handler) -> None:
            type(self).built.append(handler)
            self.server_port = address[1]

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    def test_ctrl_c_closes_the_send_queue(self) -> None:
        self._Interrupted.built = []
        original = serve.server.ServeHTTPServer
        serve.server.ServeHTTPServer = self._Interrupted  # type: ignore[misc]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = serve.run(
                    "127.0.0.1", 0, StubSender().send, token=TOKEN, min_interval=2.0
                )
        finally:
            serve.server.ServeHTTPServer = original  # type: ignore[misc]

        self.assertEqual(code, 130)
        handler = self._Interrupted.built[0]
        with self.assertRaises(_ShuttingDown):
            handler._send_queue.wait_for_turn()


class DisconnectNoiseTests(unittest.TestCase):
    """A reader hanging up is SSE's success case, not a server error."""

    class Reporting:
        """Stands in for ``socketserver``, which would print a traceback."""

        def __init__(self, reported) -> None:
            self.reported = reported

        def handle_error(self, request, client_address) -> None:
            self.reported.append(client_address)

    def _server(self):
        reported = []

        class Server(serve.QuietDisconnects, self.Reporting):
            pass

        return Server(reported), reported

    def test_ordinary_disconnects_are_swallowed(self) -> None:
        server, reported = self._server()
        for error in (BrokenPipeError(), ConnectionResetError(),
                      ConnectionAbortedError(), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                try:
                    raise error
                except OSError:
                    server.handle_error(object(), ("127.0.0.1", 1))
        self.assertEqual(reported, [])

    def test_anything_else_is_still_reported(self) -> None:
        server, reported = self._server()
        try:
            raise ValueError("something genuinely wrong")
        except ValueError:
            server.handle_error(object(), ("127.0.0.1", 1))
        self.assertEqual(reported, [("127.0.0.1", 1)])


class ReadGateTests(ThreadedServerCase):
    def test_a_read_beyond_the_slots_is_refused_and_the_slot_comes_back(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_rooms():
            entered.set()
            self.assertTrue(release.wait(10))
            return []

        self.serve(_start_server(StubSender(), rooms_provider=slow_rooms, max_concurrent_reads=1))
        first: dict = {}
        worker = threading.Thread(
            target=lambda: first.update(answer=_request(self.base, "GET", "/rooms"))
        )
        worker.start()
        self.assertTrue(entered.wait(10), "the first read never reached its provider")
        try:
            status, body, _ = _request(self.base, "GET", "/rooms")
            self.assertEqual(status, 503)
            self.assertEqual(body["error"]["code"], "BUSY")
            self.assertEqual(_request(self.base, "GET", "/health", token=None)[0], 200)
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(first["answer"][0], 200)
        self.assertEqual(_request(self.base, "GET", "/rooms")[0], 200, "the slot was not given back")


class StoppingTests(ThreadedServerCase):
    def test_a_stream_ends_when_the_server_stops_rather_than_after_its_interval(self) -> None:
        watcher = StubWatcher([])
        handler = _start_server(StubSender(), poll_interval=60.0, watcher_factory=lambda: watcher)
        self.serve(handler)
        self.open_stream()
        handler._stopping.set()
        self.assertTrue(watcher.shut.wait(5), "the stream slept through the stop")

    def test_a_write_waiting_through_a_stop_is_refused_and_the_one_running_is_drained(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        typed: list[str] = []

        class Parked(StubSender):
            def send(self, room, text, dry_run=False, **_ignored):
                typed.append(text)
                if text == "first":
                    entered.set()
                    release.wait(10)
                return {"submitted": True, "dryRun": dry_run, "room": room,
                        "length": len(text), "clearedDraft": False}

        handler = _start_server(Parked())
        self.serve(handler)
        answers: dict = {}

        def send(text: str) -> None:
            answers[text] = _request(self.base, "POST", "/send", {"room": "Synthetic", "text": text})

        first = threading.Thread(target=send, args=("first",))
        first.start()
        self.assertTrue(entered.wait(10), "the first send never reached the client")
        second = threading.Thread(target=send, args=("second",))
        second.start()
        handler._stopping.set()
        handler._send_queue.close()
        release.set()
        self.assertTrue(handler._drain(10), "the running write was not drained")
        first.join(10)
        second.join(10)
        self.assertEqual(answers["first"][0], 200)
        self.assertEqual(answers["second"][0], 503)
        self.assertEqual(answers["second"][1]["error"]["code"], "BUSY")
        self.assertEqual(typed, ["first"])


if __name__ == "__main__":
    unittest.main()
