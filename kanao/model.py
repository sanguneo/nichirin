"""The records this tool hands to an agent, and how they are rendered as JSON.

Two rendering rules apply everywhere rather than per command.  Identifiers are
strings, because KakaoTalk ids run past 2**53 and would be silently corrupted
by an ordinary JavaScript ``JSON.parse``.  Times are ISO-8601 with an explicit
UTC offset, because a bare local timestamp invites an agent to state the wrong
hour with full confidence.

Two things a message says about *other* messages are read out of the client's
``attachement`` column here as well: who it named, and which message it
answered.  Both are parsed rather than rendered, but they belong beside the
records they become, and neither needs a database open to be tested.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Final, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

DIRECT_CHAT: Final = "DirectChat"
GROUP_CHAT: Final = "MultiChat"
SELF_CHAT: Final = "MemoChat"
CHANNEL_CHAT: Final = "PlusChat"
OPEN_GROUP_CHAT: Final = "OM"
OPEN_DIRECT_CHAT: Final = "OD"

ROOM_KINDS: Final = {
    DIRECT_CHAT: "direct",
    GROUP_CHAT: "group",
    SELF_CHAT: "self",
    CHANNEL_CHAT: "channel",
    OPEN_GROUP_CHAT: "open-group",
    OPEN_DIRECT_CHAT: "open-direct",
}
"""Client type strings mapped to stable names an agent can branch on."""

FEED_MESSAGE: Final = 0
"""The client's own membership bookkeeping, not something a person typed.

These rows carry JSON in the ``message`` column — a join, a leave, an invite —
and in one live open chat 364 of the newest 400 records were this. Showing
that JSON as message text buries the conversation and hands an agent a wall of
``{"feedType": 2, ...}`` to reason over, so the text is dropped. The nicknames
these rows record are read straight from the database by ``names.from_feeds``,
which is unaffected.
"""

TEXT_MESSAGE: Final = 1
PHOTO_MESSAGE: Final = 2
VIDEO_MESSAGE: Final = 3
REPLY_MESSAGE: Final = 26
"""A message written against another one, confirmed against a live client.

Every row of this type carried ``src_logId``, ``src_userId``, ``src_type`` and
``src_message`` in its attachment, across three rooms and 108 replies.
"""

_MESSAGE_KINDS: Final = {
    FEED_MESSAGE: "feed",
    TEXT_MESSAGE: "text",
    PHOTO_MESSAGE: "photo",
    VIDEO_MESSAGE: "video",
    REPLY_MESSAGE: "reply",
}


def message_kind(type_code: int) -> str:
    """Name the message kinds that are confirmed; leave the rest self-describing.

    Guessing a label for an unverified code would let an agent describe an
    attachment it never saw, so unknown codes keep their number.
    """
    return _MESSAGE_KINDS.get(type_code, f"other:{type_code}")


FILE_MESSAGE: Final = 18
ALBUM_MESSAGE: Final = 27

FEED_KINDS: Final = {4: "입장", 2: "퇴장"}
ERASED_PLACEHOLDER: Final = "삭제된 메시지입니다"
"""What the client shows where a message deleted for everyone was."""
COVERED_PLACEHOLDER: Final = "세이프봇에 의해 가려진 메시지입니다."
"""What stands where an open chat's SafeBot covered a message.

The wording is the one piratetalk renders beside the Windows client; it has
not been read off this client's own screen, so it is a label, not a quote.
"""
SAFEBOT: Final = "safebot"
"""The one kind of covering this build recognises.

An open chat's SafeBot marks the message itself: ``{"safeBot": true}`` in the
attachment, on 11 rows of the readable rooms measured. The message keeps its
text, which is why hiding it is this tool's decision and can be undone.
"""
EDIT_FEED: Final = 25
"""The feed an edit writes, ``{"feedType": 25, "logId": …, "targetRevision": …}``.

