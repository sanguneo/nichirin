"""Who wore which name, and when.

KakaoTalk stores only a user's *current* nickname: ``Profile`` and ``talkUser``
are overwritten in place, and no table anywhere keeps the previous value.  Two
consequences shape this module.

* **A rename leaves no trace of its own.**  The client writes a member's name
  into the log only when they join, leave, or are invited — ``chatLogs`` rows
  of type 0, whose ``message`` is JSON like
  ``{"feedType": 4, "members": [{"userId": …, "nickName": "…"}]}``.  Measured
  against one live open chat: 6,037 feed rows, of which 156 carried a name, for
  72 users, 18 of whom appear under more than one name.  Only feed types 4, 6,
  2 and 11 carry names at all; types 21 and 14, which are the bulk, carry none.
  So the feed history is real but sparse, and someone who renames without ever
  leaving the room is invisible in it.
* **Recovering the rest means writing it down ourselves.**  :class:`NameLedger`
  appends what the databases say right now, so a name that changes between two
  reads is captured even though the client never recorded the change.

Unlike the address cache, which deliberately stores no key, and the row memory,
which stores a hash rather than a title, this ledger holds real nicknames.  That
is the feature: a history of names cannot be kept without keeping the names.  It
lives beside the other local state under ``~/Library/Application Support``,
never in the repository, and is written only by callers that opt in with a path.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from . import paths
from .model import FEED_MESSAGE, to_iso
"""The ``chatLogs.type`` of a membership feed, the only row that names anyone."""

_MEMBER_KEYS = ("members", "member", "inviter", "leaver")
"""Feed payload keys that have been observed to hold ``{userId, nickName}``."""

FEED = "feed"
"""A name the client itself recorded, at a join, leave or invite."""

OBSERVED = "observed"
"""A name this tool read out of the databases and wrote down."""

CURRENT = "current"
"""The name the databases show right now, which no history may have reached."""


@dataclass(frozen=True)
class NameRecord:
    """One sighting of one user under one name."""

    user_id: int
    name: str
    at: Optional[int]
    source: str

    def as_json(self) -> dict[str, Any]:
        return {
            "userId": str(self.user_id),
            "name": self.name,
            "at": to_iso(self.at),
            "source": self.source,
        }


def _records_in(payload: object, at: Optional[int]) -> list[NameRecord]:
    """Pull every ``{userId, nickName}`` pair out of one decoded feed body."""
    if not isinstance(payload, Mapping):
        return []
    found: list[NameRecord] = []
    for key in _MEMBER_KEYS:
        entry = payload.get(key)
        candidates = [entry] if isinstance(entry, Mapping) else entry
        if not isinstance(candidates, (list, tuple)):
            continue
        for member in candidates:
            if not isinstance(member, Mapping):
                continue
            user_id, nickname = member.get("userId"), member.get("nickName")
            if isinstance(user_id, int) and isinstance(nickname, str) and nickname:
                found.append(NameRecord(int(user_id), nickname, at, FEED))
    return found


def from_feeds(rows: Iterable[tuple[Optional[int], str]]) -> list[NameRecord]:
    """Read every name the client wrote into one room's log.

    Takes the rows rather than a connection because the table they come from
    is the reader's business, not this module's: one schema keeps a log per
    room and another keeps one table for every room, and neither changes what
    a feed body means once it is in hand.

    A body that is not JSON, or is JSON of an unexpected shape, is skipped
    rather than raising: these rows are the client's own bookkeeping and their
    format is observed, not documented.
    """
    found: list[NameRecord] = []
    for sent_at, body in rows:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            continue
        found.extend(_records_in(payload, None if sent_at is None else int(sent_at)))
    return found


def ledger_path() -> Path:
    """Where observed names are remembered, outside the repository."""
    return paths.state_directory() / "names.jsonl"


class NameLedger:
    """An append-only record of the names the databases showed over time.

    One line per sighting keeps appending cheap and keeps a truncated write
    from destroying the entries before it; a line that will not parse is
    dropped, because a damaged tail must not make the whole history unreadable.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path if path is not None else ledger_path()

    def records(self) -> list[NameRecord]:
        """Return every sighting written so far, oldest first."""
        try:
            text = self.path.read_text("utf-8")
        except (OSError, ValueError):
            return []
        found: list[NameRecord] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                user_id = int(entry["userId"])
                name = str(entry["name"])
                at = entry.get("at")
            except (TypeError, ValueError, KeyError):
                continue
            if name:
                found.append(
                    NameRecord(user_id, name, None if at is None else int(at), OBSERVED)
                )
        return found

    def observe(self, names: Mapping[int, str], at: Optional[int] = None) -> int:
        """Append the names that differ from this user's last recorded one.

        Returns how many sightings were written.  Nothing is written when
        nothing changed, so the common case costs one read and no write, and a
        failure to write costs a lost observation rather than a failed read.
        """
        latest: dict[int, str] = {}
        for record in self.records():
            latest[record.user_id] = record.name
        moment = int(time.time()) if at is None else int(at)
        fresh = [
            NameRecord(user_id, name, moment, OBSERVED)
            for user_id, name in sorted(names.items())
            if name and latest.get(user_id) != name
        ]
        if not fresh:
            return 0
        lines = "".join(
            json.dumps(
                {"userId": str(record.user_id), "name": record.name, "at": record.at},
                ensure_ascii=False,
            )
            + "\n"
            for record in fresh
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(lines)
        except OSError:
            return 0
        return len(fresh)


def timeline(records: Iterable[NameRecord]) -> dict[int, list[NameRecord]]:
    """Group sightings per user in time order, collapsing repeats.

    A user seen under the same name a hundred times is one step in their
    history, not a hundred; only a change is worth a line.  Records with no
    timestamp sort first, since the only way they arise is a log row the client
    left without one.
    """
    grouped: dict[int, list[NameRecord]] = {}
    for record in records:
        grouped.setdefault(record.user_id, []).append(record)
    history: dict[int, list[NameRecord]] = {}
    for user_id, sightings in grouped.items():
        sightings.sort(key=lambda record: (record.at is not None, record.at or 0))
        steps: list[NameRecord] = []
        for sighting in sightings:
            if not steps or steps[-1].name != sighting.name:
                steps.append(sighting)
        history[user_id] = steps
    return history


def steps_with_current(
    user_id: int, steps: Iterable[NameRecord], current: Optional[str]
) -> list[NameRecord]:
    """Close a user's history with the name they answer to now.

    The recorded history routinely stops short of the present, because the
    client logs a name at a join and not at a rename.  Showing the live name as
    the final step is what makes a history end where the reader is standing,
    and it is marked :data:`CURRENT` so it is never mistaken for a sighting
    that carries a time.
    """
    closed = list(steps)
    if current and (not closed or closed[-1].name != current):
        closed.append(NameRecord(user_id, current, None, CURRENT))
    return closed


def carriers(history: Mapping[int, list[NameRecord]], name: str) -> list[int]:
    """Return every user id that has ever appeared under *name*.

    More than one answer is the interesting answer: it means two accounts wore
    the same name, which is what impersonation looks like from the outside.
    """
    needle = name.casefold().strip()
    return sorted(
        user_id
        for user_id, steps in history.items()
        if any(step.name.casefold() == needle for step in steps)
    )
