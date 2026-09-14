"""Synthetic send tests. No desktop calls and no real messages."""

import unittest
import ctypes
from unittest.mock import Mock, patch

from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.winauto import ChatWindow, Sender, Win32Backend


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

    def test_existing_draft_is_not_overwritten(self) -> None:
        self.backend.draft = "an unfinished synthetic draft"
        with self.assertRaises(KakaoReadError) as caught:
            self.sender.send("Synthetic room", "replacement")
        self.assertEqual(caught.exception.code, ErrorCode.BUSY)
        self.assertEqual(self.backend.draft, "an unfinished synthetic draft")
        self.assertEqual(self.backend.actions, [])

    def test_dry_run_never_types_or_submits(self) -> None:
        result = self.sender.send("Synthetic room", "synthetic text", dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.backend.actions, [])

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


class Win32MessageTests(unittest.TestCase):
    def setUp(self) -> None:
        # Patch the OS boundary. The real adapter still builds its native calls.
        self.user32 = Mock()
        dll = patch("ctypes.WinDLL", return_value=self.user32)
        dll.start()
        self.addCleanup(dll.stop)
        self.backend = Win32Backend(99)

    def test_edit_uses_selection_replacement_not_settext(self) -> None:
        message = Mock(return_value=0)
        reads = iter(["", "synthetic \uac00\U0001f642\nnext"])
        with patch.object(self.backend, "_message", message, create=True), patch.object(
            self.backend, "get_text", side_effect=lambda _: next(reads)
        ):
            self.backend.set_text(84, "synthetic \uac00\U0001f642\nnext")
        codes = [call.args[1] for call in message.call_args_list]
        self.assertEqual(codes, [0x00B1, 0x00C2])
        self.user32.SendMessageW.assert_not_called()

    def test_changed_draft_at_write_time_is_preserved(self) -> None:
        with patch.object(self.backend, "get_text", return_value="new user draft"):
            with self.assertRaises(KakaoReadError) as caught:
                self.backend.set_text(84, "synthetic")
        self.assertEqual(caught.exception.code, ErrorCode.BUSY)
        self.user32.SendMessageW.assert_not_called()

    def test_incorrect_insert_never_reports_success(self) -> None:
        with patch.object(self.backend, "_message", return_value=0, create=True), patch.object(
            self.backend, "get_text", side_effect=["", "truncated"]
        ):
            with self.assertRaises(OSError):
                self.backend.set_text(84, "synthetic")

    def test_enter_has_scan_code_and_transition_bits(self) -> None:
        self.backend.submit(84)
        calls = self.user32.PostMessageW.call_args_list
        self.assertEqual([call.args for call in calls], [
            (84, 0x0100, 0x0D, 0x001C0001),
            (84, 0x0101, 0x0D, 0xC01C0001),
        ])

    def test_failed_keyup_is_not_ignored(self) -> None:
        self.user32.PostMessageW.side_effect = [1, 0]
        with self.assertRaises(OSError):
            self.backend.submit(84)

    def test_korean_empty_hint_is_not_a_draft(self) -> None:
        hint = "메시지 입력"

        def message(hwnd: int, code: int, wp: int = 0, lp: int = 0) -> int:
            if code == 0x000E:
                return len(hint)
            source = ctypes.create_unicode_buffer(hint)
            ctypes.memmove(lp, source, ctypes.sizeof(source))
            return len(hint)

        with patch.object(self.backend, "_message", side_effect=message, create=True):
            self.assertEqual(self.backend.get_text(84), "")

    def test_a_message_timeout_raises(self) -> None:
        self.user32.SendMessageTimeoutW.return_value = 0
        with self.assertRaises(OSError):
            self.backend._message(84, 0x000E)