Measured: 140 of them across nine rooms, every one ``hidden: true`` and every
one pointing at a message whose ``revision`` it raised. The edited message is
marked where it stands, so the feed is bookkeeping and is never shown.
"""
REACTION_META: Final = 2
"""``chatLogMeta.type`` for a message's 공감, keyed by ``logId``.

Measured: 746 rows, each ``{"rx": [{"k", "c", "o", "a": {"ko"}}]}``. ``c`` is
how many people reacted with ``o``, a mini emoticon's code, and ``a.ko`` is
the word the client shows for it (사랑, 엄지척, 웃음 …). Who reacted is not
stored.
"""
_FEED_NAMES_SHOWN: Final = 3
"""The feed types whose meaning has been checked against a live screen.

Read off an open chat where the client's own view and this one sat side by
side: every ``4`` named someone who had just come in and every ``2`` someone
who had left — 736 and 161 of them across the readable rooms, all naming a
member. Other codes stay numbers until they are checked the same way.
"""


def feed_detail(raw: object) -> str:
    """Describe a feed row from what it carries, naming nothing it does not.

    A feed's JSON is the client's own bookkeeping, and what a ``feedType``
    means was never confirmed for most codes — so those are passed through as
    numbers, for the same reason :func:`message_kind` leaves an unconfirmed
    code as one. The few in :data:`FEED_KINDS` are said in words. Only what the client wrote in words is
    quoted: the ``coverType`` an open chat records when its SafeBot covers a
    message, and the nicknames a membership row lists.

    Measured across 7,741 feed rows of five live rooms: every row carried
    ``feedType``, 3,648 named somebody through ``members`` or ``member``, and
    four carried ``coverType`` — all four ``openchat_blind``. Not one row
    failed to parse as a JSON object.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        payload = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""

    code = payload.get("feedType")
    listed = payload.get("members")
    if not isinstance(listed, list):
        alone = payload.get("member")
        listed = [alone] if isinstance(alone, dict) else []
    named = [
        str(one.get("nickName")).strip()
        for one in listed
        if isinstance(one, dict) and str(one.get("nickName") or "").strip()
    ]
    if isinstance(code, int) and code in FEED_KINDS:
        return feed_sentence(FEED_KINDS[code], named)

    parts: list[str] = []
    if isinstance(code, int):
        parts.append(str(code))
    cover = payload.get("coverType")
    if isinstance(cover, str) and cover.strip():
        parts.append(cover.strip())

    if named:
        parts.append("·".join(named[:_FEED_NAMES_SHOWN]))
    return " ".join(parts)


def feed_sentence(kind: str, named: Sequence[str]) -> str:
    """Say a join or a leave the way the client does: "이름님이 입장하셨습니다."."""
    shown = [f"{name}님" for name in named[:_FEED_NAMES_SHOWN]]
    if not shown:
        return f"{kind}하셨습니다."
    rest = len(named) - len(shown)
    who = ", ".join(shown) + (f" 외 {rest}명" if rest > 0 else "")
    return f"{who}이 {kind}하셨습니다."


def is_feed_sentence(detail: str) -> bool:
    """Whether a feed's detail is already a sentence rather than a label."""
    return detail.strip().endswith("하셨습니다.")


def to_iso(epoch_seconds: Optional[int]) -> Optional[str]:
    """Render a client timestamp as an explicit UTC instant."""
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class Room:
    """One conversation as the client's catalog describes it."""

    id: int
    type: str
    title: str
    member_count: int
    unread: int
    last_log_id: Optional[int]
    last_updated_at: Optional[int]
    readable: bool
    """Whether this room's message database can be decrypted right now."""
    images: tuple[str, ...] = ()
    """The profile pictures its icon is drawn from, best first; up to four."""

    @property
    def kind(self) -> str:
        return ROOM_KINDS.get(self.type, self.type)

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(self.id),
            "title": self.title,
            "kind": self.kind,
            "type": self.type,
            "memberCount": self.member_count,
            "unread": self.unread,
            "lastMessageId": None if self.last_log_id is None else str(self.last_log_id),
            "lastActivityAt": to_iso(self.last_updated_at),
            "readable": self.readable,
        }
        # Only when there is one, like every other key that reports a presence.
        if self.images:
            payload["images"] = list(self.images)
        return payload


