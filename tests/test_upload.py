"""``macupload.Uploader``: what each failed step sends, and what it puts back.

A scripted backend stands in for the desktop, so these run anywhere. Each
step can be told to fail; the assertions are about the contract's two kinds
of failure — "nothing was sent" before the confirmation's button is pressed,
an unknown outcome after — and about the clipboard, the draft and the focus
being returned whatever happened.
"""

import os
import sys
import tempfile
import unittest

from kamado.errors import ErrorCode, KamadoError
from kamado.macauto import ChatWindow
from kamado.macupload import Uploader


class ScriptedBackend:
    def __init__(self, **fail) -> None:
        self.fail = fail
        self.steps: list[str] = []
        self.pasteboard = ["person's clipboard"]
        self.composer = ""

    def screen_locked(self) -> bool:
        return self.fail.get("locked", False)

    def input_handle(self, window):
        return None if self.fail.get("no_composer") else 7

    def get_text(self, control) -> str:
        return self.composer

    def set_text(self, control, text) -> None:
        self.steps.append("set_text")
        self.composer = text

    def save_pasteboard(self):
        return list(self.pasteboard)

    def put_file(self, path) -> None:
        self.steps.append("put_file")
        self.pasteboard = [f"file://{path}"]

    def restore_pasteboard(self, saved) -> None:
        self.steps.append("restore_pasteboard")
        self.pasteboard = saved

    def focus(self, control):
        self.steps.append("focus")
        return None if self.fail.get("no_focus") else "token"

    def unfocus(self, token) -> None:
        self.steps.append("unfocus")

    def paste(self) -> bool:
        self.steps.append("paste")
        if self.fail.get("paste_as_text"):
            self.composer = "/staged/a.png"
        if self.fail.get("paste_raises"):
            raise OSError("synthetic AX failure")
        return not self.fail.get("no_paste")

    def confirmation(self, control):
        self.steps.append("confirmation")
        return None if self.fail.get("no_confirmation") or self.fail.get("paste_as_text") else 9

    def confirm(self, sheet) -> bool:
        self.steps.append("confirm")
        if self.fail.get("confirm_raises"):
            raise OSError("synthetic AX failure")
        return not self.fail.get("stays_open")

    def dismiss(self, sheet) -> None:
        self.steps.append("dismiss")


class ScriptedSender:
    def __init__(self) -> None:
        self.resolved: list[tuple] = []

    def resolve(self, room, open_if_needed=False, aliases=(), hint=None):
        self.resolved.append((room, open_if_needed))
        return ChatWindow(handle=1, title=room)


class UploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".png")
        os.write(handle, b"synthetic png bytes")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)

    def upload(self, **fail):
        backend = ScriptedBackend(**fail)
        sender = ScriptedSender()
        uploader = Uploader(backend, sender)  # type: ignore[arg-type]
        try:
            return backend, sender, uploader.upload("Room", self.path, "a.png", open_if_needed=True), None
        except KamadoError as error:
            return backend, sender, None, error

    def test_a_confirmed_file_is_submitted_and_everything_is_put_back(self) -> None:
        backend, sender, result, error = self.upload()
        self.assertIsNone(error)
        self.assertEqual(
            result, {"submitted": True, "room": "Room", "name": "a.png", "size": 19}
        )
        self.assertEqual(sender.resolved, [("Room", True)])
        self.assertEqual(backend.pasteboard, ["person's clipboard"])
        self.assertIn("unfocus", backend.steps)

    def test_a_locked_screen_is_refused_before_any_window_is_touched(self) -> None:
        backend, sender, _result, error = self.upload(locked=True)
        self.assertIs(error.code, ErrorCode.SEND_FAILED)
        self.assertIn("nothing was sent", error.message)
        self.assertEqual(sender.resolved, [])
        self.assertEqual(backend.steps, [])

    def test_failures_before_the_press_send_nothing_and_restore(self) -> None:
        for step in ("no_focus", "no_paste", "no_confirmation", "paste_raises"):
            with self.subTest(step=step):
                backend, _sender, _result, error = self.upload(**{step: True})
                self.assertIs(error.code, ErrorCode.SEND_FAILED)
                self.assertIn("nothing was sent", error.message)
                self.assertNotIn("confirm", backend.steps)
                self.assertEqual(backend.pasteboard, ["person's clipboard"])

    def test_a_missing_composer_is_its_own_code(self) -> None:
        backend, _sender, _result, error = self.upload(no_composer=True)
        self.assertIs(error.code, ErrorCode.INPUT_NOT_FOUND)
        self.assertNotIn("put_file", backend.steps)

    def test_a_paste_that_landed_as_text_is_taken_back_out(self) -> None:
        backend, _sender, _result, error = self.upload(paste_as_text=True)
        self.assertIn("nothing was sent", error.message)
        self.assertEqual(backend.composer, "")

    def test_a_confirmation_that_stays_open_is_closed_and_unknown(self) -> None:
        for step in ("stays_open", "confirm_raises"):
            with self.subTest(step=step):
                backend, _sender, _result, error = self.upload(**{step: True})
                self.assertIs(error.code, ErrorCode.SEND_FAILED)
                self.assertIn("outcome is unknown", error.message)
                self.assertNotIn("nothing was sent", error.message)
                self.assertEqual(backend.pasteboard, ["person's clipboard"])
        backend, _sender, _result, _error = self.upload(stays_open=True)
        self.assertIn("dismiss", backend.steps)


@unittest.skipUnless(sys.platform == "darwin", "the pasteboard is AppKit's")
class PasteboardTests(unittest.TestCase):
    """A private, uniquely named pasteboard; the person's clipboard is never used."""

    def test_contents_survive_a_file_being_put_on_and_taken_off(self) -> None:
        from kamado.macupload import Pasteboard

        board = Pasteboard(f"kamado-test-{os.getpid()}-{os.urandom(4).hex()}")
        self.addCleanup(board.release)
        original = [
            [("public.utf8-plain-text", "synthetic 클립보드".encode("utf-8"))],
            [("com.example.synthetic", bytes(range(32)))],
        ]
        board.restore(original)
        saved = board.save()
        self.assertEqual(saved, original)

        handle, path = tempfile.mkstemp(suffix=".txt")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        board.put_file(path)
        self.assertTrue((board.file_url() or "").endswith(os.path.basename(path)))

        board.restore(saved)
        self.assertEqual(board.save(), original)
        self.assertIsNone(board.file_url())


if __name__ == "__main__":
    unittest.main()
