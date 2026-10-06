"""The topic panel on screen: the key that opens it, the keys it takes, the width it leaves.

The panel shares the keyboard with a composer that owns typing, so most of
what is pinned here is about what it must *not* take: a written message is
still sent by enter, escape still keeps the draft, letters still type.

Ported from the Windows build's suite with the same names, so the two can be
read side by side. What differs is where history comes from: here every read
is an `OLDER` job for the thread that owns the database, so the paging that
opens the panel is driven through a synchronous `DatabaseOwner` for most
tests, and through a real `DatabaseWorker` for the ones about the thread.
"""

from __future__ import annotations

import os
import select
import threading
import unittest
from dataclasses import replace
from typing import Optional
from unittest.mock import patch

from kamado.errors import ErrorCode, KamadoError
from kamado.model import Message, Notice, Photo, Room
from tests.platform import require_posix_console

require_posix_console()

from kamado.tui import app as app_module
from kamado.tui import images
from kamado.tui.app import App, ChatState
from kamado.tui.console import TOPIC_FLOW, Console, Key
from kamado.tui.layout import display_width, strip_ansi
from kamado.tui.theme import Theme
from kamado.tui.worker import NOTICE, OLDER, DatabaseOwner, DatabaseWorker, Done, Job

from tests.test_topics import DINNER, NOW, RELEASE, ROOM, TRIP, message, run

HEIGHT = 40
WIDE = 140
NARROW = 90
BODY_TOP = 3
"""Tab strip, hints and the notice row sit above the body."""
WAIT = 5.0
"""How long an awaited answer from the owner thread may take before it is a failure."""