@dataclass(frozen=True)
class Mention:
    """One @name, kept as the account it points at rather than as the name.

    The text of a message carries whatever name that account wore when it was
    written, and a name can be taken by anyone; the id is the only part of a
    mention that still identifies somebody later.
    """

    user_id: int
    name: Optional[str]
    """Who that id is *now*, or None when this profile has never seen them."""
    offsets: tuple[int, ...]
    """Where the name sits in the message text, as the client recorded it."""
    length: int

    def as_json(self) -> dict[str, Any]:
        return {
            "userId": str(self.user_id),
            "name": self.name,
            "at": list(self.offsets),
            "length": self.length,
        }


@dataclass(frozen=True)
class Quote:
    """The message a reply was written against, as the reply itself recorded it.

    This is the client's own copy, taken when the reply was sent: it outlives
    the original being deleted, and it can disagree with what the original row
    says today.  It is what the sender was answering, which is the question a
    reader has.
    """

    message_id: Optional[int]
    sender_id: Optional[int]
    sender: Optional[str]
    text: str
    type: Optional[int]

    def as_json(self) -> dict[str, Any]:
        return {
            "messageId": None if self.message_id is None else str(self.message_id),
            "senderId": None if self.sender_id is None else str(self.sender_id),
            "sender": self.sender,
            "text": self.text,
            "kind": None if self.type is None else message_kind(self.type),
        }


PHOTO_ADDRESS_KEYS: Final = ("url", "thumbnailUrl")
"""Where a single photo's address lives, best first.

The full-size address comes first on purpose: the terminal scales whatever it
is given into the same cells, so a thumbnail is only a softer picture in the
same space.
"""

PHOTO_LIST_KEYS: Final = ("imageUrls", "thumbnailUrls")
"""The same for a multi-photo message, which stores lists instead."""


def photo_address(attachment: dict[str, Any]) -> Optional[str]:
    """Return the address of the picture to show, or None when there is none.

    A multi-photo message shows its first picture: the transcript has one line
    to spend on it, and the first is the one the client shows as the cover.
    """
    for key in PHOTO_ADDRESS_KEYS:
        value = attachment.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for key in PHOTO_LIST_KEYS:
        values = attachment.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return None


@dataclass(frozen=True)
class Photo:
    """Where a picture lives and how large it was sent.

    The client stores an address rather than the picture, so this is what it
    takes to go and get one. The size is what the transcript reserves rows
    against before anything has been fetched — it is the *stored* size, which
    a rotated photo can disagree with, so whatever is fetched is measured again.

    It reaches :meth:`Message.as_json` as ``photo``, only on a message that
    carries one, in the shape the macOS build already answered with — the
    address and the size, not the picture. Nothing here fetches anything: a
    browser given the address fetches it from the client's CDN itself.
    """

    url: str
    width: int
    height: int
    count: int = 1
    """How many pictures the message carried; an album's cover is ``url``."""
    images: tuple[tuple[str, int, int], ...] = ()
    """Every picture, in order, as ``(url, width, height)``; one for a single photo."""

    def as_json(self) -> dict[str, Any]:
        images = self.images or ((self.url, self.width, self.height),)
        return {
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "count": self.count,
            "images": [
                {"url": url, "width": width, "height": height} for url, width, height in images
            ],
        }


