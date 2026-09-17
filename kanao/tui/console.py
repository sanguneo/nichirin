"""Windows console setup, frame output, and key input.

Three things have to be arranged before anything can be drawn on a Korean
Windows box: the output code page must be UTF-8 or every Hangul character is
replaced, virtual terminal processing must be on or the escape sequences are
printed literally, and the alternate screen buffer must be used so quitting
returns the user's scrollback untouched.

Frames are composed in full and written with a single call. Drawing line by
line is what makes a terminal UI flicker.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Final, Iterator, Optional

from .layout import pad_to_width, truncate_to_width

_IDLE_TICK: Final = 0.01
"""How long to idle between keyboard checks; short enough to feel immediate."""

_ENABLE_VIRTUAL_TERMINAL_PROCESSING: Final = 0x0004
_ENABLE_PROCESSED_OUTPUT: Final = 0x0001
_ENABLE_WRAP_AT_EOL_OUTPUT: Final = 0x0002


def _wrap_free_mode(previous: int) -> int:
    """Return *previous* with escape handling on and end-of-line wrapping off.

    The escape for it is not enough: \x1b[?7l is accepted and ignored here,
    and a 140-column line still wrapped onto a 120-column screen. Wrapping is
    what turns one over-wide line into a frame that drifts a row at a time
    and keeps fragments of the last screen until the interface restarts.
    """
    wanted = previous | _ENABLE_VIRTUAL_TERMINAL_PROCESSING | _ENABLE_PROCESSED_OUTPUT
    return wanted & ~_ENABLE_WRAP_AT_EOL_OUTPUT
_STD_OUTPUT: Final = -11
_UTF8: Final = 65001

ALT_SCREEN_ON: Final = "\x1b[?1049h"
ALT_SCREEN_OFF: Final = "\x1b[?1049l"
HIDE_CURSOR: Final = "\x1b[?25l"
AUTOWRAP_OFF: Final = "\x1b[?7l"
"""Stop the terminal wrapping a frame that is exactly as wide as the screen.

Writing the last cell of such a line wraps the cursor, and the frame's own
newline then lands a row late: every box below drifts sideways and its right
border falls off the screen."""
AUTOWRAP_ON: Final = "\x1b[?7h"
SHOW_CURSOR: Final = "\x1b[?25h"
HOME: Final = "\x1b[H"
CLEAR_BELOW: Final = "\x1b[J"
CLEAR_LINE: Final = "\x1b[K"


_STD_INPUT: Final = -10
_KEY_EVENT: Final = 0x0001
_PEEK_DEPTH: Final = 16
_WAIT_SIGNALLED: Final = 0x0000

_SHIFT_PRESSED: Final = 0x0010
CLEAR_BOX: Final = "\x15"
"""Ctrl+U, which empties the composer; it is passed through as itself."""

_HIGH_SURROGATE: Final = range(0xD800, 0xDC00)
_LOW_SURROGATE: Final = range(0xDC00, 0xE000)


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One console input record, reduced to what a keystroke needs."""

    key_down: bool
    virtual_key: int
    character: str
    control_state: int = 0
    """Which modifiers were down, so shift+enter is not a plain enter."""


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


def _enable_vt() -> Optional[int]:
    """Turn on escape-sequence handling, returning the mode to restore."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    handle = kernel32.GetStdHandle(_STD_OUTPUT)
    if handle in (0, None) or handle == ctypes.c_void_p(-1).value:
        return None
    previous = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(previous)):
        return None
    wanted = _wrap_free_mode(previous.value)
    if not kernel32.SetConsoleMode(handle, wanted):
        return None
    return previous.value


def _restore_vt(mode: Optional[int]) -> None:
    if mode is None:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(_STD_OUTPUT)
        kernel32.SetConsoleMode(handle, mode)
    except (AttributeError, OSError):
        pass


def _set_utf8() -> Optional[int]:
    """Switch the console to UTF-8 so Hangul survives, returning the old page."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        previous = int(kernel32.GetConsoleOutputCP())
        kernel32.SetConsoleOutputCP(_UTF8)
        kernel32.SetConsoleCP(_UTF8)
    except (AttributeError, OSError, ValueError):
        return None
    return previous


def _restore_codepage(page: Optional[int]) -> None:
    if page is None:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleOutputCP(page)
        kernel32.SetConsoleCP(page)
    except (AttributeError, OSError):
        pass


class _CHARACTER(ctypes.Union):
    _fields_ = [("UnicodeChar", ctypes.c_wchar), ("AsciiChar", ctypes.c_char)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _CHARACTER),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EVENT(ctypes.Union):
    # Sized for the largest record the console can queue, not just a key.
    _fields_ = [("KeyEvent", _KEY_EVENT_RECORD), ("padding", ctypes.c_byte * 16)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


class _ScreenInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes._COORD),
        ("cursor", wintypes._COORD),
        ("attributes", wintypes.WORD),
        ("window", wintypes.SMALL_RECT),
        ("maximum", wintypes._COORD),
    ]