class FakeReader:
    """Hands back older pages from a fixed history, the way `Reader.messages` pages.

    Records the thread each read ran on, so a test can say the interface
    never read anything itself.
    """

    def __init__(self, history: list[Message], failure: Optional[Exception] = None) -> None:
        self.history = history
        self.failure = failure
        self.asked: list[Optional[int]] = []
        self.threads: list[int] = []
        self.reveal_covered = False
        self.gate: Optional[threading.Event] = None
        """When set, a read waits here until the test opens it."""
        self.entered = threading.Event()

    def messages(self, room_id, limit=None, before_id=None, include_erased=False):
        self.threads.append(threading.get_ident())
        self.entered.set()
        if self.gate is not None and not self.gate.wait(WAIT):
            raise AssertionError("the gate was never opened")
        if self.failure is not None:
            raise self.failure
        self.asked.append(before_id)
        older = [one for one in self.history if before_id is None or one.id < before_id]
        return older[-limit:] if limit else older

    def feed_details(self, room_id, wanted):
        return {}

    def close(self) -> None:
        pass


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

    def make_app(self, messages: list[Message], theme: Optional[Theme] = None) -> App:
        owner = DatabaseOwner(self.reader)  # type: ignore[arg-type]
        # The owner polls whatever watcher it holds; this one is the test's.
        owner.watcher = self.watcher  # type: ignore[assignment]
        app = App(self.reader, theme=theme or Theme(truecolor=True, enabled=False), db=owner)  # type: ignore[arg-type]
        app._wall = lambda: float(NOW)
        app.chats = [ChatState(room=room(), messages=list(messages), anchor_top=False, token=1)]
        return app

    @property
    def chat(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def frame(self, width: int = WIDE, height: int = HEIGHT) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(width, height)]

    def body(self, width: int = WIDE, height: int = HEIGHT) -> list[str]:
        frame = self.frame(width, height)
        return frame[BODY_TOP : BODY_TOP + self.app._body_rows]

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
        # SI as the terminal writes it in raw mode, one byte.
        console = Console()
        console._input_fd = 0
        console._input = bytearray(b"\x0f")
        self.assertEqual(console.read_key(0.0), ("char", TOPIC_FLOW))

    def test_raw_mode_delivers_ctrl_o_as_itself(self) -> None:
        # The terminal driver's own reading of the byte (VDISCARD) belongs to
        # IEXTEN, which raw mode clears; proven on a real pseudo-terminal.
        try:
            import pty
            import termios
            import tty
        except ImportError:
            self.skipTest("no pseudo-terminal here")
        if not hasattr(os, "openpty"):
            self.skipTest("no pseudo-terminal here")
        controller, terminal = pty.openpty()
        try:
            tty.setraw(terminal, termios.TCSANOW)
            os.write(controller, b"\x0f")
            readable, _, _ = select.select([terminal], [], [], WAIT)
            self.assertTrue(readable, "nothing reached the terminal side")
            self.assertEqual(os.read(terminal, 16), b"\x0f")
        finally:
            os.close(controller)
            os.close(terminal)

    def test_the_mouse_switch_keeps_its_own_key(self) -> None:
        mouse = self.app.mouse_clicks
        self.app.handle("char", TOPIC_FLOW)
        self.assertEqual(self.app.mouse_clicks, mouse)
        self.assertEqual(self.chat.draft, "")

    def test_the_newest_topic_is_selected_when_the_panel_opens(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.assertEqual(self.chat.topic_selected, 2)

    def test_help_and_the_hotkey_are_listed(self) -> None:
        self.type_command("/help")
        rendered = "\n".join(self.body(height=60))
        self.assertIn("^O", rendered)
        self.assertIn("/flow /topics", rendered)

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

    def test_a_multi_line_draft_keeps_the_arrows_until_its_edge(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(WIDE)
        self.chat.draft = "첫 줄\n둘째 줄"
        self.chat.caret = len(self.chat.draft)
        self.app.handle(Key.UP, "")
        self.assertEqual(self.chat.topic_selected, 2, "the caret moved up a line instead")
        self.assertLess(self.chat.caret, len("첫 줄") + 1)
        self.app.handle(Key.UP, "")
        self.assertEqual(self.chat.topic_selected, 1)

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
        body = self.body(WIDE, short)
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
                    for row in range(BODY_TOP, BODY_TOP + self.app._body_rows):
                        self.assertEqual(display_width(frame[row]), width, (row, strip_ansi(frame[row])))

    def test_the_panel_is_drawn_on_the_terminal_s_own_background(self) -> None:
        self.app = self.make_app(self.loaded, Theme(truecolor=True, enabled=True))
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.ENTER, "")
        for width in (WIDE, NARROW):
            with self.subTest(width=width):
                frame = self.app.render(width, HEIGHT)
                body = frame[BODY_TOP : BODY_TOP + self.app._body_rows]
                panel = [line.split("│", 1)[-1] if width == WIDE else line for line in body]
                self.assertFalse(any("\x1b[48;" in line for line in panel), panel)

    def test_the_rule_is_put_at_its_column_rather_than_after_the_text(self) -> None:
        # An emoji sequence the terminal draws at another width than it
        # measures must not move the rule: it is placed, not appended.
        joined = replace(self.loaded[-1], text="가족 \U0001f468‍\U0001f469‍\U0001f467 사진 ❤️")
        self.app = self.make_app(self.loaded[:-1] + [joined])
        self.app.handle("char", TOPIC_FLOW)
        left = WIDE - app_module._topic_panel_width(WIDE) - 1
        jump = f"\x1b[K\x1b[{left + 1}G"
        frame = self.app.render(WIDE, HEIGHT)
        body = frame[BODY_TOP : BODY_TOP + self.app._body_rows]
        self.assertTrue(all(jump in line for line in body), body)

    def test_key_messages_drop_what_makes_an_emoji_s_width_a_guess(self) -> None:
        joined = replace(
            self.loaded[-1],
            text="캠핑 텐트 \U0001f468‍\U0001f469‍\U0001f467 ❤️ \U0001f44d\U0001f3fd",
        )
        self.app = self.make_app(self.loaded[:-1] + [joined])
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.ENTER, "")
        # Narrow, so the panel is all there is and nothing else can hide it.
        frame = self.app.render(NARROW, HEIGHT)
        panel = "".join(frame[BODY_TOP : BODY_TOP + self.app._body_rows])
        self.assertIn("캠핑", panel)
        self.assertIn("\U0001f468\U0001f469\U0001f467", panel)
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

    def test_clicks_on_the_panel_reach_nothing_in_the_conversation(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.frame(WIDE)
        left = WIDE - app_module._topic_panel_width(WIDE) - 1
        self.assertIsNotNone(self.app._cell_at(BODY_TOP + 2, left - 1))
        self.assertIsNone(self.app._cell_at(BODY_TOP + 2, left + 3))
        self.frame(NARROW)
        self.assertIsNone(self.app._cell_at(BODY_TOP + 2, 3))

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
        frame = self.frame(WIDE, 16)
        self.assertTrue(any("▶" in line for line in frame), frame)
        self.app.handle(Key.DOWN, "")
        self.app.handle(Key.DOWN, "")
        frame = self.frame(WIDE, 16)
        self.assertTrue(any("▶" in line for line in frame), frame)

    # -- what it is cut from ------------------------------------------------

    def test_arrivals_recut_the_flow_and_the_selection_stays_put(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle(Key.UP, "")
        flow = self.chat.topic_flow
        assert flow is not None
        held = flow.topics[self.chat.topic_selected].messages[0].id
        self.watcher.queued = run(RELEASE, 301, 10, step=1)
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
        self.assertFalse(self.chat.topic_paging)
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
        self.reader.failure = KamadoError(ErrorCode.KEY_UNAVAILABLE, "열쇠 없음")
        self.app = self.make_app(run(DINNER, 101, 30))
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.chat.topics_open)
        self.assertIn("열쇠 없음", self.chat.notice)
        self.assertFalse(self.chat.topic_complete)

    def test_an_os_error_while_paging_is_said_too(self) -> None:
        self.reader.failure = OSError("디스크를 읽지 못함")
        self.app = self.make_app(run(DINNER, 101, 30))
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.chat.topics_open)
        self.assertIn("디스크를 읽지 못함", self.chat.notice)
        self.assertFalse(self.chat.topic_complete)

    def test_older_pages_asked_for_by_hand_recut_an_open_panel(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        before = self.chat.topic_flow
        self.reader.history = run(TRIP, -20, 115, step=1)  # ids -20 to -15
        self.type_command("/older")
        self.assertIsNot(self.chat.topic_flow, before)

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
        top = self.frame(width, short)[BODY_TOP]
        self.assertIn(RELEASE[0], top)
        self.app.handle(Key.ESCAPE, "")
        top = self.frame(width, short)[BODY_TOP]
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

    def test_the_wheel_scrolls_the_panel_when_it_has_the_conversation_s_place(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(NARROW, 16)
        self.app.handle(Key.UP, "")
        self.app.handle(Key.UP, "")
        self.app.handle(Key.ENTER, "")
        self.app.render(NARROW, 16)
        offset, scroll = self.chat.offset, self.chat.topic_scroll
        self.app.handle(Key.WHEEL_DOWN, "")
        self.app.render(NARROW, 16)
        self.assertEqual(self.chat.offset, offset)
        self.assertGreater(self.chat.topic_scroll, scroll)

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


class DeferredOwner(DatabaseOwner):
    """Takes jobs in order like `DatabaseWorker`, and answers one only when told.

    What lets a test decide which of two questions in flight is answered
    first, without a thread or a clock.
    """

    def __init__(self, reader: FakeReader) -> None:
        super().__init__(reader)  # type: ignore[arg-type]
        self.queue: list[Job] = []

    def submit(self, job: Job) -> bool:
        if any(queued.key == job.key for queued in self.queue):
            return False
        self.queue.append(job)
        return True

    def answer(self) -> Job:
        job = self.queue.pop(0)
        self.sink(self._run(job))
        return job


def busy_history() -> list[Message]:
    """Six hours of one unbroken conversation, a line every twelve seconds."""
    return [
        message(index + 1, 360 - index * 0.2, f"주제{index // 40} 이야기 계속 대화 {index}", "가나다"[index % 3])
        for index in range(1800)
    ]


class TopicPagingOrderTests(unittest.TestCase):
    """`/older` and the panel's pages are both `OLDER` jobs, answered in either order."""

    def setUp(self) -> None:
        self.history = busy_history()
        self.reader = FakeReader(history=self.history)
        self.owner = DeferredOwner(self.reader)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False), db=self.owner)  # type: ignore[arg-type]
        self.app._wall = lambda: float(NOW)
        self.app.chats = [ChatState(room=room(), messages=list(self.history[-100:]), anchor_top=False, token=1)]
        self.app.render(WIDE, HEIGHT)

    @property
    def chat(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def answer_all(self) -> None:
        for _ in range(50):
            if not self.owner.queue:
                return
            self.owner.answer()
        self.fail("the paging never stopped asking")

    def test_a_page_answered_after_older_with_the_same_rows_keeps_paging(self) -> None:
        self.app.load_older()
        self.app.handle("char", TOPIC_FLOW)
        self.assertEqual([job.about for job in self.owner.queue], ["", app_module.TOPIC_PAGES])
        self.owner.answer()  # /older lands first
        self.owner.answer()  # the panel's page: every row already held
        self.assertTrue(self.chat.topic_paging, "the held rows were taken for the end of the room")
        self.assertEqual(self.owner.queue[0].about, app_module.TOPIC_PAGES)
        self.assertEqual(self.owner.queue[0].payload[0], self.chat.messages[0].id)
        self.answer_all()
        floor = NOW - 4 * 3600
        self.assertTrue(self.chat.topic_complete)
        self.assertLess(self.chat.messages[0].sent_at, floor)  # type: ignore[operator]

    def test_older_answered_after_a_topic_page_does_not_say_the_history_ended(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.load_older()
        self.owner.answer()  # the panel's page lands first and asks for the next
        self.owner.answer()  # /older: every row already held
        self.assertNotIn("이전 기록이 더 없습니다", self.chat.notice)
        again = [job for job in self.owner.queue if job.kind == OLDER and job.about == ""]
        self.assertEqual(len(again), 1, self.owner.queue)
        self.assertEqual(again[0].payload[0], self.chat.messages[0].id)

    def test_older_still_says_when_the_room_has_nothing_older(self) -> None:
        self.reader.history = []
        self.app.load_older()
        self.owner.answer()
        self.assertEqual(self.chat.notice, "이전 기록이 더 없습니다")

    def test_every_page_cuts_the_window_again_before_the_next_is_asked(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        before = self.chat.topic_flow
        self.owner.answer()
        self.assertTrue(self.chat.topic_paging, "one page is not enough for this room")
        self.assertIsNot(self.chat.topic_flow, before)
        flow = self.chat.topic_flow
        assert flow is not None
        held = {one.id for topic in flow.topics for one in topic.messages}
        self.assertIn(self.chat.messages[0].id, held, "the page just added is part of the cut")


class BackgroundTabTests(TopicPanelCase):
    def test_an_answer_for_a_tab_in_the_background_leaves_its_panel_open(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        behind = self.chat
        other = ChatState(room=replace(room(), id=ROOM + 1, title="다른 방"), messages=run(TRIP, 501, 20), anchor_top=False, token=2)
        self.app.chats.append(other)
        self.app.focus = 1
        self.app.render(WIDE, HEIGHT)  # the last frame is the other tab's, with no panel
        notice = Notice("공지 내용", "notice", None, None, None, "legacy")
        self.app._settle(Done(Job(NOTICE, room_id=ROOM, token=behind.token, about="show"), notice))
        self.assertTrue(behind.panel, "the notice was put up")
        self.assertTrue(behind.topics_open, "a wide screen has room for both")

    def test_on_a_narrow_screen_the_answer_still_takes_the_panel_s_place(self) -> None:
        self.app.handle("char", TOPIC_FLOW)
        self.app.render(NARROW, HEIGHT)
        notice = Notice("공지 내용", "notice", None, None, None, "legacy")
        self.app._settle(Done(Job(NOTICE, room_id=ROOM, token=self.chat.token, about="show"), notice))
        self.assertTrue(self.chat.panel)
        self.assertFalse(self.chat.topics_open)


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
            photo=Photo(url="https://example.invalid/p.png", width=20, height=60),
        )
        self.app = self.make_app(self.loaded + [photo], Theme(truecolor=True, enabled=True))
        self.app._protocol = images.ITERM2
        # Seeded rather than fetched, so no store and no conversion is involved.
        self.app._bytes.put(self.PHOTO, b"synthetic")
        self.app._shapes[self.PHOTO] = (20, 60)
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


class TopicPagingThreadTests(TopicPanelCase):
    """The panel's history is read by the thread that owns the database.

    Gated on events rather than a clock, as `test_tui_worker` is: the read is
    held open while the screen is driven, so "the keyboard kept working" is
    the order things happened in.
    """

    def start(self, recent: list[Message]) -> DatabaseWorker:
        worker = DatabaseWorker(self.reader).start()  # type: ignore[arg-type]
        self.addCleanup(worker.close)
        app = App(self.reader, theme=Theme(truecolor=True, enabled=False), db=worker)  # type: ignore[arg-type]
        app._wall = lambda: float(NOW)
        app.chats = [ChatState(room=room(), messages=list(recent), anchor_top=False, token=1)]
        self.app = app
        return worker

    def settle(self, worker: DatabaseWorker) -> None:
        """Apply answers until the paging the panel started has finished."""
        while self.chat.topic_paging:
            worker.drain(timeout=WAIT)

    def test_opening_returns_at_once_and_says_it_is_still_reading(self) -> None:
        older = run(TRIP, 1, 300, step=10) + run(RELEASE, 11, 200, step=5)
        recent = run(DINNER, 101, 30)
        self.reader.history = older + recent
        worker = self.start(recent)
        self.reader.gate = threading.Event()
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.reader.entered.wait(WAIT))
        # The read is held open on the owner thread; the screen answers anyway.
        self.assertTrue(self.chat.topics_open)
        self.assertTrue(self.chat.topic_paging)
        self.assertTrue(any("앞 기록을 불러오는 중" in line for line in self.body()))
        self.app.handle(Key.UP, "")
        self.app.handle("char", "가")
        self.assertEqual(self.chat.draft, "가")
        self.reader.gate.set()
        self.settle(worker)
        self.assertEqual(self.chat.messages[0].id, older[0].id)
        self.assertTrue(self.chat.topic_complete)
        self.assertNotIn(threading.get_ident(), self.reader.threads)
        self.assertFalse(any("앞 기록을 불러오는 중" in line for line in self.body()))

    def test_closing_while_a_page_is_read_keeps_it_and_asks_for_no_more(self) -> None:
        older = run(TRIP, 1, 300, step=10) + run(RELEASE, 11, 200, step=5)
        recent = run(DINNER, 101, 30)
        self.reader.history = older + recent
        worker = self.start(recent)
        self.reader.gate = threading.Event()
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.reader.entered.wait(WAIT))
        self.app.handle(Key.ESCAPE, "")
        self.reader.gate.set()
        self.settle(worker)
        self.assertFalse(self.chat.topics_open)
        self.assertEqual(len(self.reader.asked), 1)
        self.assertLess(self.chat.messages[0].id, recent[0].id)

    def test_reopening_while_a_page_is_read_waits_for_it_instead_of_asking_twice(self) -> None:
        older = run(TRIP, 1, 300, step=10) + run(RELEASE, 11, 200, step=5)
        recent = run(DINNER, 101, 30)
        self.reader.history = older + recent
        worker = self.start(recent)
        self.reader.gate = threading.Event()
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.reader.entered.wait(WAIT))
        self.app.handle("char", TOPIC_FLOW)
        self.app.handle("char", TOPIC_FLOW)
        self.assertNotIn("밀려", self.chat.notice)
        self.reader.gate.set()
        self.settle(worker)
        self.assertTrue(self.chat.topics_open)
        self.assertTrue(self.chat.topic_complete)
        self.assertEqual(self.chat.messages[0].id, older[0].id)

    def test_a_tab_closed_while_its_page_is_read_is_left_alone(self) -> None:
        recent = run(DINNER, 101, 30)
        self.reader.history = run(TRIP, 1, 300, step=10) + recent
        worker = self.start(recent)
        self.reader.gate = threading.Event()
        self.app.handle("char", TOPIC_FLOW)
        self.assertTrue(self.reader.entered.wait(WAIT))
        self.app.handle(Key.ESCAPE, "")
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.chats, [])
        self.reader.gate.set()
        worker.drain(timeout=WAIT)
        self.assertEqual(self.app.chats, [])


if __name__ == "__main__":
    unittest.main()
