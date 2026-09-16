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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Final, Optional, Sequence

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

    @property
    def kind(self) -> str:
        return ROOM_KINDS.get(self.type, self.type)

    def as_json(self) -> dict[str, Any]:
        return {
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
