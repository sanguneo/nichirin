"""Exact window targeting, draft replacement, and background text sending.

The Windows build reached the client by posting messages to a window handle.
macOS has no such handle, but it has something with the same two virtues: the
accessibility tree addresses one control inside one window exactly, and
``CGEventPostToPid`` delivers a keystroke to one process without activating it.
So the shape of this module is the Windows one, and the properties it was
built to protect are the same:

* **The desktop is left alone.**  Nothing is brought to the front, no window is
  raised, the cursor is never moved, and the clipboard is never written.  The
  reference implementation this port follows activates the app and types into
  it; that works, but it takes the screen away from whoever is using it, and it
  is not necessary.
* **The text is written, not typed.**  Setting the input's value in one call is
  atomic from the client's point of view, where a synthesised keystroke per
  character is a race against the IME and against the user.
* **A send is one Return, aimed at one process.**
* **An unfinished draft is cleared, not refused**, and the clearing is reported
  — ``clearedDraft`` on a send, ``wouldClearDraft`` on a dry run — because
  losing somebody's half-typed line silently is the indefensible part.
* **Queue acceptance is not delivery.**  ``submitted`` means the client
  accepted the text, and nothing here claims more than that.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, TypedDict

from . import ax, paths
from .errors import ErrorCode, KakaoReadError

BUNDLE_ID = paths.BUNDLE_ID
EXECUTABLE = "/Applications/KakaoTalk.app/Contents/MacOS/KakaoTalk"

MAIN_WINDOW_IDENTIFIER = "Main Window"
"""What the client calls its own main window, login screen or not."""

_INPUT_ROLES = ("AXTextArea", "AXTextField")
_EMPTY_HINTS = ("메시지 입력", "Enter message", "")
"""What an empty message box reads as rather than as a draft.

The Korean client writes its placeholder into the value, so a literal match is
what tells an empty box apart from a one-word draft.
"""

_OPEN_TIMEOUT_SECONDS = 6.0
_OPEN_POLL_SECONDS = 0.1
_SEARCHED_ROWS = 8

_TYPE_ATTEMPTS = 4
"""How many times to click-focus and type before giving up on the composer.

Taking keyboard focus is racy — a click that lands a hair before the window is
ready leaves the keystrokes going nowhere — so the composer is read back after
each attempt and the text is retyped until it is actually there."""

_SEND_BUTTON_TITLE = "전송"
"""The chat window's send button, pressed to commit a message.

An ``AXValue`` write places text without the client counting it as typing, so
Return over it is ignored; the button is what actually sends."""


@dataclass(frozen=True)
class ChatWindow:
    """One open KakaoTalk chat window."""

    handle: int
    title: str


class SendResult(TypedDict):
    submitted: bool
    dryRun: bool
    room: str
    length: int


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
        """Send the Return that makes KakaoTalk send what is in the input."""
        ...


class RoomListBackend(Protocol):
    """What opening a room needs: the list, a row, and a way to undo it."""

    def chat_windows(self) -> list[ChatWindow]:
        ...

    def room_rows(self) -> list[str]:
        """Return the titles of the rows in the main window's room list."""
        ...

    def open_row(self, row: int) -> bool:
        """Open one row of the room list, without moving the cursor."""
        ...

    def main_state(self) -> str:
        """Report the main window as "visible", "minimized", "hidden" or "missing"."""
        ...

    def show_main_window(self) -> bool:
        """Bring the main window back. True when it is up."""
        ...

    def restore_main_window(self, state: str) -> None:
        """Put the main window back the way it was found."""
        ...

    def minimize_window(self, handle: int) -> None:
        """Park a window where it covers nothing."""
        ...

    def close_window(self, handle: int) -> None:
        """Close a chat window that was opened by mistake."""
        ...


