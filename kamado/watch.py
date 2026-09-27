"""Follow conversations as they change, without paying to decrypt them.

Watching is only affordable because detecting "nothing happened" is far
cheaper than reading.  A poll stats each watched room's write-ahead log; a room
whose log is byte-for-byte where it was cannot have new messages, so it is
skipped before any decryption is considered.  Measured on this machine a
targeted stat costs about 0.09ms against roughly 1 second to decrypt a large
room, so an idle conversation costs effectively nothing to follow.

On macOS every room lives in one database, so the stat that decides whether
anything happened is a *single* stat for the whole account rather than one per
room.  An idle client therefore costs one ``stat`` per poll no matter how many
conversations are being followed, and a busy one costs a single decryption that
every watched room then reads from.

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

from .errors import KamadoError
from .model import Message
from .reader import Reader
from .snapshot import Fingerprint

DEFAULT_INTERVAL_SECONDS = 1.0
_CATCH_UP_LIMIT = 500
_ROOM_FAILURES = (KamadoError, OSError, sqlite3.Error)


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
        self._print: Optional[Fingerprint] = None
        self._seen: dict[int, int] = {}
        self._primed = False
        self.moved: list[int] = []
        """Rooms read in the last poll because the database had moved.

        A database can move without a new message: a message deleted for
        everyone keeps its id and has its text replaced. A caller holding
        messages it has already shown needs to know that happened, and the
        new ones alone cannot tell it.
        """

    def watch(self, room_id: int) -> None:
        """Follow one more room, from now rather than from its beginning.

        A second room costs no second poll: the fingerprint that decides
        whether anything happened covers the whole account, so one watcher
        answers for every room it is given. Joining late is the point of
        recording where the room already stands — without that, the next pass
        would report its whole recent history as new.
        """
        if self._requested is None or room_id in self._requested:
            return
        self._requested.append(room_id)
        if not self._primed:
            return
        try:
            self._seen[room_id] = self._position(room_id)
        except _ROOM_FAILURES:
            return

    def _position(self, room_id: int) -> int:
        """Where a room stands now, which is 0 for a room with no messages yet.

        An empty room is a place to start from like any other. Leaving it
        unrecorded sent its first message down the path for a room that was
        never primed, which records the position without reporting anything -
        so the first message a room ever received was the one message a
        follower was guaranteed not to see.
        """
        newest = self._reader.newest_log_id(room_id, include_erased=self._include_erased)
        return 0 if newest is None else newest

    def unwatch(self, room_id: int) -> None:
        """Stop following a room, forgetting where it had got to."""
        if self._requested is not None and room_id in self._requested:
            self._requested.remove(room_id)
        self._seen.pop(room_id, None)

    def watched_rooms(self) -> list[int]:
        """Return the room ids being followed, defaulting to every room."""
        if self._requested is not None:
            return list(self._requested)
        try:
            return [room.id for room in self._reader.rooms()]
        except _ROOM_FAILURES:
            return []

    def _database(self):
        return self._reader.database

    def resume(self, cursor: Mapping[Identifier, Identifier]) -> None:
        """Start from what a caller has already seen rather than from now.

        *cursor* maps room id to the last log id that reader received. Priming
        would make the watcher agree that everything up to this instant is old,
        which is exactly wrong for somebody reconnecting: the messages they
        missed are the ones between their cursor and now.

        The account fingerprint is deliberately left unset, so the first poll
        goes through and pages each cursored room forward from where its reader
        stopped. A room the cursor does not name is started from now, because a
        caller that never saw a message from it has no place to be resumed to;
        that is a gap, and it is the caller's to close with ``context`` if it
        matters.
        """
        for room, log in cursor.items():
            self._seen[int(room)] = int(log)
        self._primed = True

    def prime(self) -> None:
        """Record the current state so existing history is not replayed as new."""
        self._print = Fingerprint.of(self._database())
        for room_id in self.watched_rooms():
            try:
                self._seen[room_id] = self._position(room_id)
            except _ROOM_FAILURES:
                continue
        self._primed = True

    def poll(self) -> list[Message]:
        """Run exactly one detection pass and return whatever is new.

        A database that has not moved is never opened, so an unchanged poll
        performs one stat for the whole account and no decryption at all.
        """
        self.moved = []
        if not self._primed:
            self.prime()
            return []

        current = Fingerprint.of(self._database())
        if self._print == current:
            return []

        per_room: list[list[Message]] = []
        failed = False
        for room_id in self.watched_rooms():
            since = self._seen.get(room_id)
            try:
                if since is None:
                    self._seen[room_id] = self._position(room_id)
                    continue
                arrived = self._catch_up(room_id, since)
            except _ROOM_FAILURES:
                # Following must outlive any single room: a conversation can
                # lose its key, be closed in the client, or have its database
                # removed entirely while the loop is running, and none of that
                # is a reason to stop reporting the other rooms.
                failed = True
                continue
            self.moved.append(room_id)
            if arrived:
                self._seen[room_id] = max(since, arrived[-1].id)
                per_room.append(arrived)

        # Only a pass where every room was read stands in for this state of
        # the database.  Recording it after a failure would let the failed
        # read pass for a successful one: the next poll would find the
        # fingerprint unchanged, return immediately, and whatever arrived
        # during the failure would wait for the next write by somebody else.
        if not failed:
            self._print = current

        return _interleave(per_room)

    def _catch_up(self, room_id: int, since: int) -> list[Message]:
        """Collect every message after *since*, a page at a time.

        One page is not enough.  The client backfills from the server after a
        reconnection, so a room can gain far more than a page between two
        polls, and a single read would report the newest page and leave the
        cursor to skip the rest for good.

        It is also bounded.  Each page is read from whatever snapshot the
        cache holds by then, so a room written to faster than it is paged
        through would keep every page full and never let the loop end.  The
        newest id when the catch-up starts is the ceiling: anything past it
        is left to the next poll, whose fingerprint will have moved.  That
        bounds the number of pages, not the rows kept.
        """
        ceiling = self._reader.newest_log_id(room_id, include_erased=self._include_erased)
        collected: list[Message] = []
        cursor = since
        while ceiling is not None and cursor < ceiling:
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
    what a cursor resumes from.  Sorting everything by time broke it whenever
    a room's clock disagreed with its ids - ``(10, 12:01)`` then
    ``(11, 12:00)`` came out as 11, 10, and a follower storing the last id it
    saw stored 10 and was handed 11 again on reconnecting.  A merge only ever
    compares the next message of each room, so across rooms the earlier one
    still goes first, and within a room nothing moves.
    """
    return list(
        heapq.merge(*per_room, key=lambda message: (message.sent_at or 0, message.id))
    )
