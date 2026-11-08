"""Optional local-log observation; never submit or retry a message."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Final, Optional, Protocol, TypedDict

from . import trace

DEFAULT_SECONDS: Final = 3.0
MAX_SECONDS: Final = 30.0
POLL_SECONDS: Final = 0.1


def parse_seconds(value: str | float) -> float:
    """Parse the bounded CLI/HTTP confirmation budget."""
    if isinstance(value, bool):
        raise ValueError("confirm must be seconds greater than 0 and at most 30")
    try:
        seconds = float(value)
    except OverflowError:
        raise ValueError("confirm must be seconds greater than 0 and at most 30") from None
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS:
        raise ValueError("confirm must be seconds greater than 0 and at most 30")
    return seconds


class Message(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def from_me(self) -> bool: ...


class Reader(Protocol):
    def newest_log_id(self, room_id: int) -> Optional[int]: ...
    def messages(
        self, room_id: int, limit: int = 40, *, after_id: Optional[int] = None
    ) -> Sequence[Message]: ...


class _Logged(TypedDict):
    logged: bool


class Result(_Logged, total=False):
    messageId: str


class Confirmation:
    """Capture at the input boundary and observe only later exact from-me rows.

    The caller owns the reader and serialization for this entire lifetime.
    A failed baseline is unknown, never an empty-room baseline.
    """

    def __init__(
        self, reader: Reader, room_id: int, text: str, seconds: float,
        errors: tuple[type[Exception], ...],
    ) -> None:
        self.reader = reader
        self.room_id = room_id
        self.text = text
        self.seconds = seconds
        self.errors = errors
        self.cursor: Optional[int] = None

    def capture(self) -> None:
        """Run immediately before the sender first writes the composer."""
        try:
            self.cursor = self.reader.newest_log_id(self.room_id) or 0
        except self.errors:
            self.cursor = None

    def check(self) -> Result:
        """Bound polling with a monotonic deadline; read errors remain unknown."""
        deadline = time.monotonic() + self.seconds
        first = True
        with trace.stage("log"):
            while self.cursor is not None and (first or time.monotonic() < deadline):
                first = False
                try:
                    rows = self.reader.messages(self.room_id, limit=100, after_id=self.cursor)
                except self.errors:
                    break
                # A synchronous snapshot read cannot be cancelled, but its
                # late answer must not count as observation within the budget.
                if time.monotonic() >= deadline:
                    break
                for row in rows:
                    if row.id > self.cursor and row.from_me and row.text == self.text:
                        trace.logged(True)
                        return {"logged": True, "messageId": str(row.id)}
                if rows:
                    self.cursor = max(self.cursor, *(row.id for row in rows))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Full pages are drained without a delay, still under the deadline.
                if len(rows) < 100:
                    time.sleep(min(POLL_SECONDS, remaining))
            trace.logged(False)
            return {"logged": False}
