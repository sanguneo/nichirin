"""The one call an agent makes to understand a conversation.

The failure this shape is designed against is an agent that reads a partial
local cache, sees the last message is from the other person, and confidently
drafts a reply the user already sent.  So the answer always carries how far the
records reach, never guesses which room was meant, and never implies the server
history was consulted.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence, Union

from .errors import ErrorCode, KanaoError, success
from .model import Message
from .reader import Reader

DEFAULT_LIMIT = 40


def read_context(
    reader: Reader, target: Union[int, str], limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Resolve one room and return its recent messages with explicit coverage.

    *target* is either a numeric room id or a title.  An ambiguous title is
    refused with the candidates attached rather than resolved to whichever room
    happened to sort first.
    """
    if limit <= 0:
        raise KanaoError(ErrorCode.INVALID_REQUEST, "limit must be greater than zero")

    room = _resolve(reader, target)
    if not room.readable:
        raise KanaoError(
            ErrorCode.KEY_UNAVAILABLE,
            f"KakaoTalk has not opened this conversation, so its messages cannot be read; "
            f"open it in KakaoTalk and try again",
            room=room.as_json(),
        )

    # One extra record answers "is there history before this window" without a
    # second query.
    window = reader.messages(room.id, limit=limit + 1)
    has_earlier = len(window) > limit
    messages = window[-limit:] if has_earlier else window

    newest_read = max((message.id for message in messages), default=None)
    catalog_latest = room.last_log_id
    reaches_latest: Optional[bool] = None
    if catalog_latest is not None:
        reaches_latest = newest_read is not None and newest_read >= catalog_latest

    return success(
        {
            "room": room.as_json(),
            "messages": [message.as_json() for message in messages],
            "lastMessageFromMe": messages[-1].from_me if messages else None,
            "attention": _attention(messages),
            "coverage": {
                "localOnly": True,
                "reachesLatestKnown": reaches_latest,
                "catalogLatestMessageId": None if catalog_latest is None else str(catalog_latest),
                "newestReadMessageId": None if newest_read is None else str(newest_read),
                "hasEarlier": has_earlier,
                "storedLocally": reader.count_messages(room.id),
            },
        }
    )


def _attention(messages: Sequence[Message]) -> dict[str, Any]:
    """Say which messages in this window were addressed at the user.

    A reply and an @name are the two ways somebody asks for an answer, and
    neither is visible in the text: the quoted message lives in the reply's
    attachment, and a mention reads as an ordinary name that anybody in the
    room could also be wearing.  Messages the user sent are left out, so this
    is a list of things waiting on them rather than of things they did.

    Ids only.  The messages themselves are already in the answer, and pointing
    at them twice invites a summary written from the pointer.
    """
    incoming = [message for message in messages if not message.from_me]
    return {
        "mentionsMe": [str(message.id) for message in incoming if message.mentions_me],
        "repliesToMe": [str(message.id) for message in incoming if message.replies_to_me],
        "mentionsEveryone": [str(message.id) for message in incoming if message.mentions_all],
    }


def _resolve(reader: Reader, target: Union[int, str]):
    if isinstance(target, int):
        room = reader.room(target)
        if room is None:
            raise KanaoError(
                ErrorCode.CHAT_NOT_FOUND, f"No conversation has id {target}"
            )
        return room

    matches = reader.resolve(target)
    if not matches:
        raise KanaoError(
            ErrorCode.CHAT_NOT_FOUND,
            f"No conversation is titled {target!r}; list conversations to see the stored titles",
        )
    if len(matches) > 1:
        raise KanaoError(
            ErrorCode.AMBIGUOUS_CHAT,
            f"{len(matches)} conversations match {target!r}; choose one and retry with its id",
            candidates=[match.as_json() for match in matches],
        )
    return matches[0]
