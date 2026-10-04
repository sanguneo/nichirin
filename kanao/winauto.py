"""Send a message by driving KakaoTalk's own input box, no cursor, no focus.

This is the one place the tool writes rather than reads, and it does so through
the real client: a message is placed in the chat window's ``RICHEDIT50W`` input
with ``EM_REPLACESEL`` and submitted by posting Enter, both delivered to the
control's message queue directly. The mouse is never moved and focus is never
taken. WM_SETTEXT does not update this client's send-enabled state and must
not be substituted here. A send is reported as submitted only once the input
box has emptied, which is the client's own account of having taken the
message; that is still not delivery, for which only a read-back of the new
message will do.

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
from typing import Callable, Iterable, Optional, Protocol, TypedDict, cast

from .errors import ErrorCode, KanaoError

MAIN_WINDOW_TITLE = "카카오톡"
INPUT_CLASS = "RICHEDIT50W"
_INPUT_HINT = "메시지 입력"
ROOM_LIST_PREFIX = "ChatRoomListCtrl"
SEARCH_LIST_PREFIX = "SearchListCtrl"
LIST_VIEW_PREFIX = "ChatRoomListView"

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
SEARCH_SETTLE_SECONDS = 0.1
"""How long the client is given to fill the result list once it is shown.

The early misses that made this 0.3 s clicked the hidden room list, not the
result list; once the result list is shown it already holds the matches.
"""
ROOM_SIZE = (380, 640)
"""The size a room window is given back when it was remembered as nothing."""
MIN_ROOM_SIZE = (200, 200)
"""Below this a remembered room window is the minimised one, not a real size."""
PARKED_POLLS = 10
"""Looks in a row a parked window must stay down before it is trusted to.

Down means minimised *and* not holding the keyboard. A room the client never
opened can be raised again later than this, which is why the interface keeps
re-parking while it waits for the room's key rather than this waiting longer:
1.5 s here was most of what opening a room cost.
"""
POLL_ATTEMPTS = 40
"""How long to keep looking before treating the row as the wrong one."""
SUBMIT_ATTEMPTS = 60
"""How many times to look for an emptied input box before giving up on a send.

With :data:`POLL_SECONDS` between looks this is three seconds, and it is a
ceiling rather than a cost: a client that has taken the message empties the
box before the first look.
"""


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


class UploadResult(TypedDict):
    """What a file send reports back.

    ``submitted`` says the client's own file-send confirmation closed after
    its send button was pressed, which is the client taking the file; it is
    no more proof of delivery than an emptied input box is for text.
    """

    submitted: bool
    room: str
    name: str
    size: int


FILE_BUTTON = (88, 24)
"""The toolbar's file button: from the room window's left edge, and up from its bottom.

Measured on a live 380x640 room window at 100% scaling on 2026-10-04: the row
under the input box holds +, the emoticon and the file buttons at 24, 56 and
88, 24 px above the bottom edge. It is owner-drawn, so like a room-list row it
can only be addressed by coordinate.
"""
FILE_POPUP_SIZE = (300, 185)
"""The client's "파일 전송" confirmation at 100% scaling, measured with the button below."""
FILE_POPUP_SEND = (150, 145)
"""The confirmation's "1개 전송" button, from the popup's top-left corner."""
DIALOG_CLASS = "#32770"
DIALOG_FILENAME_ID = 1148
"""The file name box of the Windows open-file dialog the file button shows."""
DIALOG_OPEN_ID = 1
DIALOG_CANCEL_ID = 2
UPLOAD_ATTEMPTS = 80
"""Looks for each step of a file send before giving up: four seconds at POLL_SECONDS."""


