"""Retry decisions are data, not interpretations of native error messages."""

import unittest

from kamado.errors import ErrorCode, KamadoError, as_envelope, success


class DispositionTests(unittest.TestCase):
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
