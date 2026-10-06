"""Synthetic file-send tests. No desktop calls and no real files beyond a temp one."""

import os
import tempfile
import unittest

from kanao.errors import ErrorCode, KanaoError
from kanao.winauto import ChatWindow, FILE_BUTTON, Sender, scaled

DIALOG = 100
POPUP = 200


class FakeFileWindows:
    """The three windows of a file send, each able to fail the way the client can."""

    def __init__(self) -> None:
        self.windows = [ChatWindow(42, "Synthetic room")]
        self.actions: list[str] = []
        self.visible: set[int] = set()
        self.parked = True
        self.restores = True
        self.dialog_appears = True
        self.name_takes = True
        self.popup_appears = True
        self.popup_closes = True
        self.foreground_window = 7
        self.chosen = ""
        self.locked = False
        self.minimise_taken = True
        self.presses_ignored = 0
        """How many presses of the file button the client lets go by first."""

    def chat_windows(self) -> list[ChatWindow]:
        return self.windows

    def input_handle(self, window: int) -> int | None:
        return 84

    def get_text(self, control: int) -> str:
        return ""

    def set_text(self, control: int, text: str) -> None:
        self.actions.append("fill")

    def submit(self, control: int) -> None:
        self.actions.append("submit")

    def top_windows(self) -> set[int]:
        return set(self.visible)

    def show_unfocused(self, handle: int) -> bool:
        was = self.parked
        if was:
            self.actions.append("restore")
            if self.restores:
                self.parked = False
        return was

    def is_minimized(self, handle: int) -> bool:
        return self.parked

    def minimize_window(self, handle: int) -> None:
        self.actions.append("park")
        if self.minimise_taken:
            self.parked = True

    def park_quietly(self, handle: int) -> None:
        self.actions.append("park quietly")
        self.parked = True

    def desktop_locked(self) -> bool:
        return self.locked

    def foreground(self) -> int | None:
        return self.foreground_window

    def return_focus(self, handle: int) -> None:
        self.actions.append(f"focus {handle}")

    def click_file_button(self, handle: int) -> None:
        self.actions.append("file button")
        if self.presses_ignored:
            self.presses_ignored -= 1
            return
        if self.dialog_appears:
            self.visible.add(DIALOG)

    def file_dialog(self, handle: int) -> int | None:
        return DIALOG if DIALOG in self.visible else None

    def choose_file(self, dialog: int, path: str) -> None:
        self.actions.append("choose")
        if not self.name_takes:
            raise OSError("synthetic: the file name box did not take the path")
        self.chosen = path
        self.visible.discard(DIALOG)
        if self.popup_appears:
            self.visible.add(POPUP)

    def cancel_dialog(self, dialog: int) -> None:
        self.actions.append("cancel")
        self.visible.discard(dialog)

    def file_popup(self, before: set[int]) -> int | None:
        return POPUP if POPUP in self.visible and POPUP not in before else None

    def press_popup_send(self, popup: int) -> None:
        self.actions.append("send")
        if self.popup_closes:
            self.visible.discard(popup)

    def close_window(self, handle: int) -> None:
        self.actions.append(f"close {handle}")
        self.visible.discard(handle)

    def is_visible(self, handle: int) -> bool:
        return handle in self.visible


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeFileWindows()
        self.sender = Sender(self.backend, running=lambda: True, settle=lambda: None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "synthetic.png")
        with open(self.path, "wb") as handle:
            handle.write(b"\x89PNG synthetic bytes")

    def failure(self) -> KanaoError:
        with self.assertRaises(KanaoError) as caught:
            self.sender.upload("Synthetic room", self.path)
        return caught.exception

    def test_a_file_goes_through_the_button_the_dialog_and_the_confirmation(self) -> None:
        result = self.sender.upload("Synthetic room", self.path)
        self.assertEqual(
            result,
            {"submitted": True, "room": "Synthetic room", "name": "synthetic.png", "size": 20},
        )
        self.assertEqual(self.backend.chosen, self.path)
        self.assertEqual(
            self.backend.actions,
            ["restore", "file button", "choose", "send", "park", "focus 7"],
        )

    def test_a_room_already_open_is_neither_restored_nor_parked(self) -> None:
        self.backend.parked = False
        self.sender.upload("Synthetic room", self.path)
        self.assertEqual(self.backend.actions, ["file button", "choose", "send", "focus 7"])

    def test_the_keyboard_is_not_handed_to_the_room_itself(self) -> None:
        self.backend.foreground_window = 42
        self.sender.upload("Synthetic room", self.path)
        self.assertNotIn("focus 42", self.backend.actions)

    def test_a_press_the_client_lets_go_by_is_pressed_once_more(self) -> None:
        self.backend.presses_ignored = 1
        self.sender.upload("Synthetic room", self.path)
        self.assertEqual(self.backend.actions.count("file button"), 2)
        self.assertIn("send", self.backend.actions)

    def test_no_dialog_sends_nothing_and_parks_the_room_again(self) -> None:
        self.backend.dialog_appears = False
        error = self.failure()
        self.assertIs(error.code, ErrorCode.SEND_FAILED)
        self.assertIn("nothing was sent", error.message)
        self.assertEqual(self.backend.actions.count("file button"), 2)
        self.assertNotIn("send", self.backend.actions)
        self.assertEqual(self.backend.actions[-2:], ["park", "focus 7"])

    def test_a_name_that_does_not_take_cancels_the_dialog(self) -> None:
        self.backend.name_takes = False
        error = self.failure()
        self.assertIn("nothing was sent", error.message)
        self.assertIn("cancel", self.backend.actions)
        self.assertNotIn("send", self.backend.actions)

    def test_no_confirmation_sends_nothing(self) -> None:
        self.backend.popup_appears = False
        error = self.failure()
        self.assertIn("nothing was sent", error.message)
        self.assertNotIn("send", self.backend.actions)

    def test_a_confirmation_that_stays_is_closed_and_reported_unknown(self) -> None:
        self.backend.popup_closes = False
        error = self.failure()
        self.assertIs(error.code, ErrorCode.SEND_FAILED)
        self.assertIn("unknown", error.message)
        self.assertNotIn("nothing was sent", error.message)
        self.assertIn(f"close {POPUP}", self.backend.actions)
        self.assertEqual(self.backend.actions.count("send"), 1)

    def test_a_room_that_will_not_restore_is_never_clicked(self) -> None:
        self.backend.restores = False
        error = self.failure()
        self.assertIn("nothing was sent", error.message)
        self.assertNotIn("file button", self.backend.actions)
        self.assertIn("park", self.backend.actions)

    def test_a_missing_or_empty_file_touches_no_window(self) -> None:
        empty = self.path + ".empty"
        open(empty, "wb").close()
        for path in (self.path + ".missing", empty):
            with self.subTest(path=path):
                with self.assertRaises(KanaoError) as caught:
                    self.sender.upload("Synthetic room", path)
                self.assertIs(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, [])

    def test_a_locked_desktop_sends_nothing_and_touches_no_window(self) -> None:
        self.backend.locked = True
        error = self.failure()
        self.assertIs(error.code, ErrorCode.SEND_FAILED)
        self.assertIn("locked", error.message)
        self.assertIn("nothing was sent", error.message)
        self.assertEqual(self.backend.actions, [])

    def test_a_room_whose_minimise_is_not_taken_is_parked_without_activation(self) -> None:
        self.backend.minimise_taken = False
        self.sender.upload("Synthetic room", self.path)
        self.assertEqual(self.backend.actions[-3:], ["park", "park quietly", "focus 7"])
        self.assertTrue(self.backend.parked)

    def test_a_coordinate_follows_the_window_s_scaling(self) -> None:
        self.assertEqual(scaled(FILE_BUTTON, 96), FILE_BUTTON)
        self.assertEqual(scaled(FILE_BUTTON, 144), (132, 36))
        self.assertEqual(scaled(FILE_BUTTON, 0), FILE_BUTTON)


if __name__ == "__main__":
    unittest.main()
