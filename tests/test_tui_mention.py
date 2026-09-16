from __future__ import annotations

import unittest

from kakaoread.model import Room
from kakaoread.tui.app import App, ChatState
from kakaoread.tui.console import Key
from kakaoread.tui.theme import Theme
from tests.fixtures import build_reader


class TuiMentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.room = Room(
            id=12345,
            type="group",
            title="테스트 대화방",
            member_count=4,
            unread=0,
            last_log_id=None,
            last_updated_at=None,
            readable=True,
        )
        self.app.chat = ChatState(
            room=self.room,
            members=["카잣둠", "비와가요", "하얀강아지", "샤론머스크", "노고노고"],
        )

    def test_typing_at_symbol_triggers_mention_candidates(self) -> None:
        chat = self.app.chat
        assert chat is not None

        self.app.handle("char", "@")
        self.assertEqual(chat.draft, "@")
        self.assertEqual(chat.caret, 1)
        self.assertEqual(chat.mention_candidates, ["카잣둠", "비와가요", "하얀강아지", "샤론머스크", "노고노고"])
        self.assertEqual(chat.mention_index, 0)
        self.assertEqual(chat.mention_start, 0)

        self.app.handle("char", "카")
        self.assertEqual(chat.draft, "@카")
        self.assertEqual(chat.mention_candidates, ["카잣둠"])

        self.app.handle(Key.TAB, "")
        self.assertEqual(chat.draft, "@카잣둠 ")
        self.assertEqual(chat.caret, len("@카잣둠 "))
        self.assertEqual(chat.mention_candidates, [])

    def test_navigating_and_selecting_candidate_with_enter(self) -> None:
        chat = self.app.chat
        assert chat is not None

        self.app.handle("char", "@")
        self.app.handle(Key.DOWN, "")
        self.assertEqual(chat.mention_index, 1)

        self.app.handle(Key.ENTER, "")
        self.assertEqual(chat.draft, "@비와가요 ")
        self.assertIsNone(self.app.pending_send)
        self.assertEqual(chat.mention_candidates, [])

    def test_mention_popup_closes_on_escape(self) -> None:
        chat = self.app.chat
        assert chat is not None

        self.app.handle("char", "@")
        self.assertTrue(bool(chat.mention_candidates))

        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(chat.mention_candidates, [])
        self.assertEqual(chat.draft, "@")
        self.assertIsNotNone(self.app.chat)

    def test_mention_popup_renders_in_chat_view(self) -> None:
        chat = self.app.chat
        assert chat is not None

        self.app.handle("char", "@")
        lines = self.app.render(80, 24)
        rendered = "\n".join(lines)
        self.assertIn("멘션", rendered)
        self.assertIn("@카잣둠", rendered)
