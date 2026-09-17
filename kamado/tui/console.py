"""POSIX terminal ownership, whole-frame ANSI output, and UTF-8 key input.

The input descriptor stays in raw mode only inside the context manager. Reads
use select with a single deadline, retaining incomplete UTF-8 and escape
sequences for the next call instead of blocking the application's refresh.
"""

from __future__ import annotations

import fcntl
import os
import select
import shutil
import struct
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Final, Iterator, Optional

from .layout import pad_to_width, truncate_to_width

ALT_SCREEN_ON: Final = "\x1b[?1049h"
ALT_SCREEN_OFF: Final = "\x1b[?1049l"
HIDE_CURSOR: Final = "\x1b[?25l"
SHOW_CURSOR: Final = "\x1b[?25h"
AUTOWRAP_OFF: Final = "\x1b[?7l"
AUTOWRAP_ON: Final = "\x1b[?7h"
HOME: Final = "\x1b[H"
CLEAR_BELOW: Final = "\x1b[J"
CLEAR_LINE: Final = "\x1b[K"
_RESET: Final = "\x1b[0m"
_SAVE_AUTOWRAP: Final = "\x1b[?7s"
_RESTORE_AUTOWRAP: Final = "\x1b[?7r"
_ESCAPE_WAIT: Final = 0.03
"""Disambiguate a lone Escape without extending read_key's deadline."""
_SHIFT_PRESSED: Final = 0x0010
CLEAR_BOX: Final = "\x15"
"""Ctrl+U, which empties the composer; it is passed through as itself."""
_HIGH_SURROGATE: Final = range(0xD800, 0xDC00)
_LOW_SURROGATE: Final = range(0xDC00, 0xE000)


@dataclass(frozen=True)
class KeyEvent:
    """A normalized key; the legacy numeric identifiers remain API-compatible."""

    key_down: bool
    virtual_key: int
    character: str
    control_state: int = 0


class Key:
    """Names for the keys this interface reacts to."""

    UP: Final = "up"
    DOWN: Final = "down"
    LEFT: Final = "left"
    RIGHT: Final = "right"
    PAGE_UP: Final = "pageup"
    PAGE_DOWN: Final = "pagedown"
    HOME: Final = "home"
    END: Final = "end"
    ENTER: Final = "enter"
    ESCAPE: Final = "escape"
    BACKSPACE: Final = "backspace"
    DELETE: Final = "delete"
    TAB: Final = "tab"
    INTERRUPT: Final = "interrupt"


_BY_VIRTUAL_KEY: Final = {
    0x08: Key.BACKSPACE,
    0x09: Key.TAB,
    0x0D: Key.ENTER,
    0x1B: Key.ESCAPE,
    0x21: Key.PAGE_UP,
    0x22: Key.PAGE_DOWN,
    0x23: Key.END,
    0x24: Key.HOME,
    0x25: Key.LEFT,
    0x26: Key.UP,
    0x27: Key.RIGHT,
    0x28: Key.DOWN,
    0x2E: Key.DELETE,
}
_BY_CHARACTER: Final = {
    "\x08": Key.BACKSPACE,
    "\x7f": Key.BACKSPACE,
    "\r": Key.ENTER,
    "\n": Key.ENTER,
    "\x1b": Key.ESCAPE,
    "\t": Key.TAB,
    "\x03": Key.INTERRUPT,
}
_BY_ESCAPE_FINAL: Final = {
    "A": 0x26, "B": 0x28, "C": 0x27, "D": 0x25,
    "H": 0x24, "F": 0x23, "Z": 0x09,
}
_BY_TILDE: Final = {1: 0x24, 3: 0x2E, 4: 0x23, 5: 0x21, 6: 0x22, 7: 0x24, 8: 0x23}

RIGHT_MARGIN_VARIABLE: Final = "KAMADO_TUI_MARGIN"
DEFAULT_RIGHT_MARGIN: Final = 0
"""Terminal.app and iTerm reserve scrollbar space outside the cell grid."""


def _wrap_free_mode(previous: int) -> int:
    """Keep the old numeric helper for callers; POSIX uses AUTOWRAP_OFF instead."""
    return (previous | 0x0004 | 0x0001) & ~0x0002


def right_margin() -> int:
    raw = os.environ.get(RIGHT_MARGIN_VARIABLE)
    if raw is None:
        return DEFAULT_RIGHT_MARGIN
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return DEFAULT_RIGHT_MARGIN


def _visible_size() -> Optional[tuple[int, int]]:
    """Read the actual cell grid, not environment overrides or scrollback."""
    for stream in (sys.stdout, sys.stdin):
        try:
            packed = fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        except (AttributeError, OSError, ValueError):
            continue  # Redirected streams need the library fallback.
        rows, columns, _xpixels, _ypixels = struct.unpack("HHHH", packed)
        if columns > 0 and rows > 0:
            return columns, rows
    return None