def photo_in(type_code: int, attachment: dict[str, Any]) -> Optional[Photo]:
    """Return the picture a photo message carries, or None for anything else.

    Only the kinds that really are pictures: a single photo and an album. A
    video stores the same shaped attachment and is deliberately left out, so it
    keeps its label rather than offering to open something that is not there.
    """
    if type_code not in (2, 27):
        return None
    url = photo_address(attachment)
    if url is None:
        return None
    width = attachment.get("w") or _first_number(attachment.get("wl"))
    height = attachment.get("h") or _first_number(attachment.get("hl"))
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    if width <= 0 or height <= 0:
        return None
    images = album_images(attachment) or ((url, width, height),)
    return Photo(url=url, width=width, height=height, count=len(images), images=images)


def album_images(attachment: dict[str, Any]) -> tuple[tuple[str, int, int], ...]:
    """Every picture an album lists, in order, with the size stored beside it.

    An album keeps parallel lists: ``imageUrls`` with the originals' sizes in
    ``wl``/``hl``, and ``thumbnailUrls`` with ``thumbnailWidths``/``Heights``.
    Measured on live albums of 2, 4 and 6 pictures, every list held one entry
    per picture on both builds. The originals are preferred; a size the list
    does not hold is 0 rather than a guess. Empty for a single photo.
    """
    for key, widths_key, heights_key in (
        ("imageUrls", "wl", "hl"),
        ("thumbnailUrls", "thumbnailWidths", "thumbnailHeights"),
    ):
        addresses = attachment.get(key)
        if not isinstance(addresses, list):
            continue
        widths, heights = attachment.get(widths_key), attachment.get(heights_key)
        found = [
            (address, _nth_number(widths, index), _nth_number(heights, index))
            for index, address in enumerate(addresses)
            if isinstance(address, str) and address.startswith("http")
        ]
        if found:
            return tuple(found)
    return ()


def _nth_number(values: object, index: int) -> int:
    if isinstance(values, list) and index < len(values):
        value = values[index]
        if isinstance(value, int) and value > 0:
            return value
    return 0


def _first_number(values: object) -> Optional[int]:
    """The first usable number of an album's parallel size list."""
    if isinstance(values, list):
        for value in values:
            if isinstance(value, int) and value > 0:
                return value
    return None


