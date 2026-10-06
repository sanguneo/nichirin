"""Have the client open a room, for a caller that wants to send to it or read it.

The command and the server both need this, and ``serve`` must not import
``cli``, so it lives below both. It reads the catalog through a reader and
drives the client through ``winauto``; nothing here decides *whether* a room
should be opened — that is the caller's, because opening a room marks it read.
"""

from __future__ import annotations

from typing import Optional

from .errors import ErrorCode, KanaoError
from .model import Room
from .reader import Reader


def window_target(
    reader: Reader, room: Room
) -> tuple[list[str], Optional[int], Optional[int]]:
    """The titles a room's window may carry and where the list likely shows it.

    Returns the aliases, the catalog's recency position and the row the
    client's own search should list it at. Refuses a title two catalogued
    rooms share: a window can only be told apart by its title, so neither of
    them can be opened or written to.
    """
    catalogued = reader.rooms()
    # A window title cannot distinguish two catalog rooms with the same name,
    # even if the caller supplied an id and only one window is currently open.
    duplicates = [r for r in catalogued if r.title.casefold() == room.title.casefold()]
    if len(duplicates) != 1:
        raise KanaoError(
            ErrorCode.AMBIGUOUS_CHAT,
            "multiple catalog rooms share this window title; give the target a unique title",
            candidates=[r.as_json() for r in duplicates],
        )
    # The window carries the catalog's own title, which differs from the one
    # resolved here for the self-chat and for groups with no custom name.
    stored = reader.stored_title(room.id)
    aliases = [stored] if stored and stored != room.title else []
    # The client lists rooms by recency too, so this is close before it is
    # right — close enough that opening one usually costs a single click.
    hint = next((index for index, r in enumerate(catalogued) if r.id == room.id), None)
    return aliases, hint, reader.search_position(room.id, rooms=catalogued)


def open_for_reading(reader: Reader, room: Room) -> None:
    """Have the client open *room* so its key is loaded, then wait for the key.

    The window is left minimised rather than closed: the key belongs to the
    open log, and nothing says the client keeps it once the room is closed.
    """
    from .winauto import make_sender

    aliases, hint, search_row = window_target(reader, room)
    make_sender().resolve(
        room.title, open_if_needed=True, aliases=aliases, hint=hint, search_row=search_row
    )
    if not reader.unlock(room.id):
        raise KanaoError(
            ErrorCode.KEY_UNAVAILABLE,
            f"opened {room.title!r} in KakaoTalk but its key did not appear; try again",
        )
