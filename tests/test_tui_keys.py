"""Key decoding contract.

The interface used to read keys with ``msvcrt.kbhit()`` plus
``msvcrt.getwch()``. A console queues more than characters — key releases,
focus changes, mouse moves, buffer resizes — and ``kbhit`` answers yes to all
of them while ``getwch`` then blocks with no deadline. Driving a real console
showed the cost: after typing 가나다 the screen still read 가나, and one
backspace later it read 가나다, because the loop sat inside ``getwch`` holding
the last keystroke until another key arrived.

These tests drive the decoder with synthetic console events, so they pin the
contract without a console.
"""

from __future__ import annotations

import unittest

from tests.platform import require_windows_console

require_windows_console()

from kanao.tui.console import Console, Key, KeyEvent

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_SHIFT = 0x10


class FakeConsole(Console):
    """A console whose input queue is a list instead of a device."""

    def __init__(self, events: list[KeyEvent]) -> None:
        super().__init__()
        self.queued = list(events)
        self.waits = 0

    def _events(self, timeout: float):
        self.waits += 1
        while self.queued:
            yield self.queued.pop(0)

    def _pending(self) -> bool:
        return bool(self.queued)


def down(virtual_key: int = 0, character: str = "", control: int = 0) -> KeyEvent:
    return KeyEvent(
        key_down=True, virtual_key=virtual_key, character=character, control_state=control
    )


def up(virtual_key: int = 0, character: str = "") -> KeyEvent:
    return KeyEvent(key_down=False, virtual_key=virtual_key, character=character)


SHIFT = 0x0010


class DecodingTests(unittest.TestCase):
    def read(self, *events: KeyEvent) -> tuple[str, str]:
        return FakeConsole(list(events)).read_key(0.0)

    def test_a_key_release_alone_never_blocks(self) -> None:
        # The whole bug: this used to hold the loop until the next keystroke.
        self.assertEqual(self.read(up(VK_BACK, "\x08")), ("", ""))

    def test_a_release_before_a_press_does_not_swallow_the_press(self) -> None:
        self.assertEqual(self.read(up(0, "a"), down(0, "b")), ("char", "b"))

    def test_backspace_is_backspace(self) -> None:
        self.assertEqual(self.read(down(VK_BACK, "\x08")), (Key.BACKSPACE, ""))

    def test_a_backspace_without_its_character_still_decodes(self) -> None:
        self.assertEqual(self.read(down(VK_BACK, "")), (Key.BACKSPACE, ""))

    def test_delete_character_form_is_backspace_too(self) -> None:
        self.assertEqual(self.read(down(0, "\x7f")), (Key.BACKSPACE, ""))

    def test_enter_escape_and_tab(self) -> None:
        self.assertEqual(self.read(down(VK_RETURN, "\r")), (Key.ENTER, ""))
        self.assertEqual(self.read(down(VK_ESCAPE, "\x1b")), (Key.ESCAPE, ""))
        self.assertEqual(self.read(down(0, "\t")), (Key.TAB, ""))

    def test_shift_tab_is_a_key_of_its_own(self) -> None:
        """A console reports it as the Tab record with SHIFT_PRESSED set."""
        self.assertEqual(self.read(down(VK_TAB, "\t", control=SHIFT)), (Key.BACKTAB, ""))
        self.assertEqual(self.read(down(VK_TAB, "", control=SHIFT)), (Key.BACKTAB, ""))
        self.assertEqual(self.read(down(VK_TAB, "\t")), (Key.TAB, ""))

    def test_arrows_and_paging(self) -> None:
        for virtual_key, expected in (
            (VK_UP, Key.UP), (VK_DOWN, Key.DOWN), (VK_LEFT, Key.LEFT),
            (VK_RIGHT, Key.RIGHT), (VK_PRIOR, Key.PAGE_UP), (VK_NEXT, Key.PAGE_DOWN),
            (VK_HOME, Key.HOME), (VK_END, Key.END),
        ):
            with self.subTest(virtual_key=virtual_key):
                self.assertEqual(self.read(down(virtual_key, "")), (expected, ""))

    def test_a_modifier_press_is_not_a_keystroke(self) -> None:
        self.assertEqual(self.read(down(VK_SHIFT, "")), ("", ""))

    def test_hangul_arrives_as_one_character(self) -> None:
        self.assertEqual(self.read(down(0, "가")), ("char", "가"))

    def test_a_space_is_typed_text(self) -> None:
        self.assertEqual(self.read(down(0, " ")), ("char", " "))

    def test_an_emoji_surrogate_pair_becomes_one_character(self) -> None:
        # A Windows console delivers astral characters as two UTF-16 halves.
        self.assertEqual(self.read(down(0, "\ud83d"), down(0, "\ude42")), ("char", "🙂"))

    def test_a_lone_high_surrogate_is_not_emitted(self) -> None:
        self.assertEqual(self.read(down(0, "\ud83d")), ("", ""))

    def test_ctrl_c_is_an_interrupt(self) -> None:
        self.assertEqual(self.read(down(0, "\x03")), (Key.INTERRUPT, ""))

    def test_an_empty_queue_reports_nothing(self) -> None:
        self.assertEqual(self.read(), ("", ""))


class NewlineDecodingTests(unittest.TestCase):
    """Two ways a newline reaches a chat box, and one that must still send."""

    def read(self, *events: KeyEvent) -> tuple[str, str]:
        return FakeConsole(list(events)).read_key(0.0)

    def test_shift_and_enter_is_a_newline(self) -> None:
        self.assertEqual(self.read(down(VK_RETURN, "\r", SHIFT)), ("char", "\n"))

    def test_enter_alone_still_sends(self) -> None:
        self.assertEqual(self.read(down(VK_RETURN, "\r")), (Key.ENTER, ""))

    def test_an_enter_inside_a_paste_is_a_newline(self) -> None:
        # A paste arrives as ordinary keystrokes, so the run of characters
        # still waiting is what tells one apart from a person pressing enter.
        console = FakeConsole([down(VK_RETURN, "\r"), down(0, "b")])
        self.assertEqual(console.read_key(0.0), ("char", "\n"))

    def test_the_last_enter_of_a_paste_still_sends(self) -> None:
        self.assertEqual(self.read(down(0, "a"), down(VK_RETURN, "\r")), ("char", "a"))
        # nothing is left behind it, so the enter that follows submits
        console = FakeConsole([down(VK_RETURN, "\r")])
        self.assertEqual(console.read_key(0.0), (Key.ENTER, ""))

    def test_the_delete_key_is_named(self) -> None:
        self.assertEqual(self.read(down(0x2E, "")), (Key.DELETE, ""))

    def test_control_u_asks_for_a_clear_box(self) -> None:
        self.assertEqual(self.read(down(0, "\x15")), ("char", "\x15"))


if __name__ == "__main__":
    unittest.main()
