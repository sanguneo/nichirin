"""Send a message by driving KakaoTalk's own input box, no cursor, no focus.

This is the one place the tool writes rather than reads, and it does so through
the real client: a message is placed in the chat window's ``RICHEDIT50W`` input
with ``EM_REPLACESEL`` and submitted by posting Enter, both delivered to the
control's message queue directly. The mouse is never moved and focus is never
taken. WM_SETTEXT does not update this client's send-enabled state and must
not be substituted here. Queue acceptance is reported as submitted, not sent;
only a read-back of the new message can establish delivery.

The catch this design accepts: the target conversation must be open in its own
chat window. KakaoTalk titles that window with the room name, which is how the
right one is found; an unopened room has no window and the caller is told to
open it rather than being guessed at.

All Win32 access sits behind :class:`WindowBackend` so the resolution and
guardrail logic is testable without a live desktop.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, TypedDict

from .errors import ErrorCode, KakaoReadError

MAIN_WINDOW_TITLE = "카카오톡"
INPUT_CLASS = "RICHEDIT50W"
_INPUT_HINT = "메시지 입력"
ROOM_LIST_PREFIX = "ChatRoomListCtrl"

# The room list is owner-drawn: its rows are neither child windows nor UIA
# elements, so a row can only be addressed by coordinate. These were measured
# against a live client at 100% scaling.
ROW_PITCH = 68
FIRST_ROW_CENTER = 29
ROW_X = 134
MEASURED_DPI = 96
"""The scaling the geometry above was measured at."""
SEARCHED_ROWS = 8
"""How far down the list a room is looked for before giving up."""
POLL_SECONDS = 0.05
"""How often to look for the window a posted double-click produces."""
POLL_ATTEMPTS = 40
"""How long to keep looking before treating the row as the wrong one."""


def row_geometry(dpi: int) -> tuple[int, int, int]:
    """Return the row pitch, first centre and column for a screen's scaling.

    The measured numbers hold at 100% only. A laptop at 150% draws the list
    half again as tall, so a row computed from them lands on somebody else's
    conversation — and opening one marks it read, which is the cost worth
    avoiding here.
    """
    if dpi <= 0:
        dpi = MEASURED_DPI
    scale = dpi / MEASURED_DPI
    return (
        round(ROW_PITCH * scale),
        round(FIRST_ROW_CENTER * scale),
        round(ROW_X * scale),
    )


@dataclass(frozen=True, slots=True)
class ChatWindow:
    """One open KakaoTalk chat window."""

    handle: int
    title: str


class _Submission(TypedDict):
    submitted: bool
    dryRun: bool
    room: str
    length: int


class SendResult(_Submission, total=False):
    """What a send reports back, and what it only reports when it applies.

    ``clearedDraft`` and ``wouldClearDraft`` are the two halves of one fact —
    the box already held a line nobody finished — told by a real send and by a
    dry run respectively. Only one of them can be true of a given call, so
    each is optional rather than rendered as a false the caller has to read
    past. Losing somebody's half-typed line silently is the part that would be
    indefensible, so the key is present whenever it happened.
    """

    clearedDraft: bool
    wouldClearDraft: bool


class MessageSender(Protocol):
    """The one call an interface needs; :class:`Sender` satisfies it."""

    def send(
        self,
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
    ) -> SendResult:
        ...


class RoomListBackend(Protocol):
    """What opening a room needs: the list, a row click, and a way to undo it."""

    def chat_windows(self) -> list[ChatWindow]:
        """Return KakaoTalk's open chat windows (excluding the main window)."""
        ...

    def room_list(self) -> Optional[int]:
        """Return the room-list control inside the main window, if it is up."""
        ...

    def main_state(self) -> str:
        """Report the main window as "visible", "minimized", "hidden" or "missing"."""
        ...

    def show_main_window(self) -> bool:
        """Bring the main window back from the tray. True when it is up."""
        ...

    def restore_main_window(self, state: str) -> None:
        """Put the main window back the way it was found."""
        ...

    def minimize_window(self, handle: int) -> None:
        """Park a window in the taskbar, where it covers nothing."""
        ...

    def scroll_to_top(self, control: int) -> None:
        """Pin the list to its first row, so a row number means something."""
        ...

    def click_row(self, control: int, row: int) -> None:
        """Post a double-click at a row, without moving the cursor."""
        ...

    def close_window(self, handle: int) -> None:
        """Close a chat window that was opened by mistake."""
        ...


