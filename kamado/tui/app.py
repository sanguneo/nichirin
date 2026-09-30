"""The interactive browser: a room list, a conversation, and time digests.

The shape follows OmO's interactive mode so the two feel like one toolchain:
a title row with status pinned right, hint rows beneath it, a body, and a
single footer line that gives up its middle before its edges.

Everything on screen is rebuilt from state on each keystroke. A conversation
this large cannot be diffed cheaply and does not need to be — one frame per
key is imperceptible and removes a whole class of stale-cell bugs.

Nothing here reads the database. Every query is a :class:`~.worker.Job` handed
to the one thread that owns the reader, and every answer is applied back on
this thread when the loop drains it — so a decryption in flight costs the
keyboard nothing, and an answer to a tab that has since been closed is
dropped rather than drawn into whatever is standing in its place.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol, Sequence, TYPE_CHECKING

from .. import __version__
from ..errors import ErrorCode, KamadoError
from ..model import (
    COVERED_PLACEHOLDER,
    ERASED_PLACEHOLDER,
    FEED_MESSAGE,
    Friend,
    Message,
    Notice,
    Room,
    is_feed_sentence,
    message_kind,
)
from ..reader import Reader
from . import digest
from . import images
from .worker import (
    ATTACHMENTS,
    DatabaseWorker,
    FEEDS,
    FRIENDS,
    NAMES,
    NOTICE,
    OLDER,
    OPEN,
    POLL,
    RELEASE,
    REVEAL,
    REVISIONS,
    ROOMS,
    UNWATCH,
    Changed,
    DatabaseOwner,
    Done,
    Job,
)
from .console import (
    CLEAR_BOX,
    SELECT_MODE,
    SHOW_PHOTO,
    TOGGLE_TIMESTAMP,
    Console,
    Key,
    force_size,
    mouse_default,
)
from .layout import (
    ELLIPSIS,
    TAB_SEPARATOR,
    Segment,
    clean,
    display_width,
    elide_head,
    pad_to_width,
    plan_footer,
    plan_tabs,
    strip_ansi,
    truncate_to_width,
    window_around,
    wrap_to_width,
)
from ..watch import Watcher
from .theme import SpeakerPalette, Theme, _detect_dark_mode

if TYPE_CHECKING:
    from ..names import NameRecord
    from ..macauto import MessageSender

NEWLINE_MARK = "↵"
"""How a line break inside a draft is shown in a one-line composer."""

KST = timezone(timedelta(hours=9))
HISTORY_WINDOW = 400
LOAD_MORE_STEP = 400
KEY_WAIT_SECONDS = 0.12
"""How long a frame waits for input before checking for new messages."""
UNCERTAIN_AFTER_SECONDS = 30.0
"""How long a send may go unseen before the mark stops claiming to be sure."""
DELIVERED_TIMEOUT_SECONDS = 1.6
"""How long the '전송 완료' indicator remains visible before reverting to a clean divider."""
NOTICE_TIMEOUT_SECONDS = 1.6
"""How long a self-clearing notice stays up, matching the delivery tick."""
_MEDIA_WITHOUT_PREVIEW = {3: "[영상]"}
"""Attachments named rather than shown, because this program cannot preview them.

The client stores its own word for these in the message text -- a video arrives
as the plain text "동상열" -- which reads as if somebody typed it.
"""
INLINE_CACHE_ENTRIES = 64
"""How many built inline-image escapes to keep.

Only sixel and iTerm2 need these, because neither leaves the picture with the
terminal. Scrolling asks for a new crop every line, so the keys are unbounded
and something has to be dropped; 64 covers a picture moving through one
conversation several times over without letting one room's photos grow without limit.
"""

CLICK_HINT = "드래그 복사"
SELECT_HINT = "클릭 복귀"
"""What Ctrl+G does next, which is the half of the switch not currently on."""
SELECT_NOTICE = "선택 모드: 드래그로 긁고 ⌘C 복사 · ^G 로 사진·링크 클릭 복귀"
CLICK_NOTICE = "클릭 모드: 사진·링크 클릭 · ^G 로 드래그 복사"
"""Said on the switch itself, because the hint row alone never explains why."""

WHEEL_LINES = 3
"""How far one wheel notch moves the view, matching a terminal's own step."""
REFRESH_SECONDS = 0.15
"""How often to look for arrivals. A poll that finds nothing costs one stat."""
TYPING_GRACE_SECONDS = 0.4
"""How long after a keystroke to leave the conversation alone."""
COMPOSING_GRACE_SECONDS = 3.0
"""How long a half-written message holds a poll off, IME pauses included."""
ATTACHMENT_LIST_LIMIT = 200
"""How many of the newest messages ``/files``, ``/links`` and ``/photos`` look through."""
EDITS_SHOWN = 20
"""How many edited messages ``/edits`` lists, newest first."""
_ATTACHMENT_TITLES = {"file": "파일", "link": "링크", "photo": "사진", "video": "동영상"}


class PhotoSource(Protocol):
    """The photo capability the interface consumes."""

    def fetch(self, url: str) -> Optional[bytes]: ...


@dataclass
class ChatState:
    """What is loaded and where the viewport sits inside it."""

    room: Room
    messages: list[Message] = field(default_factory=list)
    offset: int = 0
    """Lines scrolled up from the newest message."""
    draft: str = ""
    """What the composer holds; a leading slash makes it a command."""
    caret: int = 0
    """Where the next character goes, counted in characters, not columns."""
    slices: list[digest.Slice] = field(default_factory=list)
    slice_label: str = ""
    slice_span: Optional[timedelta] = None
    """The span the digest was cut at, so it can be cut again when a message goes."""
    notice: str = ""
    notice_until: float = 0.0
    """When a passing notice stops showing; 0 keeps it until something replaces it.

    Most notices answer a question the reader asked and belong on screen until
    the next thing happens. A confirmation that a link opened is not one of
    those: the browser is already in front of them, so the line has done its job
    and should step aside the way the delivery tick does.
    """
    unseen: int = 0
    """Arrivals that landed below the viewport while the reader was scrolled up."""
    anchor_top: bool = True
    """Show the body from its first line once, when its height is known."""
    outbox: Optional[str] = None
    """Text handed to the client and not yet seen coming back."""
    outbox_at: float = 0.0
    """When that text was handed over, so waiting too long can be admitted."""
    delivered: bool = False
    """Whether the last thing sent has since appeared in the conversation."""
    delivered_at: float = 0.0
    """When delivery was confirmed, so the indicator can revert to a clean divider."""
    window_alias: Optional[str] = None
    """The title this room's window carries, when it differs from its own.

    Read when the room is opened rather than when a message is sent: the
    catalog is rewritten constantly, and re-reading it cost 3.0 s per send.
    """
    feeds: dict[int, str] = field(default_factory=dict)
    """What each feed row in this window says about itself, asked once."""
    token: int = 0
    """What this tab is called while it is open, so an answer can find it.

    A room closed and reopened is a different tab holding the same room, and
    a read the first one asked for must not land in the second.
    """
    rendered: Optional[tuple[tuple[int, int, int, frozenset, bool], list[str]]] = None
    """Transcript lines and what they were built from, so typing costs nothing."""
    versions: dict[int, int] = field(default_factory=dict)
    """What each held row contained when its message was last read, by id.

    A message on screen can change without a new one arriving - deleted for
    everyone, it keeps its id - so this is what a moved database is compared
    with. A message with no version yet is simply read again once.
    """
    revision: int = 0
    """Bumped whenever a held message is replaced or removed.

    The transcript is cached by message count, and a message deleted in
    place changes no count: without this the screen would keep the text.
    """
    message_rows: dict[int, int] = field(default_factory=dict)
    """Which transcript row each message starts on, rebuilt with it."""
    opened_photos: set[int] = field(default_factory=set)
    """Message ids whose picture is currently shown instead of ``[사진]``."""
    offset_before_photo: dict[int, int] = field(default_factory=dict)
    """Where the viewport sat before a picture was opened, to restore on close.

    Opening a photo can scroll the conversation to keep the label on screen, and
    that scroll is the interface's doing rather than the reader's, so closing
    the photo has to give the position back.
    """
    photo_rows: dict[int, int] = field(default_factory=dict)
    """Which message each transcript row belongs to, for the rows a photo owns.

    Built while the transcript is rendered, because that is the only place the
    mapping from a screen row back to a message is known.
    """
    room_notice: Optional[Notice] = None
    """The room's pinned notice, asked for once when the room is opened."""
    panel: list[tuple[str, str]] = field(default_factory=list)
    """A list shown in place of the conversation: ``(style, text)`` rows.

    What ``/notice``, ``/files``, ``/links``, ``/photos``, ``/videos`` and
    ``/edits`` put on screen. Escape takes it away, as it does a digest.
    """
    panel_title: str = ""


