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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .. import __version__
from ..errors import KakaoReadError
from ..model import Message, Room, message_kind
from ..reader import Reader
from . import digest
from .console import Console, Key, force_size
from .layout import (
    ELLIPSIS,
    Segment,
    display_width,
    pad_to_width,
    plan_footer,
    truncate_to_width,
    wrap_to_width,
)
from ..watch import Watcher
from .theme import SpeakerPalette, Theme

KST = timezone(timedelta(hours=9))
HISTORY_WINDOW = 400
LOAD_MORE_STEP = 400
KEY_WAIT_SECONDS = 0.12
"""How long a frame waits for input before checking for new messages."""
REFRESH_SECONDS = 0.6
"""How often to look for arrivals. A poll that finds nothing costs one stat."""


@dataclass
class ChatState:
    """What is loaded and where the viewport sits inside it."""

    room: Room
    messages: list[Message] = field(default_factory=list)
    offset: int = 0
    """Lines scrolled up from the newest message."""
    command: str = ""
    editing: bool = False
    slices: list[digest.Slice] = field(default_factory=list)
    slice_label: str = ""
    notice: str = ""
    watcher: Optional[Watcher] = None
    """Follows this room so arrivals show up without reopening it."""
    unseen: int = 0
    """Arrivals that landed below the viewport while the reader was scrolled up."""


