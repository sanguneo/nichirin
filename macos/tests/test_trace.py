"""Native AX branch trace coverage with no KakaoTalk access."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import kakaocommon.trace as trace
from kamado import ax
from kamado.macauto import AXBackend, Sender
from kamado.errors import KamadoError
from tests.test_send import FakeWindows


IDENTITY = trace.Identity("macos", "kamado", "test")


class TraceTests(unittest.TestCase):
    def test_native_refusal_write_and_uncertainty_are_masked(self):
        for method, stage, outcome in (
            ("input_handle", "input", "rejected"),
            ("set_text", "write", "rejected"),
            ("submit", "submit", "unknown"),
        ):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                # Given a private native failure.
                backend = FakeWindows()
                with patch.object(backend, method, side_effect=OSError("PRIVATE_SENTINEL")):
                    # When a traced send reaches it.
                    with self.assertRaises(KamadoError):
                        with trace.attempt(directory, IDENTITY, "cli"):
                            Sender(backend, running=lambda: True).send("Synthetic room", "PRIVATE_BODY")
                # Then only safe categorical information is recorded.
                raw = next(Path(directory).glob("*.jsonl")).read_text()
                rows = [json.loads(line) for line in raw.splitlines()]
                self.assertIn(stage, [row["stage"] for row in rows if row["outcome"] == outcome])
                self.assertNotIn("PRIVATE", raw)
                self.assertNotIn("Synthetic room", raw)

    def test_button_and_guarded_return_have_different_stages(self):
        for button_present, expected in ((True, "ax_button"), (False, "focused_return")):
            with self.subTest(button=button_present), tempfile.TemporaryDirectory() as directory:
                # Given a real AX adapter over synthetic native elements.
                app = Mock(spec=ax.Element)
                composer = Mock(spec=ax.Element)
                composer.value = ""
                window = Mock(spec=ax.Element)
                button = Mock(spec=ax.Element) if button_present else None
                with patch("kamado.macauto.ax.application", return_value=app):
                    backend = AXBackend(99)
                control = backend._register(composer)
                backend._windows_by_control[control] = window
                backend._text_by_control[control] = "synthetic"
                with patch.object(backend, "_send_button", return_value=button):
                    with patch.object(backend, "_wait", return_value=True):
                        with patch("kamado.macauto.activate"), patch("kamado.macauto.ax.post_key"):
                            # When the adapter submits.
                            with trace.attempt(directory, IDENTITY, "cli"):
                                backend.submit(control)
                # Then the actual selected branch is named, without claiming B.
                rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
                self.assertIn(expected, [row["stage"] for row in rows])
                self.assertEqual(rows[-1]["conditions"]["B"], "not_checked")
