"""The interface's 공감 line, edit mark, notice row, lists and SafeBot toggle."""

from __future__ import annotations

import unittest

from kanao.model import COVERED_PLACEHOLDER
from tests.platform import require_windows_console

require_windows_console()

from kanao.tui.app import App
from kanao.tui.console import Key
from kanao.tui.layout import display_width, strip_ansi
from kanao.tui.theme import Theme

from tests.extras_fixtures import BOARD_NOTICE, FILE_NAME, build_extras_reader
from tests.fixtures import GROUP_ROOM

WIDTH = 100


class ExtrasScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_extras_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app.load_rooms()
        self.app.open_room(next(room for room in self.app.rooms if room.id == GROUP_ROOM))

    def transcript(self) -> list[str]:
        chat = self.app.chat
        assert chat is not None
        return [strip_ansi(line) for line in self.app._transcript_of(chat, WIDTH)]

    def frame(self) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(WIDTH, 40)]

    def command(self, text: str) -> None:
        chat = self.app.chat
        assert chat is not None
        chat.draft = text
        chat.caret = len(text)
        self.app.handle(Key.ENTER, "")

    def test_reactions_edits_and_covers_are_drawn_where_the_message_is(self) -> None:
        lines = self.transcript()
        self.assertIn("공감 사랑 3 · 엄지척 1", [line.strip() for line in lines])
        edited = next(line for line in lines if "고친 문장" in line)
        self.assertTrue(edited.rstrip().endswith("(수정됨)"))
        self.assertTrue(any(COVERED_PLACEHOLDER in line for line in lines))
        self.assertFalse(any("가려진 말" in line for line in lines))

    def test_the_notice_takes_the_blank_row_and_nothing_moves(self) -> None:
        frame = self.frame()
        self.assertTrue(frame[1].startswith("공지 " + BOARD_NOTICE))
        self.assertTrue(all(display_width(line) <= WIDTH for line in frame))

    def test_safebot_toggles_what_the_screen_shows(self) -> None:
        self.command("/safebot")
        lines = self.transcript()
        shown = next(line for line in lines if "가려진 말" in line)
        self.assertIn("세이프봇 가림 해제", shown)
        self.command("/safebot")
        self.assertFalse(any("가려진 말" in line for line in self.transcript()))

    def test_the_lists_open_in_place_and_escape_closes_them(self) -> None:
        for text, expected in (
            ("/notice", BOARD_NOTICE),
            ("/files", FILE_NAME),
            ("/links", "https://example.com/a"),
            ("/photos", "https://talk.kakaocdn.net/synthetic/full.jpg"),
            ("/edits", "0. 처음 문장"),
        ):
            with self.subTest(command=text):
                self.command(text)
                frame = self.frame()
                self.assertTrue(any(expected in line for line in frame), frame)
                self.assertTrue(all(display_width(line) <= WIDTH for line in frame))
                self.app.handle(Key.ESCAPE, "")
                chat = self.app.chat
                assert chat is not None
                self.assertEqual(chat.panel, [])


if __name__ == "__main__":
    unittest.main()
