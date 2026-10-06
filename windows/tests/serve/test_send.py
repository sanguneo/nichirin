"""``POST /send``: how a body is framed, and how two sends are spaced."""

import itertools
import json
import socket
import threading
import unittest
from http.client import HTTPConnection
from typing import cast

from kanao.errors import ErrorCode, KanaoError
from kanao.serve.handler import _SendQueue, _ShuttingDown
from tests.serve.helpers import (
    TOKEN,
    QuietHTTPServer,
    ServedCase,
    StubSender,
    _raw_request,
    _request,
    _start_server,
)


def _clock_that_outruns(interval: float):
    """A clock that steps further than *interval* every time it is read.

    Every wait the queue computes from it is therefore already over, so the
    pacing can be driven without the suite ever waiting on a real clock. The
    step is derived from the interval rather than written down: one smaller
    than the interval would leave the tests genuinely sleeping for minutes.
    """
    return itertools.count(0.0, interval * 10.0).__next__


class _JumpingQueue(_SendQueue):
    """A queue whose one blocking point moves a fake clock instead.

    ``_SendQueue._wait`` exists as a method precisely so this can replace it:
    everything else about waiting a turn — the reservation, the arithmetic,
    the order — is then exercised exactly as it runs in the server.
    """

    def __init__(self, min_interval: float, capacity: int = 4) -> None:
        self.now = 0.0
        self.waits: list[float] = []
        super().__init__(min_interval, capacity, clock=lambda: self.now)

    def _wait(self, remaining: float) -> None:
        self.waits.append(remaining)
        self.now += remaining


class _ClosingServer(QuietHTTPServer):
    """Notes, as it closes a connection, what the caller had left in it.

    ``"end"`` means everything the caller sent was read, up to its close;
    ``"data"`` means bytes were still unread, which is what turns a close
    into a reset.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.closed = threading.Event()
        self.left = ""
        super().__init__(*args, **kwargs)

    def close_request(self, request) -> None:
        connection = cast(socket.socket, request)
        connection.setblocking(False)
        try:
            self.left = "data" if connection.recv(1, socket.MSG_PEEK) else "end"
        except BlockingIOError:
            self.left = "nothing yet"
        except OSError as error:
            self.left = type(error).__name__
        super().close_request(request)
        self.closed.set()


def _read_answer(sock: socket.socket) -> "tuple[int, dict]":
    """Read one response by its ``Content-Length``, leaving the socket open."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            break
        raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    length = next(
        int(value)
        for name, _, value in (line.partition(b":") for line in lines[1:])
        if name.strip().lower() == b"content-length"
    )
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return int(lines[0].split(b" ", 2)[1]), json.loads(body)


