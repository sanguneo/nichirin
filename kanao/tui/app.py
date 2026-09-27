"""The interactive browser: a room list, a conversation, and time digests.

The shape follows OmO's interactive mode so the two feel like one toolchain:
a title row with status pinned right, hint rows beneath it, a body, and a
single footer line that gives up its middle before its edges.

Every frame is rebuilt whole: a caller never decides what to repaint, because
that is how partial redraws flicker and strand stale cells. `Console.draw`
writes only the rows that changed, which is its business and not this module's.
The conversation is the one thing not rebuilt — deliberately so, since
`_transcript_lines` is cached
per (width, message count), because rebuilding it on every keystroke measured
71 ms per key at the default window. Anything that changes a loaded message
must change that key.
"""

from __future__ import annotations

import os
import sqlite3
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, TYPE_CHECKING

from .. import __version__
from ..errors import ErrorCode, KanaoError
from ..model import (
    ERASED_PLACEHOLDER,
    FEED_MESSAGE,
    Friend,
    Message,
    Room,
    is_feed_sentence,
    message_kind,
)
from ..reader import UNLOCK_SECONDS, Reader
from . import digest, images
from .console import CLEAR_BOX, SHOW_PHOTO, Console, Key, force_size
from .layout import (
    ELLIPSIS,
    Segment,
    clean,
    display_width,
    elide_head,
    pad_to_width,
    plan_footer,
    truncate_to_width,
    window_around,
    wrap_to_width,
    wrap_with_links,
)
from ..watch import Watcher
from .theme import SpeakerPalette, Theme

if TYPE_CHECKING:
    from ..names import NameRecord
    from ..winauto import MessageSender

NEWLINE_MARK = "↵"
"""How a line break inside a draft is shown in a one-line composer."""

KST = timezone(timedelta(hours=9))
HISTORY_WINDOW = 400
LOAD_MORE_STEP = 400
KEY_WAIT_SECONDS = 0.12
"""How long a frame waits for input before checking for new messages."""
UNCERTAIN_AFTER_SECONDS = 30.0
"""How long a send may go unseen before the mark stops claiming to be sure."""
REFRESH_SECONDS = 0.15
"""How often to look for arrivals. A poll that finds nothing costs one stat.

This was 0.35 while a poll that *did* find something re-decrypted the whole
database. Now that an arrival only lays the log over pages already decrypted,
the room can be asked more often without the screen paying for it.
"""
TYPING_GRACE_SECONDS = 0.4
"""How long after a keystroke to leave the conversation alone."""
COMPOSING_GRACE_SECONDS = 1.5
"""How long a half-written message holds a poll off, IME pauses included."""
MIN_PANE_COLUMNS = 44
"""The narrowest a conversation can be drawn in and still read as one.

Hangul takes two columns a character, so the time and a name already spend a
third of this before any words arrive.
"""


INLINE_CACHE_ENTRIES = 64
"""How many built picture escapes to keep.

Scrolling asks for a new crop every line, so the keys are unbounded and
something has to be dropped; 64 covers a picture moving through a pane several
times over without letting one room's photos grow without limit.
"""


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
    watcher: Optional[Watcher] = None
    """Follows this room so arrivals show up without reopening it."""
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
    window_alias: Optional[str] = None
    """The title this room's window carries, when it differs from its own.

    Read when the room is opened rather than when a message is sent: the
    catalog is rewritten constantly, and re-reading it cost 3.0 s per send.
    """
    feeds: dict[int, str] = field(default_factory=dict)
    """What each feed row in this window says about itself, asked once."""
    rendered: Optional[tuple[tuple[int, int, int, frozenset[int]], list[str]]] = None
    """Transcript lines and what they were built from, so typing costs nothing."""
    versions: dict[int, int] = field(default_factory=dict)
    """What each loaded row held when its message was built, by id.

    A message on screen can change without a new one arriving - deleted for
    everyone, it keeps its id - so this is what a moved log is compared with.
    """
    revision: int = 0
    """Bumped whenever a loaded message is replaced or removed.

    The transcript is cached by message count, and a message deleted in
    place changes no count: without this the screen would keep the text.
    """
    message_rows: dict[int, int] = field(default_factory=dict)
    """Which transcript row each message starts on, rebuilt with it."""
    opened_photos: set[int] = field(default_factory=set)
    """Which messages are showing their picture rather than a label."""
    photo_rows: dict[int, int] = field(default_factory=dict)
    """Which transcript row each opened picture starts on, rebuilt every frame.

    A picture is not text and cannot live in a line, so the transcript reserves
    blank rows for it and the escape that draws it is laid over them after the
    frame is painted. This is how the two halves find each other.
    """


