"""The records this tool hands to an agent, and how they are rendered as JSON.

Two rendering rules apply everywhere rather than per command.  Identifiers are
strings, because KakaoTalk ids run past 2**53 and would be silently corrupted
by an ordinary JavaScript ``JSON.parse``.  Times are ISO-8601 with an explicit
UTC offset, because a bare local timestamp invites an agent to state the wrong
hour with full confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Optional

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

TEXT_MESSAGE: Final = 1
PHOTO_MESSAGE: Final = 2
VIDEO_MESSAGE: Final = 3

_MESSAGE_KINDS: Final = {
    TEXT_MESSAGE: "text",
    PHOTO_MESSAGE: "photo",
    VIDEO_MESSAGE: "video",
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

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "roomId": str(self.room_id),
            "senderId": str(self.sender_id),
            "sender": self.sender,
            "text": self.text,
            "kind": message_kind(self.type),
            "sentAt": to_iso(self.sent_at),
            "fromMe": self.from_me,
        }


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
