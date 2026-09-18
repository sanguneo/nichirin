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
from .errors import ErrorCode, KamadoError

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

_WRITE_ATTEMPTS = 3
"""How many times to write the message before giving up on the composer."""

_SETTLE_SECONDS = 0.2
"""Long enough for the client to act on a write, and no longer."""

_WAIT_SECONDS = 3.0
"""How long to wait for the send button to enable, and for the box to empty."""

_POLL_SECONDS = 0.05
"""Gap between those polls; an accessibility read is cheap."""

_ACTIVATE_SECONDS = 0.6
"""How long the client needs to actually come forward before it can be clicked."""

_SEARCH_SECONDS = 6.0
"""How long the client may take to answer a search with its whole room list."""

_SEARCH_ROW_LIMIT = 2000
"""How many rows of that list are worth reading before giving up.

Each row's name is one accessibility round trip: 648 of them measured 10.3 s,
and the wanted room was row 116. The limit is a stop, not a budget.
"""

_SEND_BUTTON_TITLES = ("전송", "Send")
"""The chat window's send button, pressed to commit a message.

An ``AXValue`` write places the text without the client counting it as typing,
so a Return over it is ignored; the button is what actually sends."""


@dataclass(frozen=True)
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
    """What a send reports, plus the two keys that only sometimes apply.

    `clearedDraft` and `wouldClearDraft` are written by this module and were
    not declared here, so the very returns below did not match the type they
    claimed. They are optional rather than required because a caller reads one
    or the other, never both: the dry run says what *would* be lost.
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

    def locate_row(self, wanted: set[str]) -> Optional[int]:
        """Find a row for any of *wanted*, through the client's own search.

        The index is only meaningful until :meth:`clear_row_filter` puts the
        list back, because the list it counts is not the one normally shown.
        """
        ...

    def clear_row_filter(self) -> None:
        """Put the room list back the way it was found."""
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


def activate(runner=subprocess.run) -> None:
    """Bring KakaoTalk forward.

    Sending never needs this.  Opening a *closed* room does: its chat-list rows
    ignore both ``AXPress`` and a posted Return, so the only thing that opens
    one is a real double-click, and a click at screen coordinates is only safe
    aimed at the window in front.
    """
    try:
        runner(["/usr/bin/open", "-b", BUNDLE_ID], check=False)
    except (OSError, subprocess.SubprocessError):
        return
    time.sleep(_ACTIVATE_SECONDS)


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
            raise KamadoError(
                ErrorCode.WINDOW_NOT_FOUND,
                "KakaoTalk's main window is not available, so no room can be opened. "
                "If KakaoTalk is running, ensure its window is on the current desktop/Space "
                "(or assign KakaoTalk to 'All Desktops' via Dock icon options)",
            )
        already_open = self._matching_window(wanted)
        if already_open is not None:
            return already_open
        if state != "visible" and not self._backend.show_main_window():
            raise KamadoError(
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
            found = self._through_search(wanted)
            if found is not None:
                return found
            raise KamadoError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"could not open a chat window for {room!r}: the client's list "
                f"showed {len(rows)} rows and none is that room, with "
                f"{len(self._backend.chat_windows())} chat windows open. The list "
                "only exposes rows it has drawn, so open the room once in "
                "KakaoTalk; a window on another desktop/Space is invisible here "
                "too, which the Dock icon's 'All Desktops' option fixes",
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
        remembered = _recall_row(self._memory, room)
        if remembered is not None and remembered >= 0 and remembered not in order:
            order.append(remembered)
        if any(title.strip() for title in rows):
            # The list named its rows, so a row it did not name is not this
            # room. Opening it anyway puts somebody else's conversation on
            # screen and marks it read, which is worse than failing here.
            return order
        # Only a list that named nothing at all leaves the recency hint and a
        # scan of the first rows worth trying.
        if hint is not None and hint >= 0 and hint not in order:
            order.append(hint)
        order.extend(index for index in range(_SEARCHED_ROWS) if index not in order)
        return order

    def _through_search(self, wanted: set[str]) -> Optional[ChatWindow]:
        """Reach a room the drawn list does not hold, through the client's search.

        The list exposes only the rows it has drawn — 26 of 653 rooms on the
        machine this was measured on — so a conversation further down cannot
        be clicked at all, however patiently the rows are matched. Typing the
        name into the client's own search box brings its whole list within
        reach: the wanted room was found at row 116 of 648 that way.

        What is found there is deliberately not remembered. That index counts
        rows in a list that exists only while the search does, and clicking it
        afterwards would open whichever conversation had taken its place.
        """
        window: Optional[ChatWindow] = None
        try:
            index = self._backend.locate_row(wanted)
            if index is None:
                return None
            if not self._backend.open_row(index):
                return None
            window = self._await_window(wanted)
        finally:
            self._backend.clear_row_filter()
        if window is None:
            return None
        self._backend.minimize_window(window.handle)
        return window

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
            raise KamadoError(ErrorCode.INVALID_REQUEST, "a nonempty room name is required")
        if not self._running():
            raise KamadoError(
                ErrorCode.KAKAO_NOT_RUNNING,
                "KakaoTalk is not running; start it and open the conversation first",
            )
        windows = self._backend.chat_windows()
        wanted = {name.strip().casefold() for name in (room, *aliases) if name.strip()}
        matches = [window for window in windows if window.title.casefold() in wanted]

        if not matches:
            if open_if_needed and self._opener is not None:
                return self._opener.open(room, aliases, hint)
            raise KamadoError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"No open chat window matches {room!r}; open that conversation in "
                "KakaoTalk (double-click it so it pops out) and try again. "
                "If already open, ensure the window is on the current desktop/Space",
            )
        if len(matches) > 1:
            raise KamadoError(
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
            raise KamadoError(ErrorCode.INVALID_REQUEST, "refusing to send an empty message")
        if "\x00" in text:
            raise KamadoError(ErrorCode.INVALID_REQUEST, "message contains a null character")

        window = self.resolve(
            room, open_if_needed=open_if_needed, aliases=aliases, hint=hint
        )
        control = self._backend.input_handle(window.handle)
        if control is None:
            raise KamadoError(
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
            raise KamadoError(
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
        self._next = 1
        self._search_restore: Optional[str] = None
        """What the client's search box held before this process typed in it."""

    def _register(self, element: ax.Element) -> int:
        handle = self._next
        self._next += 1
        self._registry[handle] = element
        return handle

    def _element(self, handle: int) -> ax.Element:
        element = self._registry.get(handle)
        if element is None:
            raise KamadoError(
                ErrorCode.WINDOW_NOT_FOUND, "that window is no longer being tracked"
            )
        return element

    def _windows(self) -> list[ax.Element]:
        wins = self._app.windows()
        if wins:
            return wins
        # If AXWindows returned empty (e.g. windows on inactive Spaces or behind
        # full-screen apps), invoke Cocoa's Window menu action to restore accessibility
        # visibility without taking foreground or moving desktop spaces.
        self._order_front_via_menu()
        return self._app.windows()

    def _window_menu_items(self) -> dict[str, ax.Element]:
        """Map window titles to their 'makeKeyAndOrderFront:' menu items."""
        menubar = self._app.attribute("AXMenuBar")
        if not menubar:
            return {}
        items: dict[str, ax.Element] = {}
        for bar_item in menubar.children():
            if bar_item.title in ("창", "Window"):
                menus = bar_item.children()
                if not menus:
                    continue
                for m in menus[0].children():
                    if m.identifier == "makeKeyAndOrderFront:" and m.title:
                        items[m.title.strip().casefold()] = m
        return items

    def _order_front_via_menu(self, title: Optional[str] = None) -> bool:
        """Trigger Cocoa makeKeyAndOrderFront: via Window menu without activating."""
        menu_items = self._window_menu_items()
        if not menu_items:
            return False
        if title is not None:
            target = menu_items.get(title.strip().casefold())
            if target is not None and target.perform("AXPress"):
                time.sleep(0.05)
                return True
            return False
        # If no specific title requested, select '카카오톡' or first window item
        candidate = menu_items.get("카카오톡") or next(iter(menu_items.values()), None)
        if candidate is not None and candidate.perform("AXPress"):
            time.sleep(0.05)
            return True
        return False

    def _main_window(self) -> Optional[ax.Element]:
        for window in self._windows():
            if window.identifier == MAIN_WINDOW_IDENTIFIER:
                return window
        # Fallback: target '카카오톡' explicitly from the Window menu
        if self._order_front_via_menu("카카오톡"):
            for window in self._app.windows():
                if window.identifier == MAIN_WINDOW_IDENTIFIER:
                    return window
        # If still missing (e.g. on another Space or completely closed), activate client to restore
        activate()
        for window in self._app.windows():
            if window.identifier == MAIN_WINDOW_IDENTIFIER:
                return window
        return None

    def chat_windows(self) -> list[ChatWindow]:
        """Return every window that is not the main one, which is a room."""
        found: list[ChatWindow] = []
        windows = self._windows()
        for window in windows:
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
        Both levels are read as direct children rather than crawled, because
        every node is a separate IPC round trip into the client and the
        transcript below holds hundreds of them.
        """
        element = self._element(window)
        composer: Optional[ax.Element] = None
        for child in element.children():
            if child.role != "AXScrollArea":
                continue
            found = [c for c in child.children() if c.role in _INPUT_ROLES]
            if found and not any(c.role == "AXTable" for c in child.children()):
                composer = found[0]
        if composer is None:
            return None
        handle = self._register(composer)
        self._windows_by_control[handle] = element
        return handle

    def get_text(self, control: int) -> str:
        value = self._element(control).value
        text = value if isinstance(value, str) else ""
        return "" if text.strip() in _EMPTY_HINTS else text

    def set_text(self, control: int, text: str) -> None:
        """Replace the composer's contents with *text*, and prove it landed.

        One ``AXValue`` write, and nothing else.  The write needs no keyboard,
        no focus and — this is the part worth keeping — **no activation**: the
        client is never brought forward, the pointer is never moved, and
        whatever the user is doing is not interrupted.  It also enables the
        client's own send button, which :meth:`submit` then presses.

        Typing the text was tried and abandoned: synthesised keystrokes drop
        characters unpredictably, and they only arrive at all once the app has
        been activated and the field clicked, which is the whole cost this
        avoids.  The value is read back, so a refused write is a failure rather
        than a silent empty send.
        """
        composer = self._element(control)
        for attempt in range(_WRITE_ATTEMPTS):
            if composer.set_attribute("AXValue", text) and composer.value == text:
                return
            if attempt + 1 < _WRITE_ATTEMPTS:
                time.sleep(_SETTLE_SECONDS)
        raise OSError("KakaoTalk refused to accept the message text into the composer")

    def _send_button(self, control: int) -> Optional[ax.Element]:
        """Return the chat window's send button.

        It is a **direct child** of the chat window, so it is found by reading
        that one list rather than by walking the subtree: the transcript below
        holds a button per message, and crawling to find this one measured
        3.5 s against 0.002 s for the direct children.
        """
        window = self._windows_by_control.get(control)
        if window is None:
            return None
        for element in window.children():
            if element.role == "AXButton" and element.title.strip() in _SEND_BUTTON_TITLES:
                return element
        return None

    def submit(self, control: int) -> None:
        """Commit the message that :meth:`set_text` placed.

        Pressing the send button is what delivers it, and — like the write —
        it needs no keyboard focus, no pointer and no activation, so the whole
        send leaves the desktop untouched.  Success is judged by the composer
        going empty rather than by the press call's own return value, which
        this client reports as failure even on a press that works.

        A Return aimed at the process is the fallback for a client that shows
        no such button; it is posted to the pid, not to the session, so it too
        cannot land in whatever the user has in front.

        The button is enabled by the write, but not instantly, so it is waited
        for rather than pressed on faith — pressing a disabled button does
        nothing and leaves the message sitting in the box.  Both waits poll,
        so a fast client costs one read rather than a fixed sleep.
        """
        composer = self._element(control)
        button = self._send_button(control)
        if button is not None:
            self._wait(lambda: button.attribute("AXEnabled") is not False)
            button.perform("AXPress")
        else:
            ax.post_key(self._pid, ax.KEY_RETURN)

        def emptied() -> bool:
            remaining = composer.value
            if not isinstance(remaining, str):
                return True
            return not remaining.strip() or remaining.strip() in _EMPTY_HINTS

        if not self._wait(emptied):
            raise OSError("the message stayed in the composer; it was not sent")

    @staticmethod
    def _wait(ready, timeout: float = _WAIT_SECONDS) -> bool:
        """Poll *ready* until it holds, so a quick client is not made to wait."""
        deadline = time.monotonic() + timeout
        while True:
            if ready():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)

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

    def _search_field(self) -> Optional[ax.Element]:
        """The client's own room search box, which is a direct child."""
        main = self._main_window()
        if main is None:
            return None
        for child in main.children():
            if child.role == "AXTextField":
                return child
        return None

    def locate_row(self, wanted: set[str]) -> Optional[int]:
        """Find a row for any of *wanted* by searching the client's whole list.

        Writing the name into the search box is one ``AXValue`` write, the
        same mechanism sending uses, and the client answers by listing far
        more rooms than it had drawn — 648 against 26, measured here. The wait
        is for that longer list to appear, and the scan stops at the first
        match rather than reading every name.
        """
        field = self._search_field()
        if field is None or not wanted:
            return None
        if self._search_restore is None:
            self._search_restore = str(field.attribute("AXValue") or "")
        drawn = len(self._room_rows())
        if not field.set_attribute("AXValue", sorted(wanted)[0]):
            return None
        rows = self._room_rows()
        deadline = time.monotonic() + _SEARCH_SECONDS
        # Paced like every other wait here: each pass is a round trip per row,
        # so spinning on the list asks the client thousands of times for an
        # answer it is still computing, and asks hardest exactly when it is
        # slowest to reply.
        while len(rows) <= drawn and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            rows = self._room_rows()
        for index, row in enumerate(rows[:_SEARCH_ROW_LIMIT]):
            if self._row_title(row).strip().casefold() in wanted:
                return index
        return None

    def clear_row_filter(self) -> None:
        """Put the search box back to whatever it held, if this wrote in it."""
        if self._search_restore is None:
            return
        field = self._search_field()
        if field is not None:
            field.set_attribute("AXValue", self._search_restore)
        self._search_restore = None

    def open_row(self, row: int) -> bool:
        """Open one row of the chat list with a real double-click.

        This is the one part of sending that has to touch the screen.  The
        rows ignore ``AXPress``, ``AXSelected`` and a posted Return — selecting
        one only highlights it — so nothing but a genuine double-click opens a
        room.  macOS does not infer that from two clicks in a row either; the
        second has to carry a click state of 2, which :func:`ax.double_click`
        sets.

        Because it is a click at screen coordinates, the client is brought
        forward first so the click cannot land in somebody else's window, and
        the pointer is put back where it was.
        """
        rows = self._room_rows()
        if row < 0 or row >= len(rows):
            return False
        rectangle = rows[row].frame()
        if rectangle is None:
            return False
        activate()
        pointer = ax.cursor_location()
        x, y = rectangle[0] + rectangle[2] / 2, rectangle[1] + rectangle[3] / 2
        ax.double_click(x, y, restore_to=pointer)
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
            activate()
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
        raise KamadoError(
            ErrorCode.PERMISSION_REQUIRED,
            "macOS has not granted this terminal permission to control other "
            "applications. Add it under System Settings › Privacy & Security › "
            "Accessibility, then run the command again.",
            permission="accessibility",
        )
    pid = client_pid()
    if pid is None:
        raise KamadoError(
            ErrorCode.KAKAO_NOT_RUNNING,
            "KakaoTalk is not running; start it and sign in first",
        )
    backend = AXBackend(pid)
    return Sender(backend, running, RoomOpener(backend))