class App:
    """Owns the screen state and turns keystrokes into it."""

    def __init__(
        self,
        reader: Reader,
        theme: Optional[Theme] = None,
        sender: Optional["MessageSender"] = None,
    ) -> None:
        self.reader = reader
        self.theme = theme if theme is not None else Theme()
        self._sender = sender
        """Built on the first send so browsing never touches the desktop."""
        self.rooms: list[Room] = []
        self.selected = 0
        self.scroll = 0
        self.chats: list[ChatState] = []
        """Every conversation on screen, left to right."""
        self.focus = 0
        """Which of them the composer and the footer belong to."""
        self.palette = SpeakerPalette(dark=self.theme.dark)
        self.status = ""
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
        self.adding = False
        """Whether the room list is in front of conversations that stay open."""
        self.pending_send: Optional[str] = None
        self.pending_unlock: Optional[tuple[Room, bool]] = None
        """A room to open in the client, held for one frame like a send."""
        self._clock = time.monotonic
        """Read rather than called directly, so a test can hold time still."""
        """Text the last keystroke asked to send, not sent yet.

        Sending can open a window, which took 1.7 s with the screen frozen on
        the frame before the keystroke. Holding it here lets the loop paint
        once, so the wait is visible rather than mistaken for a hang.
        """
        self.cursor: Optional[tuple[int, int]] = None
        """Where the terminal caret belongs, so an IME composes in the box."""
        self._photos: images.PhotoSource = images.PhotoStore()
        """Where a picture's bytes come from; replaced outright in tests."""
        self._protocol = images.detect()
        """Which inline-image protocol this terminal draws, or ``none``."""
        self._cell: Optional[tuple[int, int]] = None
        """The console's cell size in pixels, asked once after the first frame."""
        self._photo_bytes: dict[int, bytes] = {}
        """Fetched pictures, so opening one twice costs one fetch."""
        self._photo_shapes: dict[int, tuple[int, int]] = {}
        """Each picture's measured size, which the stored one can disagree with."""
        self.placements: list[
            tuple[int, int, int, bytes, int, int, Optional[tuple[int, int]]]
        ] = []
        """What to draw over the painted frame: id, row, column, bytes, cells, crop."""
        self._drawn: dict[int, tuple[int, int, int, int, Optional[tuple[int, int]]]] = {}
        """Where each picture currently sits, so a still screen redraws nothing."""
        self._images_moved = False
        """Whether a picture moved, so the frame has to be given up whole."""
        self._pane_rows = 0
        """How tall a conversation pane last was, so a picture scales with it."""
        self._inline_cache: dict[
            tuple[str, int, int, int, Optional[tuple[int, int]]], Optional[str]
        ] = {}
        """Escapes already built, keyed by the box they were built for."""

    @property
    def chat(self) -> Optional[ChatState]:
        """The conversation the keyboard is in, of however many are open.

        Everything that acts on "the" conversation — sending, scrolling, the
        footer, the commands — means this one, which is why it stays a single
        attribute rather than every caller learning about panes.
        """
        if not self.chats:
            return None
        self.focus = max(0, min(self.focus, len(self.chats) - 1))
        return self.chats[self.focus]

    @chat.setter
    def chat(self, value: Optional[ChatState]) -> None:
        """Replace every open conversation with this one, or with none."""
        self.chats = [] if value is None else [value]
        self.focus = 0

    def _pane_width(self, width: int, panes: int) -> int:
        """How wide one pane is, once the dividers have taken their columns."""
        return max(1, (width - (panes - 1)) // max(1, panes))

    def _visible_panes(self, width: int) -> list[ChatState]:
        """The conversations that fit, keeping the focused one among them.

        A pane narrower than `MIN_PANE_COLUMNS` cannot hold a conversation —
        Hangul is two columns a character, so the name and the time alone eat
        half of a narrow one. Rooms that do not fit are hidden rather than
        closed, because the reader opened them and a window that got smaller
        is not a decision to let one go.
        """
        if not self.chats:
            return []
        fits = max(1, (width + 1) // (MIN_PANE_COLUMNS + 1))
        if fits >= len(self.chats):
            return list(self.chats)
        start = min(max(0, self.focus - fits + 1), len(self.chats) - fits)
        return self.chats[start : start + fits]

    # -- data ---------------------------------------------------------------

    def load_rooms(self) -> None:
        catalogued = self.reader.rooms()
        self.titles = [room.title for room in catalogued]
        # Every room, not only the readable ones: a room whose key is not
        # loaded can be opened in the client from here, which loads it.
        self.rooms = list(catalogued)
        self._remember_members()

    def _remember_members(self) -> None:
        """Read who is in which room while the catalog is fresh anyway.

        Predicting where the client's search lists a room needs it, and a
        send must not read the catalog to find out.
        """
        try:
            self.reader.memberships(refresh=True)
        except KanaoError:
            pass

    def visible_rooms(self) -> list[Room]:
        """The rooms the filter leaves, or all of them."""
        if not self.filter:
            return self.rooms
        needle = self.filter.casefold()
        return [room for room in self.rooms if needle in room.title.casefold()]

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
        self.friends = self.reader.friends()

    def open_friend(self, friend: Friend) -> None:
        """Open a friend's 1:1 room, if the client has cached its key."""
        if friend.direct_chat_id is None:
            self.status = f"{friend.name}: 1:1 대화방이 로컬에 없습니다"
            return
        room = next((r for r in self.rooms if r.id == friend.direct_chat_id), None)
        if room is None:
            self.status = f"{friend.name}: 1:1 대화방이 목록에 없습니다"
            return
        self.show_friends = False
        self.request_open(room)

    def request_open(self, room: Room, beside: bool = False) -> None:
        """Open a conversation, first having the client load it if it must.

        A room whose key is not in memory is one the client has not opened,
        so the client is asked to open it - which marks it read there, the
        cost of reading it here. That takes a second or more, so it is held
        for one frame the way a send is, and the screen says what it waits on.
        """
        if room.readable:
            self.open_room(room, beside=beside)
            return
        self.pending_unlock = (room, beside)
        self._say(f"카카오톡에서 '{room.title}' 방을 여는 중…")

    def _unlock(self, room: Room, beside: bool) -> None:
        """Open *room* in the client, wait for its key, then show it."""
        try:
            self._refuse_ambiguous(room)
            stored = self.reader.stored_title(room.id)
            sender = self._require_sender()
            window = sender.resolve(
                room.title,
                open_if_needed=True,
                aliases=[stored] if stored and stored != room.title else [],
                hint=self.titles.index(room.title) if room.title in self.titles else None,
                search_row=self.reader.search_position(room.id, rooms=self.rooms),
            )
            repark = getattr(sender, "repark", None)

            def settle() -> None:
                # The client can raise the window again while it loads a room
                # it never opened, taking the keyboard from this terminal.
                if repark is not None and window is not None:
                    repark(window)
                time.sleep(UNLOCK_SECONDS)

            unlocked = self.reader.unlock(room.id, settle=settle)
            if repark is not None and window is not None:
                repark(window)
            if not unlocked:
                raise KanaoError(
                    ErrorCode.KEY_UNAVAILABLE,
                    f"'{room.title}' 방을 열었지만 키가 아직 올라오지 않았습니다. 다시 시도해 주세요",
                )
            self.load_rooms()
            opened = next((one for one in self.rooms if one.id == room.id), None)
            if opened is None or not opened.readable:
                raise KanaoError(
                    ErrorCode.KEY_UNAVAILABLE, f"'{room.title}' 방을 아직 읽을 수 없습니다"
                )
            self.open_room(opened, beside=beside)
            self.adding = False
            self._forget_filter()
            self.status = ""
        except KanaoError as error:
            self._say(f"열기 실패: {error.message}")
        except OSError as error:
            self._say(f"열기 실패: {error}")

    def _say(self, text: str) -> None:
        """Put a line where the current view shows one."""
        self.status = text
        if self.chat is not None:
            self.chat.notice = text

    def open_room(self, room: Room, beside: bool = False) -> None:
        """Show a conversation, replacing what is open or joining it.

        *beside* is what `/open` asks for: the rooms already on screen stay,
        and this one takes the keyboard. A room that is already open is
        focused rather than opened twice, because two panes of one
        conversation would each hold their own draft of the same message.
        """
        already = next(
            (index for index, pane in enumerate(self.chats) if pane.room.id == room.id),
            None,
        )
        if already is not None:
            self.focus = already
            return

        versions: dict[int, int] = {}
        messages = self.reader.messages(
            room.id, limit=HISTORY_WINDOW, include_erased=True, versions=versions
        )
        if not beside:
            self.palette = SpeakerPalette(dark=self.theme.dark)
        for message in messages:
            self.palette.hue(message.sender)
        watcher = Watcher(
            self.reader, room_ids=[room.id], interval=REFRESH_SECONDS, include_erased=True
        )
        watcher.prime()
        stored = self.reader.stored_title(room.id)
        kept = self.drafts.get(room.id, "")
        # A conversation opens at its newest line, not at its oldest.
        opened = ChatState(
            room=room,
            messages=messages,
            watcher=watcher,
            anchor_top=False,
            draft=kept,
            caret=len(kept),
            window_alias=stored if stored and stored != room.title else None,
            versions=versions,
        )
        if beside:
            self.chats.append(opened)
            self.focus = len(self.chats) - 1
        else:
            self.chat = opened

    def _open_beside(self, chat: ChatState, name: str) -> None:
        """Put another conversation on screen beside the ones already open.

        Named, it opens straight away; unnamed, the room list comes to the
        front and what is open stays open behind it. An ambiguous name is
        refused with the count rather than resolved by taking the first
        match, the same way every other lookup here refuses one.
        """
        if not name.strip():
            self.adding = True
            self._forget_filter()
            self.status = ""
            return
        needle = name.strip().casefold()
        matches = [room for room in self.rooms if room.title.casefold() == needle]
        if not matches:
            matches = [room for room in self.rooms if needle in room.title.casefold()]
        if not matches:
            chat.notice = f"'{name}': 그런 이름의 대화방이 없습니다"
            return
        if len(matches) > 1:
            shown = ", ".join(room.title for room in matches[:3])
            chat.notice = f"'{name}': {len(matches)}개가 맞습니다 ({shown})"
            return
        chat.notice = ""
        self.request_open(matches[0], beside=True)

    def close_pane(self, chat: ChatState) -> None:
        """Close one conversation, keeping whatever else is open.

        The focus moves to the pane on the left, the way closing a tab does,
        and the draft is kept per room so reopening restores it.
        """
        if chat not in self.chats:
            return
        index = self.chats.index(chat)
        self.chats.pop(index)
        if index <= self.focus:
            self.focus = max(0, self.focus - 1)

    def refresh(self, width: int) -> bool:
        """Pick up anything new, for every conversation on screen.

        Each pane keeps its own watcher because a Windows room is its own
        database: the poll that decides whether anything happened is one stat
        of that room's log, 0.30 ms measured, so panes cost what they read
        rather than sharing a fingerprint the way the macOS build can.
        """
        if not self.chats:
            return self._refresh_rooms()
        panes = self._visible_panes(width)
        cell = self._pane_width(width, max(1, len(panes)))
        drawn = False
        for pane in list(self.chats):
            # An off-screen pane is still followed, or its unseen count would
            # start from whenever the window happened to get wider.
            drawn |= self._refresh_pane(pane, cell if pane in panes else width)
        return drawn

    def _refresh_pane(self, chat: ChatState, width: int) -> bool:
        """Pick up what arrived in one conversation, and what changed in it."""
        if chat.watcher is None:
            return False
        try:
            arrived = chat.watcher.poll()
        except KanaoError as error:
            chat.notice = error.message
            return True
        drawn = self._append_arrivals(chat, arrived, width)
        if chat.room.id in getattr(chat.watcher, "moved", ()):
            drawn |= self._reconcile(chat, width)
        return drawn

    def _append_arrivals(self, chat: ChatState, arrived: Sequence[Message], width: int) -> bool:
        """Add what is new to the conversation, holding the reader's place."""
        if not arrived:
            return False
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
            chat.unseen += len(fresh)
        else:
            chat.unseen = 0
        return True

    def _reconcile(self, chat: ChatState, width: int) -> bool:
        """Bring messages already on screen up to date with a log that moved.

        The watcher only asks for ids above the newest it has seen, so a
        message deleted for everyone - which keeps its id and has its text
        replaced - stayed on screen as it was, and so did anything built from
        it: the digest, the export. The rows the window holds are compared by
        version, one range read with no names or quotes, and only a row that
        changed is read again; a row no longer returned at all is taken out.

        What a row held and the message built from it always come from the
        same read, so a change landing between two reads is caught by the
        next poll rather than filed as already seen.
        """
        if not chat.messages:
            return False
        low, high = chat.messages[0].id, chat.messages[-1].id
        try:
            current = self.reader.row_versions(chat.room.id, low, high, include_erased=True)
        except (KanaoError, OSError, sqlite3.Error):
            return False
        held = [message.id for message in chat.messages]
        removed = {one for one in held if one not in current}
        stale = [
            one for one in held if one in current and chat.versions.get(one) != current[one]
        ]
        if not removed and not stale:
            return False
        rebuilt: dict[int, Message] = {}
        if stale:
            fresh_versions: dict[int, int] = {}
            try:
                again = self.reader.messages_at(
                    chat.room.id, stale, include_erased=True, versions=fresh_versions
                )
            except (KanaoError, OSError, sqlite3.Error):
                return False
            rebuilt = {message.id: message for message in again}
            chat.versions.update(fresh_versions)
            # Gone between the two reads: as removed as a row the first missed.
            removed |= {one for one in stale if one not in rebuilt}
        if not removed and all(
            rebuilt[message.id] == message for message in chat.messages if message.id in rebuilt
        ):
            # A row whose version was simply not known yet - an arrival read
            # a moment earlier - says nothing new; the screen stays as it is.
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
            chat.slices = digest.slice_messages(_said(chat.messages), chat.slice_span)
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
            survivor = next((rows[one] for one in after if one in rows), len(lines))
            bottom = survivor
        chat.offset = max(0, len(lines) - bottom)

    def _confirm_delivery(self, chat: ChatState, arrived: Sequence[Message]) -> None:
        """Turn the queued mark into a tick once the client writes it back.

        The conversation is named rather than assumed: with several open, the
        message that comes back belongs to the room it arrived in, not to
        whichever one the keyboard happens to be in.
        """
        if chat.outbox is None:
            return
        # Compared by its words: the client is free to tidy the spacing of
        # what it stores, and a tick that never arrives is worse than one
        # that arrives for a message wearing slightly different whitespace.
        wanted = _same_words(chat.outbox)
        if any(message.from_me and _same_words(message.text) == wanted for message in arrived):
            chat.outbox = None
            chat.delivered = True

    def _refresh_rooms(self) -> bool:
        """Re-read the catalog so unread counts and ordering stay current."""
        try:
            catalogued = self.reader.rooms()
        except KanaoError:
            return False
        self.titles = [room.title for room in catalogued]
        rooms = list(catalogued)
        self._remember_members()
        changed = [
            (room.id, room.unread, room.last_log_id, room.readable) for room in rooms
        ] != [(room.id, room.unread, room.last_log_id, room.readable) for room in self.rooms]
        if not changed:
            return False
        selected_id = self.rooms[self.selected].id if self.rooms else None
        self.rooms = rooms
        if selected_id is not None:
            for index, room in enumerate(rooms):
                if room.id == selected_id:
                    self.selected = index
                    break
        self.selected = max(0, min(self.selected, len(self.rooms) - 1))
        return True

    def load_older(self) -> None:
        chat = self.chat
        if chat is None or not chat.messages:
            return
        # Erased rows are kept here as they are everywhere else on this
        # screen; without them an older page lost the placeholders.
        older = self.reader.messages(
            chat.room.id,
            limit=LOAD_MORE_STEP,
            before_id=chat.messages[0].id,
            include_erased=True,
            versions=chat.versions,
        )
        if not older:
            chat.notice = "이전 기록이 더 없습니다"
            return
        chat.messages = older + chat.messages
        chat.notice = f"이전 {len(older)}개 불러옴 (총 {len(chat.messages)})"

    # -- rendering ----------------------------------------------------------

    def render(self, width: int, height: int) -> list[str]:
        self.cursor = None
        # `/open` puts the list in front of conversations that stay open.
        if self.adding:
            return self._render_rooms(width, height)
        if self.chat is not None:
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
        readable = sum(1 for room in shown if room.readable)
        right = (
            self.theme.fg("accent", f"◉ {readable} readable")
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
                    ("q", "quit"),
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
            if not room.readable:
                meta = "잠김 · " + meta
            if room.unread:
                meta += f" · {room.unread} unread"
            stamp = _stamp(room.last_updated_at, self.zone)
            right_part = f"{meta}  {stamp}"

            room_width = max(10, width - 2 - display_width(right_part) - 2)
            title = truncate_to_width(clean(room.title), room_width, ELLIPSIS)
            styled = self.theme.bold(title) if selected else title
            if not room.readable:
                # Openable, not readable yet: enter has the client load it.
                styled = self.theme.fg("muted", styled)
            elif room.unread:
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
        if not self.friends:
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
        """Draw every conversation the screen holds, side by side.

        The composer, the status line and the footer belong to the focused
        one: a second box would need a second caret and a terminal has one.
        A single conversation is drawn exactly as it was, header included —
        naming a room above its own transcript only earns a row once there is
        another room beside it to tell it apart from.
        """
        chat = self.chat
        assert chat is not None
        # The room is named in the footer; repeating it here spent a row on
        # something the reader already knows.
        lines = [
            self._hints(
                [
                    ("enter", "전송"),
                    ("↑↓", "scroll"),
                    ("tab", "방 전환"),
                    ("^P", "사진"),
                    ("/open", "방 추가"),
                    ("/sum 30m", "요약"),
                    ("/older", "이전"),
                    ("esc", "back"),
                ],
                width,
            ),
            "",
        ]

        composer, caret = self._composer_lines(width)
        body_height = max(1, height - len(lines) - len(composer) - 2)
        panes = self._visible_panes(width)
        cell = self._pane_width(width, len(panes))
        # A header earns its row once it is telling two conversations apart,
        # and equally when one is open but too far off screen to show: without
        # it nothing on screen says there is another room to tab to.
        headed = len(panes) > 1 or len(self.chats) > len(panes)
        rows = max(1, body_height - (1 if headed else 0))

        self._pane_rows = rows
        body_top = len(lines) + (1 if headed else 0)
        self.placements = []
        columns: list[list[str]] = []
        focused_body = 0
        for index, pane in enumerate(panes):
            body = (
                self._digest_of(pane, cell)
                if pane.slices
                else self._transcript_of(pane, cell)
            )
            if pane is chat:
                focused_body = len(body)
            if pane.anchor_top:
                # Resolved here because the top is a distance from the bottom,
                # and neither the body nor the screen height is known earlier.
                pane.offset = max(0, len(body) - rows)
                pane.anchor_top = False
            column = [
                pad_to_width(truncate_to_width(line, cell), cell)
                for line in _window(body, pane.offset, rows)
            ]
            column.extend([" " * cell] * (rows - len(column)))
            self._place_photos(pane, body, index, cell, rows, body_top)
            if headed:
                column.insert(0, self._pane_header(pane, cell, pane is chat))
            columns.append(column)

        rule = self.theme.fg("borderMuted", "│")
        for row in range(body_height):
            # Panes are equal, so an odd screen leaves a column over; it is
            # padding at the edge rather than one pane a character wider than
            # its neighbour.
            lines.append(pad_to_width(rule.join(column[row] for column in columns), width))
        while len(lines) < height - len(composer) - 2:
            lines.append("")

        self.cursor = (len(lines) + 1, caret)
        lines.extend(composer)
        lines.append(self._status_line(width))
        lines.append(self._footer_chat(width, focused_body))
        return lines[:height]

    def _pane_header(self, chat: ChatState, width: int, focused: bool) -> str:
        """Name one pane, which is only worth a row when there are several."""
        label = f"{'▌' if focused else ' '}{clean(chat.room.title)}"
        if chat.unseen:
            label += f" +{chat.unseen}"
        return self.theme.fg(
            "accent" if focused else "muted",
            pad_to_width(truncate_to_width(label, width), width),
        )

    def _transcript_lines(self, width: int) -> list[str]:
        """Return the transcript, rebuilding it only when it could have changed.

        Typing does not change a single conversation line, and rebuilding 400
        wrapped Hangul messages measured 71 ms — once per keystroke, which is
        exactly what made the composer feel sluggish. Messages are immutable
        and only ever added, so the count and the width identify the result.
        """
        chat = self.chat
        assert chat is not None
        return self._transcript_of(chat, width)

    def _transcript_of(self, chat: ChatState, width: int) -> list[str]:
        """The same, for one named conversation rather than the focused one."""
        # Opening a picture reserves rows, so it changes the transcript as
        # surely as a new message does and has to change the key with it;
        # the revision is what a message changed in place changes.
        key = (width, len(chat.messages), chat.revision, frozenset(chat.opened_photos))
        if chat.rendered is not None and chat.rendered[0] == key:
            return chat.rendered[1]
        lines = self._build_transcript(chat, width)
        chat.rendered = (key, lines)
        return lines

    def _feed_detail(self, chat: ChatState, message: Message) -> str:
        """What a feed row says about itself, beside the kind it already shows.

        Asked for the whole loaded window in one statement the first time one
        of them is drawn, and topped up as more arrive: a room whose newest
        four hundred records are mostly feeds — one live open chat had 364 of
        them — would otherwise pay a query a line. A feed with nothing to add
        is remembered as having nothing, so it is never asked about twice.
        """
        if message.type != FEED_MESSAGE:
            return ""
        if message.id not in chat.feeds:
            missing = [
                one.id
                for one in chat.messages
                if one.type == FEED_MESSAGE and one.id not in chat.feeds
            ]
            said = self.reader.feed_details(chat.room.id, missing)
            for one in missing:
                chat.feeds[one] = said.get(one, "")
        detail = chat.feeds.get(message.id, "")
        return f" {detail}" if detail else ""

    def _build_transcript(self, chat: ChatState, width: int) -> list[str]:
        stamp_width = 5
        name_width = min(14, max(8, width // 6))
        indent = stamp_width + 2 + name_width + 1
        body_width = max(12, width - indent)

        lines: list[str] = []
        previous_day = ""
        # Rebuilt with the transcript, because a row number only means anything
        # against the lines it was counted in.
        chat.photo_rows = {}
        chat.message_rows = {}
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
            detail = clean(self._feed_detail(chat, message))
            said = clean(message.text.replace("\r\n", "\n"), keep_newlines=True)
            # A join or a leave is a sentence already, the one the client
            # shows; "feed" in front of it is the storage talking.
            sentence = is_feed_sentence(detail)
            if message.erased:
                label = ERASED_PLACEHOLDER
            elif sentence:
                label = detail.strip()
            else:
                label = message_kind(message.type) + detail
            # A picture is offered only where it could actually be drawn, so a
            # terminal that cannot show one never suggests it can.
            has_picture = message.photo is not None
            openable = has_picture and self._photos_drawable()
            if openable:
                label += " ▾" if message.id in chat.opened_photos else " ▸"
            # The client writes its own word — "사진" — into the text column of
            # a photo row. Passed through it reads as if somebody typed it, and
            # says nothing about there being a picture to open.
            # A sentence the client itself shows is shown as one, not as a tag.
            bare = message.erased or sentence
            text = (
                self.theme.fg("dim", label if bare else f"[{label}]")
                if has_picture or not said
                else said
            )
            if has_picture or not said:
                wrapped = [text]
            elif self.theme.enabled:
                wrapped = wrap_with_links(said, body_width)
            else:
                wrapped = wrap_to_width(said, body_width)

            head = (
                self.theme.fg("dim", stamp)
                + self._mention_mark(message)
                + self.theme.fg(self.palette.name_colour(speaker), pad_to_width(name, name_width))
                + " "
            )
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
            for continuation in wrapped[1:]:
                lines.append(
                    truncate_to_width(" " * indent + self.theme.fg(body_colour, continuation), width)
                )
            if openable and message.id in chat.opened_photos:
                # The picture is not text and cannot live in a line, so the rows
                # it needs are reserved blank here and drawn over after the
                # frame is painted. Reserving them is what keeps the rest of
                # the conversation from being drawn underneath it.
                _columns, photo_rows = self._photo_cells(message, width, indent)
                chat.photo_rows[len(lines)] = message.id
                lines.extend([""] * photo_rows)
        return lines

    def _paint(self, console: Console, width: int, height: int) -> None:
        """Compose one frame, lay the pictures over it, park the caret.

        The frame is built first because rendering is what works out where the
        pictures go — their positions are a result of the layout, not an input
        to it. A picture that moved then needs the whole frame repainted to
        clear the old pixels, which is the one thing the row-diff painter
        cannot work out for itself: the rows it left behind are blank, and a
        blank row that is still blank is exactly the row it skips.

        The cell size is asked for once, after the first frame is on screen —
        it only matters when a picture is opened, and nothing should hold up
        the conversation appearing.
        """
        frame = self.render(width, height)
        if self._cell is None and self._photos_drawable():
            self._cell = console.cell_pixels()
        overlay = self.photo_escapes()
        if self._images_moved:
            console.forget()
            self._images_moved = False
        console.draw(frame, self.cursor, overlay)

    def _place_photos(
        self,
        pane: ChatState,
        body: list[str],
        index: int,
        cell: int,
        rows: int,
        body_top: int,
    ) -> None:
        """Work out where this pane's opened pictures land on the screen.

        The transcript knows which of *its* rows a picture starts on; only here
        is it known which of those rows are on screen, how far down the frame
        the pane's body begins, and which column the pane occupies. A picture
        scrolled out of the window is simply not placed.
        """
        if not pane.photo_rows or not self._photos_drawable():
            return
        scrollable = len(body) - rows
        offset = min(pane.offset, scrollable) if scrollable > 0 else pane.offset
        end = max(1, len(body) - max(0, offset))
        start = max(0, end - rows)
        stamp_width = 5
        name_width = min(14, max(8, cell // 6))
        indent = stamp_width + 2 + name_width + 1
        for row, message_id in pane.photo_rows.items():
            message = next((one for one in pane.messages if one.id == message_id), None)
            if message is None or message.photo is None:
                continue
            blob = self._photo_bytes.get(message_id)
            if blob is None:
                continue
            columns, height = self._photo_cells(message, cell, indent)
            # Only the part of the picture inside the window is asked for; a
            # placement is not clipped by anything the frame does, so one that
            # ran past the bottom would be drawn over the composer.
            hidden = max(0, start - row)
            shown = min(height - hidden, end - max(row, start))
            if shown <= 0:
                continue
            shape = self._photo_shapes.get(
                message_id, (message.photo.width, message.photo.height)
            )
            crop = images.crop_for(shape[1], height, hidden, shown)
            self.placements.append(
                (
                    message_id,
                    body_top + max(row, start) - start,
                    index * (cell + 1) + indent,
                    blob,
                    columns,
                    shown,
                    crop,
                )
            )

    def photo_escapes(self) -> str:
        """Return the escapes that bring the terminal's pictures up to date.

        Sixel and iTerm2 keep no image of their own, so a picture that moved is
        written again wherever it went and the frame's own repaint is what takes
        the old one away — which is why a change gives the frame up whole. A
        still screen changes nothing and writes nothing, which is what keeps
        this off the keystroke path.
        """
        if not self._photos_drawable():
            return ""
        current = {
            message_id: (row, column, columns, rows, crop)
            for message_id, row, column, _blob, columns, rows, crop in self.placements
        }
        if current == self._drawn:
            return ""
        self._images_moved = True
        pieces: list[str] = []
        for message_id, row, column, blob, columns, rows, crop in self.placements:
            # Encoding is the expensive half, and a redraw usually asks for a
            # box that was already encoded.
            key = (self._protocol, message_id, columns, rows, crop)
            if key in self._inline_cache:
                escape = self._inline_cache[key]
            else:
                escape = images.inline_escape(
                    self._protocol, blob, columns, rows, crop, self._cell
                )
                if len(self._inline_cache) >= INLINE_CACHE_ENTRIES:
                    # Scrolling walks the crop a line at a time, so the keys are
                    # unbounded; the oldest goes rather than all of them.
                    self._inline_cache.pop(next(iter(self._inline_cache)))
                self._inline_cache[key] = escape
            if escape is None:
                continue
            pieces.append(f"\x1b[{row + 1};{column + 1}H" + escape)
        self._drawn = current
        return "".join(pieces)

    def _photos_drawable(self) -> bool:
        """Whether a picture could be shown here at all.

        Both halves matter: a terminal that draws no graphics, and a theme
        turned off — the escapes are colour's neighbours, and a reader who
        asked for plain output did not ask for pictures either.
        """
        return self._protocol != images.NONE and self.theme.enabled

    def _photo_cells(self, message: Message, width: int, indent: int) -> tuple[int, int]:
        """The cell box one picture occupies, sized from its true shape."""
        assert message.photo is not None
        shape = self._photo_shapes.get(
            message.id, (message.photo.width, message.photo.height)
        )
        columns, rows, _box = images.fit(
            shape[0], shape[1], max(4, width - indent), self._photo_ceiling(), self._cell
        )
        return columns, rows

    def _photo_ceiling(self) -> Optional[int]:
        """How tall the conversation is, so a picture scales with the window."""
        return self._pane_rows if self._pane_rows else None

    def _photo_blob(self, message: Message) -> Optional[bytes]:
        """Fetch a picture once and keep it, measuring what really came back.

        The stored size cannot be trusted for shape — a rotated photo records
        the one it was taken at — and the rows reserved here and the cells the
        placement asks for have to come from the same number.
        """
        assert message.photo is not None
        if message.id in self._photo_bytes:
            return self._photo_bytes[message.id]
        blob = self._photos.fetch(message.photo.url)
        usable = None if blob is None else images.drawable(blob, self._protocol)
        if usable is None:
            return None
        self._photo_bytes[message.id] = usable
        measured = images.pixel_size(usable)
        if measured is not None and self._photo_shapes.get(message.id) != measured:
            self._photo_shapes[message.id] = measured
            for chat in self.chats:
                chat.rendered = None
        return usable

    def toggle_photo(self, message_id: int) -> bool:
        """Open or close one picture, fetching it the first time it is opened.

        Returns whether anything is now open for that message: a picture that
        cannot be fetched stays closed and says so, rather than reserving rows
        for something that will never arrive.
        """
        chat = self.chat
        if chat is None or not self._photos_drawable():
            return False
        if message_id in chat.opened_photos:
            chat.opened_photos.discard(message_id)
            chat.rendered = None
            return False
        message = next((one for one in chat.messages if one.id == message_id), None)
        if message is None or message.photo is None:
            return False
        if self._photo_blob(message) is None:
            chat.notice = "사진을 가져오지 못했습니다"
            return False
        chat.opened_photos.add(message_id)
        chat.rendered = None
        return True

    def newest_photo(self) -> Optional[int]:
        """The id of the newest message in the focused pane carrying a picture."""
        chat = self.chat
        if chat is None:
            return None
        for message in reversed(chat.messages):
            if message.photo is not None:
                return message.id
        return None

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

    def _digest_lines(self, width: int) -> list[str]:
        """Render slices the way OmO renders a reflection: a padded tinted box."""
        chat = self.chat
        assert chat is not None
        return self._digest_of(chat, width)

    def _digest_of(self, chat: ChatState, width: int) -> list[str]:
        """The same, for one named conversation rather than the focused one."""
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

    def _composer_lines(self, width: int) -> tuple[list[str], int]:
        """Draw the message box: closed on both sides, marked on the right.

        Nothing is drawn to stand in for the cursor. The terminal's own caret
        is parked at the end of the draft, and a block drawn beside it read as
        a second cursor in the wrong place. The mark for the last message sits
        against the right border, where it cannot push the prompt or the text
        sideways — and the caret's column counts everything left of the text,
        which is what it failed to do when the mark first appeared.
        """
        chat = self.chat
        assert chat is not None
        inner = max(8, width - 2)
        edge = self.theme.fg("borderMuted", "│")
        top = self.theme.fg("borderMuted", "╭" + "─" * inner + "╮")
        bottom = self.theme.fg("borderMuted", "╰" + "─" * inner + "╯")

        if chat.delivered:
            mark = self.theme.fg("success", "✓")
        elif chat.outbox is None:
            mark = " "
        elif self._clock() - chat.outbox_at < UNCERTAIN_AFTER_SECONDS:
            mark = self.theme.fg("muted", "*")
        else:
            # Long enough that "queued" would be a claim rather than a fact.
            mark = self.theme.fg("warning", "?")

        prompt = self.theme.fg("accent", "❯ ")
        # A line break has no room of its own here, so it is shown as a mark
        # and sent as the newline it really is.
        shown = chat.draft.replace("\n", NEWLINE_MARK)
        written, column = window_around(shown, chat.caret, max(1, inner - 6))
        body = " " + prompt + written
        if not chat.draft:
            body += self.theme.fg("dim", "메시지 입력 후 enter")
        body = pad_to_width(truncate_to_width(body, inner - 2), inner - 2)
        middle = edge + body + " " + mark + edge
        caret = min(width - 1, 4 + column)
        return [top, middle, bottom], caret

    def _status_line(self, width: int) -> str:
        chat = self.chat
        if chat is not None and chat.unseen:
            arrivals = self.theme.fg("accent", f"↓ 새 메시지 {chat.unseen}개")
            hint = self.theme.fg("muted", "  (end 키로 맨 아래)")
            return truncate_to_width(arrivals + hint, width)
        # A notice quotes titles, names and error text, all of it somebody
        # else's, so it is cleaned like any line of the conversation.
        message = clean((chat.notice if chat else "") or self.status)
        return truncate_to_width(self.theme.fg("muted", message), width) if message else ""

    def _footer_rooms(self, width: int) -> str:
        ring = self.reader.keyring
        anchor = [Segment("kanao-cli", self.theme.fg("accent", "kanao-cli"))]
        middle = [
            Segment(f"{len(self.rooms)} rooms", self.theme.fg("muted", f"{len(self.rooms)} rooms")),
            Segment(f"{len(ring)} keys", self.theme.fg("dim", f"{len(ring)} keys")),
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
        title = clean(room.title)
        anchor = [Segment(title, self.theme.fg("accent", title))]
        middle = [
            Segment(people, self.theme.fg("muted", people)),
            Segment(loaded, self.theme.fg("muted", loaded)),
            Segment(f"{body_lines} lines", self.theme.fg("dim", f"{body_lines} lines")),
        ]
        mode = "digest" if chat.slices else "transcript"
        tail = Segment(mode, self.theme.fg("dim", mode))
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
        if self.adding:
            self._handle_rooms(kind, text)
        elif self.chat is not None:
            self._handle_chat(kind, text)
        elif self.show_friends:
            self._handle_friends(kind, text)
        else:
            self._handle_rooms(kind, text)

    def _handle_rooms(self, kind: str, text: str) -> None:
        if self._filter_key(kind, text):
            return
        shown = self.visible_rooms()
        if kind == "char" and text in ("q", "Q") and not self.adding:
            self.running = False
        elif kind == Key.INTERRUPT or kind == Key.ESCAPE:
            if self.adding:
                # The list was asked for from inside a conversation, so
                # leaving it goes back there rather than out of the program.
                self.adding = False
            else:
                self.running = False
        elif kind == Key.UP:
            self.selected = max(0, self.selected - 1)
        elif kind == Key.DOWN:
            self.selected = min(len(shown) - 1, self.selected + 1)
        elif kind == Key.HOME:
            self.selected = 0
        elif kind == Key.END:
            self.selected = max(0, len(shown) - 1)
        elif kind == Key.ENTER and shown:
            try:
                chosen = shown[min(self.selected, len(shown) - 1)]
                self.request_open(chosen, beside=self.adding)
                if chosen.readable:
                    # An unreadable room is still on its way; _unlock tidies
                    # up once it has opened, and the waiting line stays.
                    self.adding = False
                    self._forget_filter()
                    self.status = ""
            except KanaoError as error:
                self.status = error.message
        elif kind == "char" and text in ("r", "R"):
            self.load_rooms()
            self.status = "목록 새로고침"
        elif kind == "char" and text in ("f", "F"):
            self.show_friends = True
            self._forget_filter()
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
        elif kind == Key.UP:
            self.friend_selected = max(0, self.friend_selected - 1)
        elif kind == Key.DOWN:
            self.friend_selected = min(len(shown) - 1, self.friend_selected + 1)
        elif kind == Key.HOME:
            self.friend_selected = 0
        elif kind == Key.END:
            self.friend_selected = max(0, len(shown) - 1)
        elif kind == Key.ENTER and shown:
            try:
                self.open_friend(shown[min(self.friend_selected, len(shown) - 1)])
                self._forget_filter()
            except KanaoError as error:
                self.status = error.message

    def _handle_chat(self, kind: str, text: str) -> None:
        chat = self.chat
        assert chat is not None

        if kind == Key.INTERRUPT:
            self.running = False
        elif kind == Key.TAB and len(self.chats) > 1:
            # The composer is the only box on screen, so which room it is
            # typing into has to be something the reader can move.
            self.focus = (self.focus + 1) % len(self.chats)
        elif kind == "char":
            if text == CLEAR_BOX:
                chat.draft = ""
                chat.caret = 0
            elif text == SHOW_PHOTO:
                self._toggle_newest_photo(chat)
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
        elif kind == Key.ESCAPE:
            # The draft is never thrown away here. It goes back to the room it
            # was being written in, and leaving is just leaving.
            if chat.slices:
                chat.slices = []
                chat.offset = 0
            else:
                self._leave_room(chat)
        elif kind == Key.UP:
            chat.offset += 1
        elif kind == Key.DOWN:
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

    def _leave_room(self, chat: ChatState) -> None:
        """Close this conversation, keeping whatever was half-written in it.

        Only this one: with several open, escape closes the pane the keyboard
        is in and leaves the rest where they are.
        """
        if chat.draft:
            self.drafts[chat.room.id] = chat.draft
        else:
            self.drafts.pop(chat.room.id, None)
        self.close_pane(chat)

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
        if self.pending_unlock is not None:
            room, beside = self.pending_unlock
            self.pending_unlock = None
            self._unlock(room, beside)
        text = self.pending_send
        self.pending_send = None
        if text is None or self.chat is None:
            return
        self.chat.notice = ""
        self.send_message(text)

    def _require_sender(self) -> "MessageSender":
        if self._sender is None:
            from ..winauto import make_sender

            self._sender = make_sender()
        return self._sender

    def _refuse_ambiguous(self, room: Room) -> None:
        """Refuse when a window title cannot identify one catalogued room.

        The census is the one this screen already loaded. Re-reading the
        catalog here cost up to 3.0 s per send: a busy room rewrites it
        constantly, and sending rewrites it too, so the snapshot almost always
        had to be decrypted again before the message could go out.

        The refusal names the room it is torn between, because the way out is
        in the client rather than here: a title is the only thing the window
        can be addressed by, so nothing short of one of those rooms being
        renamed resolves it, and being told only that there are two leaves
        nobody able to say which one to rename.
        """
        census = self.titles or [other.title for other in self.reader.rooms()]
        same = [title for title in census if title == room.title]
        if len(same) == 1:
            return
        described = ", ".join(
            f"{other.kind} {other.member_count}명 {_stamp(other.last_updated_at, self.zone)}"
            for other in self.rooms
            if other.title == room.title and other.id != room.id
        )
        raise KanaoError(
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
                search_row=self.reader.search_position(chat.room.id, rooms=self.rooms),
            )
        except KanaoError as error:
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
            slices = digest.slice_messages(_said(chat.messages), span)
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
        elif name == "open":
            self._open_beside(chat, " ".join(arguments))
        elif name == "close":
            self._leave_room(chat)
        elif name in ("q", "quit", "exit"):
            self.running = False
        elif name == "back":
            self.chat = None
        else:
            chat.notice = (
                f"알 수 없는 명령: /{name}  "
                "(사용 가능: /sum 30m · /older · /real 닉 · /photo · /export · /back · /quit)"
            )

    def _toggle_newest_photo(self, chat: ChatState) -> None:
        """Open the newest picture in this conversation, or close it again.

        The composer holds the keyboard the whole time a room is open, so there
        is no row to point at — the newest picture is the one somebody means
        when they ask for a picture at all, and the same key closes it.
        """
        if not self._photos_drawable():
            chat.notice = (
                "이 터미널은 사진을 그리지 못합니다 "
                "(KANAO_IMAGE=sixel 로 지정할 수 있습니다)"
            )
            return
        if chat.opened_photos:
            for message_id in list(chat.opened_photos):
                chat.opened_photos.discard(message_id)
            chat.rendered = None
            return
        newest = self.newest_photo()
        if newest is None:
            chat.notice = "이 대화에는 사진이 없습니다"
            return
        self.toggle_photo(newest)

    def _real(self, chat: ChatState, who: str) -> None:
        """Show what a name's owner used to be called, and who else wore it.

        Both halves matter.  One chain answers "what was this person before";
        two chains answer "which of these two is the impostor", and that second
        question is the one a reader has when they type this at all.
        """
        from ..names import steps_with_current

        if not who:
            chat.notice = "이름이 필요합니다 (예: /real 진짜유성진)"
            return
        try:
            history = self.reader.name_history(chat.room.id)
            found = self.reader.users_named(chat.room.id, who, history)
        except KanaoError as error:
            chat.notice = f"이름 이력을 읽지 못했습니다: {error.message}"
            return
        current = self.reader.names()
        if not found:
            chat.notice = f"'{who}' 을(를) 쓴 사람이 기록에 없습니다"
            return
        chains = []
        for user_id in found:
            steps = steps_with_current(user_id, history.get(user_id, ()), current.get(user_id))
            chains.append(" → ".join(self._name_step(step) for step in steps))
        prefix = "" if len(found) == 1 else f"이 이름을 쓴 계정 {len(found)}개 · "
        chat.notice = prefix + "  |  ".join(chains)

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
            os.path.expanduser("~"), f"kanao-cli-{chat.room.id}-digest.txt"
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
        # The first decryption is the slowest thing this program does, and it
        # used to happen before anything was drawn, so the terminal sat blank
        # for as long as it took and gave no reason. One frame first costs
        # nothing and says what is being waited on.
        width, height = force_size() or console.size
        self.status = "카카오톡 로컬 데이터베이스를 여는 중…"
        console.draw(self.render(width, height), self.cursor)

        self.load_rooms()
        self.status = (
            ""
            if any(room.readable for room in self.rooms)
            else "읽을 수 있는 대화방이 없습니다. 방을 골라 enter를 누르면 카카오톡에서 엽니다."
        )
        due_at = 0.0
        last_key_at = 0.0
        while self.running:
            width, height = force_size() or console.size
            self._paint(console, width, height)

            kind, text = console.read_key(KEY_WAIT_SECONDS)
            if kind or text:
                last_key_at = time.monotonic()
                self.handle(kind, text)
                if self.pending_send is not None or self.pending_unlock is not None:
                    self._paint(console, width, height)
                    self.flush_pending()
                continue

            now = time.monotonic()
            composing = self.chat is not None and bool(self.chat.draft)
            if not _poll_due(now, due_at, last_key_at, composing):
                continue
            self.refresh(width)
            due_at = _next_poll_at(time.monotonic(), time.monotonic() - now)


def _poll_due(now: float, due_at: float, last_key_at: float, composing: bool) -> bool:
    """Whether to look for arrivals now.

    Typing wins. A poll that finds something has to open the room — measured
    at 171 ms to 355 ms on a live 33-member room, four times in 40 s, against
    0.3 ms for a poll that finds nothing — and this loop is single-threaded,
    so doing that between keystrokes is what makes input feel stuck rather
    than slow.

    The keystroke clock cannot see somebody writing Hangul. The IME keeps
    every jamo and paints the half-made syllable at the terminal's own caret,
    so nothing reaches this loop until the syllable commits and the grace
    expires *inside* a word. Text already in the composer is the evidence the
    clock is missing, and it buys the longer grace: long enough to cover the
    pauses inside a sentence, short enough that a message left half-written
    does not stop the room from moving. It is shorter than the macOS port it
    came from, where the same poll costs 1.3 s to 1.9 s; here the log overlay
    means there is less to hide.
    """
    grace = COMPOSING_GRACE_SECONDS if composing else TYPING_GRACE_SECONDS
    if now - last_key_at < grace:
        return False
    return now >= due_at


def _next_poll_at(now: float, work_seconds: float) -> float:
    """Back off behind a room that is expensive to read.

    Waiting at least as long as the last poll took keeps a busy conversation
    from consuming the loop; an idle room is unaffected because its poll costs
    one stat.
    """
    return now + max(REFRESH_SECONDS, work_seconds)


def _said(messages: Sequence[Message]) -> list[Message]:
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


def launch() -> int:
    """Run the browser, restoring the terminal whatever happens."""
    with Reader() as reader, Console() as console:
        App(reader).run(console)
    return 0
