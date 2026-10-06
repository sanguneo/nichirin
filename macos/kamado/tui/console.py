"""POSIX terminal ownership, whole-frame ANSI output, and UTF-8 key input.

The input descriptor stays in raw mode only inside the context manager. Reads
use select with a single deadline, retaining incomplete UTF-8 and escape
sequences for the next call instead of blocking the application's refresh.
"""

from __future__ import annotations

import fcntl
import os
import re
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
MOUSE_VARIABLE: Final = "KAMADO_MOUSE"
"""Set to ``0`` to start with the mouse left to the terminal."""
MOUSE_ON: Final = "\x1b[?1000h\x1b[?1006h"
"""Report button presses in SGR form, which is the only encoding wide enough.

The original X10 encoding packs a column into one byte, so it cannot address a
column past 223 -- and a photo can sit anywhere on a wide screen.  Motion is
deliberately not requested: nothing here reacts to the pointer moving.
"""
MOUSE_OFF: Final = "\x1b[?1006l\x1b[?1000l"
AUTOWRAP_OFF: Final = "\x1b[?7l"
AUTOWRAP_ON: Final = "\x1b[?7h"
HOME: Final = "\x1b[H"
CLEAR_BELOW: Final = "\x1b[J"
CLEAR_LINE: Final = "\x1b[K"
_RESET: Final = "\x1b[0m"
_SAVE_AUTOWRAP: Final = "\x1b[?7s"
_RESTORE_AUTOWRAP: Final = "\x1b[?7r"
_ESCAPE_WAIT: Final = 0.12
"""Allow a split terminal key sequence to arrive before yielding Escape."""
_CELL_QUERY_SECONDS: Final = 0.05
"""How long to wait for a cell-size reply before giving up on the question."""
_SHIFT_PRESSED: Final = 0x0010
CLEAR_BOX: Final = "\x15"
"""Ctrl+U, which empties the composer; it is passed through as itself."""
SHOW_PHOTO: Final = "\x10"
"""Ctrl+P, which opens the newest picture in the focused conversation.

The composer holds the keyboard for as long as a room is open, so an ordinary
letter cannot carry a shortcut -- it would simply be typed. A control character
can, and this is the key the Windows branch settled on, where there is no click
to offer instead.
"""
TOGGLE_TIMESTAMP: Final = "\x14"
"""Ctrl+T, which toggles timestamp visibility in the chat transcript."""
TOGGLE_STATUS: Final = "\x13"
"""Ctrl+S, which toggles bottom status line visibility in the chat screen."""
SELECT_MODE: Final = "\x07"
"""Ctrl+G, which hands the mouse to the terminal and takes it back.

While buttons are reported the terminal gives every press to this program, so
its own drag-to-select never starts and there is nothing for Cmd+C to copy.
The two uses of the mouse cannot both be on, so this key switches between them
rather than trying to tell a click from a drag -- which the button reports
this console asks for cannot answer, since motion is never requested.
"""
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
    BACKTAB: Final = "backtab"
    """Shift+Tab, which walks a list the other way."""
    INTERRUPT: Final = "interrupt"
    CLICK: Final = "click"
    """A left-button press, carrying ``"row,column"`` as its text."""
    RELEASE: Final = "release"
    """A left-button release, carrying ``"row,column"`` as its text."""
    WHEEL_UP: Final = "wheelup"
    WHEEL_DOWN: Final = "wheeldown"
    """One wheel notch.

    Asking the terminal to report buttons takes its own wheel scrolling away,
    so these have to reach the interface or a photo-sized screen has no way to
    be scrolled with the mouse at all.
    """


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
_LEFT_BUTTON: Final = 0
"""The button field of a left-button SGR mouse report."""
_WHEEL_UP_BUTTON: Final = 64
_WHEEL_DOWN_BUTTON: Final = 65
_MODIFIER_BITS: Final = 0b11100
"""Shift, meta and control ride in the button field and are ignored here."""

