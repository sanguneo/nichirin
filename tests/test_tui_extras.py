"""The interface's 공감 line, edit mark, notice row, lists and SafeBot toggle."""

from __future__ import annotations

import unittest

from kamado.model import COVERED_PLACEHOLDER
from tests.platform import require_posix_console

require_posix_console()

from kamado.tui.app import App
from kamado.tui.console import Key, TOGGLE_TIMESTAMP
from kamado.tui.layout import display_width, strip_ansi
from kamado.tui.theme import Theme

from tests.extras_fixtures import FILE_NAME, NOTICE_TEXT, build_extras_reader
from tests.fixtures import GROUP_ROOM

WIDTH = 100


class ExtrasScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_extras_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app.load_rooms()
        self.app.open_room(next(room for room in self.app.rooms if room.id == GROUP_ROOM))

    def chat(self):
        chat = self.app.chat
        if chat is None:
            self.fail("no conversation is open")
        return chat

    def transcript(self) -> list[str]:
        return [strip_ansi(line) for line in self.app._transcript_of(self.chat(), WIDTH)]

    def frame(self) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(WIDTH, 40)]

    def command(self, text: str) -> None:
        chat = self.chat()
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
        self.assertFalse(any("feed" in line and "25" in line for line in lines))

    def test_the_notice_takes_the_blank_row_and_nothing_moves(self) -> None:
        frame = self.frame()
        self.assertTrue(frame[2].startswith("공지 " + NOTICE_TEXT), frame[:4])
        self.assertTrue(all(display_width(line) <= WIDTH for line in frame))

    def test_safebot_toggles_what_the_screen_shows(self) -> None:
        self.command("/safebot")
        shown = next(line for line in self.transcript() if "가려진 말" in line)
        self.assertIn("세이프봇 가림 해제", shown)
        self.command("/safebot")
        self.assertFalse(any("가려진 말" in line for line in self.transcript()))

    def test_the_lists_open_in_place_and_escape_closes_them(self) -> None:
        for text, expected in (
            ("/notice", NOTICE_TEXT),
            ("/files", FILE_NAME),
            ("/links", "https://example.com/a"),
            ("/photos", "https://talk.kakaocdn.net/synthetic-full.jpg"),
            ("/edits", "이전 버전이 기록되지 않았습니다"),
            ("/help", "키보드 단축키"),
            ("/?", "슬래시 명령어"),
        ):
            with self.subTest(command=text):
                self.command(text)
                frame = self.frame()
                self.assertTrue(any(expected in line for line in frame), frame)
                self.assertTrue(all(display_width(line) <= WIDTH for line in frame))
                self.assertIn("list", frame[-1])
                self.app.handle(Key.ESCAPE, "")
                self.assertEqual(self.chat().panel, [])
                self.assertIsNotNone(self.app.chat)

    def test_chat_hints_include_help_and_essential_keys(self) -> None:
        frame = self.frame()
        hints_line = frame[1]
        self.assertIn("/help", hints_line)
        self.assertIn("도움말", hints_line)
        self.assertIn("enter", hints_line)
        self.assertIn("esc", hints_line)
        self.assertTrue(all(display_width(line) <= WIDTH for line in frame))

    def test_timestamp_toggle_via_shortcut_and_command(self) -> None:
        initial = self.transcript()
        self.assertTrue(any("07:20" in line for line in initial))
        self.assertTrue(self.app.show_timestamps)

        # Toggle via Ctrl+T
        self.app.handle("char", TOGGLE_TIMESTAMP)
        self.assertFalse(self.app.show_timestamps)
        self.assertEqual(self.chat().notice, "시간 표시 끔")
        hidden = self.transcript()
        # Header date might have 2023-11-15, but message line timestamps 07:20 should be gone
        message_lines = [l for l in hidden if "Friend Nick" in l or "내가 남긴 줄" in l]
        self.assertTrue(message_lines)
        self.assertFalse(any("07:20" in l for l in message_lines))
        # Speaker name should start directly at column 0 without leading space
        friend_line = next(l for l in hidden if "Friend Nick" in l)
        self.assertTrue(friend_line.startswith("Friend Nick"), f"Unexpected indentation: {repr(friend_line)}")

        # Toggle via /time
        self.command("/time")
        self.assertTrue(self.app.show_timestamps)
        self.assertEqual(self.chat().notice, "시간 표시 켬")
        restored = self.transcript()
        restored_messages = [l for l in restored if "Friend Nick" in l or "내가 남긴 줄" in l]
        self.assertTrue(any("07:20" in l for l in restored_messages))

    def test_help_panel_lists_timestamp_toggle(self) -> None:
        self.command("/help")
        frame = self.frame()
        self.assertTrue(any("^T" in line and "타임스탬프" in line for line in frame))
        self.assertTrue(any("/time" in line and "타임스탬프" in line for line in frame))
        self.app.handle(Key.ESCAPE, "")


if __name__ == "__main__":
    unittest.main()
