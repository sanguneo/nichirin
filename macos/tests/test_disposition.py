"""Retry decisions are data, not interpretations of native error messages."""

import unittest

from kamado.errors import ErrorCode, KamadoError, as_envelope, success
from kamado.macauto import Sender
from tests.test_send import FakeWindows


class DispositionTests(unittest.TestCase):
    def test_native_pre_input_refusals_report_non_submission(self):
        for text, running in (("", True), ("synthetic\x00", True), ("synthetic", False)):
            # Given a synthetic native sender, including a stopped-client case.
            backend = FakeWindows()
            with self.subTest(text=text, running=running):
                # When native preflight refuses.
                with self.assertRaises(KamadoError) as caught:
                    Sender(backend, running=lambda: running).send("Synthetic room", text)
                # Then direct users get the same non-submission decision.
                self.assertEqual(backend.actions.count("submit"), 0)
                self.assertEqual(
                    caught.exception.disposition,
                    {"delivery": "not_delivered", "retry": "safe"},
                )

    def test_known_write_refusal_does_not_mutate_read_default(self):
        # Given a read default with no delivery assertion.
        failure = KamadoError(ErrorCode.INVALID_REQUEST, "synthetic failure")
        original = failure.envelope()
        # When the same known error crosses a writing boundary.
        answer = as_envelope(failure, writing=True)
        # Then the write is not delivered, without changing the read contract.
        self.assertEqual(
            answer["error"]["disposition"], {"delivery": "not_delivered", "retry": "safe"}
        )
        self.assertEqual(failure.envelope(), original)

    def test_write_context_preserves_explicit_uncertainty(self):
        # Given an explicit outcome from after a possible submission.
        failure = KamadoError(
            ErrorCode.READ_FAILED, "synthetic failure",
            disposition={"delivery": "unknown", "retry": "after_check"},
        )
        # When a writing boundary renders it.
        answer = as_envelope(failure, writing=True)
        # Then known uncertainty is not downgraded to a safe retry.
        self.assertEqual(
            answer["error"]["disposition"], {"delivery": "unknown", "retry": "after_check"}
        )

    def test_every_error_code_has_a_retry_decision(self):
        for code in ErrorCode:
            with self.subTest(code=code):
                answer = KamadoError(code, "synthetic failure").envelope()
                expected = {"retry": "safe"}
                if code.value == "SEND_FAILED":
                    expected = {"delivery": "unknown", "retry": "after_check"}
                elif code.value in {
                    "WINDOW_NOT_FOUND", "INPUT_NOT_FOUND", "UNAUTHORIZED", "RATE_LIMITED"
                }:
                    expected = {"delivery": "not_delivered", "retry": "safe"}
                self.assertEqual(answer["error"]["disposition"], expected)

    def test_known_pre_submission_failure_can_override_unknown_default(self):
        answer = KamadoError(
            ErrorCode.SEND_FAILED,
            "synthetic preflight failure",
            disposition={"delivery": "not_delivered", "retry": "safe"},
            roomId="123",
        ).envelope()
        self.assertEqual(
            answer["error"]["disposition"],
            {"delivery": "not_delivered", "retry": "safe"},
        )
        self.assertNotIn("disposition", {k: v for k, v in answer.items() if k != "error"})
        self.assertEqual(answer["roomId"], "123")

    def test_submitted_success_does_not_claim_log_delivery(self):
        answer = success({"submitted": True, "roomId": "123"})
        self.assertEqual(
            answer["disposition"], {"delivery": "submitted", "retry": "unsafe"}
        )
        self.assertTrue(answer["submitted"])

    def test_dry_run_is_safe_and_not_delivered(self):
        answer = success({"submitted": False, "dryRun": True})
        self.assertEqual(
            answer["disposition"], {"delivery": "not_delivered", "retry": "safe"}
        )

    def test_unexpected_read_error_is_repeatable(self):
        answer = as_envelope(OSError("synthetic read failure"))
        self.assertEqual(answer["error"]["disposition"], {"retry": "safe"})
        self.assertNotIn("disposition", success({"count": 0}))

    def test_unexpected_write_error_is_not_safe_to_repeat(self):
        answer = as_envelope(RuntimeError("synthetic write failure"), writing=True)
        self.assertEqual(answer["error"]["code"], "SEND_FAILED")
        self.assertEqual(
            answer["error"]["disposition"], {"delivery": "unknown", "retry": "after_check"}
        )