def scaled(point: tuple[int, int], dpi: int) -> tuple[int, int]:
    """A coordinate measured at 100% scaling, at a window's own scaling."""
    scale = (dpi if dpi > 0 else MEASURED_DPI) / MEASURED_DPI
    return round(point[0] * scale), round(point[1] * scale)


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
        search_row: Optional[int] = None,
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

    def foreground(self) -> Optional[int]:
        """The window that has the keyboard right now, if any."""
        ...

    def return_focus(self, handle: int) -> None:
        """Give the keyboard back to the window that had it before."""
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

    def search_rooms(self, text: str) -> Optional[int]:
        """Narrow the room list to *text* through the client's search box.

        Returns the list the results are drawn in, which is **not** the room
        list: the client swaps in a separate ``SearchListCtrl`` and hides the
        other, so a row clicked on the room list opens a room from the
        unfiltered order. None when there is no search box to type into or no
        result list appeared, which leaves nothing to click.
        """
        ...

    def clear_search(self) -> None:
        """Empty the search box again, so the list goes back to recency order."""
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
    return Path(root) / "kanao-cli" / "rows.json"


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

        With a hint, only two rows are guessed at: the one that worked before
        and the one the hint names. Past those the client's search box finds
        the room in one open, where alternating across the top rows opened
        every one of them — each a stranger's conversation marked read —
        whenever the screen's order disagreed with the catalog's, which it
        does for every pinned room. A hint past the searched rows is not
        guessed at at all. The sweep is kept only for a caller with no hint.
        """
        tried: list[int] = []

        def add(row: int) -> None:
            if 0 <= row < self._rows and row not in tried:
                tried.append(row)

        add(_recall_row(self._remembered, room))
        if hint is not None:
            add(hint)
            return tried
        for row in range(self._rows):
            add(row)
        return tried

    def _try_row(
        self, control: int, row: int, wanted: set[str], known: set[str], scroll: bool = True
    ) -> Optional[ChatWindow]:
        """Open one row; keep the window if it is the room, close it if not."""
        # Opening a room moves the list, so a row number only means
        # something after the list is put back at its first row.
        if scroll:
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
        found: Optional[ChatWindow] = None
        for window in opened:
            if found is None and window.title in wanted:
                # Parked rather than left in front: a room this opened was
                # not asked for by the person looking at the screen.
                self._park(window.handle)
                found = window
                continue
            # Opened by mistake: put the desktop back as it was.
            self._backend.close_window(window.handle)
        return found

    def _park(self, handle: int) -> None:
        """Minimise a window and keep it minimised while the client settles.

        The window appears before KakaoTalk has finished showing it, so a
        minimise sent the moment it appears can be undone by the client's own
        show a moment later: a room opened from the search results was left
        standing in front. It is watched until it has stayed down for
        :data:`PARKED_POLLS` looks in a row, and put down again if it rose.
        """
        self._backend.minimize_window(handle)
        is_minimized = getattr(self._backend, "is_minimized", None)
        if is_minimized is None:
            return
        steady = 0
        for _attempt in range(max(self._attempts, PARKED_POLLS * 2)):
            if is_minimized(handle) and not self._holds_keyboard(handle):
                steady += 1
                if steady >= PARKED_POLLS:
                    return
            else:
                steady = 0
                self._backend.minimize_window(handle)
            self._settle()

    def _holds_keyboard(self, handle: int) -> bool:
        foreground = getattr(self._backend, "foreground", None)
        return foreground is not None and foreground() == handle

    def repark(self, handle: int) -> None:
        """Put a parked window down again if the client has raised it since."""
        is_minimized = getattr(self._backend, "is_minimized", None)
        if is_minimized is None:
            return
        if not is_minimized(handle) or self._holds_keyboard(handle):
            self._backend.minimize_window(handle)

    def _search(
        self, room: str, wanted: set[str], known: set[str], row: int = 0
    ) -> tuple[bool, Optional[ChatWindow]]:
        """Type the title into the client's search box and open the first row.

        The filtered list still names nothing, so the first row is verified
        exactly like a guessed one — but it is one room at most, where
        sweeping the top of an unfiltered list was every room up there.
        Returns whether a row was opened at all, beside what it opened: a
        search that ran and missed must not be followed by guessing rows,
        each of which is another stranger's window on the screen.
        """
        search = getattr(self._backend, "search_rooms", None)
        if search is None:
            return False, None
        try:
            results = search(room)
            if results is None:
                return False, None
            return True, self._try_row(results, row, wanted, known, scroll=False)
        finally:
            clear = getattr(self._backend, "clear_search", None)
            if clear is not None:
                clear()

    def open(
        self,
        room: str,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
        search_row: Optional[int] = None,
    ) -> ChatWindow:
        """Open *room*, accepting any of *aliases* as its window title.

        The titles differ more often than it looks: the self-chat is stored
        under the account's own name while the reader shows it as 나와의 채팅,
        and a group with no custom title is named after its members by each
        side independently.
        """
        foreground = getattr(self._backend, "foreground", None)
        had_focus = foreground() if foreground is not None else None
        try:
            return self._open(room, aliases, hint, search_row)
        finally:
            # Whatever opened took the keyboard with it: the client activates
            # a room's window as it shows it, and the person was typing
            # somewhere else. It goes back to where it was.
            if had_focus and foreground is not None and foreground() != had_focus:
                self._backend.return_focus(had_focus)  # type: ignore[attr-defined]

    def _open(
        self,
        room: str,
        aliases: Iterable[str],
        hint: Optional[int],
        search_row: Optional[int] = None,
    ) -> ChatWindow:
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
            raise KanaoError(
                ErrorCode.WINDOW_NOT_FOUND,
                "KakaoTalk's room list is not on screen and the main window "
                "would not come back; restore it and try again",
            )

        known = {window.title for window in self._backend.chat_windows()}
        # The search first: it opens the room itself or, at worst, one room
        # whose title contains it. Guessing rows is kept for a client whose
        # search could not be used, because every wrong guess is somebody
        # else's window appearing on the screen and being closed again.
        # The search lists members' rooms too, so the room asked for is not
        # always the first result; the caller predicts which row it is.
        searched, found = self._search(room, wanted, known, search_row or 0)
        if not searched:
            for row in self._order(room, hint):
                found = self._try_row(control, row, wanted, known)
                if found is not None:
                    _learn_row(self._remembered, room, row)
                    break
        if found is not None:
            if disturbed is not None:
                self._backend.restore_main_window(disturbed)
            return found
        if disturbed is not None:
            self._backend.restore_main_window(disturbed)
        raise KanaoError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"{room!r} was not among the first {self._rows} rooms in the list "
            "and the client's search did not find it; open it once by hand",
        )


class FileBackend(WindowBackend, Protocol):
    """What a file send drives besides the input box: the client's own file path.

    A person sends a file by pressing the room's file button, choosing it in
    the Windows open-file dialog and pressing "1개 전송" in the client's
    confirmation. These are those three steps; nothing is injected and nothing
    of the client's is read or written beyond the windows a person sees.
    """

    def top_windows(self) -> set[int]:
        """Every visible top-level window the client owns, by handle."""
        ...

    def show_unfocused(self, handle: int) -> bool:
        """Restore a parked room without taking the keyboard; say if it was parked."""
        ...

    def is_minimized(self, handle: int) -> bool:
        """Whether a room is still parked, so its buttons have no position yet."""
        ...

    def park_quietly(self, handle: int) -> None:
        """Minimise without activating, for when the ordinary minimise was not taken."""
        ...

    def desktop_locked(self) -> bool:
        """Whether Windows is locked, so no dialog the client opens can be shown."""
        ...

    def minimize_window(self, handle: int) -> None:
        """Park a room again."""
        ...

    def foreground(self) -> Optional[int]:
        """The window holding the keyboard, so it can be handed back."""
        ...

    def return_focus(self, handle: int) -> None:
        """Hand the keyboard back to where the person left it."""
        ...

    def click_file_button(self, handle: int) -> None:
        """Press the room's file button."""
        ...

    def file_dialog(self, handle: int) -> Optional[int]:
        """The open-file dialog the client showed for this room, if it is up."""
        ...

    def choose_file(self, dialog: int, path: str) -> None:
        """Name *path* in the dialog and press Open; raise if the name did not take."""
        ...

    def cancel_dialog(self, dialog: int) -> None:
        """Dismiss the dialog, which sends nothing."""
        ...

    def file_popup(self, before: set[int]) -> Optional[int]:
        """The client's file-send confirmation, if one appeared since *before*."""
        ...

    def press_popup_send(self, popup: int) -> None:
        """Press the confirmation's send button."""
        ...

    def close_window(self, handle: int) -> None:
        """Close a window the way its own close button would."""
        ...

    def is_visible(self, handle: int) -> bool:
        """Whether a window still exists and is shown."""
        ...


