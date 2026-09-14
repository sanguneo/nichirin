"""Composer tests: the box, what typing does, and what sending refuses.

No desktop is touched. The sender is a stand-in that records calls and fails
the way the real one does, so a test cannot pass against an error type
production never raises.
"""

from __future__ import annotations

import unittest

from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.tui.app import App, ChatState
from kakaoread.tui.console import Key
from kakaoread.tui.layout import display_width, strip_ansi
from kakaoread.tui.theme import Theme
from kakaoread.winauto import SendResult

from tests.fixtures import DIRECT_ROOM, build_reader

WIDTH = 80
HEIGHT = 24


class FakeSender:
    """Stands in for winauto.Sender, recording what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    def send(self, room: str, text: str, dry_run: bool = False) -> SendResult:
        if self.failure is not None:
            raise self.failure
        self.sent.append((room, text))
        return {"submitted": True, "dryRun": False, "room": room, "length": len(text)}


class ComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.sender = FakeSender()
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False), sender=self.sender)
        self.app.load_rooms()
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        self.app.open_room(room)

    @property
    def chat(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def frame(self) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT)]

    def composer(self) -> str:
        return "\n".join(self.frame()[-5:-2])

    def type(self, written: str) -> None:
        for character in written:
            self.app.handle("char", character)

    def test_an_open_room_shows_a_bordered_message_box(self) -> None:
        box = self.composer()
        self.assertIn("╭", box)
        self.assertIn("╰", box)
        self.assertIn("❯", box)

    def test_the_room_list_has_no_message_box(self) -> None:
        self.app.chat = None
        self.assertNotIn("❯", "\n".join(self.frame()))

    def test_typing_reaches_the_box_rather_than_a_shortcut(self) -> None:
        self.type("q")
        self.assertTrue(self.app.running, "a letter quit the browser instead of being typed")
        self.assertEqual(self.chat.draft, "q")
        self.assertIn("q", self.composer())

    def test_backspace_removes_the_last_character(self) -> None:
        self.type("안녕")
        self.app.handle(Key.BACKSPACE, "")
        self.assertEqual(self.chat.draft, "안")

    def test_the_box_never_overflows_the_width(self) -> None:
        self.type("가" * 200)
        for line in self.app.render(WIDTH, HEIGHT):
            self.assertLessEqual(display_width(line), WIDTH)

    def test_enter_hands_the_text_to_the_client_and_clears_the_box(self) -> None:
        self.type("보낼 말")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.sender.sent, [(self.chat.room.title, "보낼 말")])
        self.assertEqual(self.chat.draft, "")

    def test_nothing_is_sent_and_nothing_is_echoed_before_the_client_writes_it(self) -> None:
        loaded = len(self.chat.messages)
        self.type("보낼 말")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(len(self.chat.messages), loaded)

    def test_an_empty_box_sends_nothing(self) -> None:
        self.app.handle(Key.ENTER, "")
        self.type("   ")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.sender.sent, [])

    def test_a_slash_line_runs_a_command_instead_of_sending(self) -> None:
        self.type("/sum 30m")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.sender.sent, [])

    def test_older_moved_from_a_letter_to_a_command(self) -> None:
        self.type("/older")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.sender.sent, [])
        self.assertNotEqual(self.chat.notice, "")

    def test_escape_clears_the_draft_before_leaving_the_room(self) -> None:
        self.type("쓰다 만 글")
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.chat.draft, "")
        self.assertIsNotNone(self.app.chat, "escape left the room with text still in the box")

        self.app.handle(Key.ESCAPE, "")
        self.assertIsNone(self.app.chat)

    def test_a_refused_send_is_reported_and_keeps_the_room_open(self) -> None:
        self.sender.failure = KakaoReadError(ErrorCode.WINDOW_NOT_FOUND, "창이 없습니다")
        self.type("안녕")
        self.app.handle(Key.ENTER, "")
        self.assertIn("창이 없습니다", self.chat.notice)
        self.assertIsNotNone(self.app.chat)

    def test_a_native_failure_is_reported_rather_than_raised(self) -> None:
        self.sender.failure = OSError("PostMessage failed")
        self.type("안녕")
        self.app.handle(Key.ENTER, "")
        self.assertIn("PostMessage failed", self.chat.notice)

    def test_two_rooms_sharing_a_title_refuse_to_send(self) -> None:
        room = self.chat.room
        twin = [room, room]
        self.reader.rooms = lambda limit=None: twin  # type: ignore[method-assign]
        self.type("안녕")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.sender.sent, [])
        self.assertIn("특정할 수 없습니다", self.chat.notice)


if __name__ == "__main__":
    unittest.main()
