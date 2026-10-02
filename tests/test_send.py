"""Synthetic send tests. No desktop calls and no real messages."""

from __future__ import annotations

import unittest
from typing import cast
from unittest.mock import Mock, patch

from kamado.errors import ErrorCode, KamadoError
from kamado import ax
from kamado.macauto import AXBackend, ChatWindow, Sender


class FakeWindows:
    def __init__(self) -> None:
        self.windows = [ChatWindow(42, "Synthetic room")]
        self.draft = ""
        self.control = 84
        self.actions: list[str] = []
        self.fail_submit = False

    def chat_windows(self, hit_test: bool = True) -> list[ChatWindow]:
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
        with self.assertRaises(KamadoError) as caught:
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
        self.assertIs(self.sender.send("Synthetic room", "replacement").get("clearedDraft"), True)

    def test_an_empty_box_reports_that_nothing_was_cleared(self) -> None:
        self.assertIs(self.sender.send("Synthetic room", "synthetic text").get("clearedDraft"), False)

    def test_dry_run_never_types_or_submits(self) -> None:
        result = self.sender.send("Synthetic room", "synthetic text", dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.backend.actions, [])

    def test_dry_run_warns_that_a_draft_would_be_lost(self) -> None:
        """The one call that changes nothing is where the warning is worth having."""
        self.backend.draft = "an unfinished synthetic draft"
        result = self.sender.send("Synthetic room", "replacement", dry_run=True)
        self.assertIs(result.get("wouldClearDraft"), True)
        self.assertEqual(self.backend.draft, "an unfinished synthetic draft")

    def test_queue_acceptance_is_not_reported_as_delivery(self) -> None:
        result = self.sender.send("Synthetic room", "synthetic text")
        self.assertTrue(result["submitted"])
        self.assertNotIn("sent", result)
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_duplicate_exact_titles_are_refused(self) -> None:
        self.backend.windows.append(ChatWindow(43, "Synthetic room"))
        with self.assertRaises(KamadoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertEqual(self.backend.actions, [])

    def test_exact_title_beats_substring(self) -> None:
        self.backend.windows.append(ChatWindow(43, "Synthetic room extra"))
        self.assertEqual(self.sender.resolve("Synthetic room").handle, 42)

    def test_unopened_exact_room_cannot_target_a_longer_title(self) -> None:
        self.backend.windows = [ChatWindow(43, "Synthetic room extra")]
        with self.assertRaises(KamadoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(self.backend.actions, [])

    def test_a_hidden_window_is_recovered_before_any_write(self) -> None:
        self.backend.windows = []

        def recover(wanted: set[str]) -> list[ChatWindow]:
            self.backend.actions.append("recover")
            self.assertEqual(wanted, {"synthetic room"})
            return [ChatWindow(42, "Synthetic room")]

        sender = Sender(self.backend, running=lambda: True, recoverer=recover)
        sender.send("Synthetic room", "synthetic text")

        self.assertEqual(self.backend.actions, ["recover", "fill", "submit"])

    def test_invalid_message_is_refused_without_writes(self) -> None:
        for text in ("", " \n", "hello\x00world"):
            with self.subTest(text=text):
                with self.assertRaises(KamadoError) as caught:
                    self.sender.send("Synthetic room", text)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, [])

    def test_closed_client_is_a_typed_failure(self) -> None:
        sender = Sender(self.backend, running=lambda: False)
        with self.assertRaises(KamadoError) as caught:
            sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.KAKAO_NOT_RUNNING)

    def test_missing_input_is_a_typed_failure(self) -> None:
        self.backend.control = 0
        with self.assertRaises(KamadoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.INPUT_NOT_FOUND)
        self.assertEqual(self.backend.actions, [])

    def test_submit_failure_does_not_retry_or_erase_draft(self) -> None:
        self.backend.fail_submit = True
        with self.assertRaises(KamadoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)
        self.assertEqual(self.backend.actions, ["fill", "submit"])
        self.assertEqual(self.backend.draft, "synthetic text")


class AXMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        # Patch only the AX boundary; no live app and no Accessibility grant.
        # Nothing here stubs activation, because the send path never activates.
        self.element = Mock()
        self.element.value = "draft"
        with patch("kamado.macauto.ax.application", return_value=Mock()):
            self.backend = AXBackend(99)
        self.control = self.backend._register(self.element)
        self.elapsed = 0.0
        clock = patch("kamado.macauto.time.monotonic", side_effect=lambda: self.elapsed)
        sleep = patch("kamado.macauto.time.sleep", side_effect=self.advance)
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds

    def test_the_message_is_written_with_a_value_and_nothing_else(self) -> None:
        # One AXValue write fills the composer and enables the client's send
        # button; success is confirmed by reading the value back. Nothing is
        # activated, clicked or typed - that is the whole point of this path.
        text = "synthetic \uac00\U0001f642\nnext"
        self.element.set_attribute.return_value = True
        self.element.value = text  # the write sticks
        with patch("kamado.macauto.subprocess.run") as run:
            self.backend.set_text(self.control, text)
        self.element.set_attribute.assert_called_once_with("AXValue", text)
        run.assert_not_called()  # the client is never brought forward

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

    def _window_with_send_button(self, press_returns=False) -> "Mock":
        button = Mock()
        button.role = "AXButton"
        button.title = "전송"
        button.attribute.return_value = True
        button.perform.return_value = press_returns
        window = Mock()
        # The button is a *direct* child of the chat window, so submit reads
        # that one list instead of crawling the transcript below it.
        window.children.return_value = [button]
        self.backend._windows_by_control[self.control] = window
        return button

    def test_the_send_button_is_pressed_and_success_is_the_box_emptying(self) -> None:
        # submit presses 전송; it is a success because the composer goes empty,
        # not because the press call says so (this client lies about that).
        button = self._window_with_send_button(press_returns=False)
        self.element.value = ""  # the box emptied → sent
        with patch("kamado.macauto.subprocess.run") as run:
            self.backend.submit(self.control)
        button.perform.assert_called_once_with("AXPress")
        run.assert_not_called()  # sending never activates the client

    def test_the_button_is_found_among_direct_children_not_by_crawling(self) -> None:
        button = self._window_with_send_button()
        # The test put a Mock in there; the mapping is typed with the real
        # element, which is what makes `assert_not_called` unreachable.
        window = cast(Mock, self.backend._windows_by_control[self.control])
        self.element.value = ""
        self.backend.submit(self.control)
        button.perform.assert_called_once_with("AXPress")
        # Crawling the subtree costs an IPC round trip per node; it is not done.
        window.descendants.assert_not_called()

    def test_a_message_left_in_the_box_is_a_failed_send(self) -> None:
        self._window_with_send_button()
        self.element.value = "still here"  # never emptied → not sent
        with self.assertRaises(OSError):
            self.backend.submit(self.control)

    def test_a_missing_button_never_sends_return_to_another_composer(self) -> None:
        # A PID names every chat, not the selected chat. Missing controls must
        # fail rather than deliver Return to whichever window happens to be key.
        self.element.value = ""
        with patch("kamado.macauto.ax.post_key") as post_key:
            with self.assertRaises(OSError):
                self.backend.submit(self.control)
        post_key.assert_not_called()

    def test_an_unreadable_composer_is_not_a_successful_submission(self) -> None:
        self._window_with_send_button()
        self.element.value = None
        with self.assertRaises(OSError):
            self.backend.submit(self.control)

    def test_a_disabled_or_unreadable_button_is_never_pressed(self) -> None:
        for enabled in (False, None):
            with self.subTest(enabled=enabled):
                button = self._window_with_send_button()
                button.attribute.return_value = enabled
                self.element.value = ""
                with self.assertRaises(OSError):
                    self.backend.submit(self.control)
                button.perform.assert_not_called()

    def test_the_button_can_enable_after_the_write_without_a_fixed_delay(self) -> None:
        button = self._window_with_send_button()
        button.attribute.side_effect = [False, True]
        self.element.value = ""
        self.backend.submit(self.control)
        button.perform.assert_called_once_with("AXPress")

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
            with self.assertRaises(KamadoError) as raised:
                Sender(backend, running=lambda: True).send("Synthetic room", "text")
        self.assertEqual(raised.exception.code, ErrorCode.SEND_FAILED)
