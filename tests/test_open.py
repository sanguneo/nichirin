"""Opening a room by posting a double-click, and refusing to send if it missed.

The room list is owner-drawn: its rows are not windows and not UIA elements,
so a row can only be addressed by coordinate. That makes a miss ordinary
rather than exceptional, which is why every test here is about what happens
when the wrong room opens.
"""

from __future__ import annotations

import unittest

from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.winauto import ChatWindow, RoomOpener, Sender

LIST_CONTROL = 900


class FakeDesktop:
    """A room list where each row opens the room the test put there."""

    def __init__(self, rows: list[str], already_open: list[str] | None = None) -> None:
        self.rows = rows
        self.windows = [
            ChatWindow(handle=100 + index, title=title)
            for index, title in enumerate(already_open or [])
        ]
        self.clicks: list[int] = []
        self.closed: list[str] = []
        self.list_control: int | None = LIST_CONTROL
        self.draft = ""
        self.actions: list[str] = []

    # -- opening --------------------------------------------------------
    def room_list(self) -> int | None:
        return self.list_control

    def click_row(self, control: int, row: int) -> None:
        assert control == LIST_CONTROL
        self.clicks.append(row)
        if row < len(self.rows):
            title = self.rows[row]
            if title not in [window.title for window in self.windows]:
                self.windows.append(ChatWindow(handle=200 + row, title=title))

    def close_window(self, handle: int) -> None:
        for window in list(self.windows):
            if window.handle == handle:
                self.closed.append(window.title)
                self.windows.remove(window)

    # -- sending --------------------------------------------------------
    def chat_windows(self) -> list[ChatWindow]:
        return list(self.windows)

    def input_handle(self, window: int) -> int | None:
        return window + 1

    def get_text(self, control: int) -> str:
        return self.draft

    def set_text(self, control: int, text: str) -> None:
        self.actions.append("fill")
        self.draft = text

    def submit(self, control: int) -> None:
        self.actions.append("submit")


def opener(desktop: FakeDesktop, rows: int = 6) -> RoomOpener:
    # The settle is injected so the suite never waits on a clock.
    return RoomOpener(desktop, settle=lambda: None, rows=rows)


class OpeningTests(unittest.TestCase):
    def test_the_row_holding_the_room_opens_it(self) -> None:
        desktop = FakeDesktop(["첫 번째", "두 번째", "세 번째"])
        window = opener(desktop).open("세 번째")
        self.assertEqual(window.title, "세 번째")

    def test_a_room_that_opened_by_mistake_is_closed_again(self) -> None:
        desktop = FakeDesktop(["남의 방", "찾는 방"])
        opener(desktop).open("찾는 방")
        self.assertEqual(desktop.closed, ["남의 방"], "a stray window was left open")

    def test_the_target_is_never_closed(self) -> None:
        desktop = FakeDesktop(["남의 방", "찾는 방"])
        opener(desktop).open("찾는 방")
        self.assertNotIn("찾는 방", desktop.closed)

    def test_a_room_that_is_not_in_the_list_is_refused(self) -> None:
        desktop = FakeDesktop(["하나", "둘"])
        with self.assertRaises(KakaoReadError) as caught:
            opener(desktop).open("없는 방")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)

    def test_every_room_opened_while_searching_is_closed_again(self) -> None:
        desktop = FakeDesktop(["하나", "둘"])
        with self.assertRaises(KakaoReadError):
            opener(desktop).open("없는 방")
        self.assertEqual(sorted(desktop.closed), ["둘", "하나"])

    def test_a_missing_room_list_is_a_typed_failure(self) -> None:
        desktop = FakeDesktop(["하나"])
        desktop.list_control = None
        with self.assertRaises(KakaoReadError) as caught:
            opener(desktop).open("하나")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)

    def test_the_search_stops_at_the_row_budget(self) -> None:
        desktop = FakeDesktop(["a", "b", "c", "d"])
        with self.assertRaises(KakaoReadError):
            opener(desktop, rows=2).open("d")
        self.assertEqual(desktop.clicks, [0, 1])


class SendWithOpeningTests(unittest.TestCase):
    def sender(self, desktop: FakeDesktop) -> Sender:
        return Sender(desktop, running=lambda: True, opener=opener(desktop))

    def test_a_closed_room_is_opened_and_then_written_to(self) -> None:
        desktop = FakeDesktop(["남의 방", "받는 방"])
        result = self.sender(desktop).send("받는 방", "안녕", open_if_needed=True)
        self.assertTrue(result["submitted"])
        self.assertEqual(desktop.actions, ["fill", "submit"])
        self.assertEqual(desktop.closed, ["남의 방"])

    def test_without_the_flag_a_closed_room_is_still_refused(self) -> None:
        desktop = FakeDesktop(["받는 방"])
        with self.assertRaises(KakaoReadError) as caught:
            self.sender(desktop).send("받는 방", "안녕")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(desktop.clicks, [], "a send without --open touched the room list")

    def test_an_open_room_is_never_clicked_for(self) -> None:
        desktop = FakeDesktop(["받는 방"], already_open=["받는 방"])
        self.sender(desktop).send("받는 방", "안녕", open_if_needed=True)
        self.assertEqual(desktop.clicks, [])

    def test_a_dry_run_may_open_but_never_writes(self) -> None:
        desktop = FakeDesktop(["받는 방"])
        result = self.sender(desktop).send("받는 방", "안녕", dry_run=True, open_if_needed=True)
        self.assertFalse(result["submitted"])
        self.assertEqual(desktop.actions, [])


if __name__ == "__main__":
    unittest.main()
