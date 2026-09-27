"""``GET /stream``: the events, the failures, and letting the watcher go."""

import json
import unittest

from tests.serve.helpers import (
    TOKEN,
    ServedCase,
    StubMessage,
    StubSender,
    StubWatcher,
    _read_sse,
    _request,
    _start_server,
)


class StreamTests(ServedCase):
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
        # Seven rather than four: each batch that carries messages is now
        # followed by the cursor they advanced, so the same two messages and a
        # heartbeat sit further down the stream than they used to.
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=7)
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

    # ---- resuming ------------------------------------------------------

    def test_a_cursor_resumes_rather_than_priming(self) -> None:
        """`since` is what stops a reconnection being a hole in the record.

        Priming declares everything up to this instant old, which is the right
        default for a new follower and exactly wrong for one coming back.
        """
        watcher = StubWatcher([[StubMessage(9, 1, "alice", "missed")], []])
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream?since=1:8&since=2:40",
                           timeout=2.0, max_events=3)
        self.assertEqual(watcher.resumed_from, {"1": "8", "2": "40"})
        self.assertFalse(watcher.primed, "a resumed stream must not prime over the cursor")
        ready = json.loads(events[0]["data"])
        self.assertTrue(ready["resumed"])
        self.assertEqual(ready["cursor"], {"1": "8", "2": "40"})

    def test_without_a_cursor_the_stream_primes_and_says_so(self) -> None:
        watcher = StubWatcher([[]])
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=2)
        self.assertTrue(watcher.primed)
        self.assertIsNone(watcher.resumed_from)
        ready = json.loads(events[0]["data"])
        self.assertFalse(ready["resumed"])
        self.assertEqual(ready["cursor"], {})

    def test_the_cursor_is_published_after_the_messages_it_covers(self) -> None:
        """A client stores this and hands it back, so it must never run ahead."""
        watcher = StubWatcher(
            [
                [StubMessage(11, 1, "alice", "one"), StubMessage(4, 2, "bob", "two")],
                [],
            ]
        )
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=5)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[:4], ["ready", "message", "message", "cursor"])
        published = json.loads(events[3]["data"])["cursor"]
        self.assertEqual(published, {"1": "11", "2": "4"})

    def test_the_cursor_never_moves_a_room_backwards(self) -> None:
        """Compared as numbers, and only ever advanced.

        A published cursor promises that everything up to it has arrived, so
        a batch that went backwards - or a "9" read as text after a "10" -
        would hand a reconnecting caller its own messages again.
        """
        watcher = StubWatcher(
            [
                [StubMessage(11, 1, "alice", "one"), StubMessage(10, 1, "alice", "two")],
                [StubMessage(9, 1, "alice", "late")],
                [],
            ]
        )
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=8)
        cursors = [
            json.loads(event["data"])["cursor"]
            for event in events if event["event"] == "cursor"
        ]
        self.assertEqual(cursors, [{"1": "11"}])

    def test_a_resumed_cursor_is_compared_as_a_number(self) -> None:
        watcher = StubWatcher([[StubMessage(10, 1, "alice", "next")], []])
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream?since=1:9", timeout=2.0, max_events=4)
        cursors = [
            json.loads(event["data"])["cursor"]
            for event in events if event["event"] == "cursor"
        ]
        self.assertEqual(cursors, [{"1": "10"}])

    def test_a_poll_that_finds_nothing_publishes_no_cursor(self) -> None:
        watcher = StubWatcher([[], []])
        self.serve(_start_server(StubSender(), watcher=watcher))
        events = _read_sse(self.base, "/stream", timeout=2.0, max_events=3)
        self.assertNotIn("cursor", [event["event"] for event in events])

    def test_a_malformed_cursor_is_refused_before_a_watcher_is_built(self) -> None:
        built = []

        def factory():
            built.append(True)
            return StubWatcher([[]])

        self.serve(_start_server(StubSender(), watcher_factory=factory))
        for bad in ("since=1", "since=notanid:4", "since=1:notalogid", "since=1:-3"):
            with self.subTest(bad=bad):
                status, body, _ = _request(self.base, "GET", f"/stream?{bad}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(built, [], "a bad cursor must not cost a reader")

    def test_a_server_that_cannot_resume_refuses_rather_than_starting_over(self) -> None:
        """Silently starting from now would tell the caller it missed nothing."""

        class WithoutResume:
            def prime(self) -> None:
                pass

            def poll(self):
                return []

        self.serve(_start_server(StubSender(), watcher_factory=WithoutResume))
        status, body, _ = _request(self.base, "GET", "/stream?since=1:8")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "READ_FAILED")

    def test_stream_requires_token(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(self.base, "GET", "/stream", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_stream_without_watcher_is_a_service_error(self) -> None:
        from kamado import serve

        self.serve(serve.build_handler(StubSender().send, token=TOKEN,
                                       poll_interval=0.05))
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
        self.assertTrue(
            watcher.shut.wait(5), "the watcher outlived the client that left"
        )


if __name__ == "__main__":
    unittest.main()