class App:
    """Owns the screen state and turns keystrokes into it."""

    def __init__(self, reader: Reader, theme: Optional[Theme] = None) -> None:
        self.reader = reader
        self.theme = theme if theme is not None else Theme()
        self.rooms: list[Room] = []
        self.selected = 0
        self.scroll = 0
        self.chat: Optional[ChatState] = None
        self.palette = SpeakerPalette()
        self.status = ""
        self.running = True
        self.zone = KST

    # -- data ---------------------------------------------------------------

    def load_rooms(self) -> None:
        self.rooms = [room for room in self.reader.rooms() if room.readable]

    def open_room(self, room: Room) -> None:
        messages = self.reader.messages(room.id, limit=HISTORY_WINDOW)
        self.palette = SpeakerPalette()
        for message in messages:
            self.palette.hue(message.sender)
        watcher = Watcher(self.reader, room_ids=[room.id], interval=REFRESH_SECONDS)
        watcher.prime()
        self.chat = ChatState(room=room, messages=messages, watcher=watcher)

    def refresh(self, width: int) -> bool:
        """Pick up anything new. Returns whether the screen needs redrawing."""
        chat = self.chat
        if chat is None:
            return self._refresh_rooms()
        if chat.watcher is None:
            return False
        try:
            arrived = chat.watcher.poll()
        except KakaoReadError as error:
            chat.notice = error.message
            return True
        if not arrived:
            return False

        known = {message.id for message in chat.messages}
        fresh = [message for message in arrived if message.id not in known]
        if not fresh:
            return False

        before = len(self._transcript_lines(width))
        for message in fresh:
            self.palette.hue(message.sender)
        chat.messages.extend(fresh)

        if chat.offset > 0:
            # The reader is up in the history; hold their place rather than
            # yanking the viewport down to the newest line.
            chat.offset += len(self._transcript_lines(width)) - before
            chat.unseen += len(fresh)
        else:
            chat.unseen = 0
        return True

    def _refresh_rooms(self) -> bool:
        """Re-read the catalog so unread counts and ordering stay current."""
        try:
            rooms = [room for room in self.reader.rooms() if room.readable]
        except KakaoReadError:
            return False
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
        if self.chat is None:
            return self._render_rooms(width, height)
        return self._render_chat(width, height)

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

    def _render_rooms(self, width: int, height: int) -> list[str]:
        total = len(self.rooms)
        right = (
            self.theme.fg("accent", "◉ Readable")
            + self.theme.fg("muted", f"  {total} rooms")
        )
        lines = [
            self._title_row("KakaoTalk Rooms", right, width),
            self._hints(
                [("↑↓", "move"), ("enter", "open"), ("r", "reload"), ("q", "quit")], width
            ),
            "",
        ]

        body_height = max(1, height - len(lines) - 2)
        self.scroll = _clamp_scroll(self.selected, self.scroll, body_height, total)
        visible = self.rooms[self.scroll : self.scroll + body_height]

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

    def _render_chat(self, width: int, height: int) -> list[str]:
        chat = self.chat
        assert chat is not None
        room = chat.room
        right = self.theme.fg("muted", f"{room.kind} · {room.member_count}명")
        lines = [
            self._title_row(room.title, right, width),
            self._hints(
                [
                    ("↑↓", "scroll"),
                    ("pgup/pgdn", "page"),
                    ("o", "older"),
                    ("/", "command"),
                    ("esc", "back"),
                ],
                width,
            ),
            "",
        ]

        body_height = max(1, height - len(lines) - 2)
        body = (
            self._digest_lines(width)
            if chat.slices
            else self._transcript_lines(width)
        )
        window = _window(body, chat.offset, body_height)
        lines.extend(window)
        while len(lines) < height - 2:
            lines.append("")

        lines.append(self._status_line(width))
        lines.append(self._footer_chat(width, len(body)))
        return lines[:height]

    def _transcript_lines(self, width: int) -> list[str]:
        chat = self.chat
        assert chat is not None
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
            wrapped = wrap_to_width(message.text, body_width) if message.text else [text]

            head = (
                self.theme.fg("dim", stamp)
                + "  "
                + self.theme.fg(self.palette.name_colour(speaker), pad_to_width(name, name_width))
                + " "
            )
            body_colour = self.palette.body_colour(speaker)
            lines.append(truncate_to_width(head + self.theme.fg(body_colour, wrapped[0]), width))
            for continuation in wrapped[1:]:
                lines.append(
                    truncate_to_width(" " * indent + self.theme.fg(body_colour, continuation), width)
                )
        return lines

    def _digest_lines(self, width: int) -> list[str]:
        """Render slices the way OmO renders a reflection: a padded tinted box."""
        chat = self.chat
        assert chat is not None
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

    def _status_line(self, width: int) -> str:
        chat = self.chat
        if chat is not None and chat.editing:
            prompt = self.theme.fg("accent", "› ") + chat.command
            return truncate_to_width(prompt + self.theme.fg("dim", "▏"), width)
        if chat is not None and chat.unseen:
            arrivals = self.theme.fg("accent", f"↓ 새 메시지 {chat.unseen}개")
            hint = self.theme.fg("muted", "  (end 키로 맨 아래)")
            return truncate_to_width(arrivals + hint, width)
        message = (chat.notice if chat else "") or self.status
        return truncate_to_width(self.theme.fg("muted", message), width) if message else ""

    def _footer_rooms(self, width: int) -> str:
        ring = self.reader.keyring
        anchor = [Segment("kakaoread", self.theme.fg("accent", "kakaoread"))]
        middle = [
            Segment(f"{len(self.rooms)} rooms", self.theme.fg("muted", f"{len(self.rooms)} rooms")),
            Segment(f"{len(ring)} keys", self.theme.fg("dim", f"{len(ring)} keys")),
        ]
        tail = Segment("read-only", self.theme.fg("dim", "read-only"))
        right = Segment(f"v{__version__}", self.theme.fg("muted", f"v{__version__}"))
        return self._compose_footer(anchor, middle, tail, right, width)

    def _footer_chat(self, width: int, body_lines: int) -> str:
        chat = self.chat
        assert chat is not None
        room = chat.room
        loaded = f"{len(chat.messages)} loaded"
        anchor = [Segment(room.title, self.theme.fg("accent", room.title))]
        middle = [
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
        if self.chat is None:
            self._handle_rooms(kind, text)
        else:
            self._handle_chat(kind, text)

    def _handle_rooms(self, kind: str, text: str) -> None:
        if kind == "char" and text in ("q", "Q"):
            self.running = False
        elif kind == Key.INTERRUPT or kind == Key.ESCAPE:
            self.running = False
        elif kind == Key.UP:
            self.selected = max(0, self.selected - 1)
        elif kind == Key.DOWN:
            self.selected = min(len(self.rooms) - 1, self.selected + 1)
        elif kind == Key.HOME:
            self.selected = 0
        elif kind == Key.END:
            self.selected = max(0, len(self.rooms) - 1)
        elif kind == Key.ENTER and self.rooms:
            try:
                self.open_room(self.rooms[self.selected])
                self.status = ""
            except KakaoReadError as error:
                self.status = error.message
        elif kind == "char" and text in ("r", "R"):
            self.load_rooms()
            self.status = "목록 새로고침"

    def _handle_chat(self, kind: str, text: str) -> None:
        chat = self.chat
        assert chat is not None

        if chat.editing:
            self._handle_command_key(kind, text)
            return

        if kind == Key.INTERRUPT:
            self.running = False
        elif kind == Key.ESCAPE:
            if chat.slices:
                chat.slices = []
                chat.offset = 0
            else:
                self.chat = None
        elif kind == "char" and text in ("q", "Q"):
            self.running = False
        elif kind == "char" and text == "/":
            chat.editing = True
            chat.command = "/"
        elif kind == "char" and text in ("o", "O"):
            chat.notice = ""
            self.load_older()
        elif kind == "char" and text in ("e", "E") and chat.slices:
            self._export(chat)
        elif kind == Key.UP:
            chat.offset += 1
        elif kind == Key.DOWN:
            chat.offset = max(0, chat.offset - 1)
        elif kind == Key.PAGE_UP:
            chat.offset += 10
        elif kind == Key.PAGE_DOWN:
            chat.offset = max(0, chat.offset - 10)
        elif kind == Key.HOME:
            chat.offset = 10_000_000
        elif kind == Key.END:
            chat.offset = 0
        if chat.offset == 0:
            chat.unseen = 0

    def _handle_command_key(self, kind: str, text: str) -> None:
        chat = self.chat
        assert chat is not None
        if kind == Key.ESCAPE:
            chat.editing = False
            chat.command = ""
        elif kind == Key.BACKSPACE:
            chat.command = chat.command[:-1]
            if not chat.command:
                chat.editing = False
        elif kind == Key.ENTER:
            command = chat.command
            chat.editing = False
            chat.command = ""
            self._run_command(command)
        elif kind == "char":
            chat.command += text

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
            chat.offset = 0
            chat.notice = ""
        elif name in ("q", "quit", "exit"):
            self.running = False
        elif name == "back":
            self.chat = None
        else:
            chat.notice = f"알 수 없는 명령: /{name}  (사용 가능: /sum <30m|2h>)"

    def _export(self, chat: ChatState) -> None:
        """Write the sliced text beside the user's other files, never into the repo."""
        target = os.path.join(
            os.path.expanduser("~"), f"kakaoread-{chat.room.id}-digest.txt"
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
        self.load_rooms()
        if not self.rooms:
            self.status = "읽을 수 있는 대화방이 없습니다. 카카오톡에서 대화를 열어주세요."
        last_refresh = 0.0
        while self.running:
            width, height = force_size() or console.size
            console.draw(self.render(width, height))

            kind, text = console.read_key(KEY_WAIT_SECONDS)
            if kind or text:
                self.handle(kind, text)
                continue

            now = time.monotonic()
            if now - last_refresh >= REFRESH_SECONDS:
                last_refresh = now
                self.refresh(width)


def _clamp_scroll(selected: int, scroll: int, height: int, total: int) -> int:
    if total <= height:
        return 0
    scroll = min(scroll, selected)
    scroll = max(scroll, selected - height + 1)
    return max(0, min(scroll, total - height))


def _window(lines: list[str], offset: int, height: int) -> list[str]:
    """Take *height* lines ending *offset* lines above the bottom."""
    if not lines:
        return []
    end = max(1, len(lines) - max(0, offset))
    end = min(end, len(lines))
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
