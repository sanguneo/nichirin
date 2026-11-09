"""A search typed after another one is not answered with the earlier results (#110).

Measured on Windows 11 (2026-11-09): emptied, the client hides its result list
after about 0.31 s; typed into empty, it shows one after about 0.33 s; typed
over earlier text, the list never hides and keeps the earlier results until
the client refilters. ``FakeClient`` replays those timings on a clock that only
the patched ``time.sleep`` advances, so nothing here waits.
"""

from __future__ import annotations

import unittest
from typing import Optional
from unittest.mock import patch

from kanao import winauto

BOX, RESULTS = 11, 22
HIDE_AFTER, SHOW_AFTER, REFILTER_AFTER = 0.31, 0.33, 0.33


class FakeClient:
    def __init__(self) -> None:
        self.now = 0.0
        self.text = ""
        self.shown: Optional[str] = None
        self.pending: Optional[tuple[float, Optional[str]]] = None

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.pending is not None and self.now >= self.pending[0]:
            self.shown = self.pending[1]
            self.pending = None

    def replace(self, _box: int, text: str) -> None:
        self.text = text
        if not text:
            self.pending = (self.now + HIDE_AFTER, None)
        elif self.shown is None:
            self.pending = (self.now + SHOW_AFTER, text)
        else:
            self.pending = (self.now + REFILTER_AFTER, text)

    def results(self) -> Optional[int]:
        return RESULTS if self.shown is not None else None


def backend(client: FakeClient) -> winauto.Win32Backend:
    made = winauto.Win32Backend.__new__(winauto.Win32Backend)
    made._search_box = lambda: BOX  # type: ignore[method-assign]
    made._replace = client.replace  # type: ignore[method-assign]
    made._search_results = client.results  # type: ignore[method-assign]
    return made


class SearchBoxTests(unittest.TestCase):
    def search(self, client: FakeClient, made: winauto.Win32Backend, text: str) -> None:
        with patch.object(winauto.time, "sleep", client.sleep):
            self.assertEqual(made.search_rooms(text), RESULTS)

    def test_a_first_search_answers_with_its_own_results(self) -> None:
        client = FakeClient()
        made = backend(client)
        self.search(client, made, "금융결제원")
        self.assertEqual(client.shown, "금융결제원")

    def test_a_search_typed_while_results_show_waits_for_its_own(self) -> None:
        client = FakeClient()
        made = backend(client)
        self.search(client, made, "금융결제원")
        self.search(client, made, "토스인컴")
        self.assertEqual(client.shown, "토스인컴", "the earlier search's first row would be opened")

    def test_clearing_waits_until_the_results_are_gone(self) -> None:
        client = FakeClient()
        made = backend(client)
        self.search(client, made, "금융결제원")
        with patch.object(winauto.time, "sleep", client.sleep):
            made.clear_search()
        self.assertIsNone(client.shown)


if __name__ == "__main__":
    unittest.main()
