"""HTTP trace contract over synthetic senders and a deterministic queue."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from tests.serve.helpers import ServedCase, StubSender, _request, _start_server


class TraceHTTPTests(ServedCase):
    def test_request_validation_is_a_rejection_not_a_successful_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given an authenticated request rejected before queue admission.
            sender = StubSender()
            self.serve(_start_server(sender, trace_dir=directory))
            # When its text is empty.
            status, _, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": ""}
            )
            # Then the trace alone identifies the pre-submission refusal.
            rows = self.rows(directory)
            self.assertEqual(status, 400)
            self.assertEqual(rows[-1]["outcome"], "rejected")
            self.assertEqual(rows[-1]["code"], "INVALID_REQUEST")
            self.assertNotIn("execution", [row["stage"] for row in rows])
            self.assertEqual(sender.calls, [])

    def rows(self, directory):
        """Wait for the single HTTP handler to finish and close its attempt."""
        self.server.shutdown()
        self.thread.join(timeout=5)
        return [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]

    def test_queue_saturation_is_a_refusal_not_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given an occupied bounded queue.
            sender = StubSender()
            handler = _start_server(sender, min_interval=2, max_queued_sends=1, trace_dir=directory)
            getattr(handler, "_send_queue").reserve()
            self.serve(handler)
            # When the request cannot enter.
            status, body, _ = _request(self.base, "POST", "/send", {"room": "Synthetic", "text": "PRIVATE"})
            # Then no execution or private content is recorded.
            rows = self.rows(directory)
            self.assertEqual(status, 429)
            self.assertEqual(body["error"]["code"], "RATE_LIMITED")
            self.assertIn("queue_full", [row["stage"] for row in rows])
            self.assertNotIn("execution", [row["stage"] for row in rows])
            self.assertEqual(rows[-1]["outcome"], "rejected")
            self.assertEqual(sender.calls, [])
            self.assertNotIn("PRIVATE", json.dumps(rows))

    def test_queue_wait_and_execution_have_separate_durations(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a virtual clock and a pending pacing interval.
            now = [0.0]
            sender = StubSender()
            handler = _start_server(sender, min_interval=2, clock=lambda: now[0], trace_dir=directory)
            queue = getattr(handler, "_send_queue")
            queue.commit(0)

            def advance(remaining):
                now[0] += remaining

            queue._wait = advance
            self.serve(handler)
            # When the real HTTP route waits without a real sleep.
            with patch("kakaocommon.trace.time", Mock(monotonic=lambda: now[0])):
                status, body, _ = _request(self.base, "POST", "/send", {"room": "Synthetic", "text": "synthetic"})
            # Then queue and execution timings are independent.
            rows = self.rows(directory)
            ends = {row["stage"]: row for row in rows if row["event"] == "end"}
            self.assertEqual(status, 200, body)
            self.assertEqual(ends["queue"]["elapsed_ms"], 2000)
            self.assertEqual(ends["execution"]["elapsed_ms"], 0)
            self.assertEqual(ends["queue"]["outcome"], "pass")
            self.assertEqual(len(sender.calls), 1)

    def test_shutdown_is_a_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a shutting-down host.
            sender = StubSender()
            handler = _start_server(sender, trace_dir=directory)
            getattr(handler, "_stopping").set()
            self.serve(handler)
            # When a valid send arrives.
            status, _, _ = _request(self.base, "POST", "/send", {"room": "Synthetic", "text": "synthetic"})
            # Then it is refused before execution.
            rows = self.rows(directory)
            self.assertEqual(status, 503)
            self.assertIn("shutdown", [row["stage"] for row in rows])
            self.assertEqual(rows[-1]["outcome"], "rejected")
            self.assertEqual(sender.calls, [])

    def test_request_cannot_choose_trace_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a host without tracing.
            self.serve(_start_server(StubSender()))
            # When an HTTP caller includes a trace path.
            status, body, _ = _request(self.base, "POST", "/send", {
                "room": "Synthetic", "text": "synthetic", "trace": directory,
            })
            # Then it does not enable tracing.
            self.assertEqual(status, 200, body)
            self.assertEqual(list(Path(directory).iterdir()), [])
