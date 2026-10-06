"""Who owns the mouse while the interface is running.

Asking the terminal to report buttons is what makes a photo label and a link
clickable, and it is also what takes ordinary drag-to-select away: the terminal
hands the drag to this program instead of painting a selection, so there is
nothing to copy with Cmd+C. Both are wanted, never at the same moment, so the
reader switches between them with Ctrl+G.

What is pinned here is the switch itself — that reports start on unless the
environment says otherwise, that the key never reaches the composer, that the
loop carries the state to the console, and that leaving hands the mouse back to
the terminal whichever mode was last chosen.
"""

from __future__ import annotations

import io
import os
import re
import unittest
from unittest.mock import patch

from tests.platform import require_posix_console

require_posix_console()

from kamado.tui import app as app_module
from kamado.tui import console as console_module
from kamado.tui.app import App
from kamado.tui.console import (
    MOUSE_OFF,
    MOUSE_ON,
    SELECT_MODE,
    Console,
    Key,
    mouse_default,
)
from kamado.tui.layout import strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import GROUP_ROOM, build_reader

WIDTH = 80
HEIGHT = 24


class WrittenConsole:
    """A real console writing to a buffer, with no terminal underneath it."""

    def __init__(self, console: "Console | None" = None) -> None:
        self.console = console if console is not None else Console()
        self.buffer = io.StringIO()
        self._patches = [patch("sys.stdout", self.buffer), patch("sys.stdin", io.StringIO())]
        self._open = False

    def __enter__(self) -> "WrittenConsole":
        for item in self._patches:
            item.start()
        return self

    def enter(self) -> Console:
        """Own the (absent) terminal, and leave it before stdout is real again."""
        self.console.__enter__()
        self._open = True
        return self.console

    def leave(self) -> None:
        self.console.__exit__()
        self._open = False

    def __exit__(self, *_exception: object) -> None:
        # Leaving writes escapes; they belong in the buffer, never on the
        # terminal running the suite.
        if self._open:
            self.leave()
        for item in reversed(self._patches):
            item.stop()

    def taken(self) -> str:
        """Return what has been written since the last time it was asked."""
        written = self.buffer.getvalue()
        self.buffer.seek(0)
        self.buffer.truncate(0)
        return written


class ConsoleMouseTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"KAMADO_MOUSE": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_entering_asks_the_terminal_for_button_reports(self) -> None:
        with WrittenConsole() as owner:
            console = owner.enter()
            self.assertIn(MOUSE_ON, owner.taken())
            self.assertTrue(console.mouse)

    def test_the_environment_can_start_with_the_mouse_left_alone(self) -> None:
        with WrittenConsole() as owner, patch.dict(os.environ, {"KAMADO_MOUSE": "0"}):
            console = owner.enter()
            self.assertNotIn(MOUSE_ON, owner.taken())
            self.assertFalse(console.mouse)

    def test_giving_the_mouse_back_stops_the_reports(self) -> None:
        with WrittenConsole() as owner:
            console = owner.enter()
            owner.taken()
            console.set_mouse(False)
            self.assertIn(MOUSE_OFF, owner.taken())
            self.assertFalse(console.mouse)

    def test_asking_for_the_state_it_is_already_in_writes_nothing(self) -> None:
        with WrittenConsole() as owner:
            console = owner.enter()
            console.set_mouse(False)
            owner.taken()
            console.set_mouse(False)
            self.assertEqual(owner.taken(), "")

    def test_taking_the_mouse_back_restores_clicks(self) -> None:
        with WrittenConsole() as owner:
            console = owner.enter()
            console.set_mouse(False)
            owner.taken()
            console.set_mouse(True)
            self.assertIn(MOUSE_ON, owner.taken())
            self.assertTrue(console.mouse)

    def test_leaving_hands_the_mouse_back_from_selection_mode(self) -> None:
        with WrittenConsole() as owner:
            console = owner.enter()
            console.set_mouse(False)
            owner.taken()
            owner.leave()
            self.assertIn(MOUSE_OFF, owner.taken())

    def test_a_still_screen_writes_nothing_over_a_selection(self) -> None:
        # Selection mode is only useful if the background redraw leaves the
        # terminal's own highlight alone, which it does by writing no frame at
        # all when nothing on screen changed.
        rows = ["one", "two", "three"]
        with WrittenConsole() as owner, patch.object(
            console_module, "_visible_size", lambda: (WIDTH, HEIGHT)
        ):
            owner.console.draw(rows)
            self.assertNotEqual(owner.taken(), "")
            owner.console.draw(rows)
            self.assertEqual(owner.taken(), "")

    def test_the_default_is_reports_on(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAMADO_MOUSE", None)
            self.assertTrue(mouse_default())
        with patch.dict(os.environ, {"KAMADO_MOUSE": "0"}):
            self.assertFalse(mouse_default())


class AppCase(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"KAMADO_MOUSE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        # The fixture supplies an externally owned cache; Reader.close does
        # not close that cache's connection.
        self.addCleanup(self.reader.connection().close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.app.load_rooms()

    def open_chat(self):
        self.app.open_room(next(r for r in self.app.rooms if r.id == GROUP_ROOM))
        chat = self.app.chat
        assert chat is not None
        return chat

    def frame(self) -> str:
        return "\n".join(strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT))


class ToggleTests(AppCase):
    def test_the_toggle_key_hands_the_mouse_to_the_terminal(self) -> None:
        self.open_chat()
        self.assertTrue(self.app.mouse_clicks)
        self.app.handle("char", SELECT_MODE)
        self.assertFalse(self.app.mouse_clicks)
        self.app.handle("char", SELECT_MODE)
        self.assertTrue(self.app.mouse_clicks)

    def test_toggling_types_nothing_and_sends_nothing(self) -> None:
        chat = self.open_chat()
        chat.draft = "안녕"
        chat.caret = len(chat.draft)
        self.app.handle("char", SELECT_MODE)
        self.assertEqual(chat.draft, "안녕")
        self.assertEqual(chat.caret, 2)
        self.assertIsNone(self.app.pending_send)
        self.assertIsNone(chat.outbox)

    def test_the_room_list_toggles_without_typing_a_filter(self) -> None:
        self.app.handle("char", SELECT_MODE)
        self.assertFalse(self.app.mouse_clicks)
        self.assertEqual(self.app.filter, "")
        self.assertFalse(self.app.filtering)

    def test_a_started_filter_still_toggles_rather_than_taking_the_key(self) -> None:
        self.app.handle("char", "/")
        self.app.handle("char", SELECT_MODE)
        self.assertFalse(self.app.mouse_clicks)
        self.assertEqual(self.app.filter, "")

    def test_the_environment_can_start_in_selection_mode(self) -> None:
        with patch.dict(os.environ, {"KAMADO_MOUSE": "0"}):
            app = App(self.reader, theme=Theme(truecolor=True, enabled=True))
        self.assertFalse(app.mouse_clicks)

    def test_the_mode_is_said_on_screen_both_ways(self) -> None:
        self.open_chat()
        self.assertIn(app_module.CLICK_HINT, self.frame())
        self.app.handle("char", SELECT_MODE)
        self.assertIn(app_module.SELECT_HINT, self.frame())

    def test_switching_says_what_to_do_next(self) -> None:
        chat = self.open_chat()
        self.app.handle("char", SELECT_MODE)
        self.assertEqual(chat.notice, app_module.SELECT_NOTICE)
        self.app.handle("char", SELECT_MODE)
        self.assertEqual(chat.notice, app_module.CLICK_NOTICE)

    def test_switching_outside_a_room_is_said_on_the_status_line(self) -> None:
        self.app.handle("char", SELECT_MODE)
        self.assertEqual(self.app.status, app_module.SELECT_NOTICE)


class RecordingConsole(Console):
    """A console that answers the loop from a queue and records the mode."""

    def __init__(self, keys: list[tuple[str, str]]) -> None:
        super().__init__()
        self.keys = list(keys)
        self._mouse = True
        self.modes: list[bool] = []
        self.frames = 0

    @property
    def size(self) -> tuple[int, int]:
        return WIDTH, HEIGHT

    def draw(self, lines, cursor=None, overlay: str = "") -> None:
        self.frames += 1

    def forget(self) -> None:
        pass

    def cell_pixels(self):
        return None

    def supports_sixel(self) -> bool:
        return False

    def set_mouse(self, enabled: bool) -> None:
        self.modes.append(enabled)
        self._mouse = enabled

    def read_key(self, timeout: float = 0.0) -> tuple[str, str]:
        assert self.keys, "the loop asked for a key after the last one"
        return self.keys.pop(0)


class LoopTests(AppCase):
    def test_the_loop_carries_the_mode_to_the_console(self) -> None:
        console = RecordingConsole([("char", SELECT_MODE), (Key.INTERRUPT, "")])
        self.app.run(console)
        self.assertEqual(console.modes, [True, False])
        self.assertFalse(console.mouse)

    def test_a_reader_who_switches_back_gets_clicks_again(self) -> None:
        console = RecordingConsole(
            [("char", SELECT_MODE), ("char", SELECT_MODE), (Key.INTERRUPT, "")]
        )
        self.app.run(console)
        self.assertEqual(console.modes, [True, False, True])
        self.assertTrue(console.mouse)


class QueuedConsole(Console):
    """A real console — real escapes, real frames — fed keys from a queue."""

    def __init__(self, keys: list[tuple[str, str]], buffer: io.StringIO) -> None:
        super().__init__()
        self.keys = list(keys)
        self.buffer = buffer
        self.marks: list[int] = []
        """How much had been written by the time each key was asked for."""

    def read_key(self, timeout: float = 0.0) -> tuple[str, str]:
        self.marks.append(len(self.buffer.getvalue()))
        assert self.keys, "the loop asked for a key after the last one"
        return self.keys.pop(0)


class EscapeTests(AppCase):
    """What actually reaches the terminal when the reader presses Ctrl+G."""

    def setUp(self) -> None:
        super().setUp()
        # A still clock, so an expiring notice cannot change a frame and make
        # an idle tick look like a redraw.
        self.app._clock = lambda: 1000.0

    def driven(self, keys: list[tuple[str, str]]) -> tuple[QueuedConsole, str]:
        owner = WrittenConsole()
        console = QueuedConsole(keys, owner.buffer)
        owner.console = console
        with owner, patch.object(console_module, "_visible_size", lambda: (WIDTH, HEIGHT)):
            owner.enter()
            self.app.run(console)
            owner.leave()
            return console, owner.buffer.getvalue()

    def test_the_switch_reaches_the_terminal_as_the_mode_escapes(self) -> None:
        _console, written = self.driven(
            [("char", SELECT_MODE), ("char", SELECT_MODE), (Key.INTERRUPT, "")]
        )
        pattern = f"{re.escape(MOUSE_ON)}|{re.escape(MOUSE_OFF)}"
        # On at startup, off on the first Ctrl+G, on again on the second, and
        # off for good when the interface gives the terminal back.
        self.assertEqual(
            re.findall(pattern, written),
            [MOUSE_ON, MOUSE_OFF, MOUSE_ON, MOUSE_OFF],
        )

    def test_an_idle_loop_in_selection_mode_writes_nothing(self) -> None:
        # Whatever is being selected with the mouse survives only if the
        # background loop leaves the screen alone between keystrokes.
        console, _written = self.driven(
            [("char", SELECT_MODE), ("", ""), ("", ""), (Key.INTERRUPT, "")]
        )
        settled = console.marks[1:]
        self.assertEqual(settled, [settled[0]] * len(settled))


if __name__ == "__main__":
    unittest.main()