RIGHT_MARGIN_VARIABLE: Final = "KAMADO_TUI_MARGIN"
DEFAULT_RIGHT_MARGIN: Final = 0
"""Terminal.app and iTerm reserve scrollbar space outside the cell grid."""


def _wrap_free_mode(previous: int) -> int:
    """Keep the old numeric helper for callers; POSIX uses AUTOWRAP_OFF instead."""
    return (previous | 0x0004 | 0x0001) & ~0x0002


def mouse_default() -> bool:
    """Whether to ask for button reports at startup; ``KAMADO_MOUSE=0`` says no."""
    raw = os.environ.get(MOUSE_VARIABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "off", "false", "no")


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
        self._painted: Optional[list[str]] = None
        """The rows currently on screen, so a redraw can write only the rest."""
        self._painted_cursor: Optional[tuple[int, int]] = None
        self._painted_shape: Optional[tuple[int, int, str]] = None
        """What those rows were cut to; a different screen invalidates them."""
        self._mouse = False
        """Whether the terminal is currently reporting buttons to this program."""

    @property
    def mouse(self) -> bool:
        """Whether clicks reach this program instead of the terminal's selection."""
        return self._mouse

    def set_mouse(self, enabled: bool) -> None:
        """Ask for button reports, or give the mouse back to the terminal.

        A request for the state the terminal is already in writes nothing: the
        loop calls this every frame, and a write while a selection is up would
        be one more thing that could clear it.
        """
        if not self._entered or enabled == self._mouse:
            return
        self._mouse = enabled
        _write(MOUSE_ON if enabled else MOUSE_OFF)

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
                descriptor = sys.stdin.fileno()
                self._input_fd = descriptor
                self._mode = termios.tcgetattr(descriptor)
                # TCSANOW preserves input already queued, including a paste.
                tty.setraw(descriptor, termios.TCSANOW)
            self._entered = True
            # The alternate screen starts blank, so the first frame is whole.
            self._painted = None
            self._mouse = mouse_default()
            _write(
                _SAVE_AUTOWRAP
                + ALT_SCREEN_ON
                + HIDE_CURSOR
                + AUTOWRAP_OFF
                + (MOUSE_ON if self._mouse else "")
            )
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exception: object) -> None:
        try:
            if self._entered:
                # ON is also a fallback for terminals without DEC mode saving.
                _write(
                    MOUSE_OFF
                    + _RESET
                    + AUTOWRAP_ON
                    + SHOW_CURSOR
                    + ALT_SCREEN_OFF
                    + _RESTORE_AUTOWRAP
                )
        finally:
            self._entered = False
            self._mouse = False
            if self._mode is not None and self._input_fd is not None:
                termios.tcsetattr(self._input_fd, termios.TCSANOW, self._mode)
                # macOS sets PENDIN when ICANON returns. A readiness check
                # reprocesses queued input and clears it without consuming it.
                select.select([self._input_fd], [], [], 0.0)
                self._mode = None
            self._input_fd = None

    def forget(self) -> None:
        """Discard the remembered frame so the next draw repaints every row.

        Kitty keeps a picture outside the cell grid and takes it back by id, but
        sixel and iTerm2 put pixels *into* the cells the transcript left blank --
        and a blank row that is still blank is exactly the row :meth:`draw`
        skips, which would leave the picture behind after it moved. Giving up
        the remembered frame is what clears it.
        """
        self._painted = None

    def draw(
        self,
        lines: list[str],
        cursor: Optional[tuple[int, int]] = None,
        overlay: str = "",
    ) -> None:
        """Cut and pad every row, write what changed, and park the caret.

        Cursor coordinates are zero-based. The minimum layout size is kept
        for callers, but painting is clipped to the physical window even if
        it shrinks below that minimum. There is no erase after a full row:
        with autowrap off the caret still occupies its last painted cell.

        Only rows that differ from what this console last painted are written,
        and a frame the screen already holds writes nothing at all. The
        interface redraws unconditionally — after every keystroke and again
        each time the key wait expires — so a whole-screen frame meant 12 KB
        at 200x50 rewritten eight times a second, every one of them ending in
        a cursor move. A Hangul IME anchors its composition to that cursor and
        redraws it with the cells underneath, which is why typing Korean
        lagged while ASCII did not. A keystroke now writes the composer's own
        rows. What the cache describes is the alternate screen this console
        owns and nothing else writes to; a resize and a re-entry both paint in
        full again.

        *overlay* is written after the rows and before the caret is parked. It
        carries escapes that are not text -- an image placement addressed by
        screen position -- so it cannot be diffed against the painted rows and
        is emitted verbatim whenever the caller supplies one.
        """
        columns, rows = self.size
        physical = _visible_size()
        if physical is not None:
            columns = min(columns, max(1, physical[0] - right_margin()))
            rows = min(rows, physical[1])
        # Clear only the unused margin.
        margin = CLEAR_LINE if physical is not None and columns < physical[0] else ""

        painted = []
        for row in range(rows):
            body = lines[row] if row < len(lines) else ""
            body = body.replace("\r", " ").replace("\n", " ").replace("\t", " ")
            painted.append(pad_to_width(truncate_to_width(body, columns), columns))

        shape = (columns, rows, margin)
        previous = self._painted if self._painted_shape == shape else None
        if previous is None:
            frame = [HOME, _RESET]
            for row, body in enumerate(painted):
                frame.append(body + _RESET + margin)
                if row < rows - 1:
                    frame.append("\r\n")
        else:
            frame = [
                f"\x1b[{row + 1};1H" + body + _RESET + margin
                for row, body in enumerate(painted)
                if body != previous[row]
            ]
            if not frame and cursor == self._painted_cursor and not overlay:
                return
        if overlay:
            frame.append(overlay)
        if cursor is None:
            frame.append(HIDE_CURSOR)
        else:
            row, column = cursor
            row = max(0, min(rows - 1, row))
            column = max(0, min(columns - 1, column))
            frame.append(f"\x1b[{row + 1};{column + 1}H" + SHOW_CURSOR)
        _write("".join(frame))
        self._painted = painted
        self._painted_cursor = cursor
        self._painted_shape = shape

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
            if final in ("M", "m") and text[:1] == "<":
                return self._mouse_event(text)
            fields = text[:-1].split(";") if text[:-1] else []
            # CSI-u can carry an event type: 3 denotes key release.
            if len(fields) > 1 and fields[1].endswith(":3"):
                return ignored
            numbers = [int(field.split(":")[0]) if field else 1 for field in fields]
        except (UnicodeDecodeError, ValueError):
            return ignored
        modifier = numbers[1] if len(numbers) > 1 else 1
        control = _SHIFT_PRESSED if (modifier - 1) & 1 else 0
        if final == "Z":
            # CBT is what a terminal sends for Shift+Tab, and the shift is
            # the sequence itself: it carries no modifier field to read.
            control |= _SHIFT_PRESSED
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

    def _mouse_event(self, text: str) -> KeyEvent:
        """Decode one SGR mouse report into a click, a wheel notch, or nothing.

        Only the left button going *down* becomes a click.  The release that
        follows every press would otherwise toggle a photo straight back.  A
        wheel notch has no release and is taken on its own report.  Coordinates
        are one-based on the wire and zero-based everywhere in this program, so
        they are converted here rather than at each caller.
        """
        ignored = KeyEvent(False, 0, "")
        try:
            button, column, row = (int(field) for field in text[1:-1].split(";"))
        except ValueError:
            return ignored
        bare = button & ~_MODIFIER_BITS
        if bare == _WHEEL_UP_BUTTON:
            return KeyEvent(True, 0, "\x00wheel:up")
        if bare == _WHEEL_DOWN_BUTTON:
            return KeyEvent(True, 0, "\x00wheel:down")
        if bare != _LEFT_BUTTON:
            return ignored
        if text[-1] == "m":
            return KeyEvent(True, 0, f"\x00release:{row - 1},{column - 1}")
        if text[-1] != "M":
            return ignored
        return KeyEvent(True, 0, f"\x00click:{row - 1},{column - 1}")

    def cell_pixels(self) -> Optional[tuple[int, int]]:
        """Ask the terminal how large one cell is, in device pixels.

        ``CSI 16 t`` is answered with ``CSI 6 ; height ; width t`` by terminals
        that support it.  The reply is read with a short deadline and anything
        else is put back: a terminal that does not answer must not be waited on,
        and a keystroke that arrives first is still the reader's.
        """
        if self._input_fd is None:
            return None
        _write("\x1b[16t")
        deadline = time.monotonic() + _CELL_QUERY_SECONDS
        collected = bytearray()
        pattern = re.compile(rb"\x1b\[6;(\d+);(\d+)t")
        while True:
            found = pattern.search(bytes(collected))
            if found is not None:
                del collected[found.start() : found.end()]
                self._input[:0] = collected
                height, width = int(found.group(1)), int(found.group(2))
                return (width, height) if width > 0 and height > 0 else None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self._input_fd], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(self._input_fd, 64)
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
        self._input[:0] = collected
        return None

    def supports_sixel(self) -> bool:
        """Ask the terminal whether it draws sixel, and believe only a yes.

        Nothing in the environment announces sixel: a terminal reached over SSH
        forwards ``TERM`` and nothing else, so Windows Terminal -- which does
        draw sixel -- looks exactly like a terminal that draws nothing. The
        primary device-attributes request (``CSI c``) is answered with
        ``CSI ? 62 ; ... c``, and attribute ``4`` in that list means sixel.

        Read on the same short deadline as :meth:`cell_pixels`, and for the same
        reason: a terminal that stays silent must not hold up the conversation,
        and a keystroke that arrives first is still the reader's.
        """
        if self._input_fd is None:
            return False
        _write("\x1b[c")
        deadline = time.monotonic() + _CELL_QUERY_SECONDS
        collected = bytearray()
        pattern = re.compile(rb"\x1b\[\?([0-9;]*)c")
        while True:
            found = pattern.search(bytes(collected))
            if found is not None:
                del collected[found.start() : found.end()]
                self._input[:0] = collected
                return b"4" in found.group(1).split(b";")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self._input_fd], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(self._input_fd, 64)
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
        self._input[:0] = collected
        return False

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
            if character.startswith("\x00click:"):
                self._pending_surrogate = ""
                return Key.CLICK, character[len("\x00click:"):]
            if character.startswith("\x00release:"):
                self._pending_surrogate = ""
                return Key.RELEASE, character[len("\x00release:"):]
            if character == "\x00wheel:up":
                self._pending_surrogate = ""
                return Key.WHEEL_UP, ""
            if character == "\x00wheel:down":
                self._pending_surrogate = ""
                return Key.WHEEL_DOWN, ""
            named = _BY_CHARACTER.get(character)
            if named is not None:
                self._pending_surrogate = ""
                if named == Key.ENTER and self._is_newline(event):
                    return "char", "\n"
                return self._modified(named, event)
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
        return self._modified(named, event) if named is not None else None

    @staticmethod
    def _modified(named: str, event: KeyEvent) -> tuple[str, str]:
        """Name the key a modifier turns a plain one into.

        Shift+Tab is a key of its own rather than Tab wearing a flag: a
        terminal reports the backward walk as CBT, or as a shifted Tab where
        a modified-key protocol is on, and either way the interface has two
        directions to walk. Reading it as Tab would make one of them dead.
        """
        if named == Key.TAB and event.control_state & _SHIFT_PRESSED:
            return Key.BACKTAB, ""
        return named, ""

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