RIGHT_MARGIN_VARIABLE: Final = "KANAO_TUI_MARGIN"
DEFAULT_RIGHT_MARGIN: Final = 1
"""Columns left unused at the right edge.

Windows Terminal paints its scrollbar over the last cells rather than
shrinking the area it reports, so a frame drawn to the full reported width
disappears under it. One column was enough in practice; set the variable to 0
on a terminal that needs none, or higher on one that hides more.
"""


def right_margin() -> int:
    raw = os.environ.get(RIGHT_MARGIN_VARIABLE)
    if raw is None:
        return DEFAULT_RIGHT_MARGIN
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return DEFAULT_RIGHT_MARGIN


def _visible_size() -> Optional[tuple[int, int]]:
    """Return the columns and rows actually on screen, if they can be read.

    The buffer is usually wider than the window showing it — a scrollbar alone
    takes a couple of columns — and :func:`shutil.get_terminal_size` reports the
    buffer. Drawing to that width puts the right edge of every frame past the
    edge of the screen.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(_STD_OUTPUT)
        info = _ScreenInfo()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None
    except (AttributeError, OSError, ValueError):
        return None
    columns = int(info.window.Right) - int(info.window.Left) + 1
    rows = int(info.window.Bottom) - int(info.window.Top) + 1
    if columns <= 0 or rows <= 0:
        return None
    return columns, rows


class Console:
    """Owns the terminal while the interface is running."""

    def __init__(self) -> None:
        self._mode: Optional[int] = None
        self._codepage: Optional[int] = None
        self._entered = False
        self._kernel32: Optional[ctypes.WinDLL] = None
        self._pending_surrogate = ""
        """Half an astral character; a console delivers the two halves apart."""

    @property
    def size(self) -> tuple[int, int]:
        """Current visible ``(columns, rows)``, with a fallback when unknown."""
        visible = _visible_size()
        if visible is None:
            fallback = shutil.get_terminal_size(fallback=(100, 30))
            visible = (fallback.columns, fallback.lines)
        return max(20, visible[0] - right_margin()), max(8, visible[1])

    def __enter__(self) -> "Console":
        self._codepage = _set_utf8()
        self._mode = _enable_vt()
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR + AUTOWRAP_OFF)
        sys.stdout.flush()
        self._entered = True
        return self

    def __exit__(self, *_exception: object) -> None:
        if self._entered:
            sys.stdout.write(AUTOWRAP_ON + SHOW_CURSOR + ALT_SCREEN_OFF)
            sys.stdout.flush()
            self._entered = False
        _restore_vt(self._mode)
        _restore_codepage(self._codepage)

    def draw(self, lines: list[str], cursor: Optional[tuple[int, int]] = None) -> None:
        """Paint one whole frame in a single write.

        *cursor* is where the caret is parked afterwards. It matters more than
        it looks: a Korean IME draws its composition at the terminal caret, so
        leaving the caret wherever the frame happened to end puts half-typed
        Hangul next to the footer instead of in the message box.
        """
        columns, rows = self.size
        frame = [HOME]
        for row in range(rows):
            body = lines[row] if row < len(lines) else ""
            # Cut to the screen and filled out to it. A line wider than the
            # console wraps, and every row after it lands a row late and at
            # the wrong column; a line narrower than it leaves whatever the
            # last frame put there. Neither is the caller's to get right.
            body = pad_to_width(truncate_to_width(body, columns), columns)
            frame.append(body + CLEAR_LINE)
            if row < rows - 1:
                frame.append("\r\n")
        frame.append(CLEAR_BELOW)
        if cursor is None:
            frame.append(HIDE_CURSOR)
        else:
            row, column = cursor
            frame.append(f"\x1b[{row + 1};{column + 1}H" + SHOW_CURSOR)
        sys.stdout.write("".join(frame))
        sys.stdout.flush()
        del columns

    def _events(self, timeout: float) -> Iterator[KeyEvent]:
        """Yield whatever the console queued, waiting at most *timeout*.

        Reading the records directly is what keeps the loop alive. The
        previous version asked ``msvcrt.kbhit()`` whether anything was
        waiting, but a console also queues key releases, focus changes, mouse
        moves and buffer resizes; ``kbhit`` says yes to all of them and the
        ``getwch`` that followed blocks with no deadline, so one stray event
        held the last keystroke until another key was pressed.
        """
        if self._kernel32 is None:
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32 = self._kernel32
        handle = kernel32.GetStdHandle(_STD_INPUT)
        if handle in (0, None) or handle == ctypes.c_void_p(-1).value:
            return
        if kernel32.WaitForSingleObject(handle, int(max(0.0, timeout) * 1000)) != _WAIT_SIGNALLED:
            return

        record = _INPUT_RECORD()
        read = wintypes.DWORD()
        waiting = wintypes.DWORD()
        while True:
            if not kernel32.GetNumberOfConsoleInputEvents(handle, ctypes.byref(waiting)):
                return
            if waiting.value == 0:
                return
            if not kernel32.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(read)):
                return
            if read.value == 0:
                return
            if record.EventType != _KEY_EVENT:
                continue
            key = record.Event.KeyEvent
            character = key.uChar.UnicodeChar
            yield KeyEvent(
                key_down=bool(key.bKeyDown),
                virtual_key=int(key.wVirtualKeyCode),
                character="" if character in ("", "\x00") else character,
                control_state=int(key.dwControlKeyState),
            )

    def _pending(self) -> bool:
        """Whether another typed character is already waiting to be read.

        Peeked rather than counted: a console queues the release of the key
        just read, and treating that as "more is coming" would turn every
        enter into a newline. Only a key *press* carrying a character counts.
        """
        if self._kernel32 is None:
            return False
        kernel32 = self._kernel32
        handle = kernel32.GetStdHandle(_STD_INPUT)
        if handle in (0, None) or handle == ctypes.c_void_p(-1).value:
            return False
        records = (_INPUT_RECORD * _PEEK_DEPTH)()
        read = wintypes.DWORD()
        if not kernel32.PeekConsoleInputW(
            handle, ctypes.byref(records), _PEEK_DEPTH, ctypes.byref(read)
        ):
            return False
        for index in range(read.value):
            record = records[index]
            if record.EventType != _KEY_EVENT:
                continue
            key = record.Event.KeyEvent
            if key.bKeyDown and key.uChar.UnicodeChar not in ("", "\x00"):
                return True
        return False

    def read_key(self, timeout: float = 0.0) -> tuple[str, str]:
        """Wait up to *timeout* seconds for a key, returning ``(kind, text)``.

        *kind* is a name from :class:`Key` for anything special, ``"char"``
        when *text* holds a typed character, and empty when nothing arrived.
        Waiting with a deadline rather than blocking is what lets the loop
        notice messages arriving while nobody is typing.
        """
        for event in self._events(timeout):
            if not event.key_down:
                continue
            decoded = self._decode(event)
            if decoded is not None:
                return decoded
        return "", ""

    def _decode(self, event: KeyEvent) -> Optional[tuple[str, str]]:
        """Turn one key press into a keystroke, or nothing at all."""
        character = event.character
        if character:
            if character == CLEAR_BOX:
                self._pending_surrogate = ""
                return "char", character
            named = _BY_CHARACTER.get(character)
            if named is not None:
                self._pending_surrogate = ""
                if named == Key.ENTER and self._is_newline(event):
                    return "char", "\n"
                return named, ""
            if ord(character) in _HIGH_SURROGATE:
                self._pending_surrogate = character
                return None
            if ord(character) in _LOW_SURROGATE:
                if not self._pending_surrogate:
                    return None
                pair = self._pending_surrogate + character
                self._pending_surrogate = ""
                # The console hands an astral character over as two UTF-16
                # halves; joined as text they stay two unusable code points.
                whole = pair.encode("utf-16", "surrogatepass").decode("utf-16")
                return "char", whole
            self._pending_surrogate = ""
            if character.isprintable() or character == " ":
                return "char", character
            return None

        named = _BY_VIRTUAL_KEY.get(event.virtual_key)
        if named == Key.ENTER and self._is_newline(event):
            return "char", "\n"
        return (named, "") if named is not None else None

    def _is_newline(self, event: KeyEvent) -> bool:
        """Whether an enter means a line break rather than "send this".

        Two cases: the person held shift, or the enter is one character of a
        paste. A paste reaches a console as ordinary keystrokes, so the run of
        characters still queued behind it is the only thing that tells them
        apart — and getting it wrong sent the first line of a pasted message
        on its own.
        """
        return bool(event.control_state & _SHIFT_PRESSED) or self._pending()


def supports_tui() -> bool:
    """Whether this process is attached to a console that can host the UI."""
    return sys.platform == "win32" and sys.stdin.isatty() and sys.stdout.isatty()


def terminal_size() -> tuple[int, int]:
    columns, rows = shutil.get_terminal_size(fallback=(100, 30))
    return max(20, columns), max(8, rows)


def force_size() -> Optional[tuple[int, int]]:
    """Honour an explicit size, which is how the UI is captured for review."""
    raw = os.environ.get("KANAO_TUI_SIZE")
    if not raw or "x" not in raw:
        return None
    columns, _, rows = raw.partition("x")
    try:
        return max(20, int(columns)), max(8, int(rows))
    except ValueError:
        return None