class SendTests(ServedCase):
    # ---- body framing --------------------------------------------------

    def test_a_refused_body_is_read_to_its_end_before_the_connection_closes(self) -> None:
        # A body that lands on a socket the server has already closed is
        # answered with a reset, and the reset destroys the 400 the caller had
        # not read yet: the test below failed that way now and then, when its
        # body arrived after the close. Here the body is sent only once the
        # answer is in, which is that order on purpose, so the server must
        # still be reading. What it had left at its close is the evidence,
        # and no ordering of the two threads can make it "end" unless the
        # server read the body and the caller's close.
        self.serve(_start_server(StubSender()), server_class=_ClosingServer)
        server = self.server
        assert isinstance(server, _ClosingServer)
        host, _, port = self.base.partition("://")[2].partition(":")
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            sock.sendall(
                f"POST /send HTTP/1.1\r\n"
                f"Host: x\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                f"Content-Type: application/json\r\n\r\n".encode("ascii")
            )
            status, body = _read_answer(sock)
            sock.sendall(b'{"room": "r", "text": "t"}')
            sock.shutdown(socket.SHUT_WR)
            self.assertTrue(server.closed.wait(timeout=5))
        finally:
            sock.close()
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(server.left, "end")

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

    def test_back_to_back_sends_are_queued_rather_than_dropped(self) -> None:
        """Both arrive, in order. Spacing them is not a reason to lose one.

        The clock is injected because the interval has to be long enough that
        a real one could not step over it by accident — which is also long
        enough to be a minute of sleeping. That injection is exactly why this
        test alone does not catch the old limiter: a clock far enough ahead
        that nobody waits is also far enough ahead that nobody was refused.
        What this pins is the contract, in place of the test that asserted a
        429 here; the pacing itself is ``SendQueueTests``, and the fix the
        limiter needed is ``test_a_refused_send_does_not_spend_the_interval``.
        """
        sender = StubSender()
        self.serve(
            _start_server(sender, min_interval=60.0, clock=_clock_that_outruns(60.0))
        )
        for word in ("one", "two"):
            status, body, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": word}
            )
            self.assertEqual(status, 200, body)
        self.assertEqual([call["text"] for call in sender.calls], ["one", "two"])

    def test_a_full_queue_is_refused_and_that_message_is_not_sent(self) -> None:
        """429 still exists; it now means the line is full.

        The line is filled directly rather than by holding a request open:
        this harness answers one request at a time, so a send parked inside
        the handler could never be overtaken by the request that is supposed
        to be refused — the test would deadlock against itself.
        """
        sender = StubSender()
        handler = _start_server(sender, min_interval=60.0, max_queued_sends=1)
        handler._send_queue.reserve()  # type: ignore[attr-defined]
        self.serve(handler)
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "turned away"}
        )
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "RATE_LIMITED")
        self.assertEqual(sender.calls, [])

    def test_a_dry_run_walks_past_a_full_queue(self) -> None:
        sender = StubSender()
        handler = _start_server(sender, min_interval=60.0, max_queued_sends=1)
        handler._send_queue.reserve()  # type: ignore[attr-defined]
        self.serve(handler)
        status, body, _ = _request(
            self.base, "POST", "/send",
            {"room": "Synthetic", "text": "probe", "dryRun": True},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["dryRun"])
        self.assertFalse(body["submitted"])

    def test_zero_min_interval_makes_nobody_wait_and_refuses_nobody(self) -> None:
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

    def test_concurrent_sends_are_serialized_and_both_are_delivered(self) -> None:
        sender = StubSender()
        entered_holder: list = []
        barrier = threading.Barrier(2)
        results: list = []

        self.serve(
            _start_server(
                sender, min_interval=3600.0, clock=_clock_that_outruns(3600.0)
            )
        )

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
        # One wins the lock and the other waits behind it — two real sends,
        # one at a time through the single input box, and never a 500. The
        # one that waited is not the one that gets thrown away.
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(len(sender.calls), 2)

    def test_a_refused_send_does_not_spend_the_interval(self) -> None:
        """Nothing was written anywhere, so nobody is made to wait for it.

        A real clock is the point: if a failed send booked the next turn, the
        second request here would sit for the whole minute rather than answer.
        """
        sender = StubSender()
        self.serve(_start_server(sender, min_interval=60.0))
        status, _, _ = _request(
            self.base, "POST", "/send", {"room": "Missing room", "text": "one"}
        )
        self.assertEqual(status, 404)
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "two"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual([call["text"] for call in sender.calls], ["two"])

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


class _ClosingQueue(_SendQueue):
    """Shuts itself down the moment a send would really have waited."""

    def __init__(self, min_interval: float) -> None:
        self.now = 0.0
        super().__init__(min_interval, 4, clock=lambda: self.now)

    def _wait(self, remaining: float) -> None:
        self.close()


class SendQueueTests(unittest.TestCase):
    """The pacing itself, driven by a clock the test moves.

    These cover what the HTTP tests could not: the old limiter passed its
    suite because ``StubSender`` returns instantly, and a send that takes no
    time is the one timing production never has. Here the send's duration is
    the variable.
    """

    def test_a_send_slower_than_the_interval_leaves_nothing_to_wait_for(self) -> None:
        queue = _JumpingQueue(2.0)
        self.assertIsNone(queue.reserve())
        started = queue.begin()
        queue.now = 3.0  # the send took three seconds
        queue.commit(started)
        queue.release()

        self.assertIsNone(queue.reserve())
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [])

    def test_a_quick_send_leaves_only_the_remainder_to_wait(self) -> None:
        """Measured from the start: half a second used is half a second off.

        From the finish it would be the whole two seconds again, and the
        spacing a caller was promised would quietly become 2.5.
        """
        queue = _JumpingQueue(2.0)
        queue.reserve()
        started = queue.begin()
        queue.now = 0.5
        queue.commit(started)
        queue.release()

        queue.reserve()
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [1.5])
        self.assertEqual(queue.now, 2.0)

    def test_the_line_has_an_end_and_it_says_how_long_it_is(self) -> None:
        queue = _JumpingQueue(2.0, capacity=2)
        self.assertIsNone(queue.reserve())
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 2)

    def test_a_place_given_back_can_be_taken_again(self) -> None:
        queue = _JumpingQueue(2.0, capacity=1)
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 1)
        queue.release()
        self.assertIsNone(queue.reserve())

    def test_a_zero_interval_reserves_nothing_and_waits_for_nothing(self) -> None:
        queue = _JumpingQueue(0.0, capacity=1)
        for _ in range(5):
            self.assertIsNone(queue.reserve())
        queue.commit(queue.begin())
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [])

    def test_closing_the_queue_answers_whoever_is_still_in_line(self) -> None:
        queue = _ClosingQueue(2.0)
        queue.commit(queue.begin())
        with self.assertRaises(_ShuttingDown):
            queue.wait_for_turn()


if __name__ == "__main__":
    unittest.main()
