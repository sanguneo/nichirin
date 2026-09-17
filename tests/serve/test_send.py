"""``POST /send``: how a body is framed, and how two sends are spaced."""

import itertools
import json
import threading
import time
import unittest
from http.client import HTTPConnection

from kamado.errors import ErrorCode, KamadoError
from kamado.serve.handler import _SendQueue, _ShuttingDown
from tests.serve.helpers import (
    TOKEN,
    ServedCase,
    StubSender,
    _raw_request,
    _request,
    _start_server,
)


def _clock_that_outruns(interval: float):
    """A clock whose every read jumps well past *interval*.

    The pacing a queued send would spend in production is expressed rather than
    spent: by the time the next caller reads the clock, its turn has long since
    come. The step is derived from the interval on purpose -- a hardcoded one
    that happened to be *smaller* would turn these into a real wait of minutes.
    """
    return itertools.count(0.0, interval * 10.0).__next__


class SendTests(ServedCase):
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

    def test_every_macos_failure_has_a_status_of_its_own(self) -> None:
        """The three codes Windows has no equivalent of used to leave as 500.

        `PERMISSION_REQUIRED` is the one that mattered: it is the failure a
        user fixes in half a minute by granting Accessibility or Full Disk
        Access, and reporting it as an internal server error tells a remote
        caller there is nothing to be done.
        """
        expected = {
            ErrorCode.PERMISSION_REQUIRED: 503,
            ErrorCode.NO_USER_DATA: 503,
            ErrorCode.INPUT_NOT_FOUND: 404,
        }
        for code, status in expected.items():
            with self.subTest(code=code.value):
                sender = StubSender(fail_with=KamadoError(code, "synthetic"))
                self.serve(_start_server(sender))
                got, body, _ = _request(
                    self.base, "POST", "/send", {"room": "Synthetic", "text": "hi"}
                )
                self.assertEqual(got, status)
                self.assertEqual(body["error"]["code"], code.value)
                self.tearDown()

    def test_back_to_back_sends_are_queued_rather_than_dropped(self) -> None:
        """Two lines in a row both arrive; the second waits its turn.

        This asserted ``429`` until the queue replaced the refusal. Throwing
        the second message away was the wrong answer to a caller doing the most
        ordinary thing there is, and the only use a client had for that status
        was to sleep and try again -- which is what the queue now does once,
        here, instead of in every client.

        The clock jumps a long way between reads so the pacing this would spend
        in production is expressed without spending it in the suite.
        """
        sender = StubSender()
        self.serve(
            _start_server(
                sender, min_interval=60.0, clock=_clock_that_outruns(60.0)
            )
        )
        for word in ("one", "two"):
            status, body, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": word}
            )
            self.assertEqual(status, 200, body)
        self.assertEqual([call["text"] for call in sender.calls], ["one", "two"])

    def test_a_full_queue_refuses_rather_than_growing(self) -> None:
        """429 is kept for the case it actually describes: the line is full.

        A queue that never says no is only a slower way to empty a thousand
        messages into somebody's chat, so the line has an end.

        The line is filled directly rather than by holding a request open: this
        server answers one request at a time on purpose, so a send blocked
        inside the handler could never be overtaken by the one meant to be
        refused. Taking the place is what a caller ahead in the queue does.
        """
        sender = StubSender()
        handler = _start_server(sender, min_interval=60.0, max_queued_sends=1)
        self.assertIsNone(
            handler._send_queue.reserve(),  # type: ignore[attr-defined]
            "somebody is now ahead in the line",
        )
        self.serve(handler)

        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "overflow"}
        )
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "RATE_LIMITED")
        self.assertEqual(sender.calls, [], "the refused message was never sent")

    def test_a_freed_place_lets_the_next_send_through(self) -> None:
        sender = StubSender()
        handler = _start_server(sender, min_interval=60.0, max_queued_sends=1)
        handler._send_queue.reserve()  # type: ignore[attr-defined]
        self.serve(handler)

        status, _, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "refused"}
        )
        self.assertEqual(status, 429)

        handler._send_queue.release()  # type: ignore[attr-defined]
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "allowed"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual([call["text"] for call in sender.calls], ["allowed"])

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

    def test_concurrent_sends_are_serialized_and_both_land(self) -> None:
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
        # One wins the lock and the other waits behind it, but both are sent:
        # never two at once through the single input box, never a 500, and --
        # since the queue replaced the refusal -- never a message thrown away.
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(len(sender.calls), 2)

    def test_lock_is_released_after_a_sender_error(self) -> None:
        sender = StubSender(fail_with=KamadoError(ErrorCode.SEND_FAILED, "boom"))
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

    def test_a_failed_send_does_not_spend_the_quota(self) -> None:
        """Nothing was delivered, so the caller's next try is not charged.

        The failure paths return before the quota is marked. Charging them
        would rate-limit somebody's retry of a room they had simply not opened
        yet — a 404 followed by a 429 that outlasts their patience.
        """
        sender = StubSender(fail_with=KamadoError(ErrorCode.WINDOW_NOT_FOUND, "shut"))
        self.serve(_start_server(sender, min_interval=3600.0))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "x"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "WINDOW_NOT_FOUND")
        sender.fail_with = None
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "y"}
        )
        self.assertEqual(status, 200, body)


