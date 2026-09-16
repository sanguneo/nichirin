"""Synthetic send tests. No desktop calls and no real messages."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread import ax
from kakaoread.macauto import AXBackend, ChatWindow, Sender


class FakeWindows:
    def __init__(self) -> None:
        self.windows = [ChatWindow(42, "Synthetic room")]
        self.draft = ""
        self.control = 84
        self.actions: list[str] = []
        self.fail_submit = False

    def chat_windows(self) -> list[ChatWindow]:
        return self.windows

    def input_handle(self, window: int) -> int | None:
        return self.control or None

    def get_text(self, control: int) -> str:
        return self.draft

    def set_text(self, control: int, text: str) -> None:
        self.actions.append("fill")
        self.draft = text

    def submit(self, control: int) -> None:
        self.actions.append("submit")
        if self.fail_submit:
            raise OSError("synthetic queue failure")


class SendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeWindows()
        self.sender = Sender(self.backend, running=lambda: True)

    def test_empty_target_cannot_resolve_the_only_window(self) -> None:
        with self.assertRaises(KakaoReadError) as caught:
            self.sender.send("  ", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, [])

    def test_an_unfinished_draft_is_cleared_and_the_message_goes(self) -> None:
        self.backend.draft = "an unfinished synthetic draft"
        result = self.sender.send("Synthetic room", "replacement")
        self.assertTrue(result["submitted"])
        self.assertEqual(self.backend.draft, "replacement")
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_clearing_a_draft_is_reported_rather_than_done_quietly(self) -> None:
        self.backend.draft = "an unfinished synthetic draft"
        self.assertIs(self.sender.send("Synthetic room", "replacement")["clearedDraft"], True)

    def test_an_empty_box_reports_that_nothing_was_cleared(self) -> None:
        self.assertIs(self.sender.send("Synthetic room", "synthetic text")["clearedDraft"], False)

    def test_dry_run_never_types_or_submits(self) -> None:
        result = self.sender.send("Synthetic room", "synthetic text", dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.backend.actions, [])

    def test_dry_run_warns_that_a_draft_would_be_lost(self) -> None:
        """The one call that changes nothing is where the warning is worth having."""
        self.backend.draft = "an unfinished synthetic draft"
        result = self.sender.send("Synthetic room", "replacement", dry_run=True)
        self.assertIs(result["wouldClearDraft"], True)
        self.assertEqual(self.backend.draft, "an unfinished synthetic draft")

    def test_queue_acceptance_is_not_reported_as_delivery(self) -> None:
        result = self.sender.send("Synthetic room", "synthetic text")
        self.assertTrue(result["submitted"])
        self.assertNotIn("sent", result)
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_duplicate_exact_titles_are_refused(self) -> None:
        self.backend.windows.append(ChatWindow(43, "Synthetic room"))
        with self.assertRaises(KakaoReadError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertEqual(self.backend.actions, [])

    def test_exact_title_beats_substring(self) -> None:
        self.backend.windows.append(ChatWindow(43, "Synthetic room extra"))
        self.assertEqual(self.sender.resolve("Synthetic room").handle, 42)

    def test_unopened_exact_room_cannot_target_a_longer_title(self) -> None:
        self.backend.windows = [ChatWindow(43, "Synthetic room extra")]
        with self.assertRaises(KakaoReadError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(self.backend.actions, [])

    def test_invalid_message_is_refused_without_writes(self) -> None:
        for text in ("", " \n", "hello\x00world"):
            with self.subTest(text=text):
                with self.assertRaises(KakaoReadError) as caught:
                    self.sender.send("Synthetic room", text)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, [])

    def test_closed_client_is_a_typed_failure(self) -> None:
        sender = Sender(self.backend, running=lambda: False)
        with self.assertRaises(KakaoReadError) as caught:
            sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.KAKAO_NOT_RUNNING)

    def test_missing_input_is_a_typed_failure(self) -> None:
        self.backend.control = 0
        with self.assertRaises(KakaoReadError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.INPUT_NOT_FOUND)
        self.assertEqual(self.backend.actions, [])

    def test_submit_failure_does_not_retry_or_erase_draft(self) -> None:
        self.backend.fail_submit = True
        with self.assertRaises(KakaoReadError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)
        self.assertEqual(self.backend.actions, ["fill", "submit"])
        self.assertEqual(self.backend.draft, "synthetic text")


class AXMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        # Patch only the AX boundary; no live app or Accessibility grant. The
        # composer reports a screen rectangle so the click can be aimed, and
        # activation/foreground restore are stubbed so nothing touches the
        # desktop.
        self.element = Mock()
        self.element.value = "draft"
        self.element.frame.return_value = (100.0, 200.0, 80.0, 16.0)
        # The composer reports itself focused after the click, so typing runs.
        self.element.attribute.return_value = True
        with patch("kakaoread.macauto.ax.application", return_value=Mock()):
            self.backend = AXBackend(99)
        self.control = self.backend._register(self.element)
        activate = patch.object(self.backend, "_activate")
        restore = patch.object(self.backend, "_restore")
        self.activate = activate.start()
        self.restore = restore.start()
        self.addCleanup(activate.stop)
        self.addCleanup(restore.stop)

    def test_the_message_is_written_with_a_value_then_verified(self) -> None:
        # The text is placed with an AXValue write, which both fills the
        # composer and enables the client's send button; success is confirmed
        # by reading the value back.
        text = "synthetic \uac00\U0001f642\nnext"
        self.element.set_attribute.return_value = True
        self.element.value = text  # the write sticks
        self.backend.set_text(self.control, text)
        self.activate.assert_called_once_with()
        self.element.set_attribute.assert_any_call("AXValue", text)

    def test_a_write_that_does_not_stick_is_retried_then_fails(self) -> None:
        self.element.set_attribute.return_value = True
        self.element.value = "draft"  # never becomes the text
        with self.assertRaises(OSError):
            self.backend.set_text(self.control, "synthetic")
        # It tried more than once before giving up.
        self.assertGreater(self.element.set_attribute.call_count, 1)

    def test_a_refused_write_is_a_failure(self) -> None:
        self.element.set_attribute.return_value = False
        self.element.value = "draft"
        with self.assertRaises(OSError):
            self.backend.set_text(self.control, "synthetic")

    def test_the_box_is_never_read_as_a_draft_while_writing(self) -> None:
        self.element.set_attribute.return_value = True
        self.element.value = "synthetic"
        with patch.object(self.backend, "get_text", side_effect=AssertionError("draft read")):
            self.backend.set_text(self.control, "synthetic")

    def test_the_send_button_is_pressed_and_success_is_the_box_emptying(self) -> None:
        # submit presses 전송; it is a success because the composer goes empty,
        # not because the press call says so (this client lies about that).
        button = Mock()
        button.role = "AXButton"
        button.title = "전송"
        button.perform.return_value = False  # the client under-reports success
        window = Mock()
        window.descendants.return_value = [button]
        self.backend._windows_by_control[self.control] = window
        self.element.value = ""  # the box emptied → sent
        self.backend.submit(self.control)
        button.perform.assert_called_once_with("AXPress")
        self.restore.assert_called_once_with()

    def test_a_message_left_in_the_box_is_a_failed_send(self) -> None:
        button = Mock()
        button.role = "AXButton"
        button.title = "전송"
        window = Mock()
        window.descendants.return_value = [button]
        self.backend._windows_by_control[self.control] = window
        self.element.value = "still here"  # never emptied → not sent
        with self.assertRaises(OSError):
            self.backend.submit(self.control)
        self.restore.assert_called_once_with()

    def test_return_is_the_fallback_when_there_is_no_send_button(self) -> None:
        # With no 전송 button, submit falls back to a session Return.
        self.element.value = ""
        with patch("kakaoread.macauto.ax.send_key") as send_key:
            self.backend.submit(self.control)
        send_key.assert_called_once_with(ax.KEY_RETURN)
        self.restore.assert_called_once_with()

    def test_the_foreground_is_restored_even_when_sending_fails(self) -> None:
        with patch("kakaoread.macauto.ax.send_key", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.backend.submit(self.control)
        self.restore.assert_called_once_with()

    def test_empty_hints_are_not_drafts(self) -> None:
        for hint in ("메시지 입력", "Enter message", "", None):
            with self.subTest(hint=hint):
                self.element.value = hint
                self.assertEqual(self.backend.get_text(self.control), "")
        self.element.value = "synthetic draft"
        self.assertEqual(self.backend.get_text(self.control), "synthetic draft")

    def test_accessibility_denial_is_reported_as_a_send_failure(self) -> None:
        backend = FakeWindows()
        with patch.object(backend, "submit", side_effect=ax.AccessibilityDenied("synthetic denial")):
            with self.assertRaises(KakaoReadError) as raised:
                Sender(backend, running=lambda: True).send("Synthetic room", "text")
        self.assertEqual(raised.exception.code, ErrorCode.SEND_FAILED)