class Sender:
    """Resolve a room's chat window and hand a message to its input."""

    def __init__(
        self,
        backend: WindowBackend,
        running: Callable[[], bool],
        opener: Optional[RoomOpener] = None,
        settle: Optional[Callable[[], None]] = None,
        attempts: int = SUBMIT_ATTEMPTS,
    ) -> None:
        self._backend = backend
        self._running = running
        self._opener = opener
        self._settle = settle if settle is not None else (lambda: time.sleep(POLL_SECONDS))
        self._attempts = attempts

    def resolve(
        self,
        room: str,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
        search_row: Optional[int] = None,
    ) -> ChatWindow:
        """Find the one open chat window for *room*, refusing to guess.

        The caller resolves catalog substrings first. A longer window title
        must never substitute for an unopened, already-resolved room.
        """
        if not room.strip():
            raise KanaoError(ErrorCode.INVALID_REQUEST, "a nonempty room name is required")
        if not self._running():
            raise KanaoError(
                ErrorCode.KAKAO_NOT_RUNNING,
                "KakaoTalk is not running; start it and open the conversation first",
            )
        windows = self._backend.chat_windows()
        wanted = {name.strip().casefold() for name in (room, *aliases) if name.strip()}
        matches = [window for window in windows if window.title.casefold() in wanted]

        if not matches:
            if open_if_needed and self._opener is not None:
                return self._opener.open(room, aliases, hint, search_row)
            raise KanaoError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"No open chat window matches {room!r}; open that conversation in "
                "KakaoTalk (double-click it so it pops out) and try again",
            )
        if len(matches) > 1:
            raise KanaoError(
                ErrorCode.AMBIGUOUS_CHAT,
                f"{len(matches)} open chat windows match {room!r}; use a more specific name",
                candidates=[window.title for window in matches],
            )
        return matches[0]

    def repark(self, window: ChatWindow) -> None:
        """Keep a room this opened minimised while the caller waits on it."""
        if self._opener is not None:
            self._opener.repark(window.handle)

    def _confirm_sent(self, control: int) -> None:
        """Wait for the input box to empty, which is what says the text left it.

        Posting Enter only puts a keystroke on the control's queue, so
        :meth:`WindowBackend.submit` returning says nothing about whether
        KakaoTalk acted on it. The box going empty is the client's own account
        of having taken the message, and reading it is something this module
        already does once before the write.

        An unreadable box is **not** an empty one. A cross-process read that
        times out is an unknown outcome, and reporting an unknown as submitted
        is the specific failure this exists to prevent, so the ``OSError`` is
        left to travel: the caller turns it into ``SEND_FAILED``, and a send
        that may have happened is never claimed to have.
        """
        for attempt in range(self._attempts):
            if not self._backend.get_text(control).strip():
                return
            if attempt + 1 < self._attempts:
                self._settle()
        raise OSError("the message stayed in the input box; it was not sent")

    def send(
        self,
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
        search_row: Optional[int] = None,
    ) -> SendResult:
        """Type *text* into *room*'s window and send it, or preview when dry.

        The message is validated and the window resolved before anything is
        typed. ``submitted`` says the input box emptied, which is the client's
        own account of having taken the message; it is still not proof of
        network delivery.
        """
        if not text or not text.strip():
            raise KanaoError(ErrorCode.INVALID_REQUEST, "refusing to send an empty message")
        if "\x00" in text:
            raise KanaoError(ErrorCode.INVALID_REQUEST, "message contains a null character")

        window = self.resolve(
            room,
            open_if_needed=open_if_needed,
            aliases=aliases,
            hint=hint,
            search_row=search_row,
        )
        control = self._backend.input_handle(window.handle)
        if control is None:
            raise KanaoError(
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
            self._confirm_sent(control)
        except OSError as error:
            raise KanaoError(
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


    def upload(
        self,
        room: str,
        path: str,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
        search_row: Optional[int] = None,
    ) -> UploadResult:
        """Send the file at *path* to *room* the way a person would.

        The room's file button, the open-file dialog it shows, and the
        client's own "파일 전송" confirmation. Until that confirmation's send
        button is pressed nothing has left, so every failure before it closes
        what was opened and says nothing was sent. After it, the only account
        the client gives is the confirmation closing; a confirmation that
        stays is closed and reported as an unknown outcome, never retried.

        The open-file dialog is a real window and takes the keyboard while it
        is up; the keyboard is handed back to where it was afterwards.
        """
        if not os.path.isfile(path):
            raise KanaoError(ErrorCode.INVALID_REQUEST, "there is no file at that path")
        size = os.path.getsize(path)
        if size <= 0:
            raise KanaoError(ErrorCode.INVALID_REQUEST, "refusing to send an empty file")
        # Every backend this is built with drives files too; the text-only
        # protocol is narrower only so the text tests need not fake files.
        backend = cast(FileBackend, self._backend)
        if backend.desktop_locked():
            raise KanaoError(
                ErrorCode.SEND_FAILED,
                "Windows is locked, so the client cannot show its open-file dialog; "
                "nothing was sent",
            )
        window = self.resolve(
            room,
            open_if_needed=open_if_needed,
            aliases=aliases,
            hint=hint,
            search_row=search_row,
        )
        previous = backend.foreground()
        parked = backend.show_unfocused(window.handle)
        try:
            if parked and self._wait_for(
                lambda: None if backend.is_minimized(window.handle) else window.handle
            ) is None:
                raise KanaoError(
                    ErrorCode.SEND_FAILED,
                    f"the window for {window.title!r} could not be restored to press its "
                    "file button; nothing was sent",
                    room=window.title,
                )
            return self._upload_into(backend, window, path, size)
        finally:
            if parked:
                backend.minimize_window(window.handle)
                # A posted minimise is asked of the client and can go untaken;
                # the room is never left standing where it was parked before.
                if self._wait_for(
                    lambda: window.handle if backend.is_minimized(window.handle) else None
                ) is None:
                    backend.park_quietly(window.handle)
            if previous is not None and previous != window.handle:
                backend.return_focus(previous)

    def _upload_into(
        self, backend: FileBackend, window: ChatWindow, path: str, size: int
    ) -> UploadResult:
        def nothing_sent(step: str) -> KanaoError:
            return KanaoError(
                ErrorCode.SEND_FAILED,
                f"{step} in {window.title!r}; nothing was sent",
                room=window.title,
            )

        before = backend.top_windows()
        try:
            backend.click_file_button(window.handle)
        except OSError as error:
            raise nothing_sent(f"the file button could not be pressed ({error})") from error
        dialog = self._wait_for(lambda: backend.file_dialog(window.handle))
        if dialog is None:
            raise nothing_sent("the file button showed no open-file dialog")
        try:
            backend.choose_file(dialog, path)
        except OSError as error:
            backend.cancel_dialog(dialog)
            raise nothing_sent(f"the file could not be named in the dialog ({error})") from error
        popup = self._wait_for(lambda: backend.file_popup(before))
        if popup is None:
            if backend.is_visible(dialog):
                backend.cancel_dialog(dialog)
            raise nothing_sent("the client showed no file-send confirmation")
        try:
            backend.press_popup_send(popup)
        except OSError as error:
            backend.close_window(popup)
            raise nothing_sent(f"the confirmation's send button could not be pressed ({error})") from error
        if self._wait_for(lambda: True if not backend.is_visible(popup) else None) is None:
            backend.close_window(popup)
            raise KanaoError(
                ErrorCode.SEND_FAILED,
                f"the file-send confirmation in {window.title!r} stayed open after its send "
                "button was pressed and was closed; whether the file went is unknown, so read "
                "the room back before trying again",
                room=window.title,
            )
        return {
            "submitted": True,
            "room": window.title,
            "name": os.path.basename(path),
            "size": size,
        }

    def _wait_for(self, look: Callable[[], Optional[int]]) -> Optional[int]:
        """Look up to UPLOAD_ATTEMPTS times, settling between looks."""
        for attempt in range(UPLOAD_ATTEMPTS):
            found = look()
            if found is not None:
                return found
            if attempt + 1 < UPLOAD_ATTEMPTS:
                self._settle()
        return None


# -- Win32 backend -----------------------------------------------------------

_WM_CLOSE = 0x0010
_WM_SYSCOMMAND = 0x0112
_SC_MINIMIZE = 0xF020
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
_SW_SHOWNOACTIVATE = 4
_HWND_BOTTOM = 1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEMOVE = 0x0200
_WM_SETTEXT = 0x000C
_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_BM_CLICK = 0x00F5
_GW_OWNER = 4
_DESKTOP_SWITCHDESKTOP = 0x0100
_POPUP_CLASS = "EVA_Window_Dblclk"
_POPUP_SLACK = 0.2
"""How far a confirmation may differ from FILE_POPUP_SIZE and still be it."""
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

    def _search_box(self) -> Optional[int]:
        """The search field inside the room-list view, shown or not.

        It exists while the search row is closed too — hidden, but sized —
        and typing into it swaps the result list in all the same. That
        matters because the magnifier that opens the row takes a posted click
        only while the main window is the active one, which this module never
        makes it. Only the view's own ``Edit`` counts: the main window holds
        others, 0x0 or belonging to other panes.
        """
        view = self._list_view()
        if view is None:
            return None
        found: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if self._class(handle) == "Edit":
                found.append(int(handle))
                return False
            return True

        self._user32.EnumChildWindows(view, self._proc(visit), 0)
        return found[0] if found else None

    def _replace(self, control: int, text: str) -> None:
        buffer = ctypes.create_unicode_buffer(text)
        self._message(control, 0x00B1, 0, -1)  # EM_SETSEL
        self._message(control, 0x00C2, 1, ctypes.addressof(buffer))  # EM_REPLACESEL

    def _search_results(self) -> Optional[int]:
        """The visible result list; the main window holds hidden ones too."""
        main = self._main_window()
        if main is None:
            return None
        found: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if self._title(handle).startswith(SEARCH_LIST_PREFIX) and self._user32.IsWindowVisible(
                handle
            ):
                found.append(int(handle))
                return False
            return True

        self._user32.EnumChildWindows(main, self._proc(visit), 0)
        return found[0] if found else None

    def _list_view(self) -> Optional[int]:
        main = self._main_window()
        if main is None:
            return None
        found: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if self._title(handle).startswith(LIST_VIEW_PREFIX):
                found.append(int(handle))
                return False
            return True

        self._user32.EnumChildWindows(main, self._proc(visit), 0)
        return found[0] if found else None

    def search_rooms(self, text: str) -> Optional[int]:
        box = self._search_box()
        if box is None:
            return None
        try:
            self._replace(box, text)
        except OSError:
            return None
        # The client filters as the box changes and swaps the result list in
        # when it has; a row clicked before that lands on the unfiltered list,
        # which measured as the most recent room opening instead.
        for _attempt in range(POLL_ATTEMPTS):
            results = self._search_results()
            if results is not None:
                time.sleep(SEARCH_SETTLE_SECONDS)
                return results
            time.sleep(POLL_SECONDS)
        return None

    def clear_search(self) -> None:
        # Emptied, never escaped: Escape in this box sends the main window to
        # the tray. An emptied box puts the room list back by itself.
        box = self._search_box()
        if box is None:
            return
        try:
            self._replace(box, "")
        except OSError:
            pass

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
            # SW_MINIMIZE activates the next window in line; this one was
            # not the active window, so nothing should be activated for it.
            self._user32.ShowWindow(handle, _SW_SHOWMINNOACTIVE)

    def is_minimized(self, handle: int) -> bool:
        return bool(self._user32.IsIconic(handle))

    def minimize_window(self, handle: int) -> None:
        # A window that took the keyboard is minimised the ordinary way, which
        # hands activation to the window behind it; SW_SHOWMINNOACTIVE leaves
        # a minimised window holding the keyboard, so typing went nowhere.
        # One that did not take it is minimised without being activated, so
        # it spends no time in front of whatever was there.
        # Asked of the window rather than done to it: a posted SC_MINIMIZE is
        # the minimise button, handled on the client's own thread after it
        # has finished showing — and activating — the room, so activation is
        # handed back the way a click on the button hands it back. Minimising
        # from here raced the client: done first, the client then activated
        # a window already minimised, which kept the keyboard from the
        # terminal; done synchronously, it waited 0.98 s on the client.
        self._user32.PostMessageW(handle, _WM_SYSCOMMAND, _SC_MINIMIZE, 0)
        self._repair_placement(handle)

    def foreground(self) -> Optional[int]:
        handle = self._user32.GetForegroundWindow()
        return int(handle) if handle else None

    def return_focus(self, handle: int) -> None:
        """Hand the keyboard back to the window that had it before a room opened.

        One plain request and nothing more: this only ever returns focus to
        where the person left it, and a refusal from Windows is accepted
        rather than worked around.
        """
        if self._user32.IsWindow(handle) and self._user32.IsWindowVisible(handle):
            self._user32.SetForegroundWindow(handle)

    def _repair_placement(self, handle: int) -> None:
        """Give a parked room somewhere on screen to come back to.

        A room closed while minimised is remembered by the client at the
        minimised coordinates, -32000,-32000 and 160x28, and reopens there:
        clicking it in the taskbar then restores it off every screen, and only
        maximising shows it. Measured live on the room this was found in. A
        normal position that touches no monitor is moved onto the main
        window's monitor, still minimised, so nothing comes to the front.
        """
        from ctypes import wintypes

        class _Placement(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT),
            ]

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", ctypes.c_uint),
            ]

        placement = _Placement()
        placement.length = ctypes.sizeof(_Placement)
        if not self._user32.GetWindowPlacement(handle, ctypes.byref(placement)):
            return
        normal = placement.rcNormalPosition
        width, height = normal.right - normal.left, normal.bottom - normal.top
        if (
            width >= MIN_ROOM_SIZE[0]
            and height >= MIN_ROOM_SIZE[1]
            and self._user32.MonitorFromRect(ctypes.byref(normal), 0)  # MONITOR_DEFAULTTONULL
        ):
            return
        anchor = self._main_window() or handle
        monitor = self._user32.MonitorFromWindow(anchor, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not monitor or not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return
        work = info.rcWork
        width = min(max(width, ROOM_SIZE[0]), work.right - work.left)
        height = min(max(height, ROOM_SIZE[1]), work.bottom - work.top)
        # Workspace coordinates: GetWindowPlacement's rectangle excludes the
        # taskbar, so the work area's own origin is the zero here.
        left = (work.right - work.left - width) // 2
        top = (work.bottom - work.top - height) // 2
        placement.rcNormalPosition = wintypes.RECT(left, top, left + width, top + height)
        placement.showCmd = _SW_SHOWMINNOACTIVE
        placement.flags = 0
        self._user32.SetWindowPlacement(handle, ctypes.byref(placement))

    def show_main_window(self) -> bool:
        """Bring the main window back, including from the tray.

        A window minimised to the tray is hidden rather than destroyed, so it
        is still enumerable — it just fails the visibility test every other
        lookup here applies. Showing it moves no cursor and posts no input.
        """
        for handle in self._all_top_level():
            if self._title(handle) != MAIN_WINDOW_TITLE:
                continue
            # Shown without being activated: SW_SHOW and SW_RESTORE both hand
            # the main window the keyboard, which is where it was found after
            # a room had been opened, instead of where the person was typing.
            if not self._user32.IsWindowVisible(handle) or self._user32.IsIconic(handle):
                self._user32.ShowWindow(handle, _SW_SHOWNOACTIVATE)
                # Beneath everything, too: shown on top, it was the window
                # behind a room when the room was minimised, so it — not the
                # terminal the person was typing in — was handed the keyboard.
                self._user32.SetWindowPos(
                    handle, _HWND_BOTTOM, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
                )
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

    # -- the file path: button, open-file dialog, confirmation ----------------

    def top_windows(self) -> set[int]:
        found: set[int] = set()

        def visit(handle: int, _param: int) -> bool:
            if self._pid_of(handle) == self._pid and self._user32.IsWindowVisible(handle):
                found.add(int(handle))
            return True

        self._user32.EnumWindows(self._proc(visit), 0)
        return found

    def show_unfocused(self, handle: int) -> bool:
        # A parked room's buttons are at -32000; restored without activation
        # they have a place to be clicked and the keyboard stays where it was.
        if not self._user32.IsIconic(handle):
            return False
        self._user32.ShowWindow(handle, _SW_SHOWNOACTIVATE)
        return True

    def is_visible(self, handle: int) -> bool:
        return bool(self._user32.IsWindow(handle) and self._user32.IsWindowVisible(handle))

    def park_quietly(self, handle: int) -> None:
        self._user32.ShowWindow(handle, _SW_SHOWMINNOACTIVE)

    def desktop_locked(self) -> bool:
        # The input desktop of a locked session is the secure one, which this
        # process cannot switch to: measured 2026-10-04 with the lock screen
        # up, when the client's open-file dialog never appeared.
        desktop = self._user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        try:
            return not self._user32.SwitchDesktop(desktop)
        finally:
            self._user32.CloseDesktop(desktop)

    def _rect(self, handle: int) -> tuple[int, int, int, int]:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(handle, ctypes.byref(rect)):
            raise OSError(f"GetWindowRect failed: {ctypes.get_last_error()}")
        return rect.left, rect.top, rect.right, rect.bottom

    def _post_click(self, handle: int, x: int, y: int) -> None:
        """Post a click at a point measured from the window's corner. No cursor moves."""
        from ctypes import wintypes

        left, top, _right, _bottom = self._rect(handle)
        point = wintypes.POINT(left + x, top + y)
        self._user32.ScreenToClient(handle, ctypes.byref(point))
        position = ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)
        for message, button in (
            (_WM_MOUSEMOVE, 0),
            (_WM_LBUTTONDOWN, _MK_LBUTTON),
            (_WM_LBUTTONUP, 0),
        ):
            if not self._user32.PostMessageW(handle, message, button, position):
                raise OSError(f"PostMessage(click) failed: {ctypes.get_last_error()}")

    def click_file_button(self, handle: int) -> None:
        _left, top, _right, bottom = self._rect(handle)
        x, up = scaled(FILE_BUTTON, self._dpi(handle))
        self._post_click(handle, x, (bottom - top) - up)

    def file_dialog(self, handle: int) -> Optional[int]:
        # The client's own open-file dialog is owned by the room that asked
        # for it; any other dialog the client has up is not this one.
        dialogs = [
            window for window in self.top_windows() if self._class(window) == DIALOG_CLASS
        ]
        owned = [
            window for window in dialogs if int(self._user32.GetWindow(window, _GW_OWNER) or 0) == handle
        ]
        if len(owned) == 1:
            return owned[0]
        return dialogs[0] if not owned and len(dialogs) == 1 else None

    def _dialog_child(self, dialog: int, class_name: str, control_id: int) -> Optional[int]:
        found: list[int] = []

        def visit(handle: int, _param: int) -> bool:
            if (
                self._class(handle) == class_name
                and int(self._user32.GetDlgCtrlID(handle)) == control_id
            ):
                found.append(int(handle))
                return False
            return True

        self._user32.EnumChildWindows(dialog, self._proc(visit), 0)
        return found[0] if found else None

    def _read(self, control: int) -> str:
        count = self._message(control, _WM_GETTEXTLENGTH)
        buffer = ctypes.create_unicode_buffer(count + 1)
        self._message(control, _WM_GETTEXT, count + 1, ctypes.addressof(buffer))
        return buffer.value

    def choose_file(self, dialog: int, path: str) -> None:
        # The dialog's name box is an ordinary Edit, so WM_SETTEXT is what it
        # answers to, and reading it back is how a wrong name is caught before
        # Open is pressed.
        edit = self._dialog_child(dialog, "Edit", DIALOG_FILENAME_ID)
        if edit is None:
            raise OSError("the open-file dialog has no file name box")
        buffer = ctypes.create_unicode_buffer(path)
        self._message(edit, _WM_SETTEXT, 0, ctypes.addressof(buffer))
        if self._read(edit) != path:
            raise OSError("the file name box did not take the path")
        button = self._dialog_child(dialog, "Button", DIALOG_OPEN_ID)
        if button is None:
            raise OSError("the open-file dialog has no Open button")
        if not self._user32.PostMessageW(button, _BM_CLICK, 0, 0):
            raise OSError(f"PostMessage(BM_CLICK) failed: {ctypes.get_last_error()}")

    def cancel_dialog(self, dialog: int) -> None:
        button = self._dialog_child(dialog, "Button", DIALOG_CANCEL_ID)
        if button is not None:
            self._user32.PostMessageW(button, _BM_CLICK, 0, 0)
        else:
            self._user32.PostMessageW(dialog, _WM_CLOSE, 0, 0)

    def file_popup(self, before: set[int]) -> Optional[int]:
        for handle in self.top_windows() - before:
            if self._class(handle) != _POPUP_CLASS or self._title(handle):
                continue
            left, top, right, bottom = self._rect(handle)
            width, height = scaled(FILE_POPUP_SIZE, self._dpi(handle))
            if (
                abs((right - left) - width) <= width * _POPUP_SLACK
                and abs((bottom - top) - height) <= height * _POPUP_SLACK
            ):
                return handle
        return None

    def press_popup_send(self, popup: int) -> None:
        x, y = scaled(FILE_POPUP_SEND, self._dpi(popup))
        self._post_click(popup, x, y)


def make_sender() -> Sender:
    """Build a sender wired to the running KakaoTalk process."""
    from .process import find_pid

    pid = find_pid()
    if pid is None:
        raise KanaoError(
            ErrorCode.KAKAO_NOT_RUNNING,
            "KakaoTalk is not running; start it and open the conversation first",
        )
    backend = Win32Backend(pid)
    opener = RoomOpener(backend, settle=lambda: time.sleep(POLL_SECONDS))
    return Sender(backend, running=lambda: find_pid() == pid, opener=opener)
