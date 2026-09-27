"""Follow conversations as they change, without paying to decrypt them.

Watching is only affordable because detecting "nothing happened" is far
cheaper than reading.  A poll stats each watched room's write-ahead log; a room
whose log is byte-for-byte where it was cannot have new messages, so it is
skipped before any decryption is considered.  Measured on this machine a
targeted stat costs about 0.09ms against roughly 1 second to decrypt a large
room, so an idle conversation costs effectively nothing to follow.

The detection has to be stat-based.  ``ReadDirectoryChangesW`` — and therefore
C#'s ``FileSystemWatcher``, which wraps it — reported zero events across a
30-second observation of the live client, because NTFS defers directory-entry
updates while SQLite holds the log handle open.  Polling the file's own
metadata sees every change that notifications miss.

Separating :meth:`Watcher.poll` from :meth:`Watcher.follow` is deliberate: one
pass is fully testable without waiting on a clock, and only the loop sleeps.
"""

from __future__ import annotations

import heapq
import sqlite3
import time
from typing import Iterator, Mapping, Optional, Sequence, Union

Identifier = Union[str, int]
"""How an id arrives here: as text over the wire, or as an int in process.

Ids exceed 2^53 so they cross the boundary as strings, and a caller inside
this process has no reason to stringify one first. Both are accepted and both
are converted once, at the door.
"""

from . import paths
from .errors import KanaoError
from .model import Message
from .reader import Reader
from .snapshot import Fingerprint

DEFAULT_INTERVAL_SECONDS = 1.0
_CATCH_UP_LIMIT = 500
_ROOM_FAILURES = (KanaoError, OSError, sqlite3.Error)


class Watcher:
    """Emit messages that appear after the watcher starts."""

    def __init__(
        self,
        reader: Reader,
        room_ids: Optional[Sequence[int]] = None,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        include_erased: bool = False,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._reader = reader
        self._include_erased = include_erased
        self._requested = None if room_ids is None else list(room_ids)
        self.interval = interval
        self._prints: dict[int, Fingerprint] = {}
        self._seen: dict[int, int] = {}
        self._primed = False

    def watched_rooms(self) -> list[int]:
        """Return the room ids being followed, defaulting to every readable room."""
        if self._requested is not None:
            return list(self._requested)
        return self._reader.keyring.chat_ids()

    def _log_path(self, room_id: int):
        return self._reader.keyring.path_for(paths.chat_log_name(room_id))

    def resume(self, cursor: Mapping[Identifier, Identifier]) -> None:
        """Start from what a caller has already seen rather than from now.

        *cursor* maps room id to the last log id that reader received. Priming
        would make the watcher agree that everything up to this instant is old,
        which is exactly wrong for somebody reconnecting: the messages they
        missed are the ones between their cursor and now.

        No fingerprints are recorded, so the first poll looks at every watched
        room and pages each cursored one forward from where its reader stopped.
        A room the cursor does not name is started from now, because a caller
        that never saw a message from it has no place to be resumed to; that is
        a gap, and it is the caller's to close with ``context`` if it matters.
        """
        for room, log in cursor.items():
            self._seen[int(room)] = int(log)
        self._primed = True

    def prime(self) -> None:
        """Record the current state so existing history is not replayed as new."""
        for room_id in self.watched_rooms():
            path = self._log_path(room_id)
            self._prints[room_id] = Fingerprint.of(path)
            try:
                newest = self._reader.newest_log_id(room_id)
            except _ROOM_FAILURES:
                newest = None
            if newest is not None:
                self._seen[room_id] = newest
        self._primed = True

    def poll(self) -> list[Message]:
        """Run exactly one detection pass and return whatever is new.

        Rooms whose log has not moved are never opened, so an unchanged poll
        performs one stat per room and no decryption at all.
        """
        if not self._primed:
            self.prime()
            return []

        per_room: list[list[Message]] = []
        for room_id in self.watched_rooms():
            path = self._log_path(room_id)
            current = Fingerprint.of(path)
            if self._prints.get(room_id) == current:
                continue

            since = self._seen.get(room_id)
            try:
                if since is None:
                    newest = self._reader.newest_log_id(room_id)
                    if newest is not None:
                        self._seen[room_id] = newest
                    arrived = []
                else:
                    arrived = self._catch_up(room_id, since)
            except _ROOM_FAILURES:
                # Following must outlive any single room: a conversation can
                # lose its key, be closed in the client, or have its database
                # removed entirely while the loop is running, and none of that
                # is a reason to stop reporting the other rooms.
                #
                # The fingerprint is deliberately left where it was.  Recording
                # it here would let a failed read stand in for a successful
                # one, and the next poll would find the log unchanged and skip
                # the room — so whatever arrived during the failure would never
                # be reported at all.
                continue

            self._prints[room_id] = current
            if arrived:
                self._seen[room_id] = max(since, arrived[-1].id)
                per_room.append(arrived)

        return _interleave(per_room)

    def _catch_up(self, room_id: int, since: int) -> list[Message]:
        """Collect every message after *since*, a page at a time.

        One page is not enough.  The client backfills from the server after a
        reconnection, so a room can gain far more than a page between two
        polls, and a single read would report the first page and leave the
        cursor to skip the rest for good.

        It is also bounded. Each page is read from whatever snapshot the cache
        holds by then, so a room written to faster than it is paged through
        would keep every page full and never let the loop end. The newest id
        when the catch-up starts is the ceiling: anything past it is left to
        the next poll, whose fingerprint will have moved. That bounds the
        number of pages, not the rows kept - ``M`` arrivals still cost
        ``M`` messages here, and a caller that cannot hold them has to ask for
        less history rather than trust the page size.
        """
        ceiling = self._reader.newest_log_id(room_id, include_erased=self._include_erased)
        collected: list[Message] = []
        cursor = since
        while ceiling is None or cursor < ceiling:
            page = self._reader.messages(
                room_id,
                limit=_CATCH_UP_LIMIT,
                after_id=cursor,
                include_erased=self._include_erased,
            )
            if not page:
                break
            collected.extend(page)
            cursor = page[-1].id
            if len(page) < _CATCH_UP_LIMIT:
                break
        return collected

    def follow(self) -> Iterator[Message]:
        """Yield new messages forever, sleeping between passes."""
        self.prime()
        while True:
            for message in self.poll():
                yield message
            time.sleep(self.interval)


def _interleave(per_room: Sequence[Sequence[Message]]) -> list[Message]:
    """Merge rooms by time without reordering anything inside one room.

    Each room arrives in log-id order, and that order is the contract: it is
    what a cursor resumes from. Sorting everything by time broke it whenever
    a room's clock disagreed with its ids - ``(10, 12:01)`` then
    ``(11, 12:00)`` came out as 11, 10, and a follower storing the last id
    it saw stored 10 and was handed 11 again on reconnecting. A merge only
    ever compares the next message of each room, so across rooms the earlier
    one still goes first, and within a room nothing moves.
    """
    return list(
        heapq.merge(*per_room, key=lambda message: (message.sent_at or 0, message.id))
    )
