"""Synthetic send tests. No desktop calls and no real messages."""

import unittest
import ctypes
from unittest.mock import Mock, patch

from kanao.errors import ErrorCode, KanaoError
from kanao.winauto import ChatWindow, Sender, Win32Backend

from tests.platform import require_windows_libraries


class FakeWindows:
    def __init__(self) -> None:
        self.windows = [ChatWindow(42, "Synthetic room")]
        self.draft = ""
        self.control = 84
        self.actions: list[str] = []
        self.fail_submit = False
        self.keeps_draft = False
        """A client that never takes the message, leaving it in the box."""
        self.unreadable = False
        self.unreadable_after_submit = False
        """A box that cannot be read once the Enter has been posted."""

    def chat_windows(self) -> list[ChatWindow]:
        return self.windows

    def input_handle(self, window: int) -> int | None:
        return self.control or None

    def get_text(self, control: int) -> str:
        if self.unreadable:
            raise OSError("synthetic cross-process read failure")
        return self.draft

    def set_text(self, control: int, text: str) -> None:
        self.actions.append("fill")
        self.draft = text

    def submit(self, control: int) -> None:
        self.actions.append("submit")
        if self.fail_submit:
            raise OSError("synthetic queue failure")
        # The real client empties its input box when it takes the message,
        # and that emptying is the only evidence a send has that it happened.
        if self.unreadable_after_submit:
            self.unreadable = True
        elif not self.keeps_draft:
            self.draft = ""


class SendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeWindows()
        self.settles = 0
        self.sender = Sender(
            self.backend, running=lambda: True, settle=self.settle, attempts=4
        )

    def settle(self) -> None:
        """Stand in for the wait between looks at the box; no test sleeps."""
        self.settles += 1

    def test_empty_target_cannot_resolve_the_only_window(self) -> None:
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("  ", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, [])

    def test_an_unfinished_draft_is_cleared_and_the_message_goes(self) -> None:
        self.backend.draft = "an unfinished synthetic draft"
        result = self.sender.send("Synthetic room", "replacement")
        self.assertTrue(result["submitted"])
        # The box is empty because the client took the message, which is the
        # same fact `submitted` now reports.
        self.assertEqual(self.backend.draft, "")
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

    def test_a_client_that_took_the_message_is_not_waited_for(self) -> None:
        """The ceiling is three seconds; the cost on a working client is one read."""
        self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(self.settles, 0)

    def test_a_message_left_in_the_box_is_not_reported_as_submitted(self) -> None:
        """Posting Enter only queues a keystroke; the client may never act on it."""
        self.backend.keeps_draft = True
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)
        self.assertEqual(self.backend.draft, "synthetic text")
        self.assertEqual(self.settles, 3)

    def test_an_unreadable_box_is_not_a_successful_submission(self) -> None:
        """An unknown outcome reported as success is the failure being fixed."""
        self.backend.unreadable_after_submit = True
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)

    def test_the_box_is_read_until_it_empties_rather_than_once(self) -> None:
        """A client that is slow to take the message has not failed to take it."""
        self.backend.keeps_draft = True
        counted = self.settle

        def settle_then_clear() -> None:
            counted()
            if self.settles == 2:
                self.backend.draft = ""

        self.sender._settle = settle_then_clear
        self.assertTrue(self.sender.send("Synthetic room", "synthetic text")["submitted"])
        self.assertEqual(self.settles, 2)

    def test_duplicate_exact_titles_are_refused(self) -> None:
        self.backend.windows.append(ChatWindow(43, "Synthetic room"))
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertEqual(self.backend.actions, [])

    def test_exact_title_beats_substring(self) -> None:
        self.backend.windows.append(ChatWindow(43, "Synthetic room extra"))
        self.assertEqual(self.sender.resolve("Synthetic room").handle, 42)

    def test_unopened_exact_room_cannot_target_a_longer_title(self) -> None:
        self.backend.windows = [ChatWindow(43, "Synthetic room extra")]
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(self.backend.actions, [])

    def test_invalid_message_is_refused_without_writes(self) -> None:
        for text in ("", " \n", "hello\x00world"):
            with self.subTest(text=text):
                with self.assertRaises(KanaoError) as caught:
                    self.sender.send("Synthetic room", text)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, [])

    def test_closed_client_is_a_typed_failure(self) -> None:
        sender = Sender(self.backend, running=lambda: False)
        with self.assertRaises(KanaoError) as caught:
            sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.KAKAO_NOT_RUNNING)

    def test_missing_input_is_a_typed_failure(self) -> None:
        self.backend.control = 0
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.INPUT_NOT_FOUND)
        self.assertEqual(self.backend.actions, [])

    def test_submit_failure_does_not_retry_or_erase_draft(self) -> None:
        self.backend.fail_submit = True
        with self.assertRaises(KanaoError) as caught:
            self.sender.send("Synthetic room", "synthetic text")
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)
        self.assertEqual(self.backend.actions, ["fill", "submit"])
        self.assertEqual(self.backend.draft, "synthetic text")


