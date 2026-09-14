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
        self.log: list[object] = []
        self.closed: list[str] = []
        self.list_control: int | None = LIST_CONTROL
        self.restored = 0
        self.restorable = True
        self.draft = ""
        self.actions: list[str] = []

    # -- opening --------------------------------------------------------
    def room_list(self) -> int | None:
        return self.list_control

    def show_main_window(self) -> bool:
        self.restored += 1
        self.list_control = LIST_CONTROL if self.restorable else None
        return self.list_control is not None

    def scroll_to_top(self, control: int) -> None:
        assert control == LIST_CONTROL
        self.log.append("top")

    def click_row(self, control: int, row: int) -> None:
        assert control == LIST_CONTROL
        self.clicks.append(row)
        self.log.append(row)
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


class AliasTests(unittest.TestCase):
    """A window carries the client's title, which is not always the catalog's."""

    def test_a_room_known_by_another_title_still_opens(self) -> None:
        desktop = FakeDesktop(["나상권"])
        window = opener(desktop).open("나와의 채팅", aliases=["나상권"])
        self.assertEqual(window.title, "나상권")

    def test_an_alias_is_matched_for_an_already_open_window(self) -> None:
        desktop = FakeDesktop(["나상권"], already_open=["나상권"])
        sender = Sender(desktop, running=lambda: True, opener=opener(desktop))
        result = sender.send("나와의 채팅", "안녕", aliases=["나상권"])
        self.assertEqual(result["room"], "나상권")
        self.assertEqual(desktop.clicks, [])

    def test_an_alias_does_not_widen_an_ordinary_match(self) -> None:
        desktop = FakeDesktop(["다른 방"])
        with self.assertRaises(KakaoReadError):
            opener(desktop).open("받는 방", aliases=["또 다른 방"])


class MainWindowTests(unittest.TestCase):
    """The room list lives in the main window, which may be in the tray."""

    def test_a_hidden_main_window_is_restored_before_giving_up(self) -> None:
        desktop = FakeDesktop(["받는 방"])
        desktop.list_control = None
        window = opener(desktop).open("받는 방")
        self.assertEqual(window.title, "받는 방")
        self.assertEqual(desktop.restored, 1)

    def test_a_main_window_that_cannot_be_restored_is_a_typed_failure(self) -> None:
        desktop = FakeDesktop(["받는 방"])
        desktop.list_control = None
        desktop.restorable = False
        with self.assertRaises(KakaoReadError) as caught:
            opener(desktop).open("받는 방")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)

    def test_a_visible_main_window_is_left_alone(self) -> None:
        desktop = FakeDesktop(["받는 방"])
        opener(desktop).open("받는 방")
        self.assertEqual(desktop.restored, 0, "the main window was disturbed for nothing")


class StoredTitleTests(unittest.TestCase):
    """The alias the CLI passes comes from the catalog's own title column."""

    def test_the_self_chat_keeps_the_name_its_window_carries(self) -> None:
        from tests.fixtures import SELF_ROOM, SELF_STORED_TITLE, build_reader

        with build_reader() as reader:
            room = reader.room(SELF_ROOM)
            assert room is not None
            self.assertEqual(room.title, "나와의 채팅")
            self.assertEqual(reader.stored_title(SELF_ROOM), SELF_STORED_TITLE)

    def test_a_room_without_a_stored_title_has_no_alias(self) -> None:
        from tests.fixtures import GROUP_ROOM, build_reader

        with build_reader() as reader:
            self.assertIsNone(reader.stored_title(GROUP_ROOM))


class ScrollTests(unittest.TestCase):
    """A row number only means something when the list is at the top.

    Opening rooms moves the list, so a scan that did not reset it addressed
    different rooms as it went: a room found at row 0 one minute was missed
    across all eight rows the next.
    """

    def test_the_list_is_pinned_to_the_top_before_every_row(self) -> None:
        desktop = FakeDesktop(["남의 방", "찾는 방"])
        opener(desktop).open("찾는 방")
        self.assertEqual(desktop.log, ["top", 0, "top", 1])

    def test_an_open_room_needs_no_scrolling(self) -> None:
        desktop = FakeDesktop(["받는 방"], already_open=["받는 방"])
        Sender(desktop, running=lambda: True, opener=opener(desktop)).send(
            "받는 방", "안녕", open_if_needed=True
        )
        self.assertEqual(desktop.log, [])


if __name__ == "__main__":
    unittest.main()
