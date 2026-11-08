"""Opt-in trace regressions using only synthetic windows and local files."""

import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import kakaocommon.trace as trace
from kanao import cli
from kanao.errors import KanaoError
from kanao.winauto import Sender
from tests.test_send import FakeWindows
from tests.fixtures import DIRECT_ROOM, build_reader
from tests.test_send_cli import isolate_gate


IDENTITY = trace.Identity("windows", "kanao", "test")


class TraceTests(unittest.TestCase):
    def test_refusal_write_failure_and_unknown_have_distinct_last_stages(self):
        for method, stage, outcome in (
            ("input_handle", "input", "rejected"),
            ("set_text", "write", "rejected"),
            ("submit", "submit", "unknown"),
        ):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                # Given a private exception at one native boundary.
                backend = FakeWindows()
                sender = Sender(backend, running=lambda: True)
                with patch.object(backend, method, side_effect=OSError("PRIVATE_SENTINEL")):
                    # When a traced send reaches it.
                    with self.assertRaises(KanaoError):
                        with trace.attempt(directory, IDENTITY, "cli"):
                            sender.send("Synthetic room", "PRIVATE_BODY")
                # Then the trace names the failed boundary without private data.
                raw = next(Path(directory).glob("*.jsonl")).read_text()
                rows = [json.loads(line) for line in raw.splitlines()]
                failed = [row for row in rows if row["outcome"] == outcome]
                self.assertIn(stage, [row["stage"] for row in failed])
                self.assertNotIn("PRIVATE", raw)
                self.assertNotIn("Synthetic room", raw)
                self.assertNotIn("42", [row.get("window") for row in rows])

    def test_success_does_not_claim_unobserved_history_or_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a successful synthetic client.
            sender = Sender(FakeWindows(), running=lambda: True)
            # When traced.
            with trace.attempt(directory, IDENTITY, "cli"):
                result = sender.send("Synthetic room", "synthetic")
            rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            # Then S is observed but L and B are not.
            self.assertTrue(result["submitted"])
            final = rows[-1]
            self.assertEqual(final["conditions"]["L"], "not_checked")
            self.assertEqual(final["conditions"]["B"], "not_checked")
            self.assertEqual(final["conditions"]["S"], "pass")
            self.assertTrue(all(row["elapsed_ms"] >= 0 for row in rows))

    def test_disabled_trace_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given no requested trace destination.
            backend = FakeWindows()
            # When a send runs with tracing disabled.
            with trace.attempt(None, IDENTITY, "cli"):
                result = Sender(backend, running=lambda: True).send("Synthetic room", "synthetic")
            # Then the ordinary result has no trace fields or files.
            self.assertTrue(result["submitted"])
            self.assertNotIn("attempt_id", result)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_concurrent_attempts_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given two attempts forced to overlap.
            barrier = threading.Barrier(2)

            def send():
                with trace.attempt(directory, IDENTITY, "cli"):
                    barrier.wait(timeout=5)
                    return Sender(FakeWindows(), running=lambda: True).send("Synthetic room", "synthetic")

            # When both send.
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: send(), range(2)))
            # Then each file contains just its own attempt.
            files = list(Path(directory).glob("*.jsonl"))
            self.assertEqual(len(files), 2)
            ids = [{json.loads(line)["attempt_id"] for line in path.read_text().splitlines()} for path in files]
            self.assertTrue(all(len(value) == 1 for value in ids))
            self.assertNotEqual(ids[0], ids[1])
            self.assertTrue(all(result["submitted"] for result in results))

    def test_mid_attempt_io_failure_does_not_repeat_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a trace opened successfully, then a full disk.
            backend = FakeWindows()
            with trace.attempt(directory, IDENTITY, "cli"):
                with patch("kakaocommon.trace.os.write", side_effect=OSError("PRIVATE_DISK")):
                    # When the message is submitted.
                    result = Sender(backend, running=lambda: True).send("Synthetic room", "synthetic")
            # Then success remains success, exactly once.
            self.assertTrue(result["submitted"])
            self.assertEqual(backend.actions, ["fill", "submit"])

    def test_invalid_destination_refuses_before_ui_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a file where an output directory was requested.
            destination = Path(directory) / "not-a-directory"
            destination.touch()
            backend = FakeWindows()
            # When the CLI is invoked with it.
            with patch("kanao.winauto.make_sender", return_value=Sender(backend, running=lambda: True)):
                with redirect_stdout(io.StringIO()):
                    code = cli.main(["send", "--trace", str(destination), "--room", "Synthetic room", "synthetic"])
            # Then input was never touched.
            self.assertEqual(code, 1)
            self.assertEqual(backend.actions, [])

    def test_trace_flag_preserves_stdout_envelope(self):
        isolate_gate(self)
        with tempfile.TemporaryDirectory() as directory, build_reader() as reader:
            # Given the same synthetic room and command, with trace off and on.
            backend = FakeWindows()
            backend.windows[0] = type(backend.windows[0])(42, "Peer Renamed")
            outputs = []
            with patch("kanao.winauto.make_sender", return_value=Sender(backend, running=lambda: True)):
                for extra in ([], ["--trace", directory]):
                    arguments = cli.build_parser().parse_args([
                        "send", "--room-id", str(DIRECT_ROOM), "synthetic", *extra,
                    ])
                    output = io.StringIO()
                    # When each command runs.
                    with redirect_stdout(output):
                        cli.run(arguments, reader)
                    outputs.append(output.getvalue())
            # Then stdout is byte-identical and only the opt-in attempt is filed.
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(len(list(Path(directory).glob("*.jsonl"))), 1)

    def test_post_submit_read_failure_is_unknown_at_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a client that accepts Enter but cannot be read afterwards.
            backend = FakeWindows()
            backend.unreadable_after_submit = True
            # When confirmation is attempted.
            with self.assertRaises(KanaoError):
                with trace.attempt(directory, IDENTITY, "cli"):
                    Sender(backend, running=lambda: True).send("Synthetic room", "synthetic")
            # Then the trace distinguishes completed submission from lost confirmation.
            rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            ends = {row["stage"]: row for row in rows if row["event"] == "end"}
            self.assertEqual(ends["submit"]["outcome"], "pass")
            self.assertEqual(ends["confirm"]["outcome"], "unknown")
            self.assertEqual(backend.actions, ["fill", "submit"])
