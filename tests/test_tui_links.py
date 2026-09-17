"""Clicking a hyperlink in the conversation.

Asking the terminal to report mouse buttons takes its own click handling away,
so the OSC 8 links the transcript writes stop opening by themselves. What is
asserted here is that a click resolves back to the right URL and hands it to the
system, and that clicks which are not on a link open nothing.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from kamado.model import Message
from kamado.tui import app as app_module
from kamado.tui.app import App, _osc8_segments
from kamado.tui.console import Key
from kamado.tui.layout import strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import GROUP_ROOM, PEER_ID, build_reader

WIDTH = 80
HEIGHT = 24
URL = "https://example.com/deep/path?a=1"


class SegmentTests(unittest.TestCase):
    def test_a_link_run_carries_its_target(self) -> None:
        line = f"text \x1b]8;;{URL}\x1b\\\x1b[4mshown\x1b[24m\x1b]8;;\x1b\\ tail"
        found = [(strip_ansi(piece), link) for piece, link in _osc8_segments(line)]
        self.assertIn(("shown", URL), found)

    def test_text_outside_a_link_carries_none(self) -> None:
        line = f"text \x1b]8;;{URL}\x1b\\shown\x1b]8;;\x1b\\ tail"
        outside = [link for piece, link in _osc8_segments(line) if "text" in piece]
        self.assertEqual(outside, [None])

    def test_a_line_without_links_is_one_plain_run(self) -> None:
        self.assertEqual(_osc8_segments("nothing here"), [("nothing here", None)])

    def test_the_bel_terminated_form_is_understood(self) -> None:
        line = f"\x1b]8;;{URL}\x07shown\x1b]8;;\x07"
        self.assertIn(URL, [link for _piece, link in _osc8_segments(line)])


class LinkClickCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()
        self.app.open_room(next(r for r in self.app.rooms if r.id == GROUP_ROOM))
        chat = self.app.chat
        assert chat is not None
        chat.messages = list(chat.messages) + [
            Message(
                id=99991,
                room_id=GROUP_ROOM,
                sender_id=PEER_ID,
                sender="tester",
                text=f"see {URL} please",
                sent_at=None,
                from_me=False,
                type=1,
            )
        ]
        chat.rendered = None
        self.app.render(WIDTH, HEIGHT)

    def link_position(self) -> tuple[int, int]:
        """Return the screen cell the app itself maps to the link."""
        self.app.render(WIDTH, HEIGHT)
        geometry = self.app._pane_geometry
        assert geometry is not None
        cell = geometry[2]
        chat = self.app.chat
        assert chat is not None
        body = self.app._transcript_of(chat, cell)
        row = next(i for i, line in enumerate(body) if "example.com" in strip_ansi(line))
        column = strip_ansi(body[row]).index("https://")
        screen = next(
            s for s in range(HEIGHT)
            if (found := self.app._pane_at(s, column)) is not None and found[1] == row
        )
        return screen, column


class LinkClickTests(LinkClickCase):
    def test_clicking_a_link_hands_it_to_the_system(self) -> None:
        screen, column = self.link_position()
        with patch.object(app_module.subprocess, "Popen") as opener:
            self.app.handle(Key.CLICK, f"{screen},{column}")
            opener.assert_called_once()
            self.assertEqual(opener.call_args[0][0], ["open", URL])

    def test_clicking_the_middle_of_a_link_still_opens_it(self) -> None:
        screen, column = self.link_position()
        with patch.object(app_module.subprocess, "Popen") as opener:
            self.app.handle(Key.CLICK, f"{screen},{column + 8}")
            self.assertEqual(opener.call_args[0][0], ["open", URL])

    def test_clicking_plain_text_opens_nothing(self) -> None:
        screen, _column = self.link_position()
        with patch.object(app_module.subprocess, "Popen") as opener:
            self.app.handle(Key.CLICK, f"{screen},0")
            opener.assert_not_called()

    def test_clicking_past_the_end_of_a_line_opens_nothing(self) -> None:
        screen, _column = self.link_position()
        with patch.object(app_module.subprocess, "Popen") as opener:
            self.app.handle(Key.CLICK, f"{screen},250")
            opener.assert_not_called()

    def test_a_click_does_not_type_into_the_draft(self) -> None:
        screen, column = self.link_position()
        with patch.object(app_module.subprocess, "Popen"):
            self.app.handle(Key.CLICK, f"{screen},{column}")
        chat = self.app.chat
        assert chat is not None
        self.assertEqual(chat.draft, "")

    def test_a_failed_open_is_reported_rather_than_raised(self) -> None:
        screen, column = self.link_position()
        with patch.object(app_module.subprocess, "Popen", side_effect=OSError("no opener")):
            self.app.handle(Key.CLICK, f"{screen},{column}")
        chat = self.app.chat
        assert chat is not None
        self.assertIn("링크", chat.notice)

    def test_the_confirmation_clears_itself(self) -> None:
        now = [1_000.0]
        self.app._clock = lambda: now[0]
        screen, column = self.link_position()
        with patch.object(app_module.subprocess, "Popen"):
            self.app.handle(Key.CLICK, f"{screen},{column}")
        chat = self.app.chat
        assert chat is not None
        self.assertIn("링크", chat.notice)
        self.assertIn("링크", self.composer())
        now[0] += 2.0
        self.assertNotIn("링크", self.composer())
        self.assertEqual(chat.notice, "")

    def test_a_notice_the_reader_must_read_does_not_expire(self) -> None:
        now = [1_000.0]
        self.app._clock = lambda: now[0]
        chat = self.app.chat
        assert chat is not None
        chat.notice = "이전 기록이 더 없습니다"
        self.assertIn("이전 기록", self.composer())
        now[0] += 600.0
        self.assertIn("이전 기록", self.composer())

    def composer(self) -> str:
        return "".join(strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT))


if __name__ == "__main__":
    unittest.main()