class App:
    """Owns the screen state and turns keystrokes into it."""

    def __init__(
        self,
        reader: Optional[Reader] = None,
        theme: Optional[Theme] = None,
        sender: Optional["MessageSender"] = None,
        db: Optional[DatabaseOwner] = None,
    ) -> None:
        self.reader = reader
        """Kept for a caller that owns one; nothing here ever queries it."""
        self._db = db if db is not None else DatabaseOwner(reader)
        """The one way to the database, and the one way back from it."""
        self._db.sink = self._settle
        self._dirty = False
        """Whether an answer has landed that the screen has not shown yet."""
        self._catalogued = False
        """Whether the catalog has ever been read, so a first failure speaks."""
        self._friends_asked = False
        self._cleanup: dict[tuple[str, Optional[int]], Job] = {}
        """Coalesced teardown for closed tabs, retried when the queue has room."""
        self._token = 0
        self._opening: dict[int, int] = {}
        """Rooms being opened, by the token of the tab they will become.

        A room is read before its tab exists, and in that time the reader can
        change their mind — go back to the conversations they had open, or to
        the friend list. What they asked for is forgotten here, so the answer
        arrives to nobody rather than to a screen that has moved on.
        """
        self._last_width = 80
        self.theme = theme if theme is not None else Theme(dark=_detect_dark_mode())
        self._sender = sender
        """Built on the first send so browsing never touches the desktop."""
        self.rooms: list[Room] = []
        self.selected = 0
        self.scroll = 0
        self.chats: list[ChatState] = []
        """Conversations held open side by side, in the order they were opened."""
        self.focus = 0
        """Which of them the keyboard and the composer belong to."""
        self.palette = SpeakerPalette(dark=self.theme.dark)
        self.status = ""
        self.mouse_clicks = mouse_default()
        """Whether the terminal reports buttons here instead of selecting text.

        Held rather than asked of the console, so every caller — the loop that
        carries it to the terminal and the frame that says which mode is on —
        reads one answer, and a test can drive the switch without a terminal.
        """
        self.show_timestamps = True
        """Whether timestamps are shown on each message in the transcript."""
        self.seen_db_unreads: dict[int, int] = {}
        """Last observed DB unread count when each room was viewed in kamado."""
        self.running = True
        self.zone = KST
        self.drafts: dict[int, str] = {}
        """Unsent text per room, so leaving a conversation does not lose it."""
        self.titles: list[str] = []
        """Every catalogued title, so a send never has to re-read the catalog."""
        self.friends: list[Friend] = []
        self.friend_selected = 0
        self.friend_scroll = 0
        self.show_friends = False
        self.filter = ""
        """What the list is narrowed by; empty means the whole list."""
        self.filtering = False
        """Whether typing goes to the filter instead of the shortcuts."""
        self.picking = False
        """Whether the room list is up in front of the conversations.

        Opening a second tab has to start somewhere, and every way back to
        the list otherwise closes the conversation standing in front of it.
        """
        self.pending_send: Optional[str] = None
        self.reveal_covered = False
        """Whether the owner's reader was last told to show what SafeBot covered."""
        self._clock = time.monotonic
        """Read rather than called directly, so a test can hold time still."""
        """Text the last keystroke asked to send, not sent yet.

        Sending can open a window, which took 1.7 s with the screen frozen on
        the frame before the keystroke. Holding it here lets the loop paint
        once, so the wait is visible rather than mistaken for a hang.
        """
        self.cursor: Optional[tuple[int, int]] = None
        """Where the terminal caret belongs, so an IME composes in the box."""
        self._photos: PhotoSource = images.PhotoStore()
        self._protocol = images.detect()
        """Which inline-image protocol this terminal draws, or ``none``.

        Sixel cannot be recognised from the environment, so a ``none`` here is
        provisional until :meth:`_measure_cell` has asked the terminal itself.
        """
        self._image_ids = images.ImageIds()
        self._images_moved = False
        """Whether an inline picture moved, so the frame has to be given up."""
        self._inline_cache: dict[
            tuple[str, int, int, int, Optional[tuple[int, int]]], Optional[str]
        ] = {}
        """Inline escapes already built, keyed by the box they were built for."""
        self.placements: list[
            tuple[int, int, int, bytes, int, int, Optional[tuple[int, int]]]
        ] = []
        """What to draw over the painted frame: id, row, column, bytes, cells, crop.

        Rebuilt every frame, because a photo's row moves whenever the
        conversation scrolls and a placement is addressed by screen position.
        """
        self._drawn: dict[int, tuple[int, int, int, int, Optional[tuple[int, int]]]] = {}
        """Where each message's picture currently sits, so a still screen redraws nothing."""
        self._transmitted: set[int] = set()
        """Whose bytes the terminal already holds, so a move costs no re-upload."""
        self._shapes: dict[int, tuple[int, int]] = {}
        """Each picture's measured pixel size, read once from its own bytes."""
        self._cell_aspect = images.CELL_ASPECT
        """How many times taller than wide a cell is, once the terminal says."""
        self._bytes: dict[int, bytes] = {}
        """Drawable bytes per picture, so a conversion happens once."""
        self._framed: dict[int, tuple[tuple[int, int], bytes]] = {}
        """Letterboxed bytes and the pixel box they were padded to."""
        self._cell: Optional[tuple[int, int]] = None
        """The terminal's cell size in pixels, when it answered the question.

        Needed to letterbox a picture to an exact pixel box; without it the
        ratio can only be approximated through whole cells.
        """
        self._body_rows = 0
        """How many rows the body was given on the last painted frame."""
        self._body_geometry: Optional[tuple[int, int, int]] = None
        """The painted body: its first row, its row count, and its width.

        A click arrives as a screen cell and means nothing until it is measured
        against the frame that is actually on screen, which only the render knows.
        """
        self._tab_geometry: list[tuple[int, int, int]] = []
        """Where each drawn tab starts and ends, for a click that lands on one."""
        self._tab_row: Optional[int] = None
        """Which row the strip was painted on, or None when there is no strip."""
        self._tab_drag_start: Optional[int] = None
        """Column where mouse button went down on the tab strip."""

    @property
    def watcher(self) -> Optional[Watcher]:
        """The one watcher for every open room, held by the owner thread.

        The fingerprint a poll checks covers the whole account, so following a
        second conversation costs no second decryption — and a decryption is
        the most expensive thing this loop can be made to do.

        What comes back belongs to the owner thread. Holding the reference is
        safe; asking it questions is for a caller that knows the owner is idle,
        which in practice means a test. Nothing in this class does.
        """
        return self._db.watcher

    @property
    def chat(self) -> Optional[ChatState]:
        """The conversation the keyboard is in, of the ones held open."""
        if not self.chats:
            return None
        self.focus = max(0, min(self.focus, len(self.chats) - 1))
        return self.chats[self.focus]

    @chat.setter
    def chat(self, value: Optional[ChatState]) -> None:
        """Assigning None closes every tab; assigning one leaves only it."""
        self.chats = [] if value is None else [value]
        self.focus = 0

    # -- asking ---------------------------------------------------------------

    def _ask(self, job: Job) -> bool:
        """Hand one piece of database work to the thread that owns it."""
        return self._db.submit(job)

    def _queue_cleanup(self, job: Job) -> None:
        """A full queue may defer teardown, but must not lose it."""
        if not self._ask(job):
            self._cleanup[(job.kind, job.room_id)] = job

    def _pump(self, width: int, timeout: Optional[float] = None) -> bool:
        """Apply every answer that has come back. Returns whether to redraw.

        The width is recorded rather than passed on because an answer is
        absorbed after the frame that painted it, and the transcript it has to
        be measured against is the one the screen is actually showing.
        """
        self._last_width = width
        self._db.drain(timeout)
        wanted = {chat.room.id for chat in self.chats} | set(self._opening.values())
        for key, job in tuple(self._cleanup.items()):
            # A later open supersedes deferred teardown. Retrying it after
            # that open would silently stop arrivals in the new tab.
            if (job.kind == UNWATCH and job.room_id in wanted) or (
                job.kind == RELEASE and wanted
            ):
                self._cleanup.pop(key)
                continue
            if not self._ask(job):
                break
            self._cleanup.pop(key)
        drawn, self._dirty = self._dirty, False
        return drawn

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def _tab(self, token: int) -> Optional[ChatState]:
        """The tab that asked, or None once it has been closed."""
        return next((chat for chat in self.chats if chat.token == token), None)

    def _settle(self, done: Done) -> None:
        """Apply one finished job, on the thread that draws the screen.

        Written out rather than looked up by name: these are the only answers
        there are, and an answer this screen has no place for should fail here
        rather than be dispatched to whatever happens to share its name.
        """
        kind = done.job.kind
        if kind == ROOMS:
            drawn = self._settle_rooms(done)
        elif kind == OPEN:
            drawn = self._settle_open(done)
        elif kind == POLL:
            drawn = self._settle_poll(done)
        elif kind == OLDER:
            drawn = self._settle_older(done)
        elif kind == FRIENDS:
            drawn = self._settle_friends(done)
        elif kind == FEEDS:
            drawn = self._settle_feeds(done)
        elif kind == NAMES:
            drawn = self._settle_names(done)
        elif kind == NOTICE:
            drawn = self._settle_notice(done)
        elif kind == ATTACHMENTS:
            drawn = self._settle_attachments(done)
        elif kind == REVISIONS:
            drawn = self._settle_revisions(done)
        elif kind == REVEAL:
            drawn = self._settle_reveal(done)
        elif kind in (UNWATCH, RELEASE):
            # Nothing to draw: what they were about is already off the screen.
            drawn = False
        else:
            raise ValueError(f"nothing on screen is waiting for {kind!r}")
        self._dirty = drawn or self._dirty

    # -- data ---------------------------------------------------------------

    def load_rooms(self) -> None:
        """Ask for the catalog; it reaches the screen when it is read."""
        self._ask(Job(ROOMS))

    def _settle_rooms(self, done: Done) -> bool:
        """Take the catalog, keeping the room the cursor was on.

        One settler for both the first read and every refresh after it: the
        two differ only in that the first has a status line to answer for and
        always has something new to draw.
        """
        if done.error is not None:
            # Worth saying only the first time. After that the list is on
            # screen already, and a refresh that failed is not news.
            if self._catalogued:
                return False
            self.status = _said(done.error)
            return True
        catalogued = done.value
        self.titles = [room.title for room in catalogued]
        rooms = [room for room in catalogued if room.readable]
        if self.chat is not None and self.chat.offset == 0:
            self._mark_room_seen(self.chat.room.id)
        changed = [(room.id, self._effective_unread(room), room.last_log_id) for room in rooms] != [
            (room.id, self._effective_unread(room), room.last_log_id) for room in self.rooms
        ]
        selected_id = self.rooms[self.selected].id if self.rooms else None
        self.rooms = rooms
        if selected_id is not None:
            for index, room in enumerate(rooms):
                if room.id == selected_id:
                    self.selected = index
                    break
        self.selected = max(0, min(self.selected, len(self.rooms) - 1))
        first = not self._catalogued
        self._catalogued = True
        if first:
            self.status = (
                ""
                if self.rooms
                else "읽을 수 있는 대화방이 없습니다. 카카오톡에서 대화를 열어주세요."
            )
        return changed or first

    def visible_rooms(self) -> list[Room]:
        """The rooms the filter leaves, or all of them."""
        if not self.filter:
            return self.rooms
        needle = self.filter.casefold()
        return [room for room in self.rooms if needle in room.title.casefold()]

    def _effective_unread(self, room: Room) -> int:
        """Unread count for a room, honoring what has been viewed in this interface."""
        chat = next((c for c in self.chats if c.room.id == room.id), None)
        if chat is not None:
            return chat.unseen
        if room.id in self.seen_db_unreads:
            seen = self.seen_db_unreads[room.id]
            if room.unread < seen:
                self.seen_db_unreads[room.id] = room.unread
                return room.unread
            return max(0, room.unread - seen)
        return room.unread

    def _mark_room_seen(self, room_id: int) -> None:
        """Mark a room's current unread messages as viewed in this interface."""
        room = next((r for r in self.rooms if r.id == room_id), None)
        if room is not None:
            self.seen_db_unreads[room_id] = room.unread

    def visible_friends(self) -> list[Friend]:
        """The friends the filter leaves, or all of them.

        229 of them were reachable only by holding down an arrow key.
        """
        if not self.filter:
            return self.friends
        needle = self.filter.casefold()
        return [friend for friend in self.friends if needle in friend.name.casefold()]

    def _narrow(self, text: str) -> None:
        self.filter = text
        # A narrower list can no longer hold the old selection.
        self.selected = max(0, min(self.selected, len(self.visible_rooms()) - 1))
        self.friend_selected = max(
            0, min(self.friend_selected, len(self.visible_friends()) - 1)
        )

    def _forget_filter(self) -> None:
        self.filtering = False
        self._narrow("")

    def _filter_key(self, kind: str, text: str) -> bool:
        """Take the keystroke when it belongs to the filter.

        A letter has to stay a shortcut in a list, so the filter is asked for
        with a slash rather than taking every key the moment a list is shown.
        """
        if not self.filtering:
            return kind == "char" and text == "/" and self._start_filter()
        if kind == "char":
            self._narrow(self.filter + text)
            return True
        if kind == Key.BACKSPACE:
            self._narrow(self.filter[:-1])
            return True
        if kind == Key.ESCAPE:
            self._forget_filter()
            return True
        return False

    def _start_filter(self) -> bool:
        self.filtering = True
        self._narrow("")
        return True

    def load_friends(self) -> None:
        self._friends_asked = self._ask(Job(FRIENDS))

    def _settle_friends(self, done: Done) -> bool:
        if done.error is not None:
            self.status = _said(done.error)
            return True
        self.friends = done.value
        return True

    def open_friend(self, friend: Friend) -> None:
        """Open a friend's 1:1 room, if the client has cached its key."""
        if friend.direct_chat_id is None:
            self.status = f"{friend.name}: 1:1 대화방이 로컬에 없습니다"
            return
        room = next((r for r in self.rooms if r.id == friend.direct_chat_id), None)
        if room is None:
            self.status = f"{friend.name}: 카카오톡에서 대화방을 먼저 열어주세요"
            return
        self.show_friends = False
        self.open_room(room)

    def open_room(self, room: Room) -> None:
        """Open a room as a tab of its own, keeping the ones already open.

        Opening a conversation that is already on screen moves the keyboard to
        it rather than showing it twice.
        """
        already = next(
            (index for index, chat in enumerate(self.chats) if chat.room.id == room.id),
            None,
        )
        if already is not None:
            self.focus = already
            chat = self.chats[already]
            if chat.offset == 0:
                chat.unseen = 0
                self._mark_room_seen(room.id)
            return
        if room.id in self._opening.values():
            # Already on its way; asking twice would read the room twice.
            return
        token = self._next_token()
        self._opening[token] = room.id
        if not self._ask(Job(OPEN, room_id=room.id, token=token, payload=(room, HISTORY_WINDOW))):
            self._opening.pop(token)
            self.status = "DB 작업이 밀려 있습니다. 잠시 후 다시 열어주세요."

    def _cancel_opens(self) -> None:
        """Stop waiting for rooms that were asked for and then left behind."""
        for room_id in self._opening.values():
            self._queue_cleanup(Job(UNWATCH, room_id=room_id))
        self._opening.clear()

    def _settle_open(self, done: Done) -> bool:
        """Put the room on screen, once its window has been read.

        Following it started on the owner thread in the same job, so there is
        no gap between what this window holds and where the watcher resumes.
        """
        room, _limit = done.job.payload
        wanted = self._opening.pop(done.job.token, None) is not None
        if not wanted:
            # Asked for, then left behind. Neither the tab nor the complaint
            # belongs on a screen that has moved on.
            return False
        if done.error is not None:
            self.status = _said(done.error)
            return True
        if any(chat.room.id == room.id for chat in self.chats):
            # Asked for twice before the first answer came back; the keyboard
            # has already been moved to it and a second tab would be a second
            # viewport over one conversation.
            return False
        messages, stored, pinned = done.value
        if not self.chats:
            # Tints are kept across a tab's lifetime: rebuilding the palette
            # while another conversation is on screen would recolour it.
            self.palette = SpeakerPalette(dark=self.theme.dark)
        for message in messages:
            self.palette.hue(message.sender)
        kept = self.drafts.get(room.id, "")
        # A conversation opens at its newest line, not at its oldest.
        self.chats.append(
            ChatState(
                room=room,
                token=done.job.token,
                messages=messages,
                anchor_top=False,
                draft=kept,
                caret=len(kept),
                window_alias=stored if stored and stored != room.title else None,
                room_notice=pinned,
            )
        )
        self.focus = len(self.chats) - 1
        self.picking = False
        self._forget_filter()
        self.status = ""
        self._mark_room_seen(room.id)
        return True

    def refresh(self, width: int) -> bool:
        """Ask for anything new, and take whatever has come back.

        The asking costs this thread nothing: the poll is run by the owner
        thread, and a second one is refused while the first is still in the
        database rather than queued behind it. What returns is whatever had
        finished by now, which on a busy account is the poll before this one.
        """
        self._last_width = width
        # What landed before this call was somebody else's doing — opening a
        # room, loading older history — and they redrew for it themselves.
        # This answers for the poll it is about to ask for.
        self._dirty = False
        self._ask(Job(ROOMS) if not self.chats else Job(POLL, payload=self._held()))
        return self._pump(width)

    def _held(self) -> tuple:
        """What every tab holds, copied for the owner thread to compare.

        A copy, because the owner reads it while this thread goes on
        changing the tabs; ids and versions are all it needs.
        """
        return tuple(
            (
                chat.room.id,
                tuple((message.id, chat.versions.get(message.id)) for message in chat.messages),
            )
            for chat in self.chats
        )

    def _settle_poll(self, done: Done) -> bool:
        """Hand each arrival to the room it was written in.

        One poll answers for every open conversation, so what comes back is
        mixed. A message for a room whose tab has since been closed belongs
        to nobody on screen and is dropped here.
        """
        if done.error is not None:
            if self.chats:
                self.chats[min(self.focus, len(self.chats) - 1)].notice = _said(done.error)
            return True
        arrived = done.value
        changed = getattr(arrived, "changed", {})
        if (not arrived and not changed) or not self.chats:
            return False
        width = self._last_width
        drawn = False
        for chat in self.chats:
            mine = [message for message in arrived if message.room_id == chat.room.id]
            if mine and self._absorb(chat, mine, width):
                drawn = True
            change = changed.get(chat.room.id)
            if change is not None and self._reconcile(chat, change, width):
                drawn = True
        return drawn

    def _reconcile(self, chat: ChatState, change: Changed, width: int) -> bool:
        """Bring messages already on screen up to date with a database that moved.

        The watcher only asks for ids above the newest it has seen, so a
        message deleted for everyone - which keeps its id and has its text
        replaced - stayed on screen as it was, and so did anything built from
        it: the digest, the export. The owner thread compared the held rows
        and read again only those that changed; this puts the answer on screen.
        """
        chat.versions.update(change.versions)
        by_id = {message.id: message for message in chat.messages}
        removed = {one for one in change.removed if one in by_id}
        rebuilt = {
            message.id: message
            for message in change.rebuilt
            if message.id in by_id and by_id[message.id] != message
        }
        if not removed and not rebuilt:
            # A row whose version was simply not known yet - an arrival, or a
            # window just opened - says nothing new; the screen stays as it is.
            return False

        anchor = self._bottom_anchor(chat, width) if chat.offset > 0 else None
        chat.messages = [
            rebuilt.get(message.id, message)
            for message in chat.messages
            if message.id not in removed
        ]
        for one in removed | set(rebuilt):
            chat.feeds.pop(one, None)
        for one in removed:
            chat.versions.pop(one, None)
            chat.opened_photos.discard(one)
        for one, message in rebuilt.items():
            if message.photo is None:
                chat.opened_photos.discard(one)
        chat.revision += 1
        if chat.slices and chat.slice_span is not None:
            # Cut again from what is left, so a deleted line is not summarised.
            chat.slices = digest.slice_messages(_spoken(chat.messages), chat.slice_span)
        if anchor is not None:
            self._restore_anchor(chat, width, anchor)
        return True

    def _bottom_anchor(self, chat: ChatState, width: int) -> Optional[tuple[int, int, list[int]]]:
        """Which message the last visible line belongs to, and how far into it.

        The offset counts lines up from the bottom, so a message that changed
        below the viewport would move the text being read. Pinning the last
        visible line to the message it is part of keeps it where it was.
        """
        lines = self._transcript_of(chat, width)
        bottom = len(lines) - min(chat.offset, len(lines))
        order = [message.id for message in chat.messages if message.id in chat.message_rows]
        starts = [chat.message_rows[one] for one in order]
        if not starts or bottom <= 0:
            return None
        index = bisect_right(starts, bottom - 1) - 1
        if index < 0:
            return None
        return order[index], bottom - starts[index], order[index:]

    def _restore_anchor(
        self, chat: ChatState, width: int, anchor: tuple[int, int, list[int]]
    ) -> None:
        """Put the anchored line back at the bottom of the viewport.

        A message that is gone hands its place to the next one that is not.
        """
        owner, depth, after = anchor
        lines = self._transcript_of(chat, width)
        rows = chat.message_rows
        if owner in rows:
            following = [rows[one] for one in after[1:] if one in rows]
            limit = following[0] if following else len(lines)
            bottom = min(rows[owner] + depth, limit)
        else:
            bottom = next((rows[one] for one in after if one in rows), len(lines))
        chat.offset = max(0, len(lines) - bottom)

    def _absorb(self, chat: ChatState, arrived: Sequence[Message], width: int) -> bool:
        """Give one conversation what genuinely is new to it."""
        known = {message.id for message in chat.messages}
        fresh = [message for message in arrived if message.id not in known]
        if not fresh:
            return False
        self._confirm_delivery(chat, fresh)

        before = len(self._transcript_of(chat, width))
        for message in fresh:
            self.palette.hue(message.sender)
        chat.messages.extend(fresh)

        if chat.offset > 0:
            # The reader is up in the history; hold their place rather than
            # yanking the viewport down to the newest line.
            chat.offset += len(self._transcript_of(chat, width)) - before
        if chat.offset > 0 or chat is not self.chat or self.picking:
            # Arrivals nobody has seen: the viewport is up in this
            # conversation's history, or this is a tab the reader is not on,
            # or the reader is in the room list picking a room.
            chat.unseen += len(fresh)
        else:
            chat.unseen = 0
            self._mark_room_seen(chat.room.id)
        return True

    def _confirm_delivery(self, chat: ChatState, arrived: Sequence[Message]) -> None:
        """Turn the queued mark into a tick once the client writes it back."""
        if chat.outbox is None:
            return
        # Compared by its words: the client is free to tidy the spacing of
        # what it stores, and a tick that never arrives is worse than one
        # that arrives for a message wearing slightly different whitespace.
        wanted = _same_words(chat.outbox)
        if any(message.from_me and _same_words(message.text) == wanted for message in arrived):
            chat.outbox = None
            chat.delivered = True
            chat.delivered_at = self._clock()

    def load_older(self) -> None:
        chat = self.chat
        if chat is None or not chat.messages:
            return
        self._ask(
            Job(
                OLDER,
                room_id=chat.room.id,
                token=chat.token,
                payload=(chat.messages[0].id, LOAD_MORE_STEP),
            )
        )

    def _settle_older(self, done: Done) -> bool:
        chat = self._tab(done.job.token)
        if chat is None:
            # The tab that asked has been closed. Whatever it was reading is
            # history for a conversation nobody is looking at any more.
            return False
        if done.error is not None:
            chat.notice = f"이전 기록을 읽지 못했습니다: {_said(done.error)}"
            return True
        known = {message.id for message in chat.messages}
        older = [message for message in done.value if message.id not in known]
        if not older:
            chat.notice = "이전 기록이 더 없습니다"
            return True
        chat.messages = older + chat.messages
        chat.notice = f"이전 {len(older)}개 불러옴 (총 {len(chat.messages)})"
        return True

    # -- rendering ----------------------------------------------------------

    def render(self, width: int, height: int) -> list[str]:
        self.cursor = None
        self._last_width = width
        # Recorded afresh by whatever paints them; a click that arrives before
        # this frame's paint would otherwise be measured against the last one.
        self._tab_geometry = []
        self._tab_row = None
        if self.chat is not None and not self.picking:
            return self._render_chat(width, height)
        if self.show_friends:
            return self._render_friends(width, height)
        return self._render_rooms(width, height)

    def _title_row(self, left: str, right: str, width: int) -> str:
        right_text = truncate_to_width(right, width, "")
        available = max(0, width - display_width(right_text) - 1)
        left_text = truncate_to_width(self.theme.bold(left), available, "")
        gap = max(1, width - display_width(left_text) - display_width(right_text))
        return left_text + " " * gap + right_text

    def _hints(self, pairs: Sequence[tuple[str, str]], width: int) -> str:
        separator = self.theme.fg("muted", " · ")
        parts = [
            self.theme.fg("accent", key) + self.theme.fg("muted", f" {label}")
            for key, label in pairs
        ]
        return truncate_to_width(separator.join(parts), width, ELLIPSIS)

    def _filter_row(self, width: int) -> str:
        """Show what the list is being narrowed by, where the hints were."""
        return truncate_to_width(
            self.theme.fg("accent", "/ ")
            + self.filter
            + self.theme.fg("accent", "▏")
            + self.theme.fg("muted", "   esc 취소"),
            width,
            ELLIPSIS,
        )

    def _render_rooms(self, width: int, height: int) -> list[str]:
        shown = self.visible_rooms()
        total = len(shown)
        right = (
            self.theme.fg("accent", "◉ Readable")
            + self.theme.fg("muted", f"  {total} rooms")
        )
        lines = [
            self._title_row("KakaoTalk Rooms", right, width),
            self._filter_row(width)
            if self.filtering
            else self._hints(
                [
                    ("↑↓", "move"),
                    ("enter", "open"),
                    ("/", "찾기"),
                    ("f", "친구"),
                    # Quitting from in front of an open conversation would be a
                    # surprise, so there the same keys go back to it instead.
                    ("esc", "돌아가기") if self.picking else ("q", "quit"),
                ],
                width,
            ),
            "",
        ]

        body_height = max(1, height - len(lines) - 2)
        self.scroll = _clamp_scroll(self.selected, self.scroll, body_height, total)
        visible = shown[self.scroll : self.scroll + body_height]

        for index, room in enumerate(visible, start=self.scroll):
            selected = index == self.selected
            cursor = self.theme.fg("accent", "› ") if selected else "  "
            meta = f"{room.member_count}명"
            unread = self._effective_unread(room)
            if unread:
                meta += f" · {unread} unread"
            stamp = _stamp(room.last_updated_at, self.zone)
            right_part = f"{meta}  {stamp}"

            room_width = max(10, width - 2 - display_width(right_part) - 2)
            title = truncate_to_width(clean(room.title), room_width, ELLIPSIS)
            styled = self.theme.bold(title) if selected else title
            if unread:
                styled = self.theme.fg("warning", styled)

            left = cursor + styled
            gap = max(1, width - display_width(left) - display_width(right_part))
            line = left + " " * gap + self.theme.fg("dim", right_part)
            if selected:
                line = self.theme.bg("selectedBg", pad_to_width(line, width))
            lines.append(truncate_to_width(line, width))

        while len(lines) < height - 2:
            lines.append("")
        if total > body_height:
            lines[height - 3] = self.theme.fg("muted", f"  ({self.selected + 1}/{total})")

        lines.append(self._status_line(width))
        lines.append(self._footer_rooms(width))
        return lines[:height]

    def _render_friends(self, width: int, height: int) -> list[str]:
        if not self.friends and not self._friends_asked:
            # Asked for, not waited for: drawing is not where a database read
            # belongs. The list arrives in a later frame, and asking once is
            # what keeps an account with no friends from asking every frame.
            self.load_friends()
        shown = self.visible_friends()
        total = len(shown)
        favourites = sum(1 for friend in shown if friend.favorite)
        right = self.theme.fg("accent", "친구") + self.theme.fg("muted", f"  {total}명 · ★{favourites}")
        lines = [
            self._title_row("KakaoTalk Friends", right, width),
            self._filter_row(width)
            if self.filtering
            else self._hints(
                [
                    ("↑↓", "move"),
                    ("enter", "1:1 열기"),
                    ("/", "찾기"),
                    ("esc", "back"),
                ],
                width,
            ),
            "",
        ]

        body_height = max(1, height - len(lines) - 2)
        self.friend_scroll = _clamp_scroll(
            self.friend_selected, self.friend_scroll, body_height, total
        )
        visible = shown[self.friend_scroll : self.friend_scroll + body_height]

        for index, friend in enumerate(visible, start=self.friend_scroll):
            selected = index == self.friend_selected
            cursor = self.theme.fg("accent", "› ") if selected else "  "
            star = self.theme.fg("warning", "★ ") if friend.favorite else ""
            chat_mark = "" if friend.direct_chat_id else "  (방 없음)"
            right_part = clean(friend.status_message or "")

            name_width = max(8, width // 3)
            name = truncate_to_width(star + clean(friend.name), name_width, ELLIPSIS)
            styled = self.theme.bold(name) if selected else name

            left = cursor + styled + self.theme.fg("dim", chat_mark)
            room_right = truncate_to_width(
                self.theme.fg("dim", right_part), max(0, width - display_width(left) - 2), ELLIPSIS
            )
            gap = max(1, width - display_width(left) - display_width(room_right))
            line = left + " " * gap + room_right
            if selected:
                line = self.theme.bg("selectedBg", pad_to_width(line, width))
            lines.append(truncate_to_width(line, width))

        while len(lines) < height - 2:
            lines.append("")
        if total > body_height:
            lines[height - 3] = self.theme.fg("muted", f"  ({self.friend_selected + 1}/{total})")

        lines.append(self._status_line(width))
        lines.append(self._footer_friends(width))
        return lines[:height]

    def _footer_friends(self, width: int) -> str:
        total = len(self.friends)
        favourites = sum(1 for friend in self.friends if friend.favorite)
        anchor = [Segment("friends", self.theme.fg("accent", "friends"))]
        middle = [
            Segment(f"{total}명", self.theme.fg("muted", f"{total}명")),
            Segment(f"★{favourites}", self.theme.fg("dim", f"★{favourites}")),
        ]
        tail = Segment("local", self.theme.fg("dim", "local"))
        right = Segment(f"v{__version__}", self.theme.fg("muted", f"v{__version__}"))
        return self._compose_footer(anchor, middle, tail, right, width)

    def _render_chat(self, width: int, height: int) -> list[str]:
        """Draw the focused conversation, under the strip of every open tab.

        One conversation at a time. Two of them side by side cost each half
        the width, and half a width is where Hangul sentences start breaking
        into fragments; the room that is not on screen is one tab away, and
        the strip is what says so. A single open room draws the screen it
        always did, strip included only when there is something to switch to.
        """
        chat = self.chat
        assert chat is not None
        lines: list[str] = []
        if len(self.chats) > 1:
            lines.append(self._tab_strip(width))
        lines.extend(
            [
                self._hints(
                    [
                        ("enter", "전송"),
                        ("esc", "닫기"),
                        ("/help", "도움말"),
                    ],
                    width,
                ),
                # The row under the hints was blank; a pinned notice takes
                # it, so the conversation starts where it always did.
                self._notice_row(chat, width),
            ]
        )

        composer, (caret_row, caret_col) = self._composer_lines(width)
        body_height = max(1, height - len(lines) - len(composer) - 1)
        rows = max(1, body_height)

        # Recorded before the transcript is built: a picture's reserved rows are
        # sized against the height it will be shown in, so building first would
        # reserve against the previous frame's.
        if self._body_rows != rows:
            self._body_rows = rows
            if chat.opened_photos:
                chat.rendered = None

        self.placements = []
        body_top = len(lines)
        self._body_geometry = (body_top, rows, width)
        body = self._body_of(chat, width)
        if chat.anchor_top:
            # Resolved here because the top is a distance from the bottom,
            # and neither the body nor the screen height is known earlier.
            chat.offset = max(0, len(body) - rows)
            chat.anchor_top = False
        self._plan_placements(chat, body, rows, width, top=body_top, left=0)
        for line in _window(body, chat.offset, rows):
            lines.append(pad_to_width(truncate_to_width(line, width), width))
        while len(lines) < body_top + rows:
            # Rows the transcript does not reach are still painted: a terminal
            # keeps whatever was in them, and a frame that leaves them out
            # shows the one before it through the gaps.
            lines.append(" " * width)
        while len(lines) < height - len(composer) - 1:
            lines.append("")

        self.cursor = (len(lines) + 1 + caret_row, caret_col)
        lines.extend(composer)
        lines.append(self._footer_chat(width, len(body)))
        return lines[:height]

    def _tab_strip(self, width: int) -> str:
        """Draw every open conversation as a tab, the focused one lit.

        The strip is the only place a title appears once more than one room is
        open. The line above the composer and the footer each used to say it
        again, and a screen that says the same thing three times spends two
        rows saying nothing; what the footer keeps is the part the strip
        cannot say.

        What was drawn is recorded as well as painted, because a click arrives
        as a cell and means nothing until it is measured against the frame that
        is on screen.
        """
        strip = plan_tabs(
            [(clean(chat.room.title), chat.unseen) for chat in self.chats], self.focus, width
        )
        marker_tint = "warning" if strip.hidden_unseen else "dim"
        pieces: list[str] = []
        columns = 0
        if strip.hidden_left:
            marker = f"{ELLIPSIS}{strip.hidden_left} "
            pieces.append(self.theme.fg(marker_tint, marker))
            columns += display_width(marker)
        self._tab_geometry = []
        for position, entry in enumerate(strip.entries):
            if entry.index == self.focus:
                label = self.theme.bg(
                    "selectedBg", self.theme.bold(self.theme.fg("accent", entry.label))
                )
            else:
                label = self.theme.fg("muted", entry.label)
            if entry.unseen:
                label += self.theme.fg("warning", f" +{entry.unseen}")
            pieces.append(label)
            self._tab_geometry.append((entry.index, columns, entry.columns))
            columns += entry.columns
            if position < len(strip.entries) - 1:
                pieces.append(self.theme.fg("borderMuted", TAB_SEPARATOR))
                columns += display_width(TAB_SEPARATOR)
        if strip.hidden_right:
            marker = f" {ELLIPSIS}{strip.hidden_right}"
            pieces.append(self.theme.fg(marker_tint, marker))
            columns += display_width(marker)
        self._tab_row = 0
        return pad_to_width(truncate_to_width("".join(pieces), width), width)

    def _feed_detail(self, chat: ChatState, message: Message) -> str:
        """Render details already fetched for a feed, without reading the DB."""
        if message.type != FEED_MESSAGE:
            return ""
        detail = chat.feeds.get(message.id, "")
        return f" {detail}" if detail else ""

    def _settle_feeds(self, done: Done) -> bool:
        """Record what each feed said, including that it said nothing.

        A feed with nothing to add is remembered as having nothing, so it is
        not asked about again — and that is as true of a read that failed as
        of one that came back empty.
        """
        chat = self._tab(done.job.token)
        if chat is None:
            return False
        (asked,) = done.job.payload
        said = {} if done.error is not None else done.value
        for one in asked:
            chat.feeds[one] = said.get(one, "")
        if done.error is not None:
            chat.notice = f"기록 항목을 읽지 못했습니다: {_said(done.error)}"
        # The transcript was built without these, so it is no longer current.
        chat.rendered = None
        return True

    def _transcript_lines(self, width: int) -> list[str]:
        """Return the focused conversation's transcript."""
        chat = self.chat
        assert chat is not None
        return self._transcript_of(chat, width)

    def _transcript_of(self, chat: ChatState, width: int) -> list[str]:
        """Return a transcript, rebuilding it only when it could have changed.

        Typing does not change a single conversation line, and rebuilding 400
        wrapped Hangul messages measured 71 ms — once per keystroke, which is
        exactly what made the composer feel sluggish. Messages are immutable
        and only ever added, so the count and the width identify the result,
        and a resized window rebuilds rather than reusing a layout made for
        another width. Which photos are open joins the key, because that is the one
        other thing that changes what a line says.
        """
        # Submit one batch per conversation, even on a cached frame: a full queue may
        # have refused the previous request. In-flight duplicates are coalesced.
        missing = tuple(
            message.id for message in chat.messages
            if message.type == FEED_MESSAGE and message.id not in chat.feeds
        )
        if missing:
            self._ask(Job(FEEDS, room_id=chat.room.id, token=chat.token, payload=(missing,)))
        # The revision is what a message changed in place changes.
        key = (
            width,
            len(chat.messages),
            chat.revision,
            frozenset(chat.opened_photos),
            self.show_timestamps,
        )
        if chat.rendered is not None and chat.rendered[0] == key:
            return chat.rendered[1]
        lines = self._build_transcript(chat, width)
        chat.rendered = (key, lines)
        return lines

    def _build_transcript(self, chat: ChatState, width: int) -> list[str]:
        name_width = min(14, max(8, width // 6))
        indent = (7 if self.show_timestamps else 0) + name_width + 1
        body_width = max(12, width - indent)

        lines: list[str] = []
        chat.photo_rows = {}
        chat.message_rows = {}
        previous_day = ""
        for message in chat.messages:
            instant = (
                datetime.fromtimestamp(message.sent_at, timezone.utc).astimezone(self.zone)
                if message.sent_at is not None
                else None
            )
            day = instant.strftime("%Y-%m-%d (%a)") if instant else "unknown date"
            if day != previous_day:
                lines.append("")
                lines.append(self.theme.fg("borderMuted", truncate_to_width(f"── {day} " + "─" * width, width)))
                previous_day = day

            speaker = "나" if message.from_me else message.sender
            stamp = instant.strftime("%H:%M") if instant else "--:--"
            # Everything below that somebody else wrote is cleaned before it
            # is styled; the tint still follows the name as it was stored.
            name = truncate_to_width(clean(speaker), name_width, ELLIPSIS)
            if message.erased:
                # Said the way the client says it; what was taken back is not
                # carried here at all.
                wrapped = [self.theme.fg("dim", ERASED_PLACEHOLDER)]
            elif message.covered is not None and not message.text:
                # Covered and not revealed: the reader withheld the text, and
                # the line says why rather than looking like an empty message.
                wrapped = [self.theme.fg("dim", COVERED_PLACEHOLDER)]
            elif message.photo is not None:
                # The live client writes "사진" into the text of a photo message,
                # so its own words are not what a reader needs here; the label
                # is rebuilt so it can carry the open/close marker.
                wrapped = [self.theme.fg("dim", self._placeholder(message))]
            elif message.type in _MEDIA_WITHOUT_PREVIEW:
                # A video stores "동상열" the same way, and read as plain text it
                # is indistinguishable from somebody typing the word; it is
                # bracketed so it is clearly an attachment, and left unmarked
                # because there is nothing to open.
                wrapped = [self.theme.fg("dim", self._placeholder(message))]
            elif message.text:
                wrapped = self._wrap_body(
                    clean(message.text.replace("\r\n", "\n"), keep_newlines=True), body_width
                )
            else:
                detail = clean(self._feed_detail(chat, message))
                # A join or a leave is a sentence already, the one the client
                # shows, so it is shown as one rather than as a tag.
                if is_feed_sentence(detail):
                    wrapped = [self.theme.fg("dim", detail.strip())]
                else:
                    label = message_kind(message.type) + detail
                    wrapped = [self.theme.fg("dim", f"[{label}]")]

            if self.show_timestamps:
                head = (
                    self.theme.fg("dim", stamp)
                    + self._mention_mark(message)
                    + self.theme.fg(self.palette.name_colour(speaker), pad_to_width(name, name_width))
                    + " "
                )
            else:
                mention = ""
                if message.mentions_me:
                    mention = self.theme.fg("warning", "@")
                elif message.mentions_all:
                    mention = self.theme.fg("muted", "@")
                if mention:
                    clean_name = truncate_to_width(clean(speaker), name_width - 1, ELLIPSIS)
                    name_str = mention + self.theme.fg(
                        self.palette.name_colour(speaker), pad_to_width(clean_name, name_width - 1)
                    )
                else:
                    name_str = self.theme.fg(
                        self.palette.name_colour(speaker), pad_to_width(name, name_width)
                    )
                head = name_str + " "
            body_colour = self.palette.body_colour(speaker)
            opening = head
            chat.message_rows[message.id] = len(lines)
            if message.reply is not None:
                # Named once, above what they were answering — the way the
                # client itself stacks a reply. Their own words come next,
                # with the mark in the column the head was using anyway.
                lines.append(truncate_to_width(head + self._quoted(message, body_width), width))
                opening = " " * (indent - 1) + self.theme.fg("dim", "↳")
            lines.append(truncate_to_width(opening + self.theme.fg(body_colour, wrapped[0]), width))
            if message.photo is not None:
                chat.photo_rows[len(lines) - 1] = message.id
            for continuation in wrapped[1:]:
                lines.append(
                    truncate_to_width(" " * indent + self.theme.fg(body_colour, continuation), width)
                )
            marks = self._message_marks(message)
            if marks:
                # Beside the last line when it fits, as the client puts
                # "수정됨" beside the bubble; on a line of its own otherwise,
                # so it is never cut off the edge.
                if display_width(strip_ansi(wrapped[-1])) + display_width(marks) <= body_width:
                    lines[-1] = truncate_to_width(lines[-1] + self.theme.fg("dim", marks), width)
                else:
                    lines.append(
                        truncate_to_width(" " * indent + self.theme.fg("dim", marks.strip()), width)
                    )
            if message.reactions and not message.erased:
                felt = " · ".join(f"{clean(one.label)} {one.count}" for one in message.reactions)
                lines.append(
                    truncate_to_width(" " * indent + self.theme.fg("muted", f"공감 {felt}"), width)
                )
            if message.id in chat.opened_photos and message.photo is not None:
                for row in self._photo_rows(message, indent, width):
                    chat.photo_rows[len(lines)] = message.id
                    lines.append(row)
        return lines

    def _placeholder(self, message: Message) -> str:
        """Name what a message carries when its own words are not the point.

        A picture says so and says it is clickable, because a bare ``[photo]``
        gives no reason to click it. Media with no preview says what it is and
        stays unmarked: offering to open something that cannot be opened is
        worse than not offering.
        """
        if message.photo is None:
            if message.type in _MEDIA_WITHOUT_PREVIEW:
                return _MEDIA_WITHOUT_PREVIEW[message.type]
            return f"[{message_kind(message.type)}]"
        count = message.photo.count
        label = "[사진]" if count <= 1 else f"[사진 {count}장]"
        if not self._photos_drawable():
            return label
        if message.id in self.opened_photo_ids():
            return f"{label} ▾"
        return f"{label} ▸"

    @property
    def _photos_supported(self) -> bool:
        """Whether some protocol can draw a picture here.

        Derived rather than stored: the protocol is the fact, and a second copy
        of it is a second thing to keep in step.
        """
        return self._protocol != images.NONE

    def _photos_drawable(self) -> bool:
        """Whether this terminal can show a picture at all."""
        return self._photos_supported and self.theme.enabled

    def opened_photo_ids(self) -> set[int]:
        chat = self.chat
        return set() if chat is None else chat.opened_photos

    def newest_photo(self) -> Optional[int]:
        """The id of the newest message in this conversation carrying a picture."""
        chat = self.chat
        if chat is None:
            return None
        for message in reversed(chat.messages):
            if message.photo is not None:
                return message.id
        return None

    def toggle_photo(self, message_id: int) -> bool:
        """Open a message's picture, or close it if it is already open.

        Returns whether anything changed, so a click that landed on nothing
        does not cost a rebuild of the transcript.

        A picture belongs to its conversation, not to the screen: the tab that
        is not in front keeps what it had open, and coming back to it shows the
        picture again. What does not survive is the conversation leaving the
        screen: `_plan_placements` only places the one the frame actually
        painted, so switching tabs withdraws the image and switching back puts
        it there again.
        """
        chat = self.chat
        if chat is None or not self._photos_drawable():
            return False
        message = next((one for one in chat.messages if one.id == message_id), None)
        if message is None or message.photo is None:
            return False

        if message_id in chat.opened_photos:
            chat.opened_photos.discard(message_id)
            chat.notice = ""
            restored = chat.offset_before_photo.pop(message_id, None)
            if restored is not None:
                chat.offset = restored
        else:
            blob = self._photo_bytes(message)
            if blob is None:
                chat.notice = "사진을 가져오지 못했습니다"
                return True
            chat.opened_photos.add(message_id)
            chat.notice = ""
            chat.offset_before_photo[message_id] = chat.offset
            self._keep_photo_in_view(message_id)
        self._settle_offset()
        return True

    def _toggle_newest_photo(self, chat: ChatState) -> None:
        """Open the newest picture in this conversation, or close what is open.

        A click points at the picture it means. A key has nothing to point
        with, so it takes the newest one -- which is the picture somebody means
        when they ask for a picture at all -- and the same key closes it again.
        Closing goes back through :meth:`toggle_photo` so the scroll position
        the picture borrowed is handed back exactly as a second click hands it
        back.
        """
        if not self._photos_drawable():
            chat.notice = (
                "이 터미널은 사진을 그리지 못합니다 "
                f"({images.PROTOCOL_ENV}=sixel 로 지정할 수 있습니다)"
            )
            return
        if chat.opened_photos:
            for message_id in list(chat.opened_photos):
                self.toggle_photo(message_id)
            return
        newest = self.newest_photo()
        if newest is None:
            chat.notice = "이 대화에는 사진이 없습니다"
            return
        self.toggle_photo(newest)

    def _settle_offset(self) -> None:
        """Hold the viewport inside what there is to scroll.

        An offset counts lines held back from the newest one, so a transcript
        that just lost the rows a picture occupied can leave it pointing past
        the end -- and a viewport parked there stops responding to scrolling and
        never clears the unseen count.
        """
        chat = self.chat
        if chat is None:
            return
        geometry = self._body_geometry
        if geometry is None:
            return
        _top, height, width = geometry
        if height <= 0:
            return
        body = self._transcript_of(chat, width)
        chat.offset = max(0, min(chat.offset, max(0, len(body) - height)))
        if chat.offset == 0:
            chat.unseen = 0
            self._mark_room_seen(chat.room.id)

    def _keep_photo_in_view(self, message_id: int) -> None:
        """Scroll only as far as it takes to bring a just-opened picture into view.

        A photo already on screen stays where the reader left it: pinning its
        last row to the bottom of the window moves the conversation under the
        cursor for no reason, which reads as the view jumping away from the
        message that was clicked.

        When the picture genuinely does not fit, the smallest scroll that shows
        it is taken, and its label is preferred over its bottom edge -- the
        label is what closes it again.
        """
        chat = self.chat
        if chat is None:
            return
        geometry = self._body_geometry
        if geometry is None:
            return
        _top, height, width = geometry
        if height <= 0:
            return
        body = self._transcript_of(chat, width)
        owned = [row for row, owner in chat.photo_rows.items() if owner == message_id]
        if not owned:
            return

        # An offset counts lines held back from the newest, so a row's distance
        # from the end of the transcript is what the window is expressed in.
        first, last = min(owned), max(owned)
        scrollable = max(0, len(body) - height)
        offset = min(chat.offset, scrollable)
        end = len(body) - offset
        start = end - height

        if start <= first and last < end:
            chat.offset = offset
            return  # Already visible; leave the reader's position alone.

        if last >= end:
            offset = max(0, len(body) - (last + 1))
        if len(body) - first > height + offset:
            offset = len(body) - first - height
        chat.offset = max(0, min(offset, scrollable))

    def _plan_placements(
        self,
        chat: ChatState,
        body: list[str],
        rows: int,
        width: int,
        top: int,
        left: int,
    ) -> None:
        """Work out where each opened picture lands in this frame.

        A placement is addressed by screen position, so it has to be recomputed
        against the window that is about to be painted rather than remembered
        from the last one: the same photo sits on a different row after a single
        line of scrolling.

        A picture only partly inside the window is cropped to the part that is,
        rather than being drawn whole or dropped entirely.  Nothing the frame
        does clips a placement, so a picture drawn whole would run over the
        composer and the footer; dropping it instead made a photo vanish in one
        piece the moment its first row left the window.
        """
        if not chat.opened_photos or not self._photos_drawable():
            return

        scrollable = len(body) - rows
        offset = min(chat.offset, scrollable) if scrollable > 0 else chat.offset
        end = max(1, len(body) - max(0, offset))
        start = max(0, end - rows)

        name_width = min(14, max(8, width // 6))
        indent = 5 + 2 + name_width + 1
        columns = max(4, width - indent)

        for message_id in chat.opened_photos:
            message = next((one for one in chat.messages if one.id == message_id), None)
            if message is None or message.photo is None:
                continue
            owned = sorted(row for row, owner in chat.photo_rows.items() if owner == message_id)
            reserved = [row for row in owned if body[row : row + 1] == [""]]
            if not reserved:
                continue

            first, last = reserved[0], reserved[-1] + 1
            visible_top = max(first, start)
            visible_bottom = min(last, end)
            if visible_bottom <= visible_top:
                continue

            blob = self._photo_bytes(message)
            if blob is None:
                continue
            source_width, source_height = self._photo_shape(message)
            cols, box_rows, box = images.fit(
                source_width, source_height, columns, rows, self._cell
            )
            blob = self._framed_bytes(message_id, blob, box)
            shown = min(box_rows, visible_bottom - visible_top)
            crop = images.crop_for(box[1], box_rows, visible_top - first, shown)
            self.placements.append(
                (
                    message_id,
                    top + (visible_top - start),
                    left + indent,
                    blob,
                    cols,
                    shown,
                    crop,
                )
            )

    def _framed_bytes(
        self, message_id: int, blob: bytes, box: tuple[int, int]
    ) -> bytes:
        """Return the picture padded to *box*, converting at most once per size.

        Padding runs a subprocess, and a placement is recomputed on every frame,
        so the result is kept against the box it was made for: without that, the
        conversion would run on each keystroke and the flicker would return.
        """
        if self._cell is None:
            return blob
        cached = self._framed.get(message_id)
        if cached is not None and cached[0] == box:
            return cached[1]
        framed = images.letterbox(blob, box)
        if framed is None:
            return blob
        self._framed[message_id] = (box, framed)
        return framed

    def photo_escapes(self) -> str:
        """Return the escapes that bring the terminal's images up to date.

        Which escapes those are depends on the protocol, and the two kinds are
        not variations on one theme. Kitty holds the picture itself and hands
        out placements against an id, so a move is a cheap re-placement and a
        removal has to be spelled out. Sixel and iTerm2 hold nothing: the
        picture is the escape, drawn into the cell grid, so it is written again
        wherever it goes and the frame's own repaint is what takes it away.
        """
        if self._protocol == images.KITTY:
            return self._kitty_escapes()
        if self._protocol in (images.ITERM2, images.SIXEL):
            return self._inline_escapes()
        return ""

    def _inline_escapes(self) -> str:
        """Draw every inline picture again when any of them moved.

        There is no id to move or erase, so a changed frame redraws all of
        them -- and because the old pixels sit in cells the transcript left
        blank, which :meth:`Console.draw` would skip as unchanged, the frame is
        given up whole through :meth:`Console.forget`. A still screen changes
        nothing and writes nothing, which is what keeps this off the keystroke
        path.
        """
        current = {
            message_id: (row, column, cols, rows, crop)
            for message_id, row, column, _blob, cols, rows, crop in self.placements
        }
        if current == self._drawn:
            return ""
        self._images_moved = True
        pieces: list[str] = []
        for message_id, row, column, blob, cols, rows, crop in self.placements:
            # Encoding is the expensive half -- a sixel picture measured 1.1 s
            # for one 240x240 frame -- and a redraw usually asks for a box that
            # was already encoded, so the escape is kept rather than rebuilt.
            key = (self._protocol, message_id, cols, rows, crop)
            escape = self._inline_cache.get(key)
            if escape is None and key not in self._inline_cache:
                escape = images.inline_escape(
                    self._protocol, blob, cols, rows, crop, self._cell
                )
                if len(self._inline_cache) >= INLINE_CACHE_ENTRIES:
                    # Scrolling walks the crop one line at a time, so the keys
                    # are unbounded; the oldest goes rather than all of them.
                    self._inline_cache.pop(next(iter(self._inline_cache)))
                self._inline_cache[key] = escape
            if escape is None:
                # iTerm2 cannot crop a source, so a half-scrolled picture waits
                # rather than being drawn over the composer.
                continue
            pieces.append(f"\x1b[{row + 1};{column + 1}H" + escape)
        self._drawn = current
        return "".join(pieces)

    def _kitty_escapes(self) -> str:
        """Return the Kitty escapes that bring its images up to date.

        Only differences are written. The interface redraws on every keystroke
        and again each time the key wait expires, so re-transmitting a picture
        each time made it visibly flicker several times a second -- the
        terminal was deleting and re-uploading an image that had not moved.

        A picture that only moved is re-placed rather than re-sent: an original
        measured 294-362 KB, and pushing that down the pty on every scrolled
        line is the same flicker in a slower form.
        """
        current = {
            message_id: (row, column, cols, rows, crop)
            for message_id, row, column, _blob, cols, rows, crop in self.placements
        }
        if current == self._drawn:
            return ""

        pieces: list[str] = []
        for message_id in sorted(self._drawn):
            if message_id not in current:
                pieces.append(images.erase(self._image_ids.of(message_id)))
                self._transmitted.discard(message_id)
            elif current[message_id] != self._drawn[message_id]:
                pieces.append(images.erase(self._image_ids.of(message_id), free=False))

        for message_id, row, column, blob, cols, rows, crop in self.placements:
            if self._drawn.get(message_id) == (row, column, cols, rows, crop):
                continue
            pieces.append(f"\x1b[{row + 1};{column + 1}H")
            image_id = self._image_ids.of(message_id)
            if message_id in self._transmitted:
                pieces.append(images.place(image_id, cols, rows, crop))
            else:
                pieces.append(images.draw(image_id, blob, cols, rows, crop))
                self._transmitted.add(message_id)
        self._drawn = current
        return "".join(pieces)

    def _photo_bytes(self, message: Message) -> Optional[bytes]:
        """Return bytes for a message's picture that the terminal will draw.

        Converted once and kept: a WebP photo has to become a PNG before the
        terminal will show it, and doing that per frame would cost a subprocess
        on every keystroke.
        """
        assert message.photo is not None
        if message.id in self._bytes:
            return self._bytes[message.id]
        blob = self._photos.fetch(message.photo.url)
        usable = None if blob is None else images.drawable(blob)
        if usable is not None:
            self._bytes[message.id] = usable
            self._learn_photo_shape(message, usable)
        return usable

    def _photo_shape(self, message: Message) -> tuple[int, int]:
        """Return the picture's true pixel size, preferring its own bytes.

        The rows the transcript reserves and the cells the placement asks for
        have to be computed from the same shape, or a rotated photo reserves a
        tall box and then draws a wide one into it.
        """
        assert message.photo is not None
        cached = self._shapes.get(message.id)
        if cached is not None:
            return cached
        return message.photo.width, message.photo.height

    def _learn_photo_shape(self, message: Message, blob: bytes) -> tuple[int, int]:
        """Record what the fetched bytes say this picture's shape really is."""
        assert message.photo is not None
        measured = images.pixel_size(blob)
        shape = measured or (message.photo.width, message.photo.height)
        if self._shapes.get(message.id) != shape:
            self._shapes[message.id] = shape
            chat = self.chat
            if chat is not None:
                # The reserved rows were counted from the stored shape, so the
                # transcript has to be rebuilt against the measured one.
                chat.rendered = None
        return shape

    def _photo_rows(self, message: Message, indent: int, width: int) -> list[str]:
        """Return the blank rows a shown picture occupies.

        The picture itself is not text and cannot live in a line, so the
        transcript reserves the cells and the placement is written over them
        after the frame is painted. Reserving them is what keeps the rest of
        the conversation from being drawn underneath the image.
        """
        assert message.photo is not None
        columns = max(4, width - indent)
        source_width, source_height = self._photo_shape(message)
        # Sized through the same call the placement uses, so the rows reserved
        # here and the rows drawn there cannot disagree.
        _cols, rows, _box = images.fit(
            source_width, source_height, columns, self._photo_ceiling(), self._cell
        )
        return [""] * rows

    def _paint(self, console: Console, width: int, height: int) -> None:
        """Compose one frame, then lay the pictures over it.

        The frame is built first because rendering is what rebuilds
        :attr:`placements` -- the picture positions are a result of the layout,
        not an input to it. An inline protocol that moved a picture then needs
        the whole frame repainted to clear the old pixels, which is the one
        thing the row-diff painter cannot work out for itself.
        """
        frame = self.render(width, height)
        overlay = self.photo_escapes()
        if self._images_moved:
            console.forget()
            self._images_moved = False
        console.draw(frame, self.cursor, overlay)

    def _measure_cell(self, console: Console) -> None:
        """Learn the terminal's cell shape so pictures keep their proportions.

        Asked once, after the first frame is on screen: the answer only matters
        when a photo is opened, and a terminal that stays silent must not hold
        up the conversation appearing.

        The sixel question is asked here too, and for the same reason it is
        asked at all: no environment variable announces sixel, so a terminal
        reached over SSH -- which forwards ``TERM`` and nothing else -- can only
        be recognised by asking it. A terminal the environment already named
        is never re-asked.
        """
        if self._protocol == images.NONE:
            self._protocol = images.detect(query_sixel=console.supports_sixel)
        if not self._photos_supported:
            return
        measured = console.cell_pixels()
        if measured is None:
            return
        cell_width, cell_height = measured
        if cell_width > 0 and cell_height > 0:
            self._cell = (cell_width, cell_height)
            self._cell_aspect = cell_height / cell_width

    def _photo_ceiling(self) -> Optional[int]:
        """How many rows a conversation has, for sizing a picture against it.

        Returns None before a frame has been measured, which leaves the sizing
        on its own fixed ceiling rather than guessing a height.
        """
        geometry = self._body_geometry
        return geometry[1] if geometry is not None else None

    def _wrap_body(self, text: str, width: int) -> list[str]:
        """Wrap body text while preserving full target URLs across split lines."""
        if not self.theme.enabled or "http" not in text:
            return wrap_to_width(text, width)

        # A control character, or the picture clean() leaves in its place,
        # ends an address: neither belongs inside an OSC 8 target, where one
        # ESC or BEL would end it early and hand the rest to the terminal.
        url_pattern = re.compile(r"(https?://[^\s<>\"'()\x00-\x1f\x7f-\x9f\u2400-\u243f]+)")
        urls = [(m.start(), m.end(), m.group(0)) for m in url_pattern.finditer(text)]
        if not urls:
            return wrap_to_width(text, width)

        char_map: list[Optional[str]] = [None] * len(text)
        for start, end, url in urls:
            for idx in range(start, end):
                char_map[idx] = url

        wrapped_lines = wrap_to_width(text, width)
        result: list[str] = []
        text_pos = 0

        for line in wrapped_lines:
            while text_pos < len(text) and text[text_pos].isspace() and not line.startswith(text[text_pos]):
                text_pos += 1

            line_start = text_pos
            text_pos += len(line)

            out_segments: list[str] = []
            curr_url: Optional[str] = None
            curr_text = ""

            for char_idx, ch in enumerate(line):
                orig_idx = line_start + char_idx
                u = char_map[orig_idx] if orig_idx < len(char_map) else None
                if u == curr_url:
                    curr_text += ch
                else:
                    if curr_text:
                        if curr_url:
                            out_segments.append(
                                f"\x1b]8;;{curr_url}\x1b\\\x1b[4m{curr_text}\x1b[24m\x1b]8;;\x1b\\"
                            )
                        else:
                            out_segments.append(curr_text)
                    curr_url = u
                    curr_text = ch

            if curr_text:
                if curr_url:
                    out_segments.append(
                        f"\x1b]8;;{curr_url}\x1b\\\x1b[4m{curr_text}\x1b[24m\x1b]8;;\x1b\\"
                    )
                else:
                    out_segments.append(curr_text)

            result.append("".join(out_segments))
        return result

    def _quoted(self, message: Message, body_width: int) -> str:
        """Render what a reply is answering, to sit beside the replier's name.

        One line, collapsed and cut. It is the client's own copy, so it is
        here even when the original is far above the window or has since been
        deleted — but it is context for the answer below it, and letting it
        wrap would bury what was actually said.
        """
        quote = message.reply
        assert quote is not None
        who = "나" if message.replies_to_me else clean(quote.sender or f"User {quote.sender_id}")
        said = " ".join(clean(quote.text).split())
        if not said:
            said = f"[{message_kind(quote.type)}]" if quote.type is not None else "[원문 없음]"
        return self.theme.fg("dim", truncate_to_width(f"{who}: {said}", body_width, ELLIPSIS))

    def _mention_mark(self, message: Message) -> str:
        """Mark a message that named somebody, in the head's own two columns.

        The gap between the time and the name is already there, so a marked
        line starts its text exactly where every other line does; a mark with
        a column of its own would move the whole conversation sideways.
        """
        if message.mentions_me:
            return " " + self.theme.fg("warning", "@")
        if message.mentions_all:
            return " " + self.theme.fg("muted", "@")
        return "  "

    def _body_of(self, chat: ChatState, width: int) -> list[str]:
        """What the body shows: a digest, a list, or the conversation.

        One answer for the frame and for a click, so a click is measured
        against the rows that are actually on screen.
        """
        if chat.slices:
            return self._digest_of(chat, width)
        if chat.panel:
            return self._panel_of(chat, width)
        return self._transcript_of(chat, width)

    def _message_marks(self, message: Message) -> str:
        """The words that follow a message: edited, or shown despite a cover."""
        if message.erased:
            return ""
        marks = []
        if message.edited:
            marks.append("수정됨")
        if message.covered is not None and message.text:
            marks.append("세이프봇 가림 해제")
        return f" ({', '.join(marks)})" if marks else ""

    def _notice_row(self, chat: ChatState, width: int) -> str:
        """The pinned notice on one line, or the blank row it replaces."""
        pinned = chat.room_notice
        if pinned is None:
            return ""
        said = " ".join(clean(pinned.text).split()) or f"[{pinned.kind}]"
        label = self.theme.fg("accent", "공지 ")
        hint = self.theme.fg("dim", "  /notice")
        room = max(4, width - display_width("공지 ") - display_width("  /notice"))
        return label + self.theme.fg("muted", truncate_to_width(said, room, ELLIPSIS)) + hint

    def _panel_of(self, chat: ChatState, width: int) -> list[str]:
        """Draw a list in the digest's box: a title, its rows, how to leave."""
        inner = max(20, width - 2)
        rows: list[str] = []

        def boxed(content: str) -> str:
            return self.theme.bg("customMessageBg", pad_to_width(" " + content, width))

        rows.append(boxed(""))
        label = self.theme.fg(
            "customMessageLabel", self.theme.bold(f"[{clean(chat.panel_title)}]")
        )
        rows.append(boxed(label))
        rows.append(boxed(""))
        styles = {"head": "accent", "body": "text", "dim": "muted", "link": "dim"}
        for style, text in chat.panel:
            if not text:
                rows.append(boxed(""))
                continue
            said = clean(text.replace("\r\n", "\n"), keep_newlines=True)
            indent = "  " if style == "head" else "    "
            for piece in self._wrap_body(said, inner - 4):
                rows.append(boxed(indent + self.theme.fg(styles.get(style, "text"), piece)))
        rows.append(boxed(""))
        rows.append(boxed(self.theme.fg("muted", "  esc 대화로 돌아가기")))
        rows.append(boxed(""))
        return rows

    def _show_panel(self, chat: ChatState, title: str, rows: list[tuple[str, str]]) -> None:
        chat.slices = []
        chat.panel = rows
        chat.panel_title = title
        chat.anchor_top = True
        chat.notice = ""

    def _settle_notice(self, done: Done) -> bool:
        """Keep the notice for its row, and put it up when it was asked for."""
        chat = self._tab(done.job.token)
        if chat is None:
            return False
        asked = done.job.about == "show"
        if done.error is not None:
            if asked:
                chat.notice = f"공지를 읽지 못했습니다: {_said(done.error)}"
            return asked
        pinned: Optional[Notice] = done.value
        chat.room_notice = pinned
        if not asked:
            return True
        if pinned is None:
            chat.notice = "이 대화방에는 공지가 없습니다"
            return True
        who = pinned.author or (f"User {pinned.author_id}" if pinned.author_id else "작성자 모름")
        when = _stamp(pinned.posted_at, self.zone)
        source = "톡게시판" if pinned.source == "board" else ""
        rows: list[tuple[str, str]] = [
            ("dim", " · ".join(part for part in (who, when, source) if part)),
            ("", ""),
        ]
        rows.extend(("body", line) for line in (pinned.text or f"[{pinned.kind}]").splitlines())
        self._show_panel(chat, "공지", rows)
        return True

    def _settle_attachments(self, done: Done) -> bool:
        """List what the room carried, newest first, the way a drawer does."""
        chat = self._tab(done.job.token)
        if chat is None:
            return False
        kind, limit = done.job.payload
        title = _ATTACHMENT_TITLES.get(kind, kind)
        if done.error is not None:
            chat.notice = f"목록을 읽지 못했습니다: {_said(done.error)}"
            return True
        found = done.value
        if not found:
            chat.notice = f"최근 {limit}개 메시지에서 {title} 항목을 찾지 못했습니다"
            return True
        now_ms = int(time.time() * 1000)
        rows: list[tuple[str, str]] = []
        for item in reversed(found):
            head = " ".join(part for part in (_stamp(item.sent_at, self.zone), item.sender) if part)
            if item.kind == "file":
                size = _size(item.size) if item.size is not None else ""
                head += f"  {item.name}" + (f" · {size}" if size else "")
                if item.expires_at is not None and item.expires_at < now_ms:
                    head += " · 만료됨"
            elif item.count > 1:
                head += f"  사진 {item.count}장"
            rows.append(("head", head))
            rows.append(("link", item.url))
        self._show_panel(chat, f"{title} {len(found)}개 · 최근 {limit}개 메시지", rows)
        return True

    def _settle_revisions(self, done: Done) -> bool:
        """Every edited message in the window, each with what history it kept."""
        chat = self._tab(done.job.token)
        if chat is None:
            return False
        if done.error is not None:
            chat.notice = f"수정 이력을 읽지 못했습니다: {_said(done.error)}"
            return True
        (wanted,) = done.job.payload
        held = {message.id: message for message in chat.messages}
        rows: list[tuple[str, str]] = []
        for log_id in reversed(wanted):
            message = held.get(log_id)
            if message is None:
                continue
            who = "나" if message.from_me else message.sender
            rows.append(("head", f"{_stamp(message.sent_at, self.zone)} {who}"))
            rows.append(("body", message.text or f"[{message_kind(message.type)}]"))
            versions = done.value.get(log_id, [])
            if not versions:
                rows.append(("dim", "이전 버전이 기록되지 않았습니다"))
            for version in versions:
                said = " ".join(version.text.split()) or f"[{message_kind(version.type)}]"
                rows.append(("dim", f"{version.revision}. {said}"))
            rows.append(("", ""))
        self._show_panel(chat, f"수정된 메시지 {len(wanted)}개", rows)
        return True

    def _show_edits(self, chat: ChatState) -> None:
        edited = [message.id for message in chat.messages if message.edited and not message.erased]
        if not edited:
            chat.notice = "불러온 대화에 수정된 메시지가 없습니다"
            return
        self._ask(
            Job(REVISIONS, room_id=chat.room.id, token=chat.token, payload=(tuple(edited[-EDITS_SHOWN:]),))
        )

    def _show_help(self, chat: ChatState) -> None:
        """Show keyboard shortcuts and slash commands in a help panel."""
        rows: list[tuple[str, str]] = [
            ("head", "키보드 단축키"),
            ("body", "enter          메시지 전송 (슬래시로 시작하면 명령어 실행)"),
            ("body", "esc            패널 닫기 / 대화방 닫기 / 방 목록으로 복귀"),
            ("body", "↑ / ↓          메시지 스크롤 (또는 입력창 커서 줄 이동)"),
            ("body", "page up / down 반 화면 스크롤"),
            ("body", "tab / ⇧tab     열린 대화방 탭 전환"),
            ("body", "^P             최신 사진 미리보기 토글"),
            ("body", "^T             타임스탬프 표시 / 숨기기 토글"),
            ("body", "^G             마우스 클릭 / 텍스트 선택 모드 전환"),
            ("", ""),
            ("head", "슬래시 명령어"),
            ("body", "/help          이 도움말 화면 표시"),
            ("body", "/time          타임스탬프 표시 / 숨기기 토글"),
            ("body", "/open <이름>   대화방을 새 탭으로 열기"),
            ("body", "/close         현재 대화방 탭 닫기"),
            ("body", "/sum <구간>    최근 대화 요약 (예: /sum 30m, /sum 2h)"),
            ("body", "/export        요약(다이제스트) 텍스트 파일로 저장"),
            ("body", f"/older         이전 대화 {LOAD_MORE_STEP}개 더 불러오기"),
            ("body", "/notice        대화방 공지사항 보기"),
            ("body", "/files         파일 첨부 목록 보기"),
            ("body", "/links         링크 첨부 목록 보기"),
            ("body", "/photos        사진 첨부 목록 보기"),
            ("body", "/videos        동영상 첨부 목록 보기"),
            ("body", "/photo         최신 사진 인라인 열기 / 닫기"),
            ("body", "/edits         수정된 메시지 이력 보기"),
            ("body", "/safebot       SafeBot 가림 메시지 해제 / 설정"),
            ("body", "/real <닉네임> 친구 실명/본명 확인"),
            ("body", "/quit          종료"),
        ]
        self._show_panel(chat, "도움말", rows)

    def _toggle_timestamps(self, chat: Optional[ChatState] = None) -> None:
        """Toggle timestamp visibility across chat transcripts."""
        self.show_timestamps = not self.show_timestamps
        for tab in self.chats:
            tab.rendered = None
        notice = "시간 표시 켬" if self.show_timestamps else "시간 표시 끔"
        self.status = notice
        target = chat or self.chat
        if target is not None:
            self._passing_notice(target, notice)

    def _toggle_covered(self, chat: ChatState) -> None:
        """Show what SafeBot covered, or cover it again, in every open tab.

        The reader decides what text a covered message carries, so every
        held message is read again under the new setting, on the owner thread.
        """
        held = tuple(
            (tab.room.id, tuple(message.id for message in tab.messages)) for tab in self.chats
        )
        wanted = not self.reveal_covered
        if not self._ask(Job(REVEAL, token=chat.token, about=str(wanted), payload=(wanted, held))):
            chat.notice = "DB 작업이 밀려 있습니다. 잠시 후 다시 시도해주세요."

    def _settle_reveal(self, done: Done) -> bool:
        chat = self._tab(done.job.token) or self.chat
        wanted, _held = done.job.payload
        if done.error is not None:
            if chat is not None:
                chat.notice = f"다시 읽지 못했습니다: {_said(done.error)}"
            return True
        self.reveal_covered = wanted
        for tab in self.chats:
            answer = done.value.get(tab.room.id)
            if answer is None:
                continue
            again, versions = answer
            fresh = {message.id: message for message in again}
            # Rows that arrived since the question are kept as they came; they
            # were read under the new setting already.
            tab.messages = [fresh.get(message.id, message) for message in tab.messages]
            tab.versions.update(versions)
            tab.revision += 1
            if tab.slices and tab.slice_span is not None:
                tab.slices = digest.slice_messages(_spoken(tab.messages), tab.slice_span)
        if chat is not None:
            chat.notice = (
                "세이프봇이 가린 메시지를 보여줍니다 (/safebot 으로 다시 가리기)"
                if wanted
                else "세이프봇이 가린 메시지를 다시 가립니다"
            )
        return True

    def _digest_lines(self, width: int) -> list[str]:
        """Render the focused conversation's slices."""
        chat = self.chat
        assert chat is not None
        return self._digest_of(chat, width)

    def _digest_of(self, chat: ChatState, width: int) -> list[str]:
        """Render slices the way OmO renders a reflection: a padded tinted box."""
        inner = max(20, width - 2)
        rows: list[str] = []

        def boxed(content: str) -> str:
            return self.theme.bg("customMessageBg", pad_to_width(" " + content, width))

        rows.append(boxed(""))
        label = self.theme.fg("customMessageLabel", self.theme.bold("[digest]"))
        rows.append(boxed(label + self.theme.fg("muted", f" {clean(chat.slice_label)}")))
        rows.append(boxed(""))

        for piece in chat.slices:
            start = piece.start.astimezone(self.zone)
            end = piece.end.astimezone(self.zone)
            header = f"{start:%m-%d %H:%M} – {end:%H:%M}"
            rows.append(
                boxed(
                    self.theme.fg("accent", header)
                    + self.theme.fg("muted", f"   {piece.count} messages")
                )
            )

            speakers = ", ".join(
                f"{clean(name)} {count}" for name, count in piece.speakers[:4]
            )
            if speakers:
                rows.append(boxed(self.theme.fg("muted", "  who   ") + speakers))
            keywords = ", ".join(clean(word) for word, _count in piece.keywords[:6])
            if keywords:
                rows.append(boxed(self.theme.fg("muted", "  about ") + keywords))

            for highlight in piece.highlights():
                speaker = "나" if highlight.from_me else highlight.sender
                text = clean(highlight.text or "")
                for index, piece_line in enumerate(
                    wrap_to_width(f"{clean(speaker)}: {text}", inner - 8)
                ):
                    prefix = "  ┃ " if index == 0 else "    "
                    rows.append(
                        boxed(
                            self.theme.fg("dim", prefix)
                            + self.theme.fg(self.palette.body_colour(speaker), piece_line)
                        )
                    )
            rows.append(boxed(""))

        rows.append(boxed(self.theme.fg("muted", "  esc 대화로 돌아가기 · e 원문 내보내기")))
        rows.append(boxed(""))
        return rows

    def _composer_lines(self, width: int) -> tuple[list[str], tuple[int, int]]:
        """Draw the message box: OmO-style open input with a top divider line."""
        chat = self.chat
        assert chat is not None

        content_width = max(6, width - 4)
        paragraphs = chat.draft.split("\n")
        wrapped_rows: list[str] = []
        caret_row = 0
        caret_col = 0
        char_offset = 0

        for p_idx, p in enumerate(paragraphs):
            sub_lines = wrap_to_width(p, content_width) if p else [""]
            for s_idx, s_line in enumerate(sub_lines):
                line_chars = len(s_line)
                if char_offset <= chat.caret <= char_offset + line_chars or (
                    p_idx == len(paragraphs) - 1
                    and s_idx == len(sub_lines) - 1
                    and chat.caret >= char_offset
                ):
                    caret_row = len(wrapped_rows)
                    caret_col = display_width(s_line[: max(0, chat.caret - char_offset)])
                wrapped_rows.append(s_line)
                char_offset += line_chars
                if s_idx < len(sub_lines) - 1 and p[char_offset : char_offset + 1] == " ":
                    char_offset += 1
            char_offset += 1

        max_visible = 6
        start_row = 0
        if len(wrapped_rows) > max_visible:
            start_row = max(0, min(caret_row - max_visible // 2, len(wrapped_rows) - max_visible))
            visible_rows = wrapped_rows[start_row : start_row + max_visible]
            caret_row = caret_row - start_row
        else:
            visible_rows = wrapped_rows

        top_divider = self._status_divider(width)

        content_lines: list[str] = []
        for idx, row_text in enumerate(visible_rows):
            prompt = self.theme.fg("accent", "❯ ") if (start_row + idx == 0) else "  "
            body = prompt + row_text
            if not chat.draft and idx == 0:
                body += self.theme.fg("dim", "메시지 입력 (Enter 전송 · Shift+Enter 줄바꿈)")
            line = pad_to_width(truncate_to_width(body, width), width)
            content_lines.append(line)

        caret_cursor = (caret_row, min(width - 1, 2 + caret_col))
        bottom_divider = self.theme.fg("borderMuted", "─" * width)
        return [top_divider, *content_lines, bottom_divider], caret_cursor

    def _delivery_shown(self) -> bool:
        """Whether the tick still has something to say.

        The clock alone answers this, and both the row's presence and its
        contents ask the same question. Expiring the flag from inside the
        draw made the two disagree for exactly one frame: `_status_line`
        still saw a delivery and asked for the row, while the row had just
        expired and came back as a bare rule — a horizontal line flashing
        across the screen on its way to nothing.
        """
        chat = self.chat
        if chat is None or not chat.delivered:
            return False
        return self._clock() - chat.delivered_at < DELIVERED_TIMEOUT_SECONDS

    def _status_divider(self, width: int) -> str:
        """Draw the OmO-style active status divider right above the input."""
        chat = self.chat
        if self._delivery_shown():
            bullet = "✓"
            text = "전송 완료"
            left = f"── {bullet} {text} "
            fill = max(0, width - display_width(left))
            content = (
                self.theme.fg("accent", "── ")
                + self.theme.fg("success", bullet)
                + self.theme.fg("accent", f" {text} ")
                + self.theme.fg("borderMuted", "─" * fill)
            )
            return truncate_to_width(content, width)

        if chat is not None and chat.outbox is not None:
            elapsed_sec = self._clock() - chat.outbox_at
            if elapsed_sec < UNCERTAIN_AFTER_SECONDS:
                elapsed = f"{elapsed_sec:.1f}s"
                bullet = "•"
                left = f"── {bullet} 전송 중… ({elapsed}) "
                fill = max(0, width - display_width(left))
                content = (
                    self.theme.fg("accent", "── ")
                    + self.theme.fg("warning", bullet)
                    + self.theme.fg("accent", " 전송 중… ")
                    + self.theme.fg("dim", f"({elapsed}) ")
                    + self.theme.fg("borderMuted", "─" * fill)
                )
                return truncate_to_width(content, width)
            # Timed out waiting for acknowledgment
            return self.theme.fg("borderMuted", "─" * width)
        if chat is not None and chat.notice:
            if chat.notice_until and self._clock() >= chat.notice_until:
                chat.notice = ""
                chat.notice_until = 0.0
            else:
                bullet = "•"
                # A notice quotes titles, names and error text, all of it
                # somebody else's, so it is cleaned like any line of the
                # conversation.
                notice_text = truncate_to_width(clean(chat.notice), max(1, width - 8))
                left = f"── {bullet} {notice_text} "
                fill = max(0, width - display_width(left))
                content = (
                    self.theme.fg("accent", "── ")
                    + self.theme.fg("warning", bullet)
                    + self.theme.fg("text", f" {notice_text} ")
                    + self.theme.fg("borderMuted", "─" * fill)
                )
                return truncate_to_width(content, width)
        if chat is not None and chat.unseen:
            bullet = "↓"
            text = f" 새 메시지 {chat.unseen}개 (end 키로 이동) "
            left = f"── {bullet}{text}"
            fill = max(0, width - display_width(left))
            content = (
                self.theme.fg("accent", "── ")
                + self.theme.fg("accent", bullet)
                + self.theme.fg("accent", text)
                + self.theme.fg("borderMuted", "─" * fill)
            )
            return truncate_to_width(content, width)
        return self.theme.fg("borderMuted", "─" * width)

    def _status_line(self, width: int) -> str:
        chat = self.chat
        if (chat is not None and chat.outbox is not None) or self._delivery_shown():
            return self._status_divider(width)
        if chat is not None and chat.notice:
            return self._status_divider(width)
        if chat is not None and chat.unseen:
            return self._status_divider(width)
        message = clean(self.status)
        if message:
            return truncate_to_width(self.theme.fg("muted", message), width)
        return ""

    def _footer_rooms(self, width: int) -> str:
        # On macOS one derived key opens every room, so there is no per-room
        # "keys loaded" count to show as the Windows build did; the readable
        # room count is the honest equivalent.
        readable = sum(1 for room in self.rooms if room.readable)
        anchor = [Segment("kamado", self.theme.fg("accent", "kamado"))]
        middle = [
            Segment(f"{len(self.rooms)} rooms", self.theme.fg("muted", f"{len(self.rooms)} rooms")),
            Segment(f"{readable} readable", self.theme.fg("dim", f"{readable} readable")),
        ]
        tail = Segment("read+send", self.theme.fg("dim", "read+send"))
        right = Segment(f"v{__version__}", self.theme.fg("muted", f"v{__version__}"))
        return self._compose_footer(anchor, middle, tail, right, width)

    def _footer_chat(self, width: int, body_lines: int) -> str:
        chat = self.chat
        assert chat is not None
        room = chat.room
        loaded = f"{len(chat.messages)} loaded"
        people = f"{room.kind} · {room.member_count}명"
        if len(self.chats) > 1:
            # The strip above already names this room, and a footer that says
            # it again is the third place a title would appear on one screen.
            # What is left is what the strip cannot say: how big the room is,
            # how much of it is loaded, and what the body is showing.
            anchor = [Segment(people, self.theme.fg("muted", people))]
            middle = [
                Segment(loaded, self.theme.fg("muted", loaded)),
                Segment(f"{body_lines} lines", self.theme.fg("dim", f"{body_lines} lines")),
            ]
        else:
            title = clean(room.title)
            anchor = [Segment(title, self.theme.fg("accent", title))]
            middle = [
                Segment(people, self.theme.fg("muted", people)),
                Segment(loaded, self.theme.fg("muted", loaded)),
                Segment(f"{body_lines} lines", self.theme.fg("dim", f"{body_lines} lines")),
            ]
        mode = "digest" if chat.slices else ("list" if chat.panel else "transcript")
        mouse = CLICK_HINT if self.mouse_clicks else SELECT_HINT
        tail = Segment(f"{mode} · {mouse}", self.theme.fg("dim", f"{mode} · {mouse}"))
        right = Segment(f"v{__version__}", self.theme.fg("muted", f"v{__version__}"))
        return self._compose_footer(anchor, middle, tail, right, width)

    def _compose_footer(
        self,
        anchor: list[Segment],
        middle: list[Segment],
        tail: Segment,
        right: Segment,
        width: int,
    ) -> str:
        plan = plan_footer(anchor, middle, tail, right, width)
        separator = self.theme.fg("borderMuted", " • ")

        if plan.right_plain is not None:
            return self.theme.fg("muted", plan.right_plain)
        if plan.left_plain is not None:
            left = self.theme.fg("muted", plan.left_plain)
        else:
            head = anchor[0]
            if plan.anchor_plain is not None:
                head = Segment(plan.anchor_plain, self.theme.fg("accent", plan.anchor_plain))
            parts = [head, *anchor[1:], *middle[: plan.kept_middle]]
            if plan.show_marker:
                parts.append(Segment(ELLIPSIS, self.theme.fg("dim", ELLIPSIS)))
            parts.append(tail)
            left = separator.join(segment.coloured for segment in parts)

        gap = max(1, width - display_width(left) - display_width(right.coloured))
        return truncate_to_width(left + " " * gap + right.coloured, width)

    # -- input --------------------------------------------------------------

    def handle(self, kind: str, text: str) -> None:
        if kind == "char" and text == SELECT_MODE:
            # Taken before anything else can read it as a character: the
            # composer and the list filter both type what they are given.
            self._toggle_mouse()
            return
        if self.chat is not None and not self.picking:
            self._handle_chat(kind, text)
        elif self.show_friends:
            self._handle_friends(kind, text)
        else:
            self._handle_rooms(kind, text)

    def _handle_rooms(self, kind: str, text: str) -> None:
        if self._filter_key(kind, text):
            return
        shown = self.visible_rooms()
        if self.picking and (
            kind in (Key.ESCAPE, Key.TAB, Key.BACKTAB)
            or (kind == "char" and text in ("q", "Q"))
        ):
            # The list is in front of conversations that are still open, so
            # every way out of it goes back to them rather than out of the
            # interface. While a filter is being typed those keys are letters,
            # and the filter has already taken them.
            self.picking = False
            self._forget_filter()
            self._cancel_opens()
        elif kind == "char" and text in ("q", "Q"):
            self.running = False
        elif kind == Key.INTERRUPT or kind == Key.ESCAPE:
            self.running = False
        elif kind == Key.UP:
            self.selected = max(0, self.selected - 1)
        elif kind == Key.DOWN:
            self.selected = min(len(shown) - 1, self.selected + 1)
        elif kind == Key.WHEEL_UP:
            self.selected = max(0, self.selected - WHEEL_LINES)
        elif kind == Key.WHEEL_DOWN:
            self.selected = max(0, min(len(shown) - 1, self.selected + WHEEL_LINES))
        elif kind == Key.HOME:
            self.selected = 0
        elif kind == Key.END:
            self.selected = max(0, len(shown) - 1)
        elif kind == Key.ENTER and shown:
            # The list stays up until the room is actually read: whether it
            # opened, and what to say if it did not, is answered where the
            # answer lands rather than guessed at here.
            self.open_room(shown[min(self.selected, len(shown) - 1)])
        elif kind == "char" and text in ("r", "R"):
            self.load_rooms()
            self.status = "목록 새로고침"
        elif kind == "char" and text in ("f", "F"):
            self.show_friends = True
            self._forget_filter()
            self._cancel_opens()
            self.status = ""

    def _handle_friends(self, kind: str, text: str) -> None:
        if self._filter_key(kind, text):
            return
        shown = self.visible_friends()
        if kind == "char" and text in ("q", "Q"):
            self.running = False
        elif kind == Key.INTERRUPT:
            self.running = False
        elif kind in (Key.ESCAPE,) or (kind == "char" and text in ("f", "F")):
            self.show_friends = False
            self._forget_filter()
            self._cancel_opens()
        elif kind == Key.UP:
            self.friend_selected = max(0, self.friend_selected - 1)
        elif kind == Key.DOWN:
            self.friend_selected = min(len(shown) - 1, self.friend_selected + 1)
        elif kind == Key.WHEEL_UP:
            self.friend_selected = max(0, self.friend_selected - WHEEL_LINES)
        elif kind == Key.WHEEL_DOWN:
            self.friend_selected = max(0, min(len(shown) - 1, self.friend_selected + WHEEL_LINES))
        elif kind == Key.HOME:
            self.friend_selected = 0
        elif kind == Key.END:
            self.friend_selected = max(0, len(shown) - 1)
        elif kind == Key.ENTER and shown:
            self.open_friend(shown[min(self.friend_selected, len(shown) - 1)])
            self._forget_filter()

    def _handle_chat(self, kind: str, text: str) -> None:
        chat = self.chat
        assert chat is not None

        if kind == Key.INTERRUPT:
            self.running = False
        elif kind == Key.CLICK:
            self._click(text)
        elif kind == Key.RELEASE:
            self._release(text)
        elif kind == Key.WHEEL_UP:
            chat.offset += WHEEL_LINES
        elif kind == Key.WHEEL_DOWN:
            chat.offset = max(0, chat.offset - WHEEL_LINES)
        elif kind == "char":
            if text == CLEAR_BOX:
                chat.draft = ""
                chat.caret = 0
            elif text == SHOW_PHOTO:
                self._toggle_newest_photo(chat)
            elif text == TOGGLE_TIMESTAMP:
                self._toggle_timestamps(chat)
            else:
                chat.delivered = False
                chat.draft = chat.draft[: chat.caret] + text + chat.draft[chat.caret :]
                chat.caret += len(text)
        elif kind == Key.BACKSPACE:
            if chat.caret:
                chat.draft = chat.draft[: chat.caret - 1] + chat.draft[chat.caret :]
                chat.caret -= 1
        elif kind == Key.DELETE:
            chat.draft = chat.draft[: chat.caret] + chat.draft[chat.caret + 1 :]
        elif kind == Key.LEFT:
            chat.caret = max(0, chat.caret - 1)
        elif kind == Key.RIGHT:
            chat.caret = min(len(chat.draft), chat.caret + 1)
        elif kind == Key.ENTER:
            self._submit(chat)
        elif kind == Key.TAB and len(self.chats) > 1:
            self._focus_tab(self.focus + 1)
        elif kind == Key.BACKTAB and len(self.chats) > 1:
            self._focus_tab(self.focus - 1)
        elif kind == Key.ESCAPE:
            # The draft is never thrown away here. It goes back to the room it
            # was being written in, and leaving is just leaving.
            if chat.slices:
                chat.slices = []
                chat.offset = 0
            elif chat.panel:
                chat.panel = []
                chat.offset = 0
            else:
                self._leave_room(chat)
        elif kind == Key.UP:
            if not self._move_caret_vertical(chat, -1):
                chat.offset += 1
        elif kind == Key.DOWN:
            if not self._move_caret_vertical(chat, 1):
                chat.offset = max(0, chat.offset - 1)
        elif kind == Key.PAGE_UP:
            chat.offset += 10
        elif kind == Key.PAGE_DOWN:
            chat.offset = max(0, chat.offset - 10)
        elif kind == Key.HOME:
            # While something is being written these belong to the line; the
            # conversation only gets them back when the box is empty.
            if chat.draft:
                chat.caret = 0
            else:
                chat.anchor_top = True
        elif kind == Key.END:
            if chat.draft:
                chat.caret = len(chat.draft)
            else:
                chat.offset = 0
        if chat.offset == 0:
            chat.unseen = 0
            self._mark_room_seen(chat.room.id)

    def _toggle_mouse(self) -> None:
        """Switch between clicking photos and selecting text with the mouse.

        Only the flag moves here. The escape that changes what the terminal
        reports is written by the loop, which is the one place that holds the
        console — and it writes nothing when the mode did not change.
        """
        self.mouse_clicks = not self.mouse_clicks
        said = CLICK_NOTICE if self.mouse_clicks else SELECT_NOTICE
        self.status = said
        chat = self.chat
        if chat is not None:
            self._passing_notice(chat, said)

    def _click(self, position: str) -> None:
        """Act on the clicked cell: a tab to switch to, a photo, or a link.

        Asking the terminal to report buttons takes its own click handling away,
        so the OSC 8 hyperlinks in the transcript stop opening on their own and
        have to be resolved here. Ghostty still opens them when shift is held,
        which is its documented way past an application that grabs the mouse.

        A click on the strip is how a tab is chosen with the mouse; a click in
        the body belongs to the conversation drawn there.
        """
        try:
            row, column = (int(part) for part in position.split(","))
        except ValueError:
            return
        if self._tab_row is not None and row == self._tab_row:
            self._tab_drag_start = column
            self._tab_click(column)
            return
        self._tab_drag_start = None
        found = self._cell_at(row, column)
        if found is None:
            return
        chat, body_row, body_column = found
        owner = chat.photo_rows.get(body_row)
        if owner is not None:
            self.toggle_photo(owner)
            return
        target = self._link_at(chat, body_row, body_column)
        if target is not None:
            self._open_link(target)

    def _focus_tab(self, index: int) -> None:
        """Move the keyboard to one tab, and let go of what it was holding.

        The tabs walk in a ring, so a step past either end arrives at the
        other. Arrivals the tab was counting have been seen the moment it
        stands in front of the reader at its newest line, so the count goes
        with the move rather than waiting for the next keystroke.
        """
        self.focus = index % len(self.chats)
        chat = self.chat
        if chat is not None and chat.offset == 0:
            chat.unseen = 0
            self._mark_room_seen(chat.room.id)

    def _release(self, position: str) -> None:
        """Act on mouse release: reorder tabs if dragged across the tab strip."""
        start_col = self._tab_drag_start
        self._tab_drag_start = None
        if start_col is None:
            return
        try:
            row, column = (int(part) for part in position.split(","))
        except ValueError:
            return
        if self._tab_row is not None and row == self._tab_row:
            src = self._tab_index_at(start_col)
            dest = self._tab_index_at(column)
            if src is not None and dest is not None and src != dest:
                self._reorder_tab(src, dest)

    def _tab_index_at(self, column: int) -> Optional[int]:
        """Return the tab index under a column, or None."""
        for index, start, columns in self._tab_geometry:
            if start <= column < start + columns:
                return index
        return None

    def _reorder_tab(self, src: int, dest: int) -> None:
        """Move the tab at index *src* to index *dest*."""
        if not (0 <= src < len(self.chats) and 0 <= dest < len(self.chats)):
            return
        chat = self.chats.pop(src)
        self.chats.insert(dest, chat)
        self.focus = dest

    def _tab_click(self, column: int) -> None:
        """Switch to the tab under a clicked column, if one was under it.

        The rule between two tabs, and a marker counting the tabs off the
        edge, belong to nobody: the strip is where a reader points at a room
        they can see, and a near miss is not a choice.
        """
        index = self._tab_index_at(column)
        if index is not None:
            self._focus_tab(index)

    def _cell_at(
        self, screen_row: int, screen_column: int
    ) -> Optional[tuple[ChatState, int, int]]:
        """Return the conversation and body row a clicked cell belongs to.

        One conversation fills the screen, so the row is the only coordinate
        that still has to be resolved: a click above or below the transcript
        belongs to nothing, and the column is handed back as it was, for the
        link that may be under it.
        """
        geometry = self._body_geometry
        chat = self.chat
        if geometry is None or chat is None:
            return None
        top, rows, width = geometry
        if screen_row < top or screen_row >= top + rows:
            return None
        body = self._body_of(chat, width)
        scrollable = len(body) - rows
        offset = min(chat.offset, scrollable) if scrollable > 0 else chat.offset
        end = max(1, len(body) - max(0, offset))
        start = max(0, end - rows)
        body_row = start + (screen_row - top)
        if body_row >= end or body_row >= len(body):
            return None
        return chat, body_row, screen_column

    def _link_at(self, chat: ChatState, body_row: int, column: int) -> Optional[str]:
        """Return the URL under a clicked cell, or None when there is none.

        The transcript keeps its links as OSC 8 sequences, so the target is read
        back out of the rendered line: that is the one place the mapping from a
        visible column to a URL still exists after wrapping.
        """
        geometry = self._body_geometry
        width = geometry[2] if geometry is not None else self._last_width
        body = self._body_of(chat, width)
        if not 0 <= body_row < len(body):
            return None
        seen = 0
        for piece, link in _osc8_segments(body[body_row]):
            span = display_width(strip_ansi(piece))
            if link is not None and seen <= column < seen + span:
                return link
            seen += span
        return None

    def _open_link(self, target: str) -> None:
        """Hand a URL to the system, the way a click on it would have."""
        chat = self.chat
        try:
            subprocess.Popen(
                ["open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            if chat is not None:
                chat.notice = "링크를 여지 못했습니다"
            return
        if chat is not None:
            self._passing_notice(chat, "링크를 열었습니다")

    def _passing_notice(self, chat: ChatState, text: str) -> None:
        """Show a line that clears itself, for news the reader does not act on."""
        chat.notice = text
        chat.notice_until = self._clock() + NOTICE_TIMEOUT_SECONDS

    def _visual_rows(self, chat: ChatState, width: int) -> tuple[list[str], list[int], int]:
        """Split draft into visual rows with draft offsets and caret row."""
        content_width = max(6, width - 4)
        paragraphs = chat.draft.split("\n")
        wrapped_rows: list[str] = []
        row_starts: list[int] = []
        caret_row = 0
        char_offset = 0
        for p_idx, p in enumerate(paragraphs):
            sub_lines = wrap_to_width(p, content_width) if p else [""]
            for s_idx, s_line in enumerate(sub_lines):
                line_chars = len(s_line)
                row_starts.append(char_offset)
                if char_offset <= chat.caret <= char_offset + line_chars or (
                    p_idx == len(paragraphs) - 1
                    and s_idx == len(sub_lines) - 1
                    and chat.caret >= char_offset
                ):
                    caret_row = len(wrapped_rows)
                wrapped_rows.append(s_line)
                char_offset += line_chars
                if s_idx < len(sub_lines) - 1 and p[char_offset : char_offset + 1] == " ":
                    char_offset += 1
            char_offset += 1
        return wrapped_rows, row_starts, caret_row

    def _move_caret_vertical(self, chat: ChatState, direction: int) -> bool:
        """Move caret vertically between visual rows in a multiline draft.

        Returns True if moved within draft, False if caret is already at boundary.
        """
        width = getattr(self, "_last_width", 80)
        wrapped_rows, row_starts, caret_row = self._visual_rows(chat, width)
        if len(wrapped_rows) <= 1:
            return False
        curr_col = chat.caret - row_starts[caret_row] if row_starts else 0
        if direction == -1:  # UP
            if caret_row > 0:
                target = caret_row - 1
                chat.caret = row_starts[target] + min(curr_col, len(wrapped_rows[target]))
                return True
            return False
        if direction == 1:  # DOWN
            if caret_row < len(wrapped_rows) - 1:
                target = caret_row + 1
                chat.caret = row_starts[target] + min(curr_col, len(wrapped_rows[target]))
                return True
            return False
        return False

    def _leave_room(self, chat: ChatState) -> None:
        """Close one tab, keeping whatever was half-written in it.

        Any picture it was showing is withdrawn on the way out. A Kitty
        placement outlives the text it was drawn over, so a photo left behind
        stays on screen -- floating over rows that have nothing to do with it.
        """
        if chat.draft:
            self.drafts[chat.room.id] = chat.draft
        else:
            self.drafts.pop(chat.room.id, None)
        chat.opened_photos.clear()
        self.placements = []
        if chat.unseen == 0:
            self._mark_room_seen(chat.room.id)
        if chat in self.chats:
            index = self.chats.index(chat)
            self.chats.pop(index)
            self.focus = max(0, min(self.focus if index > self.focus else self.focus - 1,
                                    len(self.chats) - 1))
        self._queue_cleanup(Job(UNWATCH, room_id=chat.room.id))
        if not self.chats:
            # Nothing is left to follow, so the watcher is given up rather
            # than kept priming a database nobody on screen is reading.
            self._queue_cleanup(Job(RELEASE))

    def _submit(self, chat: ChatState) -> None:
        """Send what is written, or run it when it starts with a slash."""
        written = chat.draft.strip()
        chat.draft = ""
        chat.caret = 0
        self.drafts.pop(chat.room.id, None)
        if not written:
            return
        if written.startswith("/"):
            self._run_command(written)
            return
        # Held for one frame: opening a window takes over a second, and doing
        # it inside the keystroke left the screen on the frame before it.
        self.pending_send = written
        chat.notice = "보내는 중…"

    def flush_pending(self) -> None:
        """Send what the last keystroke asked for, once a frame has been drawn."""
        text = self.pending_send
        self.pending_send = None
        if text is None or self.chat is None:
            return
        self.chat.notice = ""
        self.send_message(text)

    def _require_sender(self) -> "MessageSender":
        if self._sender is None:
            from ..macauto import make_sender

            self._sender = make_sender()
        return self._sender

    def _refuse_ambiguous(self, room: Room) -> None:
        """Refuse when a window title cannot identify one catalogued room.

        The census is the one this screen already loaded. Re-reading the
        catalog here cost up to 3.0 s per send: a busy room rewrites it
        constantly, and sending rewrites it too, so the snapshot almost always
        had to be decrypted again before the message could go out.

        The refusal names the rooms it is torn between, because the way out is
        in the client rather than here: a title is the only thing the
        accessibility layer can address a window by, so nothing short of one
        of those rooms getting a name of its own resolves it. Naming one is
        enough, now that a name set in the client is read back as the room's
        title instead of being left to the member list.
        """
        # The census is the catalog this screen already holds. Re-reading it
        # here is not merely expensive now, it is impossible: the catalog
        # belongs to the owner thread, and this runs on the one handling the
        # keystroke that asked to send. A screen with no catalog at all has
        # never opened a room, so there is nothing to be torn between.
        census = self.titles
        same = [title for title in census if title == room.title]
        if len(same) <= 1:
            return
        described = ", ".join(
            f"{other.kind} {other.member_count}명 {_stamp(other.last_updated_at, self.zone)}"
            for other in self.rooms
            if other.title == room.title and other.id != room.id
        )
        raise KamadoError(
            ErrorCode.AMBIGUOUS_CHAT,
            f"'{room.title}' 이름의 대화방이 {len(same)}개라 창을 특정할 수 없습니다"
            + (f" (다른 방: {described})" if described else ""),
        )

    def send_message(self, text: str) -> None:
        """Hand *text* to this room's own input box in the running client.

        Nothing is echoed here. The message appears the way anyone else's
        does, once the watcher sees the client write it to the log, so the
        screen never shows a line the client did not actually send.
        """
        chat = self.chat
        assert chat is not None
        try:
            self._refuse_ambiguous(chat.room)
            # The room is the one being read, so opening its window needs no
            # further confirmation than having typed into it.
            self._require_sender().send(
                chat.room.title,
                text,
                open_if_needed=True,
                aliases=[chat.window_alias] if chat.window_alias else [],
                hint=self.titles.index(chat.room.title)
                if chat.room.title in self.titles
                else None,
            )
        except KamadoError as error:
            chat.notice = f"전송 실패: {error.message}"
            return
        except OSError as error:
            chat.notice = f"전송 실패: {error}"
            return
        # Marked beside the prompt rather than announced: the composer is
        # where the message was, so that is where its fate belongs.
        chat.outbox = text
        chat.outbox_at = self._clock()
        chat.delivered = False
        chat.notice = ""

    def _run_command(self, command: str) -> None:
        chat = self.chat
        assert chat is not None
        parts = command.strip().lstrip("/").split()
        if not parts:
            return
        name, arguments = parts[0].lower(), parts[1:]

        if name in ("sum", "digest"):
            if not arguments:
                chat.notice = "구간 길이가 필요합니다 (예: /sum 30m, /sum 2h)"
                return
            try:
                span = digest.parse_duration(arguments[0])
            except digest.DurationError:
                # The library speaks English to its other callers; the screen
                # the user is looking at should not switch languages mid-error.
                chat.notice = (
                    f"'{arguments[0]}' 은(는) 쓸 수 없는 구간입니다. "
                    f"30m, 2h 처럼 적어주세요 (최대 {int(digest.MAX_SPAN.total_seconds() // 3600)}h)"
                )
                return
            slices = digest.slice_messages(_spoken(chat.messages), span)
            if not slices:
                chat.notice = "요약할 메시지가 없습니다"
                return
            chat.slices = slices
            chat.slice_span = span
            chat.slice_label = f"{arguments[0]} 구간 · {len(slices)}개 · 최근 12h 이내"
            # A digest is read from its beginning, unlike a conversation.
            chat.anchor_top = True
            chat.notice = ""
        elif name in ("older", "o"):
            chat.notice = ""
            self.load_older()
        elif name in ("export", "e"):
            if not chat.slices:
                chat.notice = "내보낼 다이제스트가 없습니다 (/sum 30m 먼저)"
                return
            self._export(chat)
        elif name == "real":
            # Joined rather than indexed: a nickname routinely holds spaces,
            # and "Some Name (handle)" is one name, not two arguments.
            self._real(chat, " ".join(arguments))
        elif name in ("photo", "pic"):
            self._toggle_newest_photo(chat)
        elif name == "notice":
            self._ask(Job(NOTICE, room_id=chat.room.id, token=chat.token, about="show"))
        elif name in ("files", "file", "links", "link", "photos", "media", "videos"):
            kind = {"files": "file", "links": "link", "media": "photo", "photos": "photo",
                    "videos": "video"}.get(name, name)
            self._ask(
                Job(
                    ATTACHMENTS,
                    room_id=chat.room.id,
                    token=chat.token,
                    about=kind,
                    payload=(kind, ATTACHMENT_LIST_LIMIT),
                )
            )
        elif name == "edits":
            self._show_edits(chat)
        elif name == "safebot":
            self._toggle_covered(chat)
        elif name in ("time", "timestamp", "timestamps"):
            self._toggle_timestamps(chat)
        elif name in ("help", "?"):
            self._show_help(chat)
        elif name in ("q", "quit", "exit"):
            self.running = False
        elif name in ("back", "close"):
            self._leave_room(chat)
        elif name == "open":
            self._open_beside(chat, " ".join(arguments))
        else:
            chat.notice = (
                f"알 수 없는 명령: /{name}  "
                "(사용 가능: /help · /open 방 · /close · /sum 30m · /older · "
                "/real 닉 · /time · /photo · /notice · /files · /links · /photos · /videos · "
                "/edits · /safebot · /export · /quit)"
            )

    def _open_beside(self, chat: ChatState, wanted: str) -> None:
        """Open another conversation as a tab, by name.

        Matched against the catalog this screen already holds, so adding a
        tab costs no decryption. An exact title wins over a longer one
        containing it, and an ambiguous name is answered with its candidates
        rather than resolved by taking the first.
        """
        if not wanted:
            # Nothing named: put the list up and let them point at one.
            self.picking = True
            return
        folded = wanted.casefold()
        exact = [room for room in self.rooms if room.title.casefold() == folded]
        matches = exact or [room for room in self.rooms if folded in room.title.casefold()]
        if not matches:
            chat.notice = f"'{wanted}' 에 맞는 대화방이 없습니다"
            return
        if len(matches) > 1:
            shown = ", ".join(room.title for room in matches[:3])
            more = " …" if len(matches) > 3 else ""
            chat.notice = f"'{wanted}' 에 맞는 방이 {len(matches)}개입니다: {shown}{more}"
            return
        self.open_room(matches[0])

    def _real(self, chat: ChatState, who: str) -> None:
        """Show what a name's owner used to be called, and who else wore it.

        Both halves matter.  One chain answers "what was this person before";
        two chains answer "which of these two is the impostor", and that second
        question is the one a reader has when they type this at all.
        """
        if not who:
            chat.notice = "이름이 필요합니다 (예: /real 진짜유성진)"
            return
        self._ask(Job(NAMES, room_id=chat.room.id, token=chat.token, about=who))

    def _settle_names(self, done: Done) -> bool:
        chat = self._tab(done.job.token)
        if chat is None:
            return False
        who = done.job.about
        if done.error is not None:
            chat.notice = f"이름 이력을 읽지 못했습니다: {_said(done.error)}"
            return True
        found, steps = done.value
        if not found:
            chat.notice = f"'{who}' 을(를) 쓴 사람이 기록에 없습니다"
            return True
        chains = [
            " → ".join(self._name_step(step) for step in steps[user_id])
            for user_id in found
        ]
        prefix = "" if len(found) == 1 else f"이 이름을 쓴 계정 {len(found)}개 · "
        chat.notice = prefix + "  |  ".join(chains)
        return True

    def _name_step(self, record: "NameRecord") -> str:
        """Render one step as name and when it was seen, never as a bare name."""
        from ..names import CURRENT

        if record.source == CURRENT:
            return f"{record.name}(지금)"
        stamp = _stamp(record.at, self.zone)
        return f"{record.name}({stamp})" if stamp else record.name

    def _export(self, chat: ChatState) -> None:
        """Write the sliced text beside the user's other files, never into the repo."""
        target = os.path.join(
            os.path.expanduser("~"), f"kamado-cli-{chat.room.id}-digest.txt"
        )
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(f"# {chat.room.title}\n# {chat.slice_label}\n\n")
                handle.write(digest.as_text(chat.slices, self.zone))
        except OSError as error:
            chat.notice = f"내보내기 실패: {error}"
            return
        chat.notice = f"원문 내보냄: {target}"

    # -- loop ---------------------------------------------------------------

    def run(self, console: Console) -> None:
        width, height = force_size() or console.size
        self.status = "카카오톡 로컬 데이터베이스를 불러오는 중..."
        console.draw(self.render(width, height), self.cursor)
        self._measure_cell(console)

        # Asked for, not waited for. The catalog reaches the screen through
        # the same drain as everything else, so the first frame is drawn and
        # answering the keyboard begins before the database has said anything.
        self.load_rooms()
        due_at = 0.0
        last_key_at = 0.0
        while self.running:
            width, height = force_size() or console.size
            console.set_mouse(self.mouse_clicks)
            self._pump(width)
            self._paint(console, width, height)

            kind, text = console.read_key(KEY_WAIT_SECONDS)
            if kind or text:
                last_key_at = time.monotonic()
                self.handle(kind, text)
                if self.pending_send is not None:
                    self._paint(console, width, height)
                    self.flush_pending()
                continue

            now = time.monotonic()
            composing = self.chat is not None and bool(self.chat.draft)
            if not _poll_due(now, due_at, last_key_at, composing):
                continue
            self.refresh(width)
            due_at = _next_poll_at(time.monotonic(), time.monotonic() - now)


def _osc8_segments(line: str) -> list[tuple[str, Optional[str]]]:
    """Split a rendered line into runs, each paired with its link target.

    A hyperlink is written as ``OSC 8 ; ; url ST text OSC 8 ; ; ST``, so the
    target is not in the visible text and can only be recovered by reading the
    sequences back out. Everything outside such a pair carries no link.
    """
    opener = re.compile(r"\x1b]8;;(.*?)(?:\x1b\\|\x07)")
    runs: list[tuple[str, Optional[str]]] = []
    position = 0
    target: Optional[str] = None
    for found in opener.finditer(line):
        if found.start() > position:
            runs.append((line[position : found.start()], target))
        url = found.group(1)
        target = url or None
        position = found.end()
    if position < len(line):
        runs.append((line[position:], target))
    return runs


def _poll_due(now: float, due_at: float, last_key_at: float, composing: bool) -> bool:
    """Whether to look for arrivals now.

    Keep the existing typing grace to avoid unnecessary background work while
    composing. It is no longer the protection against blocked input: database
    jobs run on their owner thread, and completed jobs are drained every frame
    regardless of this gate.
    """
    grace = COMPOSING_GRACE_SECONDS if composing else TYPING_GRACE_SECONDS
    if now - last_key_at < grace:
        return False
    return now >= due_at


def _next_poll_at(now: float, work_seconds: float) -> float:
    """Space refresh submissions; the worker coalesces unfinished polls."""
    return now + max(REFRESH_SECONDS, work_seconds)


def _spoken(messages: Sequence[Message]) -> list[Message]:
    """What a digest is cut from: every message except the ones taken back.

    The screen keeps a deleted message as a placeholder so the gap shows, but
    a digest counts and quotes what was said, and that was unsaid.
    """
    return [message for message in messages if not message.erased]


def _same_words(text: str) -> str:
    """Reduce a message to its words, so spacing cannot hide a match."""
    return " ".join(text.split())


def _clamp_scroll(selected: int, scroll: int, height: int, total: int) -> int:
    if total <= height:
        return 0
    scroll = min(scroll, selected)
    scroll = max(scroll, selected - height + 1)
    return max(0, min(scroll, total - height))


def _window(lines: list[str], offset: int, height: int) -> list[str]:
    """Take *height* lines ending *offset* lines above the bottom.

    When the body is taller than the screen the offset stops where the first
    line reaches the top; without that stop, the home key left one line on
    screen instead of the opening screenful. A body that already fits keeps
    the older behaviour, where scrolling pushes it off the bottom — that is
    what holds arrivals below the fold in a quiet room.
    """
    if not lines:
        return []
    scrollable = len(lines) - height
    if scrollable > 0:
        offset = min(offset, scrollable)
    end = max(1, len(lines) - max(0, offset))
    start = max(0, end - height)
    return lines[start:end]


def _size(count: int) -> str:
    """A byte count the way a file list shows one."""
    if count < 1024:
        return f"{count}B"
    value = count / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _stamp(epoch: Optional[int], zone: timezone) -> str:
    if epoch is None:
        return ""
    moment = datetime.fromtimestamp(epoch, timezone.utc).astimezone(zone)
    today = datetime.now(zone).date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    if (today - moment.date()).days < 7:
        return moment.strftime("%a %H:%M")
    return moment.strftime("%m-%d")


def _said(error: BaseException) -> str:
    """What to put on screen about a failure the owner thread carried back."""
    return error.message if isinstance(error, KamadoError) else str(error)


def launch() -> int:
    """Run the browser, restoring the terminal whatever happens.

    The reader is opened by the worker on the worker's thread and closed by it
    there, so no connection this process makes is ever touched by two threads.
    The console is entered first so it is left last: the database is given up
    while the screen still holds its last frame, rather than after the
    terminal has been handed back to a shell that then appears to hang.
    """
    with Console() as console, DatabaseWorker(open_reader=Reader) as owner:
        App(db=owner).run(console)
    return 0