class _JumpingQueue(_SendQueue):
    """A queue whose one real pause moves a fake clock instead of sleeping.

    ``_wait`` is the single place the queue blocks, which is why it is a method
    rather than an inline ``wait``: replacing it turns "this send waited 1.5
    seconds" into an assertion instead of a second and a half of test time.
    """

    def __init__(self, min_interval: float, capacity: int = 4) -> None:
        self.now = 0.0
        self.waits: list[float] = []
        super().__init__(min_interval, capacity, clock=lambda: self.now)

    def _wait(self, remaining: float) -> None:
        self.waits.append(remaining)
        self.now += remaining


class SendQueueTests(unittest.TestCase):
    """Pacing runs from when a send *started*, and the line has an end."""

    def test_a_send_slower_than_the_interval_leaves_nothing_to_wait_for(self) -> None:
        """The cadence is whichever is slower, the send or the interval.

        A real send costs about three seconds. Charging a two-second interval
        on top of that would make every message wait for time the previous one
        had already spent -- and the slower the machine, the worse the penalty.
        """
        queue = _JumpingQueue(2.0)
        self.assertIsNone(queue.reserve())
        started = queue.begin()
        queue.now = 3.0  # the send took three seconds
        queue.commit(started)
        queue.release()

        self.assertIsNone(queue.reserve())
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [], "the send already outlasted the interval")

    def test_a_send_faster_than_the_interval_waits_the_remainder(self) -> None:
        queue = _JumpingQueue(2.0)
        self.assertIsNone(queue.reserve())
        started = queue.begin()
        queue.now = 0.5  # a quick send, so pacing still has something to do
        queue.commit(started)
        queue.release()

        self.assertIsNone(queue.reserve())
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [1.5])
        self.assertEqual(queue.now, 2.0)

    def test_a_failed_send_paces_nothing(self) -> None:
        """``begin`` records nothing, so a send that raised holds nobody up."""
        queue = _JumpingQueue(60.0)
        self.assertIsNone(queue.reserve())
        queue.begin()  # the send then failed, so commit is never reached
        queue.release()

        self.assertIsNone(queue.reserve())
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [])

    def test_the_line_has_an_end(self) -> None:
        queue = _JumpingQueue(2.0, capacity=2)
        self.assertIsNone(queue.reserve())
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 2, "the third is told how many are ahead")

    def test_leaving_the_line_frees_the_place(self) -> None:
        queue = _JumpingQueue(2.0, capacity=1)
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 1)
        queue.release()
        self.assertIsNone(queue.reserve())

    def test_a_zero_interval_neither_waits_nor_refuses(self) -> None:
        queue = _JumpingQueue(0.0, capacity=1)
        for _ in range(3):
            self.assertIsNone(queue.reserve())
        queue.commit(queue.begin())
        queue.wait_for_turn()
        self.assertEqual(queue.waits, [])

    def test_closing_answers_a_send_that_is_still_waiting(self) -> None:
        """Shutdown must reply, not let a daemon thread take the message away.

        Whichever order the two threads reach the queue in, the waiter is told
        the server is going -- so there is no timing assumption here.
        """
        queue = _SendQueue(60.0, 4, clock=time.monotonic)
        queue.reserve()
        queue.commit(time.monotonic())

        outcome: list[str] = []
        entered = threading.Event()

        def wait_in_line() -> None:
            entered.set()
            try:
                queue.wait_for_turn()
            except _ShuttingDown:
                outcome.append("told")
            else:
                outcome.append("ran")

        waiter = threading.Thread(target=wait_in_line, daemon=True)
        waiter.start()
        self.assertTrue(entered.wait(timeout=5))
        queue.close()
        waiter.join(timeout=5)
        self.assertEqual(outcome, ["told"])


if __name__ == "__main__":
    unittest.main()