class Win32MessageTests(unittest.TestCase):
    def setUp(self) -> None:
        # Patch the OS boundary. The real adapter still builds its native calls.
        require_windows_libraries()
        self.user32 = Mock()
        dll = patch("ctypes.WinDLL", return_value=self.user32)
        dll.start()
        self.addCleanup(dll.stop)
        self.backend = Win32Backend(99)

    def test_edit_uses_selection_replacement_not_settext(self) -> None:
        message = Mock(return_value=0)
        reads = iter(["synthetic \uac00\U0001f642\nnext"])
        with patch.object(self.backend, "_message", message, create=True), patch.object(
            self.backend, "get_text", side_effect=lambda _: next(reads)
        ):
            self.backend.set_text(84, "synthetic \uac00\U0001f642\nnext")
        codes = [call.args[1] for call in message.call_args_list]
        self.assertEqual(codes, [0x00B1, 0x00C2])
        self.user32.SendMessageW.assert_not_called()

    def test_the_box_is_never_read_before_it_is_written(self) -> None:
        """A draft typed a moment ago loses: the selection covers the whole box.

        The only read is the one that verifies what landed, so nothing the user
        types between the check and the write can stop the send.
        """
        message = Mock(return_value=0)
        reader = Mock(return_value="synthetic")
        with patch.object(self.backend, "_message", message, create=True), patch.object(
            self.backend, "get_text", reader
        ):
            self.backend.set_text(84, "synthetic")
        codes = [call.args[1] for call in message.call_args_list]
        self.assertEqual(codes, [0x00B1, 0x00C2], "the box was not selected and replaced")
        self.assertEqual(reader.call_count, 1, "the box was read before being written")

    def test_incorrect_insert_never_reports_success(self) -> None:
        with patch.object(self.backend, "_message", return_value=0, create=True), patch.object(
            self.backend, "get_text", side_effect=["truncated"]
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


class DpiTests(unittest.TestCase):
    """A room on a 200% monitor, seen by a DPI-unaware process (#82).

    Unaware, Windows reports the window at 96 DPI - 1/1.25 of its size here -
    while GetDpiForWindow still says 192. The file button is 48 px up from the
    real bottom edge at 192 DPI, so its click must land in the button row
    (y 962-1020 of a 1020-high window), not in the message list above it.
    """

    PHYSICAL = (1000, 100, 1934, 1120)
    VIRTUAL = (800, 80, 1547, 896)

    def setUp(self) -> None:
        require_windows_libraries()
        self.aware = False
        self.user32 = Mock()
        dll = patch("ctypes.WinDLL", return_value=self.user32)
        dll.start()
        self.addCleanup(dll.stop)
        self.backend = Win32Backend(99)
        self.sent: list[tuple[int, int]] = []

        def set_context(context):
            previous = "aware" if self.aware else "unaware"
            self.aware = context != "unaware"
            return previous

        def window_rect(handle, reference):
            rect = reference._obj
            rect.left, rect.top, rect.right, rect.bottom = (
                self.PHYSICAL if self.aware else self.VIRTUAL
            )
            return 1

        def to_client(handle, reference):
            point = reference._obj
            left, top = (self.PHYSICAL if self.aware else self.VIRTUAL)[:2]
            point.x -= left
            point.y -= top
            return 1

        def send(handle, message, wparam, lparam, flags, timeout, result):
            if message == 0x0201:  # WM_LBUTTONDOWN
                self.sent.append((lparam & 0xFFFF, (lparam >> 16) & 0xFFFF))
            return 1

        self.user32.SetThreadDpiAwarenessContext.side_effect = set_context
        self.user32.GetWindowRect.side_effect = window_rect
        self.user32.ScreenToClient.side_effect = to_client
        self.user32.GetDpiForWindow.return_value = 192
        self.user32.SendMessageTimeoutW.side_effect = send
        self.user32.IsWindow.return_value = 1

    def test_the_file_button_click_lands_in_the_button_row(self) -> None:
        self.backend.click_file_button(84)
        [(x, y)] = self.sent
        self.assertEqual(x, 176)
        self.assertTrue(962 <= y < 1020, f"the click landed at y={y}, outside the button row")

    def test_the_awareness_is_given_back_afterwards(self) -> None:
        self.backend.click_file_button(84)
        self.assertFalse(self.aware)
