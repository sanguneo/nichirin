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
