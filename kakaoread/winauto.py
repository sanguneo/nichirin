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
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, TypedDict

from .errors import ErrorCode, KakaoReadError

MAIN_WINDOW_TITLE = "카카오톡"
INPUT_CLASS = "RICHEDIT50W"
_INPUT_HINT = "메시지 입력"


@dataclass(frozen=True, slots=True)
class ChatWindow:
    """One open KakaoTalk chat window."""

    handle: int
    title: str


class SendResult(TypedDict):
    submitted: bool
    dryRun: bool
    room: str
    length: int


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


class Sender:
    """Resolve a room's chat window and hand a message to its input."""

    def __init__(self, backend: WindowBackend, running: Callable[[], bool]) -> None:
        self._backend = backend
        self._running = running

    def resolve(self, room: str) -> ChatWindow:
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
        needle = room.strip().casefold()
        matches = [window for window in windows if window.title.casefold() == needle]

        if not matches:
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

    def send(self, room: str, text: str, dry_run: bool = False) -> SendResult:
        """Type *text* into *room*'s window and send it, or preview when dry.

        The message is validated and the window resolved before anything is
        typed. Queue acceptance is not proof of network delivery.
        """
        if not text or not text.strip():
            raise KakaoReadError(ErrorCode.INVALID_REQUEST, "refusing to send an empty message")
        if "\x00" in text:
            raise KakaoReadError(ErrorCode.INVALID_REQUEST, "message contains a null character")

        window = self.resolve(room)
        control = self._backend.input_handle(window.handle)
        if control is None:
            raise KakaoReadError(
                ErrorCode.INPUT_NOT_FOUND,
                f"found the window for {window.title!r} but not its message box; "
                "the client version may differ",
                room=window.title,
            )

        if self._backend.get_text(control):
            raise KakaoReadError(
                ErrorCode.BUSY, "the message input contains a draft; leave it untouched",
                room=window.title,
            )

        if dry_run:
            return {"submitted": False, "dryRun": True, "room": window.title, "length": len(text)}

        try:
            self._backend.set_text(control, text)
            self._backend.submit(control)
        except OSError as error:
            raise KakaoReadError(
                ErrorCode.SEND_FAILED, f"could not send to {window.title!r}: {error}",
                room=window.title,
            ) from error
        return {"submitted": True, "dryRun": False, "room": window.title, "length": len(text)}


# -- Win32 backend -----------------------------------------------------------

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_VK_RETURN = 0x0D


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

    def chat_windows(self) -> list[ChatWindow]:
        found: list[ChatWindow] = []

        def visit(handle: int, _param: int) -> bool:
            if (
                self._pid_of(handle) == self._pid
                and self._user32.IsWindowVisible(handle)
            ):
                title = self._title(handle)
                if title and title != MAIN_WINDOW_TITLE:
                    found.append(ChatWindow(handle=int(handle), title=title))
            return True

        self._user32.EnumWindows(self._proc(visit), 0)
        return found

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
        if self.get_text(control):
            raise KakaoReadError(ErrorCode.BUSY, "the input changed; leave the draft untouched")
        # The Korean client stores its empty-input hint as actual control text.
        # Selection replacement removes it and emits the normal change event.
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
    return Sender(Win32Backend(pid), running=lambda: find_pid() == pid)
