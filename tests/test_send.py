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
        # Patch only the AX boundary; no live app or Accessibility grant.
        self.element = Mock()
        self.element.value = "draft"
        with patch("kakaoread.macauto.ax.application", return_value=Mock()):
            self.backend = AXBackend(99)
        self.control = self.backend._register(self.element)

    def test_edit_replaces_the_value_atomically_including_unicode_and_newlines(self) -> None:
        text = "synthetic \uac00\U0001f642\nnext"
        self.backend.set_text(self.control, text)
        self.element.set_attribute.assert_called_once_with("AXValue", text)

    def test_the_box_is_never_read_before_it_is_written(self) -> None:
        with patch.object(self.backend, "get_text", side_effect=AssertionError("draft read")):
            self.backend.set_text(self.control, "synthetic")
        self.element.set_attribute.assert_called_once_with("AXValue", "synthetic")

    def test_a_refused_value_write_never_reports_success(self) -> None:
        self.element.set_attribute.return_value = False
        with self.assertRaises(OSError):
            self.backend.set_text(self.control, "synthetic")

    def test_return_is_addressed_to_the_client_process(self) -> None:
        with patch("kakaoread.macauto.ax.post_key") as post:
            self.backend.submit(self.control)
        post.assert_called_once_with(99, ax.KEY_RETURN)

    def test_failed_return_is_not_ignored(self) -> None:
        with patch("kakaoread.macauto.ax.post_key", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.backend.submit(self.control)

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