class WindowBackend(Protocol):
    """Everything the sender needs from the windowing system."""

    def chat_windows(self) -> list[ChatWindow]:
        """Return KakaoTalk's open chat windows (excluding the main window)."""
        ...

    def input_handle(self, window: int) -> Optional[int]:
        """Return the message-input control inside a chat window, if present."""
        ...

    def get_text(self, control: int) -> str:
        """Read the draft without changing it."""
        ...

    def set_text(self, control: int, text: str) -> None:
        """Replace the input's text."""
        ...

    def submit(self, control: int) -> None:
        """Post the Enter that makes KakaoTalk send what is in the input."""
        ...


def _remembered_rows() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(root) / "kakaoread" / "rows.json"


def _row_key(room: str) -> str:
    """Hash the title: where a room sits is worth keeping, its name is not."""
    return hashlib.sha256(room.encode("utf-8")).hexdigest()[:16]


def _recall_row(path: Path, room: str) -> int:
    try:
        stored = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return -1
    if not isinstance(stored, dict):
        return -1
    row = stored.get(_row_key(room))
    return row if isinstance(row, int) else -1


def _learn_row(path: Path, room: str, row: int) -> None:
    """Remember where a room was. Failing to write only costs a guess."""
    try:
        stored = json.loads(path.read_text("utf-8"))
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    stored[_row_key(room)] = row
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stored), encoding="utf-8")
    except OSError:
        pass


class RoomOpener:
    """Open a room by posting a double-click at a row, and verify what opened.

    Nothing about a row identifies the room it holds, so this cannot aim: it
    tries rows and reads the title of whatever window appears. A window that
    is not the one asked for is closed again, which is what makes a miss cost
    a flicker instead of a message in the wrong conversation.
    """

    def __init__(
        self,
        backend: "RoomListBackend",
        settle: Callable[[], None],
        rows: int = SEARCHED_ROWS,
        remembered: Optional[Path] = None,
        attempts: int = POLL_ATTEMPTS,
    ) -> None:
        self._backend = backend
        self._settle = settle
        self._rows = rows
        self._attempts = attempts
        self._remembered = remembered if remembered is not None else _remembered_rows()

    def _order(self, room: str, hint: Optional[int]) -> list[int]:
        """Rows to try, nearest guess first.

        Every row tried opens a real conversation and marks it read, so the
        order matters more than the fallback. Two things can be true of a
        room: it sits where the catalog's own recency order puts it, or it is
        pinned and therefore near the top. The guesses alternate between those
        hypotheses instead of exhausting one first — a pinned room measured
        four wrong rooms opened before the top was reached.
        """
        tried: list[int] = []

        def add(row: int) -> None:
            if 0 <= row < self._rows and row not in tried:
                tried.append(row)

        add(_recall_row(self._remembered, room))
        if hint is not None:
            for step in range(self._rows):
                add(hint + step)
                add(step)
            for step in range(1, self._rows):
                add(hint - step)
        for row in range(self._rows):
            add(row)
        return tried

    def open(
        self, room: str, aliases: Iterable[str] = (), hint: Optional[int] = None
    ) -> ChatWindow:
        """Open *room*, accepting any of *aliases* as its window title.

        The titles differ more often than it looks: the self-chat is stored
        under the account's own name while the reader shows it as 나와의 채팅,
        and a group with no custom title is named after its members by each
        side independently.
        """
        wanted = {room, *aliases}
        control = self._backend.room_list()
        disturbed: Optional[str] = None
        if control is None:
            # The main window is in the tray. Note how it was so it can be put
            # back: the point of opening a room is to not take over the screen.
            disturbed = self._backend.main_state()
            if self._backend.show_main_window():
                control = self._backend.room_list()
        if control is None:
            raise KakaoReadError(
                ErrorCode.WINDOW_NOT_FOUND,
                "KakaoTalk's room list is not on screen and the main window "
                "would not come back; restore it and try again",
            )

        known = {window.title for window in self._backend.chat_windows()}
        for row in self._order(room, hint):
            # Opening a room moves the list, so a row number only means
            # something after the list is put back at its first row.
            self._backend.scroll_to_top(control)
            self._backend.click_row(control, row)
            # Watched for rather than waited on: whatever opens is in front of
            # whatever the person was doing until it is put away, so the flat
            # wait that used to sit here was a second of someone else's
            # conversation covering their screen.
            opened: list[ChatWindow] = []
            for _attempt in range(self._attempts):
                opened = [
                    window
                    for window in self._backend.chat_windows()
                    if window.title not in known
                ]
                if opened:
                    break
                self._settle()
            for window in opened:
                if window.title in wanted:
                    _learn_row(self._remembered, room, row)
                    # Parked rather than left in front: a room this opened was
                    # not asked for by the person looking at the screen.
                    self._backend.minimize_window(window.handle)
                    if disturbed is not None:
                        self._backend.restore_main_window(disturbed)
                    return window
                # Opened by mistake: put the desktop back as it was.
                self._backend.close_window(window.handle)
        if disturbed is not None:
            self._backend.restore_main_window(disturbed)
        raise KakaoReadError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"{room!r} was not among the first {self._rows} rooms in the list; "
            "scroll it into view or open it once by hand",
        )


