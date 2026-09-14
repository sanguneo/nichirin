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
from typing import Final, Optional

_IDLE_TICK: Final = 0.01
"""How long to idle between keyboard checks; short enough to feel immediate."""

_ENABLE_VIRTUAL_TERMINAL_PROCESSING: Final = 0x0004
_ENABLE_PROCESSED_OUTPUT: Final = 0x0001
_STD_OUTPUT: Final = -11
_UTF8: Final = 65001

ALT_SCREEN_ON: Final = "\x1b[?1049h"
ALT_SCREEN_OFF: Final = "\x1b[?1049l"
HIDE_CURSOR: Final = "\x1b[?25l"
SHOW_CURSOR: Final = "\x1b[?25h"
HOME: Final = "\x1b[H"
CLEAR_BELOW: Final = "\x1b[J"
CLEAR_LINE: Final = "\x1b[K"


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
    TAB: Final = "tab"
    INTERRUPT: Final = "interrupt"


_SPECIAL: Final = {
    "H": Key.UP,
    "P": Key.DOWN,
    "K": Key.LEFT,
    "M": Key.RIGHT,
    "I": Key.PAGE_UP,
    "Q": Key.PAGE_DOWN,
    "G": Key.HOME,
    "O": Key.END,
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
    wanted = previous.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING | _ENABLE_PROCESSED_OUTPUT
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


class Console:
    """Owns the terminal while the interface is running."""

    def __init__(self) -> None:
        self._mode: Optional[int] = None
        self._codepage: Optional[int] = None
        self._entered = False

    @property
    def size(self) -> tuple[int, int]:
        """Current ``(columns, rows)``, with a usable fallback when unknown."""
        size = shutil.get_terminal_size(fallback=(100, 30))
        return max(20, size.columns), max(8, size.lines)

    def __enter__(self) -> "Console":
        self._codepage = _set_utf8()
        self._mode = _enable_vt()
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR)
        sys.stdout.flush()
        self._entered = True
        return self

    def __exit__(self, *_exception: object) -> None:
        if self._entered:
            sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
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

    def read_key(self, timeout: float = 0.0) -> tuple[str, str]:
        """Wait up to *timeout* seconds for a key, returning ``(kind, text)``.

        *kind* is a name from :class:`Key` for anything special, ``"char"``
        when *text* holds a printable character, and empty when the wait
        expired. Waiting with a deadline rather than blocking is what lets the
        loop notice messages arriving while nobody is typing.
        """
        import msvcrt

        deadline = time.monotonic() + max(0.0, timeout)
        while not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return "", ""
            time.sleep(_IDLE_TICK)

        character = msvcrt.getwch()
        if character in ("\x00", "\xe0"):
            return _SPECIAL.get(msvcrt.getwch(), ""), ""
        if character == "\r":
            return Key.ENTER, ""
        if character == "\x1b":
            return Key.ESCAPE, ""
        if character in ("\x08", "\x7f"):
            return Key.BACKSPACE, ""
        if character == "\t":
            return Key.TAB, ""
        if character == "\x03":
            return Key.INTERRUPT, ""
        if character.isprintable() or character == " ":
            return "char", character
        return "", ""


def supports_tui() -> bool:
    """Whether this process is attached to a console that can host the UI."""
    return sys.platform == "win32" and sys.stdin.isatty() and sys.stdout.isatty()


def terminal_size() -> tuple[int, int]:
    columns, rows = shutil.get_terminal_size(fallback=(100, 30))
    return max(20, columns), max(8, rows)


def force_size() -> Optional[tuple[int, int]]:
    """Honour an explicit size, which is how the UI is captured for review."""
    raw = os.environ.get("KAKAOREAD_TUI_SIZE")
    if not raw or "x" not in raw:
        return None
    columns, _, rows = raw.partition("x")
    try:
        return max(20, int(columns)), max(8, int(rows))
    except ValueError:
        return None
