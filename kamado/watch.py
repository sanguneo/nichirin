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

import sqlite3
import time
from typing import Iterator, Optional, Sequence

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
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._reader = reader
        self._requested = None if room_ids is None else list(room_ids)
        self.interval = interval
        self._print: Optional[Fingerprint] = None
        self._seen: dict[int, int] = {}
        self._primed = False

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

    def prime(self) -> None:
        """Record the current state so existing history is not replayed as new."""
        self._print = Fingerprint.of(self._database())
        for room_id in self.watched_rooms():
            try:
                newest = self._reader.newest_log_id(room_id)
            except _ROOM_FAILURES:
                newest = None
            if newest is not None:
                self._seen[room_id] = newest
        self._primed = True

    def poll(self) -> list[Message]:
        """Run exactly one detection pass and return whatever is new.

        A database that has not moved is never opened, so an unchanged poll
        performs one stat for the whole account and no decryption at all.
        """
        if not self._primed:
            self.prime()
            return []

        current = Fingerprint.of(self._database())
        if self._print == current:
            return []

        fresh: list[Message] = []
        failed = False
        for room_id in self.watched_rooms():
            since = self._seen.get(room_id)
            try:
                if since is None:
                    newest = self._reader.newest_log_id(room_id)
                    if newest is not None:
                        self._seen[room_id] = newest
                    continue
                arrived = self._catch_up(room_id, since)
            except _ROOM_FAILURES:
                # Following must outlive any single room: a conversation can
                # lose its key, be closed in the client, or have its database
                # removed entirely while the loop is running, and none of that
                # is a reason to stop reporting the other rooms.
                failed = True
                continue
            if arrived:
                self._seen[room_id] = max(message.id for message in arrived)
                fresh.extend(arrived)

        # Only a pass where every room was read stands in for this state of
        # the database.  Recording it after a failure would let the failed
        # read pass for a successful one: the next poll would find the
        # fingerprint unchanged, return immediately, and whatever arrived
        # during the failure would wait for the next write by somebody else.
        if not failed:
            self._print = current

        fresh.sort(key=lambda message: (message.sent_at or 0, message.id))
        return fresh

    def _catch_up(self, room_id: int, since: int) -> list[Message]:
        """Collect every message after *since*, a page at a time.

        One page is not enough.  The client backfills from the server after a
        reconnection, so a room can gain far more than a page between two
        polls, and a single read would report the newest page and leave the
        cursor to skip the rest for good.  Each page is strictly after the
        last id of the one before it, so this terminates.
        """
        collected: list[Message] = []
        cursor = since
        while True:
            page = self._reader.messages(
                room_id, limit=_CATCH_UP_LIMIT, after_id=cursor
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
