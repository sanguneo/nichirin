"""Durable #112 receipt behavior, using only synthetic actions and temp files."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from kakaocommon import operations
from kamado.errors import ErrorCode, KamadoError, as_envelope


class OperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "operations"
        self.digest = operations.fingerprint("send", "42", b"private synthetic text")
        self.calls = []

    def send(self):
        receipt = next(self.root.glob("*/receipt.json"))
        self.assertEqual(json.loads(receipt.read_text())["phase"], "dispatching")
        self.calls.append("input")
        return {"submitted": True, "length": 22}

    def run_operation(self, action=None, operation_id: Optional[str] = "synthetic-id", digest=None):
        return operations.run(
            operation_id, digest or self.digest, action or self.send,
            root=self.root, failure=lambda error: as_envelope(error, writing=True),
            sensitive_text="private synthetic text",
        )

    def test_same_id_replays_durable_result_with_one_input(self):
        original = self.run_operation()
        replay = self.run_operation()
        self.assertEqual(replay, original)
        self.assertEqual(self.calls, ["input"])
        record = next(self.root.glob("*/receipt.json")).read_text()
        self.assertNotIn("private synthetic text", record)
        self.assertEqual(json.loads(record)["phase"], "submitted")

    def test_crash_leaves_dispatching_and_replay_performs_zero_input(self):
        def crash():
            raise SystemExit("synthetic abrupt termination")

        with self.assertRaises(SystemExit):
            self.run_operation(crash)
        with self.assertRaises(operations.OperationError) as caught:
            self.run_operation()
        self.assertEqual(self.calls, [])
        self.assertEqual(caught.exception.envelope["error"]["disposition"],
                         {"delivery": "unknown", "retry": "after_check"})

    def test_changed_fingerprint_is_invalid_without_second_input(self):
        self.run_operation()
        for digest in (
            operations.fingerprint("send", "43", b"private synthetic text"),
            operations.fingerprint("send", "42", b"different"),
            operations.fingerprint("upload", "42", b"private synthetic text"),
        ):
            with self.subTest(digest=digest), self.assertRaises(operations.OperationError) as caught:
                self.run_operation(digest=digest)
            self.assertEqual(caught.exception.envelope["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.calls, ["input"])

    def test_failure_replays_original_disposition_and_details(self):
        def refuse():
            self.calls.append("attempt")
            raise KamadoError(ErrorCode.INPUT_NOT_FOUND, "synthetic refusal", room="Synthetic")

        with self.assertRaises(operations.OperationError) as first:
            self.run_operation(refuse)
        with self.assertRaises(operations.OperationError) as replay:
            self.run_operation(refuse)
        self.assertEqual(first.exception.envelope, replay.exception.envelope)
        self.assertEqual(replay.exception.envelope["error"]["disposition"],
                         {"delivery": "not_delivered", "retry": "safe"})
        self.assertEqual(self.calls, ["attempt"])

    def test_no_id_never_touches_the_receipt_directory(self):
        def action():
            self.calls.append("input")
            return {"submitted": True}

        self.run_operation(action, operation_id=None)
        self.run_operation(action, operation_id=None)
        self.assertEqual(self.calls, ["input", "input"])
        self.assertFalse(self.root.exists())

    def test_backend_failure_does_not_store_quoted_message_body(self):
        def refuse():
            raise OSError("backend refused private synthetic text")

        with self.assertRaises(operations.OperationError) as caught:
            self.run_operation(refuse)
        receipt = next(self.root.glob("*/receipt.json")).read_text()
        self.assertNotIn("private synthetic text", receipt)
        self.assertEqual(caught.exception.envelope["error"]["message"], "backend refused [redacted]")

    def test_receipt_failure_before_input_never_dispatches(self):
        with patch.object(operations.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(operations.OperationError) as caught:
                self.run_operation()
        self.assertEqual(self.calls, [])
        self.assertEqual(caught.exception.envelope["error"]["disposition"],
                         {"delivery": "not_delivered", "retry": "safe"})

    def test_final_receipt_failure_preserves_success_and_unknown_replay(self):
        def action():
            self.calls.append("input")
            return {"submitted": True}

        original_write = operations._write

        def fail_final(path, record):
            if record["phase"] != "dispatching":
                raise OSError("disk full")
            original_write(path, record)

        with patch.object(operations, "_write", side_effect=fail_final):
            result = self.run_operation(action)
        self.assertTrue(result["submitted"])
        with self.assertRaises(operations.OperationError) as caught:
            self.run_operation()
        self.assertEqual(caught.exception.envelope["error"]["disposition"],
                         {"delivery": "unknown", "retry": "after_check"})
        self.assertEqual(self.calls, ["input"])

    def test_concurrent_duplicate_cannot_enter_backend(self):
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def action():
            self.calls.append("input")
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release synthetic backend")
            return {"submitted": True}

        worker = threading.Thread(target=lambda: outcomes.append(self.run_operation(action)))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(operations.OperationError) as caught:
                self.run_operation()
            self.assertEqual(caught.exception.envelope["error"]["disposition"]["delivery"], "unknown")
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [{"submitted": True}])
        self.assertEqual(self.calls, ["input"])

    def test_invalid_ids_are_refused_before_input(self):
        for value in json.loads(json.dumps(["", " ", "../ x", "x" * 129, 12, False])):
            with self.subTest(value=value), self.assertRaises(operations.OperationError) as caught:
                self.run_operation(operation_id=value)
            self.assertEqual(caught.exception.envelope["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.calls, [])
