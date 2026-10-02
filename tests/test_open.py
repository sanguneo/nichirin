"""Title-addressed macOS room opening, with a synthetic desktop and clock.

Pixel geometry, DPI, scroll resets and alternating row guesses are gone. The
replacement proves title-first selection, fallback memory, bounded waits and
restoring the main window without ever controlling a live application.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kamado.errors import ErrorCode, KamadoError
from kamado.macauto import ChatWindow, RoomOpener, Sender


class FakeDesktop:
    def __init__(self, rows: list[str], already_open: list[str] | None = None) -> None:
        self.rows = rows
        self.titles = list(rows)
        self.windows = [ChatWindow(100 + index, title) for index, title in enumerate(already_open or [])]
        self.clicks: list[int] = []
        self.log: list[object] = []
        self.closed: list[int] = []
        self.restored = 0
        self.restorable = True
        self.main = "visible"
        self.minimized: list[int] = []
        self.draft = ""
        self.actions: list[str] = []
        self.searchable: list[str] = []
        """Rooms the client only exposes once its own search is used."""
        self.searching = False
        self.searches: list[set[str]] = []
        self.appears = True
        self.elapsed = 0.0
        self.waits: list[float] = []

    def room_rows(self) -> list[str]:
        self.log.append("rows")
        return self.titles

    def locate_row(self, wanted: set[str]) -> int | None:
        self.searches.append(set(wanted))
        self.searching = True
        for index, title in enumerate(self.searchable):
            if title.strip().casefold() in wanted:
                return index
        return None

    def clear_row_filter(self) -> None:
        self.searching = False
        self.log.append("unsearch")

    def open_row(self, row: int) -> bool:
        self.clicks.append(row)
        self.log.append(row)
        listing = self.searchable if self.searching else self.rows
        if row >= len(listing):
            return False
        if self.appears:
            title = listing[row]
            if title not in [window.title for window in self.windows]:
                self.windows.append(ChatWindow(200 + row, title))
        return True

    def main_state(self) -> str:
        return self.main

    def show_main_window(self) -> bool:
        self.restored += 1
        if self.restorable:
            self.main = "visible"
        return self.restorable

    def restore_main_window(self, state: str) -> None:
        self.main = state
        self.log.append(f"main:{state}")

    def minimize_window(self, handle: int) -> None:
        self.minimized.append(handle)
        self.log.append("minimize")

    def close_window(self, handle: int) -> None:
        self.closed.append(handle)
        self.windows = [window for window in self.windows if window.handle != handle]

    def sleep(self, seconds: float) -> None:
        # Virtual time only: timeout behavior is deterministic and instant.
        self.waits.append(seconds)
        self.elapsed += seconds

    def now(self) -> float:
        return self.elapsed

    def chat_windows(self, hit_test: bool = True) -> list[ChatWindow]:
        return list(self.windows)

    def input_handle(self, window: int) -> int:
        return window + 1

    def get_text(self, control: int) -> str:
        return self.draft

    def set_text(self, control: int, text: str) -> None:
        self.actions.append("fill")
        self.draft = text

    def submit(self, control: int) -> None:
        self.actions.append("submit")


class DesktopTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = Path(directory.name) / "rows.json"

    def opener(self, desktop: FakeDesktop) -> RoomOpener:
        return RoomOpener(desktop, memory=self.memory, sleep=desktop.sleep, now=desktop.now)

    def sender(self, desktop: FakeDesktop) -> Sender:
        return Sender(desktop, running=lambda: True, opener=self.opener(desktop))


class OpeningTests(DesktopTests):
    def test_the_title_selects_the_row_without_opening_wrong_rooms(self) -> None:
        desktop = FakeDesktop(["other", "target"])
        window = self.opener(desktop).open("target")
        self.assertEqual(window.title, "target")
        self.assertEqual(desktop.clicks, [1])
        self.assertEqual([window.title for window in desktop.windows], ["target"])
        self.assertEqual(desktop.closed, [])

    def test_title_matching_ignores_case_and_surrounding_space(self) -> None:
        desktop = FakeDesktop(["  TARGET  "])
        self.assertEqual(self.opener(desktop).open("target").title, "  TARGET  ")

    def test_a_title_match_can_be_beyond_the_fallback_row_budget(self) -> None:
        desktop = FakeDesktop(["other"] * 12 + ["target"])
        self.opener(desktop).open("target")
        self.assertEqual(desktop.clicks, [12])

    def test_an_absent_room_is_a_typed_failure(self) -> None:
        desktop = FakeDesktop([])
        with self.assertRaises(KamadoError) as raised:
            self.opener(desktop).open("absent")
        self.assertEqual(raised.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(desktop.clicks, list(range(8)))
        self.assertEqual(desktop.waits, [])

    def test_a_named_list_without_the_room_opens_nothing(self) -> None:
        """A row the list named is not this room.

        Clicking it anyway puts somebody else's conversation on screen and
        marks it read — a side effect on another person, to answer a question
        the list already answered.
        """
        desktop = FakeDesktop(["other", "another"])
        with self.assertRaises(KamadoError) as raised:
            self.opener(desktop).open("absent")
        self.assertEqual(raised.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(desktop.clicks, [])
        self.assertEqual([window.title for window in desktop.windows], [])

    def test_a_hint_is_not_clicked_when_the_list_named_its_rows(self) -> None:
        desktop = FakeDesktop(["other", "another"])
        with self.assertRaises(KamadoError):
            self.opener(desktop).open("absent", hint=1)
        self.assertEqual(desktop.clicks, [])

    def test_the_failure_reports_how_much_of_the_list_it_could_see(self) -> None:
        """A list shows only the rows it has drawn, so the count is the clue."""
        desktop = FakeDesktop(["other", "another", "third"])
        with self.assertRaises(KamadoError) as raised:
            self.opener(desktop).open("absent")
        self.assertIn("3 rows", raised.exception.message)

    def test_a_row_that_opens_no_window_times_out_and_restores_the_main_window(self) -> None:
        desktop = FakeDesktop(["target"])
        desktop.appears = False
        desktop.main = "minimized"
        with self.assertRaises(KamadoError) as raised:
            self.opener(desktop).open("target")
        self.assertEqual(raised.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertGreaterEqual(desktop.elapsed, 6)
        self.assertLess(desktop.elapsed, 6.2)
        self.assertEqual(desktop.main, "minimized")
        self.assertFalse(self.memory.exists())

    def test_an_immediate_window_needs_no_wait(self) -> None:
        desktop = FakeDesktop(["target"])
        self.opener(desktop).open("target")
        self.assertEqual(desktop.waits, [])


class SearchFallbackTests(DesktopTests):
    """The drawn list holds a fraction of the rooms; the client's search holds all.

    Measured on a live client: 26 rows drawn out of 653 rooms, and the wanted
    conversation sitting at row 116 of the 648 the search brought within reach.
    Without this there is no way to open anything below the fold at all.
    """

    def test_a_room_the_list_never_drew_is_reached_through_the_search(self) -> None:
        desktop = FakeDesktop(["other", "another"])
        desktop.searchable = ["other", "another", "wanted"]
        window = self.opener(desktop).open("wanted")
        self.assertEqual(window.title, "wanted")
        self.assertEqual(desktop.searches, [{"wanted"}])

    def test_the_drawn_list_is_tried_before_the_search(self) -> None:
        desktop = FakeDesktop(["wanted", "other"])
        desktop.searchable = ["other", "wanted"]
        self.opener(desktop).open("wanted")
        self.assertEqual(desktop.searches, [], "the search ran for a room already listed")

    def test_the_search_is_put_back_when_it_worked(self) -> None:
        desktop = FakeDesktop(["other"])
        desktop.searchable = ["other", "wanted"]
        self.opener(desktop).open("wanted")
        self.assertFalse(desktop.searching)

    def test_the_search_is_put_back_when_it_found_nothing(self) -> None:
        desktop = FakeDesktop(["other"])
        desktop.searchable = ["other", "different"]
        with self.assertRaises(KamadoError):
            self.opener(desktop).open("wanted")
        self.assertFalse(desktop.searching)

    def test_a_row_found_by_searching_is_not_remembered(self) -> None:
        """That index counts rows in a list that exists only while the search does.

        Remembered and clicked later, it would open whichever conversation had
        taken its place in the ordinary list.
        """
        desktop = FakeDesktop(["other"])
        desktop.searchable = ["other", "wanted"]
        self.opener(desktop).open("wanted")

        again = FakeDesktop(["other", "unrelated"])
        with self.assertRaises(KamadoError):
            self.opener(again).open("wanted")
        self.assertEqual(again.clicks, [], "a remembered search row was clicked")


class SendWithOpeningTests(DesktopTests):
    def test_a_closed_room_is_opened_and_then_written_to(self) -> None:
        desktop = FakeDesktop(["other", "target"])
        result = self.sender(desktop).send("target", "hello", open_if_needed=True)
        self.assertTrue(result["submitted"])
        self.assertEqual(desktop.actions, ["fill", "submit"])
        self.assertEqual(desktop.clicks, [1])

    def test_without_the_flag_a_closed_room_is_refused_without_touching_the_list(self) -> None:
        desktop = FakeDesktop(["target"])
        with self.assertRaises(KamadoError) as raised:
            self.sender(desktop).send("target", "hello")
        self.assertEqual(raised.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(desktop.log, [])

    def test_an_existing_window_needs_no_list_scan_or_minimization(self) -> None:
        desktop = FakeDesktop(["target"], already_open=["target"])
        self.sender(desktop).send("target", "hello", open_if_needed=True)
        self.assertEqual(desktop.log, [])
        self.assertEqual(desktop.minimized, [])

    def test_a_dry_run_may_open_but_never_writes(self) -> None:
        desktop = FakeDesktop(["target"])
        result = self.sender(desktop).send("target", "hello", dry_run=True, open_if_needed=True)
        self.assertFalse(result["submitted"])
        self.assertEqual(desktop.actions, [])
        self.assertEqual(desktop.clicks, [0])

    def test_failure_to_open_never_writes_or_submits(self) -> None:
        desktop = FakeDesktop([])
        with self.assertRaises(KamadoError):
            self.sender(desktop).send("absent", "hello", open_if_needed=True)
        self.assertEqual(desktop.actions, [])


class AliasTests(DesktopTests):
    def test_a_room_known_by_another_title_still_opens(self) -> None:
        desktop = FakeDesktop(["Synthetic Owner"])
        self.assertEqual(self.opener(desktop).open("Self", aliases=["Synthetic Owner"]).title, "Synthetic Owner")

    def test_an_alias_matches_an_already_open_window(self) -> None:
        desktop = FakeDesktop([], already_open=["Synthetic Owner"])
        result = self.sender(desktop).send("Self", "hello", aliases=["Synthetic Owner"])
        self.assertEqual(result["room"], "Synthetic Owner")
        self.assertEqual(desktop.clicks, [])

    def test_an_alias_never_turns_a_substring_into_a_match(self) -> None:
        desktop = FakeDesktop(["target extra"])
        with self.assertRaises(KamadoError):
            self.opener(desktop).open("target", aliases=["extra"])


class TidinessTests(DesktopTests):
    def test_the_opened_window_is_minimized_before_restoring_the_main_window(self) -> None:
        desktop = FakeDesktop(["target"])
        window = self.opener(desktop).open("target")
        self.assertEqual(desktop.minimized, [window.handle])
        self.assertEqual(desktop.log[-2:], ["minimize", "main:visible"])

    def test_every_main_window_state_is_restored(self) -> None:
        for state in ("visible", "minimized", "hidden", "missing"):
            with self.subTest(state=state):
                desktop = FakeDesktop(["target"])
                desktop.main = state
                self.opener(desktop).open("target")
                self.assertEqual(desktop.main, state)
                self.assertEqual(desktop.restored, int(state != "visible"))

    def test_a_main_window_that_cannot_be_shown_is_a_typed_failure(self) -> None:
        desktop = FakeDesktop(["target"])
        desktop.main = "hidden"
        desktop.restorable = False
        with self.assertRaises(KamadoError) as raised:
            self.opener(desktop).open("target")
        self.assertEqual(raised.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(desktop.clicks, [])

    def test_a_closed_main_window_is_drawn_rather_than_refused(self) -> None:
        # The client runs perfectly well with every window closed, and asking
        # it for one is exactly what the reader would otherwise get up and do.
        desktop = FakeDesktop(["target"])
        desktop.main = "missing"
        self.assertEqual(self.opener(desktop).open("target").title, "target")
        self.assertEqual(desktop.restored, 1)
        self.assertEqual(desktop.clicks, [0])

    def test_a_main_window_that_was_closed_is_handed_to_restoration(self) -> None:
        desktop = FakeDesktop(["target"])
        desktop.main = "missing"
        self.opener(desktop).open("target")
        self.assertEqual(desktop.log[-2:], ["minimize", "main:missing"])

    def test_a_client_that_draws_no_main_window_is_a_typed_failure(self) -> None:
        desktop = FakeDesktop(["target"])
        desktop.main = "missing"
        desktop.restorable = False
        with self.assertRaises(KamadoError) as raised:
            self.opener(desktop).open("target")
        self.assertEqual(raised.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        self.assertEqual(desktop.clicks, [])

    def test_an_already_open_room_needs_no_main_window_at_all(self) -> None:
        # Nothing about a closed main window stops a room that is already
        # open from being written to, so nothing is drawn to reach it.
        desktop = FakeDesktop([], already_open=["target"])
        desktop.main = "missing"
        self.assertEqual(self.opener(desktop).open("target").title, "target")
        self.assertEqual(desktop.restored, 0)
        self.assertEqual(desktop.log, [])


class AimTests(DesktopTests):
    def test_title_match_wins_over_an_incorrect_hint(self) -> None:
        desktop = FakeDesktop(["target", "other"])
        self.opener(desktop).open("target", hint=1)
        self.assertEqual(desktop.clicks, [0])

    def test_hint_is_used_when_titles_are_unavailable(self) -> None:
        desktop = FakeDesktop(["other", "target"])
        desktop.titles = ["", ""]
        self.opener(desktop).open("target", hint=1)
        self.assertEqual(desktop.clicks, [1])

    def test_success_is_remembered_as_a_fallback_before_the_hint(self) -> None:
        self.opener(FakeDesktop(["other", "target"])).open("target")
        again = FakeDesktop(["other", "target"])
        again.titles = ["", ""]
        self.opener(again).open("target", hint=0)
        self.assertEqual(again.clicks, [1])

    def test_title_match_wins_over_stale_memory(self) -> None:
        self.opener(FakeDesktop(["other", "target"])).open("target")
        moved = FakeDesktop(["target", "other"])
        self.opener(moved).open("target")
        self.assertEqual(moved.clicks, [0])

    def test_the_remembered_file_does_not_hold_cleartext_titles(self) -> None:
        self.opener(FakeDesktop(["synthetic private title"])).open("synthetic private title")
        self.assertNotIn(b"synthetic private title", self.memory.read_bytes())

    def test_invalid_memory_does_not_block_title_matching(self) -> None:
        self.memory.write_text("{damaged")
        desktop = FakeDesktop(["other", "target"])
        self.opener(desktop).open("target")
        self.assertEqual(desktop.clicks, [1])


class StoredTitleTests(unittest.TestCase):
    def test_a_custom_title_is_available_as_an_alias(self) -> None:
        from tests.fixtures import NAMED_ROOM, NAMED_ROOM_TITLE, build_reader
        with build_reader() as reader:
            self.assertEqual(reader.stored_title(NAMED_ROOM), NAMED_ROOM_TITLE)

    def test_a_room_without_a_stored_title_has_no_alias(self) -> None:
        from tests.fixtures import SELF_ROOM, GROUP_ROOM, build_reader
        with build_reader() as reader:
            self.assertIsNone(reader.stored_title(SELF_ROOM))
            self.assertIsNone(reader.stored_title(GROUP_ROOM))


class FinderDesktop(FakeDesktop):
    """A list that can be read a row at a time, the way the live backend reads it."""

    def __init__(self, rows: list[str]) -> None:
        super().__init__(rows)
        self.read: list[int] = []

    def first_matching_row(self, wanted, hint=None):
        from kamado.macauto import outward

        for index in outward(hint, len(self.titles)):
            self.read.append(index)
            if self.titles[index].strip().casefold() in wanted:
                return index, len(self.titles)
        return None, len(self.titles)


class EarlyStopTests(DesktopTests):
    """Reading every name before choosing cost about 18 s on a list of 264."""

    def test_the_rows_are_read_outward_from_the_hint(self) -> None:
        from kamado.macauto import outward

        self.assertEqual(list(outward(3, 7)), [3, 4, 2, 5, 1, 6, 0])
        self.assertEqual(list(outward(None, 3)), [0, 1, 2])
        self.assertEqual(list(outward(99, 3)), [0, 1, 2])

    def test_reading_stops_at_the_room(self) -> None:
        desktop = FinderDesktop([f"방 {index}" for index in range(50)])
        window = RoomOpener(desktop, memory=self.memory, sleep=desktop.sleep, now=desktop.now).open(
            "방 21", hint=20
        )
        self.assertEqual(window.title, "방 21")
        self.assertEqual(desktop.read, [20, 21], "names were read past the room")
        self.assertNotIn("rows", desktop.log, "the whole list was read anyway")
        self.assertEqual(desktop.clicks, [21])

    def test_a_room_the_list_lacks_still_goes_to_the_search(self) -> None:
        desktop = FinderDesktop(["하나", "둘"])
        desktop.searchable = ["찾는 방"]
        window = RoomOpener(desktop, memory=self.memory, sleep=desktop.sleep, now=desktop.now).open(
            "찾는 방"
        )
        self.assertEqual(window.title, "찾는 방")
        self.assertEqual(desktop.searches, [{"찾는 방"}])


if __name__ == "__main__":
    unittest.main()