class Sender:
    """Resolve a room's chat window and hand a message to its input."""

    def __init__(
        self,
        backend: WindowBackend,
        running: Callable[[], bool],
        opener: Optional[RoomOpener] = None,
    ) -> None:
        self._backend = backend
        self._running = running
        self._opener = opener

    def resolve(
        self,
        room: str,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
    ) -> ChatWindow:
        """Find the one open chat window for *room*, refusing to guess.

        The caller resolves catalog substrings first. A longer window title
        must never substitute for an unopened, already-resolved room.
        """
        if not room.strip():
            raise KakaoReadError(ErrorCode.INVALID_REQUEST, "a nonempty room name is required")
        if not self._running():
            raise KakaoReadError(
                ErrorCode.KAKAO_NOT_RUNNING,
                "KakaoTalk is not running; start it and open the conversation first",
            )
        windows = self._backend.chat_windows()
        wanted = {name.strip().casefold() for name in (room, *aliases) if name.strip()}
        matches = [window for window in windows if window.title.casefold() in wanted]

        if not matches:
            if open_if_needed and self._opener is not None:
                return self._opener.open(room, aliases, hint)
            raise KakaoReadError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"No open chat window matches {room!r}; open that conversation in "
                "KakaoTalk (double-click it so it pops out) and try again",
            )
        if len(matches) > 1:
            raise KakaoReadError(
                ErrorCode.AMBIGUOUS_CHAT,
                f"{len(matches)} open chat windows match {room!r}; use a more specific name",
                candidates=[window.title for window in matches],
            )
        return matches[0]

    def send(
        self,
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
    ) -> SendResult:
        """Type *text* into *room*'s window and send it, or preview when dry.

        The message is validated and the window resolved before anything is
        typed. Queue acceptance is not proof of network delivery.
        """
        if not text or not text.strip():
            raise KakaoReadError(ErrorCode.INVALID_REQUEST, "refusing to send an empty message")
        if "\x00" in text:
            raise KakaoReadError(ErrorCode.INVALID_REQUEST, "message contains a null character")

        window = self.resolve(
            room, open_if_needed=open_if_needed, aliases=aliases, hint=hint
        )
        control = self._backend.input_handle(window.handle)
        if control is None:
            raise KakaoReadError(
                ErrorCode.INPUT_NOT_FOUND,
                f"found the window for {window.title!r} but not its message box; "
                "the client version may differ",
                room=window.title,
            )

        # An unfinished draft is cleared rather than refused. Whoever is
        # sending has already named the room and the words, and a refusal that
        # can only be resolved by walking to the client is worse than losing a
        # line nobody finished. It is reported rather than done quietly.
        existing = bool(self._backend.get_text(control))

        if dry_run:
            return {
                "submitted": False,
                "dryRun": True,
                "room": window.title,
                "length": len(text),
                "wouldClearDraft": existing,
            }

        try:
            self._backend.set_text(control, text)
            self._backend.submit(control)
        except OSError as error:
            raise KakaoReadError(
                ErrorCode.SEND_FAILED, f"could not send to {window.title!r}: {error}",
                room=window.title,
            ) from error
        return {
            "submitted": True,
            "dryRun": False,
            "room": window.title,
            "length": len(text),
            "clearedDraft": existing,
        }


# -- Win32 backend -----------------------------------------------------------