def _write(text: str) -> None:
    """One UTF-8 write, independent of the locale's stdout text encoding."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8", "replace"))
        buffer.flush()
    else:
        # Text-only capture streams (e.g. StringIO) have no byte encoding.
        sys.stdout.write(text)
        sys.stdout.flush()


class Console:
    """Owns the terminal while the interface is running."""

    def __init__(self) -> None:
        self._mode: Optional[list] = None
        self._input_fd: Optional[int] = None
        self._entered = False
        self._input = bytearray()
        self._escape_at: Optional[float] = None
        self._pending_surrogate = ""

    @property
    def size(self) -> tuple[int, int]:
        """Current visible ``(columns, rows)``, retaining the layout minimums."""
        visible = _visible_size()
        if visible is None:
            fallback = shutil.get_terminal_size(fallback=(100, 30))
            visible = (fallback.columns, fallback.lines)
        return max(20, visible[0] - right_margin()), max(8, visible[1])

    def __enter__(self) -> "Console":
        try:
            if sys.stdin.isatty():
                self._input_fd = sys.stdin.fileno()
                self._mode = termios.tcgetattr(self._input_fd)
                # TCSANOW preserves input already queued, including a paste.
                tty.setraw(self._input_fd, termios.TCSANOW)
            self._entered = True
            _write(_SAVE_AUTOWRAP + ALT_SCREEN_ON + HIDE_CURSOR + AUTOWRAP_OFF)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exception: object) -> None:
        try:
            if self._entered:
                # ON is also a fallback for terminals without DEC mode saving.
                _write(_RESET + AUTOWRAP_ON + SHOW_CURSOR + ALT_SCREEN_OFF + _RESTORE_AUTOWRAP)
        finally:
            self._entered = False
            if self._mode is not None and self._input_fd is not None:
                termios.tcsetattr(self._input_fd, termios.TCSANOW, self._mode)
                # macOS sets PENDIN when ICANON returns. A readiness check
                # reprocesses queued input and clears it without consuming it.
                select.select([self._input_fd], [], [], 0.0)
                self._mode = None
            self._input_fd = None

    def draw(self, lines: list[str], cursor: Optional[tuple[int, int]] = None) -> None:
        """Cut and pad every row, then write the frame once and park the caret.

        Cursor coordinates are zero-based. The minimum layout size is kept
        for callers, but painting is clipped to the physical window even if
        it shrinks below that minimum. There is no erase after a full row:
        with autowrap off the caret still occupies its last painted cell.
        """
        columns, rows = self.size
        physical = _visible_size()
        if physical is not None:
            columns = min(columns, max(1, physical[0] - right_margin()))
            rows = min(rows, physical[1])
        frame = [HOME, _RESET]
        for row in range(rows):
            body = lines[row] if row < len(lines) else ""
            body = body.replace("\r", " ").replace("\n", " ").replace("\t", " ")
            body = pad_to_width(truncate_to_width(body, columns), columns)
            frame.append(body + _RESET)
            if physical is not None and columns < physical[0]:
                frame.append(CLEAR_LINE)  # Clear only the unused margin.
            if row < rows - 1:
                frame.append("\r\n")
        if cursor is None:
            frame.append(HIDE_CURSOR)
        else:
            row, column = cursor
            row = max(0, min(rows - 1, row))
            column = max(0, min(columns - 1, column))
            frame.append(f"\x1b[{row + 1};{column + 1}H" + SHOW_CURSOR)
        _write("".join(frame))

    def _events(self, timeout: float) -> Iterator[KeyEvent]:
        """Adapt terminal bytes to normalized events within one deadline."""
        deadline = time.monotonic() + max(0.0, timeout)
        fd = self._input_fd if self._input_fd is not None else sys.stdin.fileno()
        while True:
            event = self._take_event()
            if event is not None:
                self._escape_at = None
                yield event
                if time.monotonic() >= deadline:
                    return
                continue

            now = time.monotonic()
            wait = max(0.0, deadline - now)
            if self._input.startswith(b"\x1b"):
                if self._escape_at is None:
                    self._escape_at = now
                remaining = _ESCAPE_WAIT - (now - self._escape_at)
                if remaining <= 0:
                    del self._input[:1]
                    self._escape_at = None
                    yield KeyEvent(True, 0, "\x1b")
                    return
                wait = min(wait, remaining)
            else:
                self._escape_at = None
            if not select.select([fd], [], [], wait)[0]:
                if time.monotonic() >= deadline:
                    return
                continue
            data = os.read(fd, 4096)
            if not data:
                return
            self._input.extend(data)

    def _take_event(self) -> Optional[KeyEvent]:
        """Consume a complete sequence or character; keep partial input intact."""
        if not self._input:
            return None
        first = self._input[0]
        if first == 0x1B:
            if len(self._input) == 1:
                return None
            if self._input[1] not in (ord("["), ord("O")):
                del self._input[:1]
                return KeyEvent(True, 0, "\x1b")
            for index in range(2, len(self._input)):
                if 0x40 <= self._input[index] <= 0x7E:
                    sequence = bytes(self._input[2:index + 1])
                    del self._input[:index + 1]
                    return self._escape_event(sequence)
            if len(self._input) >= 64:
                self._input.clear()
                return KeyEvent(False, 0, "")
            return None

        length = 1
        if 0xC2 <= first <= 0xDF:
            length = 2
        elif 0xE0 <= first <= 0xEF:
            length = 3
        elif 0xF0 <= first <= 0xF4:
            length = 4
        try:
            character = bytes(self._input[:length]).decode("utf-8")
        except UnicodeDecodeError as error:
            if error.reason == "unexpected end of data":
                return None
            # Invalid external input is replaced, never allowed to wedge reads.
            character, length = "\ufffd", 1
        del self._input[:length]
        return KeyEvent(True, 0, character)

    def _escape_event(self, sequence: bytes) -> KeyEvent:
        """Decode CSI/SS3 navigation and CSI-u/modifyOtherKeys modified keys.

        Shift+Enter is distinguishable only when the terminal sends a modified
        key sequence; a terminal sending ordinary CR cannot expose Shift.
        Unknown terminal reports and function keys are consumed, not typed.
        """
        ignored = KeyEvent(False, 0, "")
        try:
            text = sequence.decode("ascii")
            final = text[-1]
            fields = text[:-1].split(";") if text[:-1] else []
            # CSI-u can carry an event type: 3 denotes key release.
            if len(fields) > 1 and fields[1].endswith(":3"):
                return ignored
            numbers = [int(field.split(":")[0]) if field else 1 for field in fields]
        except (UnicodeDecodeError, ValueError):
            return ignored
        modifier = numbers[1] if len(numbers) > 1 else 1
        control = _SHIFT_PRESSED if (modifier - 1) & 1 else 0
        if final in _BY_ESCAPE_FINAL:
            return KeyEvent(True, _BY_ESCAPE_FINAL[final], "", control)
        if final == "~" and numbers:
            if len(numbers) == 3 and numbers[0] == 27:
                codepoint = numbers[2]  # xterm modifyOtherKeys
            else:
                return KeyEvent(True, _BY_TILDE.get(numbers[0], 0), "", control)
        elif final == "u" and numbers:
            codepoint = numbers[0]
        else:
            return ignored
        if not 0 <= codepoint <= 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            return ignored
        character = chr(codepoint)
        if (modifier - 1) & 4 and "a" <= character.lower() <= "z":
            character = chr(ord(character.lower()) - ord("a") + 1)
        return KeyEvent(True, 0, character, control)

    def _pending(self) -> bool:
        """Check both read-ahead and the descriptor, without waiting for input."""
        if self._input:
            return True
        fd = self._input_fd if self._input_fd is not None else sys.stdin.fileno()
        if select.select([fd], [], [], 0.0)[0]:
            self._input.extend(os.read(fd, 4096))
        return bool(self._input)  # EOF is readable, but is not another key.

    def read_key(self, timeout: float = 0.0) -> tuple[str, str]:
        """Return ``(Key name, '')``, ``('char', text)``, or ``('', '')`` on timeout."""
        for event in self._events(timeout):
            if not event.key_down:
                continue
            decoded = self._decode(event)
            if decoded is not None:
                return decoded
        return "", ""

    def _decode(self, event: KeyEvent) -> Optional[tuple[str, str]]:
        """Turn one normalized key press into the application's input contract."""
        character = event.character
        if character:
            named = _BY_CHARACTER.get(character)
            if named is not None:
                self._pending_surrogate = ""
                if named == Key.ENTER and self._is_newline(event):
                    return "char", "\n"
                return named, ""
            # Retained for KeyEvent callers; terminal UTF-8 has whole codepoints.
            if ord(character) in _HIGH_SURROGATE:
                self._pending_surrogate = character
                return None
            if ord(character) in _LOW_SURROGATE:
                if not self._pending_surrogate:
                    return None
                pair = self._pending_surrogate + character
                self._pending_surrogate = ""
                return "char", pair.encode("utf-16", "surrogatepass").decode("utf-16")
            self._pending_surrogate = ""
            if character.isprintable() or "\x01" <= character <= "\x1a":
                return "char", character
            return None
        named = _BY_VIRTUAL_KEY.get(event.virtual_key)
        if named == Key.ENTER and self._is_newline(event):
            return "char", "\n"
        return (named, "") if named is not None else None

    def _is_newline(self, event: KeyEvent) -> bool:
        """Shift+Enter or an Enter with already-queued input inserts a newline."""
        return bool(event.control_state & _SHIFT_PRESSED) or self._pending()


def supports_tui() -> bool:
    """Whether this process has an interactive POSIX terminal."""
    return os.name == "posix" and sys.stdin.isatty() and sys.stdout.isatty()


def terminal_size() -> tuple[int, int]:
    columns, rows = _visible_size() or shutil.get_terminal_size(fallback=(100, 30))
    return max(20, columns), max(8, rows)


def force_size() -> Optional[tuple[int, int]]:
    """Honour an explicit size, which is how the UI is captured for review."""
    raw = os.environ.get("KAMADO_TUI_SIZE")
    if not raw or "x" not in raw:
        return None
    columns, _, rows = raw.partition("x")
    try:
        return max(20, int(columns)), max(8, int(rows))
    except ValueError:
        return None