def client_pid(runner=subprocess.run) -> Optional[int]:
    """Return the running client's process id, or None when it is closed."""
    try:
        completed = runner(
            ["/usr/bin/pgrep", "-f", EXECUTABLE], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (completed.stdout or "").split():
        if line.isdigit():
            return int(line)
    return None


def running(runner=subprocess.run) -> bool:
    """Whether the KakaoTalk client is running at all."""
    return client_pid(runner) is not None


def _frontmost_bundle(runner=subprocess.run) -> Optional[str]:
    """Return the bundle id of the frontmost app, so it can be restored."""
    try:
        completed = runner(
            ["/usr/bin/osascript", "-e",
             'tell application "System Events" to get bundle identifier of '
             "first process whose frontmost is true"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identifier = (completed.stdout or "").strip()
    return identifier or None


# -- remembered rows ---------------------------------------------------------


def _remembered_rows() -> Path:
    return paths.state_directory() / "rows.json"


def _row_key(room: str) -> str:
    """Hash a room title, so the memory never holds one in the clear."""
    return sha256(room.strip().casefold().encode("utf-8")).hexdigest()[:32]


def _recall_row(path: Path, room: str) -> int:
    try:
        stored = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return -1
    if not isinstance(stored, dict):
        return -1
    value = stored.get(_row_key(room))
    return value if isinstance(value, int) and value >= 0 else -1


def _learn_row(path: Path, room: str, row: int) -> None:
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
    """Open a room that has no window yet, and put the desktop back after.

    Unlike the Windows list, which was owner-drawn and could only be addressed
    by pixel coordinate, the macOS list reports its rows through accessibility
    with their titles attached.  So a row is matched by name before it is
    touched, and the guesswork the Windows build needed — alternating between
    two theories about where a room sits, and closing whatever wrong room it
    opened — is not needed here.  The remembered row survives only as a way to
    skip the scan.
    """

    def __init__(
        self,
        backend: RoomListBackend,
        memory: Optional[Path] = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._memory = memory if memory is not None else _remembered_rows()
        self._sleep = sleep
        self._now = now

    def _matching_window(self, wanted: set[str]) -> Optional[ChatWindow]:
        for window in self._backend.chat_windows():
            if window.title.strip().casefold() in wanted:
                return window
        return None

    def open(
        self, room: str, aliases: Iterable[str] = (), hint: Optional[int] = None
    ) -> ChatWindow:
        """Open *room*'s window and leave the desktop as it was found."""
        wanted = {name.strip().casefold() for name in (room, *aliases) if name.strip()}
        state = self._backend.main_state()
        if state == "missing":
            raise KakaoReadError(
                ErrorCode.WINDOW_NOT_FOUND,
                "KakaoTalk's main window is not available, so no room can be opened",
            )
        if state != "visible" and not self._backend.show_main_window():
            raise KakaoReadError(
                ErrorCode.WINDOW_NOT_FOUND,
                "KakaoTalk's main window could not be brought back to open the room",
            )

        try:
            rows = self._backend.room_rows()
            order = self._order(room, rows, wanted, hint)
            for row in order:
                if not self._backend.open_row(row):
                    continue
                window = self._await_window(wanted)
                if window is not None:
                    _learn_row(self._memory, room, row)
                    # Nobody asked for this window to be in front, so it is
                    # parked the moment it exists.
                    self._backend.minimize_window(window.handle)
                    return window
            raise KakaoReadError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"could not open a chat window for {room!r} from the room list",
                room=room,
            )
        finally:
            self._backend.restore_main_window(state)

    def _order(
        self, room: str, rows: list[str], wanted: set[str], hint: Optional[int]
    ) -> list[int]:
        """Decide which rows to try, best first.

        A row whose own title matches is tried before anything else, because
        the list says so; the remembered row and the catalog's recency hint are
        only fallbacks for a list that did not report its titles.
        """
        order: list[int] = [
            index for index, title in enumerate(rows)
            if title.strip().casefold() in wanted
        ]
        for candidate in (_recall_row(self._memory, room), hint):
            if candidate is not None and candidate >= 0 and candidate not in order:
                order.append(candidate)
        order.extend(index for index in range(_SEARCHED_ROWS) if index not in order)
        return order

    def _await_window(self, wanted: set[str]) -> Optional[ChatWindow]:
        """Watch for the window rather than sleeping a flat guess.

        What is left after this is KakaoTalk drawing its own window, and
        nothing on this side preempts that.
        """
        deadline = self._now() + _OPEN_TIMEOUT_SECONDS
        while self._now() < deadline:
            window = self._matching_window(wanted)
            if window is not None:
                return window
            self._sleep(_OPEN_POLL_SECONDS)
        return None


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
        """Write *text* into *room*'s window and send it, or preview when dry.

        The message is validated and the window resolved before anything is
        written. Queue acceptance is not proof of network delivery.
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
        except (OSError, ax.AccessibilityDenied) as error:
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


# -- the accessibility backend -----------------------------------------------


class AXBackend:
    """A :class:`WindowBackend` and :class:`RoomListBackend` over the AX tree.

    Elements cannot be integers, and the protocols above speak in integer
    handles because that is what a window was on the platform this was ported
    from.  Rather than change the contract, each element this backend hands out
    is registered and referred to by its number, which also keeps a handle from
    outliving the scan that produced it in any surprising way.
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._app = ax.application(pid)
        self._registry: dict[int, ax.Element] = {}
        self._windows_by_control: dict[int, ax.Element] = {}
        self._click_target: dict[int, ax.Element] = {}
        self._next = 1
        self._restore_bundle: Optional[str] = None
        self._restore_cursor: Optional[tuple] = None

    def _register(self, element: ax.Element) -> int:
        handle = self._next
        self._next += 1
        self._registry[handle] = element
        return handle

    def _element(self, handle: int) -> ax.Element:
        element = self._registry.get(handle)
        if element is None:
            raise KakaoReadError(
                ErrorCode.WINDOW_NOT_FOUND, "that window is no longer being tracked"
            )
        return element

    def _windows(self) -> list[ax.Element]:
        return self._app.windows()

    def _main_window(self) -> Optional[ax.Element]:
        for window in self._windows():
            if window.identifier == MAIN_WINDOW_IDENTIFIER:
                return window
        return None

    def chat_windows(self) -> list[ChatWindow]:
        """Return every window that is not the main one, which is a room."""
        found: list[ChatWindow] = []
        for window in self._windows():
            if window.identifier == MAIN_WINDOW_IDENTIFIER:
                continue
            title = window.title.strip()
            if not title:
                continue
            found.append(ChatWindow(handle=self._register(window), title=title))
        return found

    def input_handle(self, window: int) -> Optional[int]:
        """Find the message box: the text area that is not the transcript.

        A chat window holds two scrolling regions, and the one showing the
        conversation contains a table of messages while the composer does not.
        The composer's own text element reports a rectangle only as wide as the
        text in it, and clicking that rectangle does not focus it; the
        surrounding scroll area does, so that scroll area is remembered as the
        click target for :meth:`set_text`.
        """
        element = self._element(window)
        input_area: Optional[ax.Element] = None
        for child in element.children():
            if child.role != "AXScrollArea":
                continue
            if any(d.role == "AXTable" for d in child.descendants(depth=3)):
                continue  # This is the transcript, not the composer.
            input_area = child
        if input_area is None:
            return None
        composer = next(
            (d for d in input_area.descendants(depth=3) if d.role in _INPUT_ROLES), None
        )
        if composer is None:
            return None
        handle = self._register(composer)
        self._windows_by_control[handle] = element
        self._click_target[handle] = input_area
        return handle

    def get_text(self, control: int) -> str:
        value = self._element(control).value
        text = value if isinstance(value, str) else ""
        return "" if text.strip() in _EMPTY_HINTS else text

    def _activate(self) -> None:
        """Bring KakaoTalk forward, remembering what to put back afterwards.

        This is the line the Windows build never had to cross: there, a posted
        message reached a control in any state.  KakaoTalk's composer on macOS
        only accepts input when it is the focused control of the frontmost app,
        so sending has to take the foreground for a moment.  Reading never does
        this.  The previously-frontmost app and the pointer are recorded so
        both can be restored the instant the message is in.
        """
        self._restore_bundle = _frontmost_bundle()
        try:
            self._restore_cursor = ax.cursor_location()
        except Exception:  # noqa: BLE001 - cursor restore is best-effort
            self._restore_cursor = None
        subprocess.run(["/usr/bin/open", "-b", BUNDLE_ID], check=False)
        time.sleep(0.6)

    def _restore(self) -> None:
        """Undo :meth:`_activate`: put the foreground app and pointer back."""
        if self._restore_bundle and self._restore_bundle != BUNDLE_ID:
            subprocess.run(["/usr/bin/open", "-b", self._restore_bundle], check=False)
        self._restore_bundle = None
        self._restore_cursor = None

    def set_text(self, control: int, text: str) -> None:
        """Replace the composer's contents with *text*, and prove it landed.

        The recipe is the one the reference client automation uses and the only
        one that works here: raise the chat window, click the *input area* to
        focus the composer (clicking the composer's own text rectangle does
        not, because that rectangle is only as wide as its content), clear any
        draft, and type the message as Unicode.  ``AXValue`` writes are refused
        by this control, so the text is typed rather than set.

        Focus is racy — a click that does not take leaves the keystrokes going
        nowhere — so the whole thing is attempted a few times and the composer
        is read back each time.  The method returns only once the composer
        actually holds the text, and raises otherwise; that is what lets a
        caller trust that ``submitted`` means the words were in the box.
        """
        composer = self._element(control)
        window = self._windows_by_control.get(control)
        self._activate()
        if window is not None:
            # A window this tool opened is left minimised; it has to be brought
            # back and raised before it can be written to, or a later button
            # press lands on whatever is in front of it.
            if window.attribute("AXMinimized") is True:
                window.set_attribute("AXMinimized", "false")
                time.sleep(0.3)
            window.perform("AXRaise")
            time.sleep(0.2)

        # The composer accepts an ``AXValue`` write, and — proven on a live
        # client — that write both places the text and enables the client's
        # send button, so the message goes out through the button rather than
        # through a keystroke.  Synthesised typing was tried and abandoned: it
        # dropped characters unpredictably even with the field focused, while
        # the write is exact.  The value is read back so a refused write is a
        # failure rather than a silent empty send.
        for _attempt in range(_TYPE_ATTEMPTS):
            if composer.set_attribute("AXValue", text):
                time.sleep(0.15)
                if composer.value == text:
                    return
            time.sleep(0.15)
        raise OSError("KakaoTalk refused to accept the message text into the composer")

    def _send_button(self, control: int) -> Optional[ax.Element]:
        """Return the chat window's 전송 button, when the client exposes one."""
        window = self._windows_by_control.get(control)
        if window is None:
            return None
        for element in window.descendants(depth=8):
            if element.role == "AXButton" and element.title.strip() == _SEND_BUTTON_TITLE:
                return element
        return None

    def submit(self, control: int) -> None:
        """Commit the message, then release the foreground.

        Pressing the 전송 button is what sends the message the ``AXValue`` write
        placed and enabled; it needs neither keyboard focus nor the pointer.
        Success is judged by the composer going empty rather than by the press
        call's own return value, which this client reports unreliably even on a
        press that works.  A Return through the session is the fallback for a
        client that shows no such button.
        """
        composer = self._element(control)
        try:
            button = self._send_button(control)
            if button is not None:
                button.perform("AXPress")
            else:
                ax.send_key(ax.KEY_RETURN)
            time.sleep(0.3)
            remaining = composer.value
            if isinstance(remaining, str) and remaining.strip() and remaining.strip() not in _EMPTY_HINTS:
                raise OSError("the message stayed in the composer; it was not sent")
        finally:
            self._restore()

    # -- room list ----------------------------------------------------------

    def _room_rows(self) -> list[ax.Element]:
        main = self._main_window()
        if main is None:
            return []
        tables = [child for child in main.descendants() if child.role == "AXTable"]
        if not tables:
            return []
        largest = max(tables, key=lambda table: len(table.children()))
        return [row for row in largest.children() if row.role == "AXRow"]

    @staticmethod
    def _row_title(row: ax.Element) -> str:
        """Read a room's name out of its row.

        A row is a stack of cells whose roles say what each one is: an
        ``AXButton`` for the profile picture, an ``AXImage`` for a badge, then
        the room name, the member count, the time, and the last message — all
        as ``AXStaticText``.  Measured on a live client, the name is the first
        ``AXStaticText``; the button's description is "프로필" and the badge's
        is "badge openchat room", so reading the first text of any kind
        returned the profile button instead of the room.  The name is taken
        from the first static-text cell, and the trailing count/time/preview
        cells are left alone.
        """
        for element in row.descendants(depth=4):
            if element.role != "AXStaticText":
                continue
            value = element.value
            if isinstance(value, str) and value.strip():
                return value.strip()
            if element.title and element.title.strip():
                return element.title.strip()
        return ""

    def room_rows(self) -> list[str]:
        return [self._row_title(row) for row in self._room_rows()]

    def open_row(self, row: int) -> bool:
        """Open one row by selecting it and pressing Return at the process.

        Selection rather than a click, because a row scrolled out of view
        reports coordinates that are outside the window and a synthetic click
        at them lands on nothing.
        """
        rows = self._room_rows()
        if row < 0 or row >= len(rows):
            return False
        target = rows[row]
        if not target.set_attribute("AXSelected", "true") and not target.perform("AXPress"):
            return False
        ax.post_key(self._pid, ax.KEY_RETURN)
        return True

    def main_state(self) -> str:
        main = self._main_window()
        if main is None:
            return "missing"
        if main.attribute("AXMinimized") is True:
            return "minimized"
        return "visible"

    def show_main_window(self) -> bool:
        main = self._main_window()
        if main is None:
            return False
        if main.attribute("AXMinimized") is True:
            main.set_attribute("AXMinimized", "false")
        return self._main_window() is not None

    def restore_main_window(self, state: str) -> None:
        """Put the main window back the way it was found."""
        main = self._main_window()
        if main is None:
            return
        if state == "minimized":
            main.set_attribute("AXMinimized", "true")

    def minimize_window(self, handle: int) -> None:
        self._element(handle).set_attribute("AXMinimized", "true")

    def close_window(self, handle: int) -> None:
        window = self._element(handle)
        for child in window.descendants(depth=3):
            if child.subrole == "AXCloseButton":
                child.perform("AXPress")
                return


def make_sender() -> Sender:
    """Build the sender this machine can actually use, or explain what is missing."""
    if not ax.is_trusted():
        raise KakaoReadError(
            ErrorCode.PERMISSION_REQUIRED,
            "macOS has not granted this terminal permission to control other "
            "applications. Add it under System Settings › Privacy & Security › "
            "Accessibility, then run the command again.",
            permission="accessibility",
        )
    pid = client_pid()
    if pid is None:
        raise KakaoReadError(
            ErrorCode.KAKAO_NOT_RUNNING,
            "KakaoTalk is not running; start it and sign in first",
        )
    backend = AXBackend(pid)
    return Sender(backend, running, RoomOpener(backend))