_WM_CLOSE = 0x0010
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_VK_RETURN = 0x0D
_MK_LBUTTON = 0x0001
_SW_SHOW = 5
_SW_RESTORE = 9
_SW_HIDE = 0
_SW_MINIMIZE = 6
_SW_SHOWMINNOACTIVE = 7
_WM_MOUSEWHEEL = 0x020A
_WHEEL_UP = 120
_WHEEL_NOTCHES = 8
"""Enough upward notches to pin a room list of any length to its top."""


class Win32Backend:
    """A :class:`WindowBackend` backed by user32, via ctypes only."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        from ctypes import wintypes

        self._proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        u = self._user32
        u.EnumWindows.argtypes = (self._proc, wintypes.LPARAM)
        u.EnumChildWindows.argtypes = (wintypes.HWND, self._proc, wintypes.LPARAM)
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.IsWindowVisible.argtypes = (wintypes.HWND,)
        u.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.SendMessageTimeoutW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        )
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.PostMessageW.restype = wintypes.BOOL

    def _message(self, control: int, code: int, wp: int = 0, lp: int = 0) -> int:
        """Bound synchronous cross-process messages to two seconds."""
        result = ctypes.c_size_t()
        ctypes.set_last_error(0)
        if not self._user32.SendMessageTimeoutW(
            control, code, wp, lp, 2, 2000, ctypes.byref(result)
        ):
            raise OSError(f"SendMessageTimeout failed: {ctypes.get_last_error()}")
        return result.value

    def _title(self, handle: int) -> str:
        length = self._user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def _class(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(handle, buffer, 256)
        return buffer.value

    def _pid_of(self, handle: int) -> int:
        from ctypes import wintypes

        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return int(pid.value)

    def _all_top_level(self) -> list[int]:
        """Every titled window this client owns, hidden ones included."""
        found: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if self._pid_of(handle) == self._pid and self._title(handle):
                found.append(int(handle))
            return True

        self._user32.EnumWindows(self._proc(visit), 0)
        return found

    def _top_level(self) -> list[ChatWindow]:
        """Every visible titled window this client owns, main window included."""
        found: list[ChatWindow] = []

        def visit(handle: int, _param: int) -> bool:
            if self._pid_of(handle) == self._pid and self._user32.IsWindowVisible(handle):
                title = self._title(handle)
                if title:
                    found.append(ChatWindow(handle=int(handle), title=title))
            return True

        self._user32.EnumWindows(self._proc(visit), 0)
        return found

    def chat_windows(self) -> list[ChatWindow]:
        return [
            window for window in self._top_level() if window.title != MAIN_WINDOW_TITLE
        ]

    def room_list(self) -> Optional[int]:
        """Return the room-list control inside the main window."""
        found: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if self._title(handle).startswith(ROOM_LIST_PREFIX):
                found.append(int(handle))
                return False
            return True

        for window in self._top_level():
            if window.title != MAIN_WINDOW_TITLE:
                continue
            self._user32.EnumChildWindows(window.handle, self._proc(visit), 0)
            if found:
                return found[0]
        return None

    def _dpi(self, handle: int) -> int:
        """The scaling this window is drawn at, or the measured one."""
        try:
            dpi = int(self._user32.GetDpiForWindow(handle))
        except (AttributeError, OSError, ValueError):
            return MEASURED_DPI
        return dpi if dpi > 0 else MEASURED_DPI

    def scroll_to_top(self, control: int) -> None:
        """Wheel the list back to its first row. No cursor is moved."""
        from ctypes import wintypes as _types

        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        _pitch, first_centre, column = row_geometry(self._dpi(control))
        point = _Point(column, first_centre)
        self._user32.ClientToScreen(control, ctypes.byref(point))
        position = ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)
        for _notch in range(_WHEEL_NOTCHES):
            self._user32.PostMessageW(
                control, _WM_MOUSEWHEEL, _WHEEL_UP << 16, position
            )
        del _types

    def click_row(self, control: int, row: int) -> None:
        """Post a double-click at a row. The cursor is never moved or read."""
        pitch, first_centre, column = row_geometry(self._dpi(control))
        position = ((first_centre + row * pitch) << 16) | column
        for message, button in (
            (_WM_LBUTTONDOWN, _MK_LBUTTON),
            (_WM_LBUTTONUP, 0),
            (_WM_LBUTTONDBLCLK, _MK_LBUTTON),
            (_WM_LBUTTONUP, 0),
        ):
            if not self._user32.PostMessageW(control, message, button, position):
                raise OSError(f"PostMessage(row {row}) failed: {ctypes.get_last_error()}")

    def close_window(self, handle: int) -> None:
        self._user32.PostMessageW(handle, _WM_CLOSE, 0, 0)

    def _main_window(self) -> Optional[int]:
        for handle in self._all_top_level():
            if self._title(handle) == MAIN_WINDOW_TITLE:
                return handle
        return None

    def main_state(self) -> str:
        handle = self._main_window()
        if handle is None:
            return "missing"
        if not self._user32.IsWindowVisible(handle):
            return "hidden"
        return "minimized" if self._user32.IsIconic(handle) else "visible"

    def restore_main_window(self, state: str) -> None:
        handle = self._main_window()
        if handle is None:
            return
        if state == "hidden":
            self._user32.ShowWindow(handle, _SW_HIDE)
        elif state == "minimized":
            self._user32.ShowWindow(handle, _SW_MINIMIZE)

    def minimize_window(self, handle: int) -> None:
        # Minimised without being activated first: the activate/deactivate
        # dance is time the window spends in front of whatever was there.
        self._user32.ShowWindow(handle, _SW_SHOWMINNOACTIVE)

    def show_main_window(self) -> bool:
        """Bring the main window back, including from the tray.

        A window minimised to the tray is hidden rather than destroyed, so it
        is still enumerable — it just fails the visibility test every other
        lookup here applies. Showing it moves no cursor and posts no input.
        """
        for handle in self._all_top_level():
            if self._title(handle) != MAIN_WINDOW_TITLE:
                continue
            if not self._user32.IsWindowVisible(handle):
                self._user32.ShowWindow(handle, _SW_SHOW)
            if self._user32.IsIconic(handle):
                self._user32.ShowWindow(handle, _SW_RESTORE)
            return bool(self._user32.IsWindowVisible(handle))
        return False

    def input_handle(self, window: int) -> Optional[int]:
        result: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if self._class(handle) == INPUT_CLASS:
                result.append(int(handle))
                return False
            return True

        self._user32.EnumChildWindows(window, self._proc(visit), 0)
        return result[0] if result else None

    def set_text(self, control: int, text: str) -> None:
        # Selecting everything and replacing it clears whatever was in the box
        # — the Korean client's empty-input hint, which it stores as real
        # control text, and an unfinished draft alike — and emits the ordinary
        # change event that WM_SETTEXT would skip.
        buffer = ctypes.create_unicode_buffer(text)
        self._message(control, 0x00B1, 0, -1)  # EM_SETSEL
        self._message(control, 0x00C2, 1, ctypes.addressof(buffer))  # EM_REPLACESEL
        # RichEdit normalizes newlines. Verify content before queuing Enter.
        expected = text.replace("\r\n", "\n").replace("\r", "\n")
        actual = self.get_text(control).replace("\r\n", "\n").replace("\r", "\n")
        if actual != expected:
            raise OSError("input text differs from the request; Enter was not posted")

    def get_text(self, control: int) -> str:
        count = self._message(control, 0x000E)
        buffer = ctypes.create_unicode_buffer(count + 1)
        self._message(control, 0x000D, count + 1, ctypes.addressof(buffer))
        return "" if buffer.value == _INPUT_HINT else buffer.value

    def submit(self, control: int) -> None:
        # Posting to the control's queue reaches KakaoTalk's Enter-to-send
        # handler without the window needing focus.
        if not self._user32.PostMessageW(control, _WM_KEYDOWN, _VK_RETURN, 0x001C0001):
            raise OSError(f"PostMessage(WM_KEYDOWN) failed: {ctypes.get_last_error()}")
        if not self._user32.PostMessageW(control, _WM_KEYUP, _VK_RETURN, 0xC01C0001):
            raise OSError(f"PostMessage(WM_KEYUP) failed: {ctypes.get_last_error()}")


def make_sender() -> Sender:
    """Build a sender wired to the running KakaoTalk process."""
    from .process import find_pid

    pid = find_pid()
    if pid is None:
        raise KakaoReadError(
            ErrorCode.KAKAO_NOT_RUNNING,
            "KakaoTalk is not running; start it and open the conversation first",
        )
    backend = Win32Backend(pid)
    opener = RoomOpener(backend, settle=lambda: time.sleep(POLL_SECONDS))
    return Sender(backend, running=lambda: find_pid() == pid, opener=opener)
