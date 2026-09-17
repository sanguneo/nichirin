"""The interactive browser: a room list, a conversation, and time digests.

The shape follows OmO's interactive mode so the two feel like one toolchain:
a title row with status pinned right, hint rows beneath it, a body, and a
single footer line that gives up its middle before its edges.

Everything on screen is rebuilt from state on each keystroke. A conversation
this large cannot be diffed cheaply and does not need to be — one frame per
key is imperceptible and removes a whole class of stale-cell bugs.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, TYPE_CHECKING

from .. import __version__
from ..errors import ErrorCode, KamadoError
from ..model import Friend, Message, Room, message_kind
from ..reader import Reader
from . import digest
from .console import CLEAR_BOX, Console, Key, force_size
from .layout import (
    ELLIPSIS,
    Segment,
    display_width,
    elide_head,
    pad_to_width,
    plan_footer,
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
MIN_PANE_COLUMNS = 44
"""Narrowest a pane may be before the screen holds one conversation fewer.

Hangul spends two columns on a character, so a pane under this stops showing
sentences and starts showing fragments of them.
"""
KEY_WAIT_SECONDS = 0.12
"""How long a frame waits for input before checking for new messages."""
UNCERTAIN_AFTER_SECONDS = 30.0
"""How long a send may go unseen before the mark stops claiming to be sure."""
DELIVERED_TIMEOUT_SECONDS = 1.6
"""How long the '전송 완료' indicator remains visible before reverting to a clean divider."""
REFRESH_SECONDS = 0.15
"""How often to look for arrivals. A poll that finds nothing costs one stat."""
TYPING_GRACE_SECONDS = 0.4
"""How long after a keystroke to leave the conversation alone."""
COMPOSING_GRACE_SECONDS = 3.0
"""How long a half-written message holds a poll off, IME pauses included."""


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
    notice: str = ""
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
    rendered: Optional[tuple[tuple[int, int], list[str]]] = None
    """Transcript lines and what they were built from, so typing costs nothing."""


class App:
    """Owns the screen state and turns keystrokes into it."""

    def __init__(
        self,
        reader: Reader,
        theme: Optional[Theme] = None,
        sender: Optional["MessageSender"] = None,
    ) -> None:
        self.reader = reader
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
        self.watcher: Optional[Watcher] = None
        """One watcher for every open room.

        The fingerprint a poll checks covers the whole account, so following a
        second conversation costs no second decryption — and a decryption is
        the most expensive thing this loop can be made to do.
        """
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
        self.picking = False
        """Whether the room list is up in front of the conversations.

        Opening a second pane has to start somewhere, and every way back to
        the list otherwise closes the conversation standing in front of it.
        """
        self.pending_send: Optional[str] = None
        self._clock = time.monotonic
        """Read rather than called directly, so a test can hold time still."""
        """Text the last keystroke asked to send, not sent yet.

        Sending can open a window, which took 1.7 s with the screen frozen on
        the frame before the keystroke. Holding it here lets the loop paint
        once, so the wait is visible rather than mistaken for a hang.
        """
        self.cursor: Optional[tuple[int, int]] = None
        """Where the terminal caret belongs, so an IME composes in the box."""

    @property
    def chat(self) -> Optional[ChatState]:
        """The conversation the keyboard is in, of the ones held open."""
        if not self.chats:
            return None
        self.focus = max(0, min(self.focus, len(self.chats) - 1))
        return self.chats[self.focus]

    @chat.setter
    def chat(self, value: Optional[ChatState]) -> None:
        """Assigning None closes every pane; assigning one leaves only it."""
        self.chats = [] if value is None else [value]
        self.focus = 0

    def _pane_width(self, width: int, panes: int) -> int:
        """How wide one conversation gets, with a rule between each pair."""
        return max(1, (width - (panes - 1)) // panes)

    def _visible_panes(self, width: int) -> list[ChatState]:
        """The conversations this screen is wide enough to show at once.

        More can be open than fit. The window slides to keep the focused one
        on screen rather than closing anything, so narrowing the terminal
        hides conversations instead of losing them.
        """
        if not self.chats:
            return []
        fits = max(1, (width + 1) // (MIN_PANE_COLUMNS + 1))
        if fits >= len(self.chats):
            return list(self.chats)
        start = min(max(0, self.focus - fits + 1), len(self.chats) - fits)
        return self.chats[start : start + fits]
        self._last_width = 80

    # -- data ---------------------------------------------------------------

    def load_rooms(self) -> None:
        catalogued = self.reader.rooms()
        self.titles = [room.title for room in catalogued]
        self.rooms = [room for room in catalogued if room.readable]

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
            self.status = f"{friend.name}: 카카오톡에서 대화방을 먼저 열어주세요"
            return
        self.show_friends = False
        self.open_room(room)

    def open_room(self, room: Room) -> None:
        """Open a room in a pane of its own, keeping the ones already open.

        Opening a conversation that is already on screen moves the keyboard to
        it rather than showing it twice.
        """
        already = next(
            (index for index, chat in enumerate(self.chats) if chat.room.id == room.id),
            None,
        )
        if already is not None:
            self.focus = already
            return
        messages = self.reader.messages(room.id, limit=HISTORY_WINDOW)
        if not self.chats:
            # Tints are kept across a pane's lifetime: rebuilding the palette
            # while another conversation is on screen would recolour it.
            self.palette = SpeakerPalette(dark=self.theme.dark)
        for message in messages:
            self.palette.hue(message.sender)
        stored = self.reader.stored_title(room.id)
        kept = self.drafts.get(room.id, "")
        # A conversation opens at its newest line, not at its oldest.
        self.chats.append(
            ChatState(
                room=room,
                messages=messages,
                anchor_top=False,
                draft=kept,
                caret=len(kept),
                window_alias=stored if stored and stored != room.title else None,
            )
        )
        self.focus = len(self.chats) - 1
        self._follow(room.id)

    def _follow(self, room_id: int) -> None:
        """Add a room to the one watcher, building it on the first pane."""
        if self.watcher is None:
            self.watcher = Watcher(
                self.reader,
                room_ids=[chat.room.id for chat in self.chats],
                interval=REFRESH_SECONDS,
            )
            self.watcher.prime()
            return
        self.watcher.watch(room_id)

    def refresh(self, width: int) -> bool:
        """Pick up anything new. Returns whether the screen needs redrawing.

        One poll answers for every open conversation, so what comes back is
        mixed and each message is handed to the room it belongs to. Line
        counts are measured at the width a pane actually renders at, or a
        reader scrolled up in a narrow pane would be moved by the arithmetic
        of a screen nobody is looking at.
        """
        if not self.chats:
            return self._refresh_rooms()
        if self.watcher is None:
            return False
        try:
            arrived = self.watcher.poll()
        except KamadoError as error:
            self.chats[min(self.focus, len(self.chats) - 1)].notice = error.message
            return True
        if not arrived:
            return False
        cell = self._pane_width(width, max(1, len(self._visible_panes(width))))
        drawn = False
        for chat in self.chats:
            mine = [message for message in arrived if message.room_id == chat.room.id]
            if mine and self._absorb(chat, mine, cell):
                drawn = True
        return drawn

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
            chat.unseen += len(fresh)
        else:
            chat.unseen = 0
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

    def _refresh_rooms(self) -> bool:
        """Re-read the catalog so unread counts and ordering stay current."""
        try:
            catalogued = self.reader.rooms()
        except KamadoError:
            return False
        self.titles = [room.title for room in catalogued]
        rooms = [room for room in catalogued if room.readable]
        changed = [
            (room.id, room.unread, room.last_log_id) for room in rooms
        ] != [(room.id, room.unread, room.last_log_id) for room in self.rooms]
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
        older = self.reader.messages(
            chat.room.id, limit=LOAD_MORE_STEP, before_id=chat.messages[0].id
        )
        if not older:
            chat.notice = "이전 기록이 더 없습니다"
            return
        chat.messages = older + chat.messages
        chat.notice = f"이전 {len(older)}개 불러옴 (총 {len(chat.messages)})"

    # -- rendering ----------------------------------------------------------

    def render(self, width: int, height: int) -> list[str]:
        self.cursor = None
        self._last_width = width
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
            if room.unread:
                meta += f" · {room.unread} unread"
            stamp = _stamp(room.last_updated_at, self.zone)
            right_part = f"{meta}  {stamp}"

            room_width = max(10, width - 2 - display_width(right_part) - 2)
            title = truncate_to_width(room.title, room_width, ELLIPSIS)
            styled = self.theme.bold(title) if selected else title
            if room.unread:
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
            right_part = (friend.status_message or "").replace("\n", " ")

            name_width = max(8, width // 3)
            name = truncate_to_width(star + friend.name, name_width, ELLIPSIS)
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

        The composer and the footer belong to the focused one: a second box
        would need a second caret, and a terminal has one. A single pane is
        drawn exactly as before, headers included — naming a room above its
        own transcript only earns its row once there is another room beside
        it to tell it apart from.
        """
        chat = self.chat
        assert chat is not None
        lines = [
            self._hints(
                [
                    ("enter", "전송"),
                    ("↑↓", "scroll"),
                    ("tab", "방 전환"),
                    ("/open", "방 추가"),
                    ("/sum 30m", "요약"),
                    ("/older", "이전"),
                    ("esc", "닫기"),
                ],
                width,
            ),
            "",
        ]

        composer, (caret_row, caret_col) = self._composer_lines(width)
        body_height = max(1, height - len(lines) - len(composer) - 1)
        panes = self._visible_panes(width)
        cell = self._pane_width(width, len(panes))
        # A header earns its row once it is telling two conversations apart,
        # and equally when one of them is open but too far off screen to show:
        # without it nothing on screen says there is another room to tab to.
        headed = len(panes) > 1 or len(self.chats) > len(panes)
        rows = max(1, body_height - (1 if headed else 0))

        columns: list[list[str]] = []
        focused_body = 0
        for pane in panes:
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
            if headed:
                column.insert(0, self._pane_header(pane, cell, pane is chat))
            columns.append(column)

        rule = self.theme.fg("borderMuted", "│")
        for row in range(body_height):
            # Panes are equal, so an odd screen leaves a column over; it is
            # padding at the edge rather than one pane a character wider than
            # its neighbour.
            lines.append(pad_to_width(rule.join(column[row] for column in columns), width))
        while len(lines) < height - len(composer) - 1:
            lines.append("")

        self.cursor = (len(lines) + 1 + caret_row, caret_col)
        lines.extend(composer)
        lines.append(self._footer_chat(width, focused_body))
        return lines[:height]

    def _pane_header(self, chat: ChatState, width: int, focused: bool) -> str:
        """Name one pane, which is only worth a row when there are several."""
        label = f"{'▌' if focused else ' '}{chat.room.title}"
        if chat.unseen:
            label += f" +{chat.unseen}"
        return self.theme.fg(
            "accent" if focused else "muted",
            pad_to_width(truncate_to_width(label, width), width),
        )

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
        and only ever added, so the count and the width identify the result;
        the width is the pane's, so panes of different sizes each keep their
        own.
        """
        key = (width, len(chat.messages))
        if chat.rendered is not None and chat.rendered[0] == key:
            return chat.rendered[1]
        lines = self._build_transcript(chat, width)
        chat.rendered = (key, lines)
        return lines

    def _build_transcript(self, chat: ChatState, width: int) -> list[str]:
        stamp_width = 5
        name_width = min(14, max(8, width // 6))
        indent = stamp_width + 2 + name_width + 1
        body_width = max(12, width - indent)

        lines: list[str] = []
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
            name = truncate_to_width(speaker, name_width, ELLIPSIS)
            text = message.text or self.theme.fg("dim", f"[{message_kind(message.type)}]")
            wrapped = (
                self._wrap_body(message.text, body_width)
                if message.text
                else [text]
            )

            head = (
                self.theme.fg("dim", stamp)
                + self._mention_mark(message)
                + self.theme.fg(self.palette.name_colour(speaker), pad_to_width(name, name_width))
                + " "
            )
            body_colour = self.palette.body_colour(speaker)
            opening = head
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
        return lines

    def _wrap_body(self, text: str, width: int) -> list[str]:
        """Wrap body text while preserving full target URLs across split lines."""
        if not self.theme.enabled or "http" not in text:
            return wrap_to_width(text, width)

        url_pattern = re.compile(r"(https?://[^\s<>\"'()]+)")
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
        who = "나" if message.replies_to_me else (quote.sender or f"User {quote.sender_id}")
        said = " ".join(quote.text.split())
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
        rows.append(boxed(label + self.theme.fg("muted", f" {chat.slice_label}")))
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
                f"{name} {count}" for name, count in piece.speakers[:4]
            )
            if speakers:
                rows.append(boxed(self.theme.fg("muted", "  who   ") + speakers))
            keywords = ", ".join(word for word, _count in piece.keywords[:6])
            if keywords:
                rows.append(boxed(self.theme.fg("muted", "  about ") + keywords))

            for highlight in piece.highlights():
                speaker = "나" if highlight.from_me else highlight.sender
                text = (highlight.text or "").replace("\n", " ")
                for index, piece_line in enumerate(
                    wrap_to_width(f"{speaker}: {text}", inner - 8)
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
            bullet = "•"
            notice_text = truncate_to_width(chat.notice, max(1, width - 8))
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
        if chat is not None and len(self.chats) > 1:
            # One box serves several conversations, so it has to say which one
            # it is about to send to. Which pane looks focused is a question
            # about a highlight; who receives the message is not.
            name = truncate_to_width(chat.room.title, max(1, width - 6))
            fill = max(0, width - display_width(f"── {name} "))
            content = (
                self.theme.fg("borderMuted", "── ")
                + self.theme.fg("accent", f"{name} ")
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
        message = self.status
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
        anchor = [Segment(room.title, self.theme.fg("accent", room.title))]
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
        if self.picking and kind in (Key.ESCAPE, Key.TAB):
            # The list is in front of conversations that are still open, so
            # leaving it goes back to them rather than out of the interface.
            self.picking = False
            self._forget_filter()
        elif kind == "char" and text in ("q", "Q"):
            self.running = False
        elif kind == Key.INTERRUPT or kind == Key.ESCAPE:
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
                self.open_room(shown[min(self.selected, len(shown) - 1)])
                self._forget_filter()
                self.picking = False
                self.status = ""
            except KamadoError as error:
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
            except KamadoError as error:
                self.status = error.message

    def _handle_chat(self, kind: str, text: str) -> None:
        chat = self.chat
        assert chat is not None

        if kind == Key.INTERRUPT:
            self.running = False
        elif kind == "char":
            if text == CLEAR_BOX:
                chat.draft = ""
                chat.caret = 0
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
            self.focus = (self.focus + 1) % len(self.chats)
        elif kind == Key.ESCAPE:
            # The draft is never thrown away here. It goes back to the room it
            # was being written in, and leaving is just leaving.
            if chat.slices:
                chat.slices = []
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
        """Close one pane, keeping whatever was half-written in it."""
        if chat.draft:
            self.drafts[chat.room.id] = chat.draft
        else:
            self.drafts.pop(chat.room.id, None)
        if chat in self.chats:
            index = self.chats.index(chat)
            self.chats.pop(index)
            self.focus = max(0, min(self.focus if index > self.focus else self.focus - 1,
                                    len(self.chats) - 1))
        if self.watcher is not None:
            self.watcher.unwatch(chat.room.id)
        if not self.chats:
            self.watcher = None

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
        census = self.titles or [other.title for other in self.reader.rooms()]
        same = [title for title in census if title == room.title]
        if len(same) == 1:
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
            slices = digest.slice_messages(chat.messages, span)
            if not slices:
                chat.notice = "요약할 메시지가 없습니다"
                return
            chat.slices = slices
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
        elif name in ("q", "quit", "exit"):
            self.running = False
        elif name in ("back", "close"):
            self._leave_room(chat)
        elif name == "open":
            self._open_beside(chat, " ".join(arguments))
        else:
            chat.notice = (
                f"알 수 없는 명령: /{name}  "
                "(사용 가능: /open 방 · /close · /sum 30m · /older · "
                "/real 닉 · /export · /quit)"
            )

    def _open_beside(self, chat: ChatState, wanted: str) -> None:
        """Open another conversation in a pane beside this one, by name.

        Matched against the catalog this screen already holds, so adding a
        pane costs no decryption. An exact title wins over a longer one
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
        try:
            self.open_room(matches[0])
        except KamadoError as error:
            chat.notice = error.message

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
        except KamadoError as error:
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

        self.load_rooms()
        self.status = "" if self.rooms else "읽을 수 있는 대화방이 없습니다. 카카오톡에서 대화를 열어주세요."
        due_at = 0.0
        last_key_at = 0.0
        while self.running:
            width, height = force_size() or console.size
            console.draw(self.render(width, height), self.cursor)

            kind, text = console.read_key(KEY_WAIT_SECONDS)
            if kind or text:
                last_key_at = time.monotonic()
                self.handle(kind, text)
                if self.pending_send is not None:
                    console.draw(self.render(width, height), self.cursor)
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

    Typing wins. A poll that finds something has to decrypt the room's whole
    log — measured at 0.62 s and 0.94 s on two live rooms — and this loop is
    single-threaded, so doing that between keystrokes is what makes input feel
    stuck rather than slow.

    The keystroke clock cannot see somebody writing Hangul. An IME keeps every
    jamo until the syllable commits, so nothing reaches the interface while a
    word is being assembled and this grace expires inside it — measured on a
    live account, the poll that lands in that gap costs 1.3 s to 1.9 s and
    returns nothing, because the running client rewrites the shared database
    every second or so whether or not the watched room moved. Text already in
    the composer is the evidence the clock is missing, and it buys the longer
    grace: long enough to cover the pauses inside a sentence, short enough
    that a message left sitting does not stop the room from moving.
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
