"""The topic panel on screen: the key that opens it, the keys it takes, the width it leaves.

The panel shares the keyboard with a composer that owns typing, so most of
what is pinned here is about what it must *not* take: a written message is
still sent by enter, escape still keeps the draft, letters still type.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from kanao.errors import ErrorCode, KanaoError
from kanao.model import Message, Photo, Room
from tests.platform import require_windows_console

require_windows_console()

from kanao.tui import app as app_module
from kanao.tui import images
from kanao.tui.app import ChatState, App
from kanao.tui.console import TOPIC_FLOW, Key
from kanao.tui.layout import display_width, strip_ansi
from kanao.tui.theme import Theme

from tests.test_topics import DINNER, NOW, RELEASE, ROOM, TRIP, run
from tests.test_tui_keys import FakeConsole, down

HEIGHT = 40
WIDE = 140
NARROW = 90
BODY_TOP = 3
"""Tab strip, hints and the notice row sit above the body."""


class FakeReader:
    """Hands back older pages from a fixed history, the way `Reader.messages` pages."""

    def __init__(self, history: list[Message], failure: Exception | None = None) -> None:
        self.history = history
        self.failure = failure
        self.asked: list[int] = []
        self.reveal_covered = False

    def messages(self, room_id, limit=None, before_id=None, include_erased=False, versions=None):
        if self.failure is not None:
            raise self.failure
        self.asked.append(before_id)
        older = [one for one in self.history if before_id is None or one.id < before_id]
        return older[-limit:] if limit else older


class FakeWatcher:
    """A watcher whose next poll returns whatever the test queued."""

    def __init__(self) -> None:
        self.queued: list[Message] = []
        self.moved: tuple[int, ...] = ()

    def poll(self) -> list[Message]:
        arrived, self.queued = self.queued, []
        return arrived


def room() -> Room:
    return Room(ROOM, "MultiChat", "합성 대화방", 4, 0, None, NOW, True)


class TopicPanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.loaded = run(RELEASE, 1, 100) + run(DINNER, 101, 70) + run(TRIP, 201, 40)
        self.reader = FakeReader(history=[])
        self.watcher = FakeWatcher()
        self.app = self.make_app(self.loaded)

    def make_app(self, messages: list[Message], theme: Theme | None = None) -> App:
        app = App(self.reader, theme=theme or Theme(truecolor=True, enabled=False))  # type: ignore[arg-type]
        app._wall = lambda: float(NOW)
        app.chats = [ChatState(room=room(), messages=list(messages), watcher=self.watcher, anchor_top=False)]  # type: ignore[arg-type]
        return app

    @property
    def chat(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def frame(self, width: int = WIDE) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(width, HEIGHT)]

    def body(self, width: int = WIDE) -> list[str]:
        return self.frame(width)[BODY_TOP : HEIGHT - 6]

    def type_command(self, text: str) -> None:
        self.chat.draft = text
        self.chat.caret = len(text)
        self.app.handle(Key.ENTER, "")


class TopicPanelTests(TopicPanelCase):
    # -- opening and closing ------------------------------------------------

    def test_the_hotkey_opens_the_panel_and_closes_it_again(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.chat.topics_open)
        flow = self.chat.topic_flow
        assert flow is not None
        self.assertEqual(len(flow.topics), 3)
        self.assertTrue(any("[주제 흐름] 최근 2시간" in line for line in self.body()))
        self.app.handle("char", TOPIC_FLOW)
        self.assertFalse(self.chat.topics_open)
        self.assertFalse(any("[주제 흐름]" in line for line in self.body()))

    def test_the_commands_do_what_the_key_does(self) -> None:
        for command in ("/flow", "/topics"):
            with self.subTest(command=command):
                self.type_command(command)
                self.assertTrue(self.chat.topics_open)
                self.type_command(command)
                self.assertFalse(self.chat.topics_open)

    def test_the_console_hands_ctrl_o_to_the_screen(self) -> None:
        # The record a console builds for Ctrl+O: VK 'O', SI as its character.
        console = FakeConsole([down(0x4F, "\x0f", control=0x0008)])
        self.assertEqual(console.read_key(0.0), ("char", TOPIC_FLOW))

    def test_the_newest_topic_is_selected_when_the_panel_opens(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.assertEqual(self.chat.topic_selected, 2)

    def test_help_and_the_hotkey_are_listed(self) -> None:
        self.type_command("/help")
        rendered = "\n".join(self.body())
        self.assertIn("ctrl+o", rendered)
        self.assertIn("/flow  /topics", rendered)

    # -- keys while it is open ----------------------------------------------

    def test_the_arrows_walk_the_topics_and_page_keys_still_scroll(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(WIDE)
        self.app.handle(Key.UP, "")
        self.assertEqual(self.chat.topic_selected, 1)
        self.app.handle(Key.UP, "")
        self.app.handle(Key.UP, "")
        self.assertEqual(self.chat.topic_selected, 0)
        self.assertEqual(self.chat.offset, 0)
        self.app.handle(Key.DOWN, "")
        self.assertEqual(self.chat.topic_selected, 1)
        self.app.handle(Key.PAGE_UP, "")
        self.assertEqual(self.chat.offset, 10)

    def test_letters_still_go_to_the_composer(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle("char", "q")
        self.assertEqual(self.chat.draft, "q")
        self.assertTrue(self.app.running)

    def test_enter_on_an_empty_box_opens_the_topic_and_brings_the_conversation_to_it(self) -> None:
        # A screen shorter than the conversation, so reaching the topic
        # really means scrolling.
        short = 20
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(WIDE, short)
        self.app.handle(Key.UP, "")
        self.app.handle(Key.UP, "")
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.chat.topic_expanded, 0)
        self.assertIsNone(self.app.pending_send)
        self.assertGreater(self.chat.offset, 0)
        body = [strip_ansi(line) for line in self.app.render(WIDE, short)][BODY_TOP : short - 6]
        # The topic's first message is the conversation's top line.
        self.assertIn(RELEASE[0], body[0])
        # Its key messages are listed under it in the panel.
        self.assertTrue(any("┃" in line and "배포" in line for line in body), body)
        self.app.handle(Key.ENTER, "")
        self.assertIsNone(self.chat.topic_expanded)

    def test_enter_with_something_written_sends_it(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        for letter in "안녕":
            self.app.handle("char", letter)
        self.app.handle(Key.ENTER, "")
        self.assertEqual(self.app.pending_send, (ROOM, "안녕"))
        self.assertIsNone(self.chat.topic_expanded)
        self.assertTrue(self.chat.topics_open)

    def test_escape_closes_the_panel_first_and_keeps_the_draft(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle("char", "초")
        self.app.handle(Key.ESCAPE, "")
        self.assertFalse(self.chat.topics_open)
        self.assertEqual(self.chat.draft, "초")
        self.assertEqual(len(self.app.chats), 1)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.chats, [])
        self.assertEqual(self.app.drafts[ROOM], "초")

    # -- layout -------------------------------------------------------------

    def test_a_wide_screen_shows_the_panel_beside_the_conversation(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        body = self.body(WIDE)
        left = WIDE - app_module._topic_panel_width(WIDE) - 1
        header = next(line for line in body if "[주제 흐름]" in line)
        self.assertEqual(display_width(header.split("│")[0]), left)
        self.assertTrue(any(TRIP[-1] in line for line in body), "the conversation is still drawn")

    def test_a_narrow_screen_gives_the_panel_the_conversation_s_place(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        body = self.body(NARROW)
        self.assertTrue(body[0].startswith(" [주제 흐름]"))
        self.assertFalse(any(TRIP[-1] in line for line in body))
        self.app.handle(Key.ESCAPE, "")
        self.assertTrue(any(TRIP[-1] in line for line in self.body(NARROW)))

    def test_every_line_is_exactly_as_wide_as_the_screen(self) -> None:
        for coloured in (False, True):
            self.app = self.make_app(self.loaded, Theme(truecolor=True, enabled=coloured))
            self.app.handle("char", TOPIC_FLOW)
            self.app.handle(Key.UP, "")
            self.app.handle(Key.ENTER, "")
            for width in (80, 99, 100, 120, 140, 200):
                with self.subTest(coloured=coloured, width=width):
                    frame = self.app.render(width, HEIGHT)
                    self.assertEqual(len(frame), HEIGHT)
                    for row, line in enumerate(frame):
                        self.assertLessEqual(display_width(line), width, row)
                    for row in range(BODY_TOP, HEIGHT - 5):
                        self.assertEqual(display_width(frame[row]), width, (row, strip_ansi(frame[row])))

    def test_the_panel_is_drawn_on_the_terminal_s_own_background(self) -> None:
        self.app = self.make_app(self.loaded, Theme(truecolor=True, enabled=True))
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.ENTER, "")
        for width in (WIDE, NARROW):
            with self.subTest(width=width):
                body = self.app.render(width, HEIGHT)[BODY_TOP : HEIGHT - 6]
                panel = [line.split("│", 1)[-1] if width == WIDE else line for line in body]
                self.assertFalse(any("\x1b[48;" in line for line in panel), panel)

    def test_the_rule_is_put_at_its_column_rather_than_after_the_text(self) -> None:
        # An emoji sequence the terminal draws at another width than it
        # measures must not move the rule: it is placed, not appended.
        joined = replace(self.loaded[-1], text="가족 👨‍👩‍👧 사진 ❤️")
        self.app = self.make_app(self.loaded[:-1] + [joined])
        self.app.handle("char", TOPIC_FLOW)
        left = WIDE - app_module._topic_panel_width(WIDE) - 1
        jump = f"\x1b[K\x1b[{left + 1}G"
        body = self.app.render(WIDE, HEIGHT)[BODY_TOP : HEIGHT - 6]
        self.assertTrue(all(jump in line for line in body), body)

    def test_key_messages_drop_what_makes_an_emoji_s_width_a_guess(self) -> None:
        joined = replace(self.loaded[-1], text="캠핑 텐트 👨‍👩‍👧 ❤️ 👍🏽")
        self.app = self.make_app(self.loaded[:-1] + [joined])
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.ENTER, "")
        # Narrow, so the panel is all there is and nothing else can hide it.
        panel = "".join(self.app.render(NARROW, HEIGHT)[BODY_TOP : HEIGHT - 6])
        self.assertIn("캠핑", panel)
        self.assertIn("👨👩👧", panel)
        for unstable in ("‍", "️", "\U0001f3fd"):
            self.assertNotIn(unstable, panel)

    def test_the_transcript_is_cached_at_the_width_it_is_drawn_at(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(WIDE)
        left = WIDE - app_module._topic_panel_width(WIDE) - 1
        assert self.chat.rendered is not None
        self.assertEqual(self.chat.rendered[0][0], left)
        self.watcher.queued = [replace(self.loaded[-1], id=900, text="새로 온 캠핑 텐트 이야기")]
        self.app.refresh(WIDE)
        assert self.chat.rendered is not None
        self.assertEqual(self.chat.rendered[0][0], left)

    def test_a_list_takes_the_conversation_s_side_and_escape_unwinds_in_order(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(WIDE)
        self.type_command("/help")
        self.assertTrue(self.chat.topics_open)
        self.app.handle(Key.ESCAPE, "")
        self.assertFalse(self.chat.topics_open)
        self.assertTrue(self.chat.panel)
        self.app.handle(Key.ESCAPE, "")
        self.assertFalse(self.chat.panel)
        self.assertEqual(len(self.app.chats), 1)

    def test_on_a_narrow_screen_a_list_replaces_the_panel(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(NARROW)
        self.type_command("/sum 30m")
        self.assertFalse(self.chat.topics_open)
        self.assertTrue(self.chat.slices)

    def test_an_empty_room_says_so(self) -> None:
        self.app = self.make_app([])
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(any("최근 2시간 대화가 없습니다" in line for line in self.body()))

    def test_a_topic_followed_back_past_the_window_is_marked(self) -> None:
        self.app = self.make_app(run(RELEASE + RELEASE, 1, 150, step=4))
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(any("↑ 2시간 이전부터" in line for line in self.body()))

    def test_a_long_timeline_scrolls_to_keep_the_selection_in_view(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.UP, "")
        self.app.handle(Key.UP, "")
        frame = [strip_ansi(line) for line in self.app.render(WIDE, 16)]
        self.assertTrue(any("▶" in line for line in frame), frame)
        self.app.handle(Key.DOWN, "")
        self.app.handle(Key.DOWN, "")
        frame = [strip_ansi(line) for line in self.app.render(WIDE, 16)]
        self.assertTrue(any("▶" in line for line in frame), frame)

    # -- what it is cut from ------------------------------------------------

    def test_arrivals_recut_the_flow_and_the_selection_stays_put(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.UP, "")
        flow = self.chat.topic_flow
        assert flow is not None
        held = flow.topics[self.chat.topic_selected].messages[0].id
        arriving = run(RELEASE, 301, 10, step=1)
        self.watcher.queued = arriving
        self.assertTrue(self.app.refresh(WIDE))
        flow = self.chat.topic_flow
        assert flow is not None
        self.assertEqual(len(flow.topics), 4)
        self.assertEqual(flow.topics[self.chat.topic_selected].messages[0].id, held)
        self.assertEqual(flow.topics[3].resumes, 0)

    def test_opening_pages_back_until_the_window_and_its_lookback_are_loaded(self) -> None:
        older = run(TRIP, 1, 300, step=10) + run(RELEASE, 11, 200, step=5)
        recent = run(DINNER, 101, 30)
        self.reader.history = older + recent
        self.app = self.make_app(recent)
        self.app.handle("char", TOPIC_FLOW)
        self.assertEqual(self.chat.messages[0].id, older[0].id)
        self.assertTrue(self.chat.topic_complete)
        self.assertEqual(self.reader.asked[0], recent[0].id)
        self.assertIn("이전", self.chat.notice)

    def test_the_history_cap_is_admitted_and_marks_the_reach_truncated(self) -> None:
        older = run((RELEASE * 10)[:40], 1, 230, step=3)
        recent = run(RELEASE, 101, 100, step=3)
        self.reader.history = older + recent
        self.app = self.make_app(recent)
        with patch.object(app_module, "TOPIC_HISTORY_CAP", 20):
            self.app.handle("char", TOPIC_FLOW)
        self.assertFalse(self.chat.topic_complete)
        self.assertEqual(len(self.chat.messages), 20)
        flow = self.chat.topic_flow
        assert flow is not None
        self.assertTrue(flow.topics[0].truncated)
        rendered = "\n".join(self.body())
        self.assertIn("앞 기록을 끝까지 불러오지 못했습니다", rendered)
        self.assertIn("↑ 2시간 이전부터…", rendered)

    def test_a_failed_page_is_said_and_the_panel_opens_anyway(self) -> None:
        self.reader.failure = KanaoError(ErrorCode.KEY_UNAVAILABLE, "열쇠 없음")
        recent = run(DINNER, 101, 30)
        self.app = self.make_app(recent)
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.chat.topics_open)
        self.assertIn("열쇠 없음", self.chat.notice)
        self.assertFalse(self.chat.topic_complete)


    # -- review regressions -------------------------------------------------

    def long_lines(self) -> list[Message]:
        """Lines that wrap at the narrowed width and not at the whole one.

        About seventy columns each: one row at 120 columns less the head, two
        once the panel takes its third — so opening it moves every offset.
        """
        tail = " 그리고 자세한 설명을 조금 덧붙이면 이렇게 됩니다"
        return (
            run([line + tail for line in RELEASE], 1, 100)
            + run([line + tail for line in DINNER], 101, 70)
            + run([line + tail for line in TRIP], 201, 40)
        )

    def test_closing_the_panel_keeps_the_topic_jumped_to_on_the_top_line(self) -> None:
        width, short = 120, 20
        self.app = self.make_app(self.long_lines())
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(width, short)
        self.app.handle(Key.UP, "")
        self.app.handle(Key.UP, "")
        self.app.handle(Key.ENTER, "")
        top = [strip_ansi(line) for line in self.app.render(width, short)][BODY_TOP]
        self.assertIn(RELEASE[0], top)
        self.app.handle(Key.ESCAPE, "")
        top = [strip_ansi(line) for line in self.app.render(width, short)][BODY_TOP]
        self.assertIn(RELEASE[0], top)

    def test_opening_the_panel_keeps_the_line_being_read_on_top(self) -> None:
        width, short = 120, 20
        self.app = self.make_app(self.long_lines())
        self.app.render(width, short)
        self.chat.offset = 12
        self.app.render(width, short)
        before = self.app._top_anchor(self.chat)
        assert before is not None
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(width, short)
        after = self.app._top_anchor(self.chat)
        assert after is not None
        self.assertEqual(after[0], before[0])
        self.assertNotEqual(after[2], before[2], "the width really changed")

    def test_a_feed_row_past_the_floor_does_not_end_the_paging(self) -> None:
        # One topic that never paused: a deleted-for-everyone line and a join
        # sit just past the floor, with the conversation going on behind them.
        talk = run((RELEASE * 10)[:30], 1, 330, step=3)  # 330 to 243 minutes ago
        feed = replace(run(["{}"], 50, 242)[0], type=0)
        recent = run((RELEASE * 10)[:46], 101, 238, step=3)  # 238 to 103 minutes ago
        self.reader.history = talk + [feed] + recent
        self.app = self.make_app([feed] + recent)
        self.app.handle("char", TOPIC_FLOW)
        self.assertLess(self.chat.messages[0].id, 50)
        flow = self.chat.topic_flow
        assert flow is not None
        self.assertTrue(flow.topics[0].truncated)

    def test_a_quiet_room_s_window_moves_with_the_clock(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        flow = self.chat.topic_flow
        assert flow is not None
        self.assertFalse(self.app.refresh(WIDE), "the same minute redraws nothing")
        self.app._wall = lambda: float(NOW + 90)
        self.assertTrue(self.app.refresh(WIDE))
        moved = self.chat.topic_flow
        assert moved is not None
        self.assertGreater(moved.end, flow.end)

    def test_page_keys_scroll_the_panel_when_it_has_the_conversation_s_place(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(NARROW, 16)
        self.app.handle(Key.UP, "")
        self.app.handle(Key.UP, "")
        self.app.handle(Key.ENTER, "")
        self.app.render(NARROW, 16)
        offset, scroll = self.chat.offset, self.chat.topic_scroll
        self.app.handle(Key.PAGE_DOWN, "")
        self.app.render(NARROW, 16)
        self.assertEqual(self.chat.offset, offset)
        self.assertGreater(self.chat.topic_scroll, scroll)
        self.app.handle(Key.END, "")
        flow = self.chat.topic_flow
        assert flow is not None
        self.assertEqual(self.chat.topic_selected, len(flow.topics) - 1)

    def test_arrivals_behind_a_narrow_panel_stay_unseen(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(NARROW, HEIGHT)
        self.watcher.queued = [replace(self.loaded[-1], id=900, text="안 보이는 사이에 온 말")]
        self.app.refresh(NARROW)
        self.assertEqual(self.chat.unseen, 1)
        self.app.handle("char", "a")
        self.assertEqual(self.chat.unseen, 1, "a keystroke behind the panel has not shown it")
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.chat.unseen, 0)

    def test_an_os_error_while_paging_is_said_too(self) -> None:
        self.reader.failure = OSError("디스크를 읽지 못함")
        self.app = self.make_app(run(DINNER, 101, 30))
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.chat.topics_open)
        self.assertIn("디스크를 읽지 못함", self.chat.notice)
        self.assertFalse(self.chat.topic_complete)

    def test_enter_on_a_topic_with_nowhere_to_jump_changes_nothing(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(WIDE)
        self.type_command("/sum 30m")
        flow = self.chat.topic_flow
        assert flow is not None
        # A topic whose messages this tab no longer holds.
        stranger = replace(flow.topics[-1], messages=tuple(run(["다른 방 말"], 9000, 5)))
        self.chat.topic_flow = replace(flow, topics=[*flow.topics[:-1], stranger])
        self.app.handle(Key.ENTER, "")
        self.assertTrue(self.chat.slices)
        self.assertIsNone(self.chat.topic_expanded)

class RecordingConsole:
    """What `_paint` hands the terminal, and whether it gave the frame up."""

    def __init__(self) -> None:
        self.forgotten = 0
        self.overlays: list[str] = []

    def forget(self) -> None:
        self.forgotten += 1

    def draw(self, lines, cursor=None, overlay: str = "") -> None:
        self.overlays.append(overlay)


class InlinePhotoBesideThePanelTests(TopicPanelCase):
    """An iTerm2 or sixel picture lives in the cells; a repainted row erases it."""

    PHOTO = 999

    def setUp(self) -> None:
        super().setUp()
        photo = replace(
            self.loaded[-1],
            id=self.PHOTO,
            text="사진",
            type=2,
            photo=Photo(url="https://example.invalid/p.png", width=20, height=60),
        )
        self.app = self.make_app(self.loaded + [photo], Theme(truecolor=True, enabled=True))
        self.app._protocol = images.ITERM2
        self.app._cell = (8, 16)
        # Seeded rather than fetched, so no store and no conversion is involved.
        self.app._photo_bytes.put(self.PHOTO, b"synthetic")
        self.app._photo_shapes[self.PHOTO] = (20, 60)
        self.chat.opened_photos.add(self.PHOTO)
        self.console = RecordingConsole()

    def paint(self) -> str:
        self.app._paint(self.console, WIDE, HEIGHT)  # type: ignore[arg-type]
        return self.console.overlays[-1]

    def test_a_change_in_the_panel_beside_a_picture_draws_it_again(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.assertIn("1337;File", self.paint())
        self.assertEqual(self.paint(), "", "a still screen writes nothing")
        forgotten = self.console.forgotten
        self.app.handle(Key.UP, "")
        self.assertIn("1337;File", self.paint(), "the arrow rewrote the picture's rows")
        self.assertGreater(self.console.forgotten, forgotten)
        self.assertEqual(self.paint(), "")

    def test_without_the_panel_a_still_picture_is_left_alone(self) -> None:
        self.assertIn("1337;File", self.paint())
        self.app.handle(Key.PAGE_UP, "")
        self.app.handle(Key.PAGE_DOWN, "")
        self.assertEqual(self.paint(), "")


if __name__ == "__main__":
    unittest.main()