@dataclass(frozen=True)
class Video:
    """Where a video lives and what the client stored about it.

    No thumbnail: across 2,937 videos on the Windows build and 877 on the macOS
    one, not one attachment carried a thumbnail address. What there is is the
    file's address, its size in pixels, its length and its size in bytes, and
    on some of them the moment the address stops working — measured at exactly
    14 days after the message was sent, on both builds.
    """

    url: str
    width: int
    height: int
    duration: int
    """Seconds, as the client stored it."""
    size: int
    """Bytes."""
    expires_at: Optional[int] = None
    """Epoch milliseconds after which the address may no longer answer."""

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "size": self.size,
        }
        if self.expires_at is not None:
            try:
                payload["expiresAt"] = datetime.fromtimestamp(
                    self.expires_at / 1000, timezone.utc
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        return payload


def video_in(type_code: int, attachment: dict[str, Any]) -> Optional[Video]:
    """Return the video a video message carries, or None for anything else."""
    if type_code != 3:
        return None
    address = attachment.get("url")
    if not isinstance(address, str) or not address.startswith("http"):
        return None

    def number(key: str) -> int:
        value = attachment.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    expire = attachment.get("expire")
    return Video(
        url=address,
        width=number("w"),
        height=number("h"),
        duration=number("d"),
        size=number("s"),
        expires_at=expire if isinstance(expire, int) and not isinstance(expire, bool) and expire > 0 else None,
    )


@dataclass(frozen=True)
class Reaction:
    """One kind of 공감 on a message, and how many people chose it."""

    label: str
    """The word the client shows for it, such as 사랑 or 엄지척."""
    count: int
    code: Optional[str] = None
    """The mini emoticon it is drawn with, as the client addresses it."""

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": self.label, "count": self.count}
        if self.code:
            payload["code"] = self.code
        return payload


@dataclass(frozen=True)
class Message:
    """One message, already attributed to a named sender."""

    id: int
    room_id: int
    sender_id: int
    sender: str
    text: str
    type: int
    sent_at: Optional[int]
    from_me: bool
    mentions: tuple[Mention, ...] = ()
    mentions_all: bool = False
    """Whether the message carried an @전체 rather than naming anybody."""
    mentions_me: bool = False
    """Whether the signed-in account is one of the ids this message named."""
    reply: Optional[Quote] = None
    replies_to_me: bool = False
    photo: Optional[Photo] = None
    """The picture this message carries, for the interface to draw on request."""
    video: Optional["Video"] = None
    """The video this message carries: its address and what the client stored."""
    erased: bool = False
    """Deleted for everyone; its text is never carried, only that it was there.

    Only the interface asks for these rows, to say "삭제된 메시지입니다" where
    the client does. The commands' answer leaves them out, as it leaves out
    ``deleted`` rows, and ``as_json`` does not mention the field.
    """
    reactions: tuple[Reaction, ...] = ()
    edited: bool = False
    """Whether the sender rewrote it; the text is the newest version."""
    covered: Optional[str] = None
    """Who covered it (``safebot``), when something did.

    The text is withheld unless the reader was asked to reveal covered
    messages, the same way the client hides it until it is tapped.
    """
    sender_image: Optional[str] = None
    """The sender's small profile picture on the client's CDN, when known."""

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    def as_json(self) -> dict[str, Any]:
        """Render a message, carrying the extra keys only when they say something.

        A window is forty of these, so a mention list, a quote and three flags
        on every ordinary line would triple the size of the common answer to
        report the absence of everything.  Their absence is the negative.
        """
        payload: dict[str, Any] = {
            "id": str(self.id),
            "roomId": str(self.room_id),
            "senderId": str(self.sender_id),
            "sender": self.sender,
            "text": self.text,
            "kind": message_kind(self.type),
            "sentAt": to_iso(self.sent_at),
            "fromMe": self.from_me,
        }
        if self.mentions:
            payload["mentions"] = [mention.as_json() for mention in self.mentions]
        if self.mentions_all:
            payload["mentionsAll"] = True
        if self.mentions_me:
            payload["mentionsMe"] = True
        if self.reply is not None:
            payload["replyTo"] = self.reply.as_json()
        if self.replies_to_me:
            payload["repliesToMe"] = True
        if self.photo is not None:
            payload["photo"] = self.photo.as_json()
        if self.video is not None:
            payload["video"] = self.video.as_json()
        if self.reactions:
            payload["reactions"] = [reaction.as_json() for reaction in self.reactions]
        if self.edited:
            payload["edited"] = True
        if self.covered is not None:
            payload["covered"] = self.covered
        if self.sender_image is not None:
            payload["senderImage"] = self.sender_image
        return payload


def parse_attachment(raw: object) -> dict[str, Any]:
    """Read the client's ``attachement`` column, treating anything odd as empty.

    The column holds a different JSON shape per message type and is written by
    a program this one does not control, so a value that cannot be read has to
    mean "nothing extra was recorded" rather than failing the conversation it
    happens to sit in.
    """
    if not isinstance(raw, str) or not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _identifier(value: object) -> Optional[int]:
    """Read a client-written number, refusing anything that is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _offsets(value: object) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    found = (_identifier(item) for item in value)
    return tuple(offset for offset in found if offset is not None)


def mentions_in(
    attachment: dict[str, Any], resolve: Callable[[int], Optional[str]]
) -> tuple[Mention, ...]:
    """Return everyone a message named, dropping entries that name nobody.

    Measured on a live client, every entry was ``{"at": [offset], "len": n,
    "user_id": id}`` with an integer id; an entry without a readable id is
    skipped rather than turned into a mention of user 0.
    """
    entries = attachment.get("mentions")
    if not isinstance(entries, list):
        return ()
    found: list[Mention] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        user_id = _identifier(entry.get("user_id"))
        if user_id is None:
            continue
        found.append(
            Mention(
                user_id=user_id,
                name=resolve(user_id),
                offsets=_offsets(entry.get("at")),
                length=_identifier(entry.get("len")) or 0,
            )
        )
    return tuple(found)


def everyone_mentioned(attachment: dict[str, Any]) -> bool:
    """Whether the message carried an @전체 instead of naming people.

    The client writes ``all_mention`` as an object holding where the token
    sits — ``{"at": 1}`` on every one observed — so its presence is the flag
    and its contents are not relied on.
    """
    return bool(attachment.get("all_mention"))


def quoted_in(
    attachment: dict[str, Any], resolve: Callable[[int], Optional[str]]
) -> Optional[Quote]:
    """Return the message a reply answered, or None when none was recorded.

    Keyed on ``src_logId`` rather than on the message type: what makes a
    message a reply here is that it recorded a source, and reading the type
    instead would drop a quote the moment the client numbers one differently.
    """
    source = _identifier(attachment.get("src_logId"))
    if source is None:
        return None
    sender_id = _identifier(attachment.get("src_userId"))
    text = attachment.get("src_message")
    return Quote(
        message_id=source,
        sender_id=sender_id,
        sender=None if sender_id is None else resolve(sender_id),
        text=text if isinstance(text, str) else "",
        type=_identifier(attachment.get("src_type")),
    )


def reactions_in(raw: object) -> tuple[Reaction, ...]:
    """Read a message's 공감 out of its ``chatLogMeta`` row.

    An entry without a positive count is nobody's reaction and is dropped;
    one without a word keeps the client's generic name for the feature.
    """
    if not isinstance(raw, str) or not raw.startswith("{"):
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ()
    entries = parsed.get("rx") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return ()
    found: list[Reaction] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        count = _identifier(entry.get("c"))
        if count is None or count <= 0:
            continue
        words = entry.get("a")
        label = words.get("ko") if isinstance(words, dict) else None
        code = entry.get("o")
        found.append(
            Reaction(
                label=label.strip() if isinstance(label, str) and label.strip() else "공감",
                count=count,
                code=code if isinstance(code, str) and code else None,
            )
        )
    return tuple(found)


def covered_in(attachment: dict[str, Any]) -> Optional[str]:
    """Say who covered a message, or None when nothing did.

    Only a literal ``true`` counts: the key is the client's own flag, and a
    string or a number there is something this build has never seen.
    """
    return SAFEBOT if attachment.get("safeBot") is True else None


LINK_PATTERN: Final = re.compile(r"https?://[^\s<>\"'()\[\]\x00-\x1f\x7f-\x9f]+")
"""An address inside message text; ends at whitespace, a bracket or a control."""


def links_in(text: str) -> tuple[str, ...]:
    """Every address a message's text carries, in order, repeats dropped."""
    seen: list[str] = []
    for match in LINK_PATTERN.finditer(text or ""):
        address = match.group().rstrip(".,!?;:")
        if address not in seen:
            seen.append(address)
    return tuple(seen)


def board_share(attachment: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Read a board share's ``(post id, text)`` out of its attachment."""
    segments = attachment.get("os")
    post_id: Optional[str] = None
    text: Optional[str] = None
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        if segment.get("t") == 1 and isinstance(segment.get("ct"), str):
            text = segment["ct"]
        elif segment.get("t") == 2 and isinstance(segment.get("url"), str):
            found = parse_qs(urlsplit(segment["url"]).query).get("post_id")
            post_id = found[0] if found else None
    return post_id, text


def instant_of(value: object) -> Optional[int]:
    """Read a client-written moment as epoch seconds, or None.

    The catalog writes seconds, a board post an ISO string, and some rows
    milliseconds; a number past 10^11 is taken as milliseconds.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return value // 1000 if value > 10**11 else value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return instant_of(int(text))
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    return None


@dataclass(frozen=True)
class Notice:
    """The notice pinned at the top of a room.

    The client keeps two. A board post (``moimMetas``) is what a current
    client pins; the older ``chatMetas`` row is what it pinned before the
    board existed, and in every room holding both it was the older one —
    2019–2023 against 2025–2026 — so it is read only when there is no post.
    """

    text: str
    kind: str
    """``text``, ``image``, ``schedule`` or whatever else the board called it."""
    author_id: Optional[int]
    author: Optional[str]
    posted_at: Optional[int]
    source: str
    """``board`` for a board post, ``legacy`` for the older notice row."""
    truncated: bool = False
    """The client stored only the post's first :data:`BOARD_PREVIEW_CHARACTERS`."""
    full_text: Optional[str] = None
    """The one message in the room that begins with the whole stored preview.

    Found by its words, not by any id — the post records none — and only when
    exactly one message qualifies.
    """
    full_text_message_id: Optional[int] = None
    full_text_truncated: bool = False
    """The message itself stopped at :data:`LONG_MESSAGE_CHARACTERS`.

    A longer message keeps its first 3,999 characters in the row and the whole
    text in a file named by its attachment's ``path``. That file is not on this
    machine and the CDN refuses it (403) without the client's own protocol, so
    what is here is all this tool can read.
    """
    full_text_candidates: tuple[int, ...] = ()
    """Messages that each begin with the preview, when more than one does; none is picked."""
    full_text_source: Optional[str] = None
    """``share`` when :attr:`full_text` came from the board post's share, ``message``
    when it came from the message the notice was pinned from."""

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": self.text,
            "kind": self.kind,
            "authorId": None if self.author_id is None else str(self.author_id),
            "author": self.author,
            "postedAt": to_iso(self.posted_at),
            "source": self.source,
        }
        # Only when they say something, like every key that reports a presence.
        if self.truncated:
            payload["truncated"] = True
        if self.full_text is not None and self.full_text_message_id is not None:
            payload["fullText"] = self.full_text
            payload["fullTextMessageId"] = str(self.full_text_message_id)
            if self.full_text_source is not None:
                payload["fullTextSource"] = self.full_text_source
            if self.full_text_truncated:
                payload["fullTextTruncated"] = True
        if self.full_text_candidates:
            payload["fullTextCandidates"] = [str(one) for one in self.full_text_candidates]
        return payload


BOARD_PREVIEW_CHARACTERS: Final = 100
"""How much of a notice the client keeps in the row that names it.

Measured: the eleven board notices on a live account were 0, 23, 28, 33, 48,
52 and 100 characters long, and both ``content`` and ``json_content`` stopped
at exactly 100 in the posts that were longer. The older ``chatMetas`` notice
row stops there too. Its longest of 15 rows was exactly 100, and it was a
2022 notice that the macOS client's own row also stops at exactly 100
(2026-10-02). What is past the cut may be in the share of the post or in
the message that was pinned.
"""

BOARD_SHARE_MESSAGE: Final = 98
"""A board post shared into the room, written when the post is made.

Its attachment's ``os`` segments are the author (``t`` 3), the text (``t`` 1,
``ct`` and the same words as ``jct``) and the post's address (``t`` 2,
``kakaomoim://post?...&post_id=``), whose ``post_id`` is the board post's
``id``. Measured 2026-10-02: 10 shares, every ``jct`` saying exactly what
``ct`` said. In the two rooms with a cut board notice, a share carried the
notice's ``post_id``. One share began with the stored preview. The other
shared its first 4 characters only, so the post had been edited since it
was shared.
"""

BOARD_SHARE_CHARACTERS: Final = 500
"""How much of a post its share carries: the 10 measured were 23 to 447
characters long, or exactly 500."""

LONG_MESSAGE_CHARACTERS: Final = 3999
"""How much of a long message the client keeps in its row.

Measured over six readable rooms: 23 messages were exactly 3,999 characters,
none longer, and every one carried a ``{k, path, s, sd}`` attachment naming the
whole text as a ``.txt`` file; no shorter message carried one.
"""


@dataclass(frozen=True)
class Revision:
    """One version of an edited message, as ``chatLogHistory`` kept it."""

    revision: int
    text: str
    type: int

    def as_json(self) -> dict[str, Any]:
        return {"revision": self.revision, "text": self.text, "kind": message_kind(self.type)}


ATTACHMENT_KINDS: Final = ("photo", "video", "file", "link")


@dataclass(frozen=True)
class Attachment:
    """Something a message carried that can be gathered in one list.

    A photo, a video, a file or a link. What is stored is an address, never
    the thing itself; a file's address stops answering once it expires.
    """

    message_id: int
    kind: str
    sender_id: int
    sender: str
    sent_at: Optional[int]
    url: str
    name: Optional[str] = None
    """A file's name as it was sent. Somebody else wrote it."""
    size: Optional[int] = None
    """Bytes, for a file or a video."""
    count: int = 1
    """How many pictures an album carried; its first is ``url``."""
    expires_at: Optional[int] = None
    """Epoch milliseconds after which the address may no longer answer."""

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messageId": str(self.message_id),
            "kind": self.kind,
            "senderId": str(self.sender_id),
            "sender": self.sender,
            "sentAt": to_iso(self.sent_at),
            "url": self.url,
        }
        if self.name is not None:
            payload["name"] = self.name
        if self.size is not None:
            payload["size"] = self.size
        if self.count > 1:
            payload["count"] = self.count
        if self.expires_at is not None:
            try:
                payload["expiresAt"] = datetime.fromtimestamp(
                    self.expires_at / 1000, timezone.utc
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        return payload


def file_in(type_code: int, attachment: dict[str, Any]) -> Optional[tuple[str, str, Optional[int], Optional[int]]]:
    """Return a file message's ``(url, name, size, expires)``, or None.

    Measured on 91 file rows: every one carried ``url``, ``name``, ``size``
    (also as ``s``) and ``expire`` in milliseconds, the address on
    ``talk.kakaocdn.net`` or ``dn.talk.kakao.com``.
    """
    if type_code != FILE_MESSAGE:
        return None
    address = attachment.get("url")
    name = attachment.get("name")
    if not isinstance(address, str) or not address.startswith("http"):
        return None
    size = _identifier(attachment.get("size"))
    if size is None:
        size = _identifier(attachment.get("s"))
    expire = _identifier(attachment.get("expire"))
    return (
        address,
        name if isinstance(name, str) and name else "file",
        size if size is not None and size >= 0 else None,
        expire if expire is not None and expire > 0 else None,
    )


FRIEND_TYPE: Final = 1
"""The talkUser.type value for an added friend, confirmed against a live client.

Other values seen: 1000 is a known non-friend (a group co-member), and the
negatives (-3, -4, -10) plus 9 are special relationships. Only 1 is the list
the client shows as 친구.
"""


@dataclass(frozen=True)
class Friend:
    """One entry from the signed-in user's friend list."""

    id: int
    name: str
    """Best display name: the name the user set wins over the friend's own."""
    nickname: Optional[str]
    """The name the friend chose for themselves."""
    phone: Optional[str]
    status_message: Optional[str]
    direct_chat_id: Optional[int]
    """The 1:1 room with this friend, when one exists — the send target."""
    favorite: bool

    def as_json(self, include_phone: bool = False) -> dict[str, Any]:
        """Render a friend.

        The phone number is withheld unless explicitly requested, because it is
        the most sensitive field and most callers only need a name and an id.
        """
        payload: dict[str, Any] = {
            "id": str(self.id),
            "name": self.name,
            "nickname": self.nickname,
            "statusMessage": self.status_message,
            "directChatId": None if self.direct_chat_id is None else str(self.direct_chat_id),
            "favorite": self.favorite,
        }
        if include_phone:
            payload["phone"] = self.phone
        return payload
