"""Bucket a conversation into time slices and describe each one.

This is extractive, not generative: there is no model here, so nothing is
invented. A slice reports how busy it was, who carried it, which words it was
actually about, and the lines most likely to carry the point. That is honest
about what a reader is getting, and it is also exactly the material to hand to
a model when one is available.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Iterable, Optional, Sequence

from kakaocommon.words import PLACEHOLDERS, STOPWORDS, WORD
from ..model import Message

MAX_SPAN: Final = timedelta(hours=12)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mh])\s*$", re.IGNORECASE)
# Shared with the topic panel, so the two never disagree about which words
# count; the private names are what this module has always used.
_WORD = WORD
_STOPWORDS: Final = STOPWORDS
_PLACEHOLDERS: Final = PLACEHOLDERS


class DurationError(ValueError):
    """The requested slice length could not be used."""


def parse_duration(text: str) -> timedelta:
    """Read ``30m`` or ``2h`` into a slice length, refusing unusable values."""
    match = _DURATION.match(text)
    if not match:
        raise DurationError(f"{text!r} is not a duration; use a value like 30m or 2h")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    span = timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount)
    if span <= timedelta(0):
        raise DurationError("a slice must be longer than zero")
    if span > MAX_SPAN:
        raise DurationError(f"a slice cannot exceed {int(MAX_SPAN.total_seconds() // 3600)}h")
    return span


@dataclass(frozen=True)
class Slice:
    """One time slice of a conversation."""

    start: datetime
    end: datetime
    messages: list[Message]

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def speakers(self) -> list[tuple[str, int]]:
        """Participants ordered by how much of the slice they carried."""
        counts = Counter(message.sender for message in self.messages)
        return counts.most_common()

    @property
    def keywords(self) -> list[tuple[str, int]]:
        """Words the slice was actually about, with fillers removed."""
        counts: Counter[str] = Counter()
        for message in self.messages:
            for word in _WORD.findall(message.text or ""):
                lowered = word.lower()
                if (
                    lowered not in _STOPWORDS
                    and word not in _PLACEHOLDERS
                    and not lowered.isdigit()
                ):
                    counts[word] += 1
        return [pair for pair in counts.most_common(8) if pair[1] > 1] or counts.most_common(5)

    def highlights(self, limit: int = 3) -> list[Message]:
        """The lines most likely to carry the point of the slice.

        Longer messages are preferred because a substantive statement is
        usually longer than the reactions around it, and the result is put
        back in time order so it still reads as a conversation.
        """
        with_text = [message for message in self.messages if (message.text or "").strip()]
        ranked = sorted(with_text, key=lambda message: len(message.text or ""), reverse=True)
        return sorted(ranked[:limit], key=lambda message: message.id)


def _instant(message: Message) -> Optional[datetime]:
    if message.sent_at is None:
        return None
    return datetime.fromtimestamp(message.sent_at, timezone.utc)


def slice_messages(
    messages: Sequence[Message],
    span: timedelta,
    max_span: timedelta = MAX_SPAN,
) -> list[Slice]:
    """Group *messages* into consecutive slices of *span*, newest window first.

    Only the most recent *max_span* of conversation is considered, so asking
    for thirty-minute slices of a year-long room returns the recent day rather
    than thousands of empty windows.
    """
    timed = [(instant, message) for message in messages if (instant := _instant(message))]
    if not timed:
        return []

    timed.sort(key=lambda pair: (pair[0], pair[1].id))
    newest = timed[-1][0]
    horizon = newest - max_span
    timed = [pair for pair in timed if pair[0] >= horizon]
    if not timed:
        return []

    oldest = timed[0][0]
    seconds = span.total_seconds()
    # Anchor slice boundaries to the newest message so the last slice is whole.
    total = int((newest - oldest).total_seconds() // seconds) + 1
    buckets: dict[int, list[Message]] = {}
    for instant, message in timed:
        index = int((newest - instant).total_seconds() // seconds)
        buckets.setdefault(min(index, total - 1), []).append(message)

    slices: list[Slice] = []
    for index in sorted(buckets, reverse=True):
        end = newest - timedelta(seconds=seconds * index)
        slices.append(Slice(start=end - span, end=end, messages=buckets[index]))
    return slices


def as_json(slices: Iterable[Slice], highlights: int = 3) -> list[dict[str, object]]:
    """Render slices as data, for a caller that will reason over them.

    Instants are explicit UTC and every identifier is a string, as everywhere
    else on the boundary. What a slice carries is the same as what the screen
    shows of it — how busy, who, which words, which lines — and nothing is
    invented: a highlight is a message that was actually sent, id and all.
    """
    return [
        {
            "from": piece.start.isoformat(),
            "to": piece.end.isoformat(),
            "count": piece.count,
            "speakers": [
                {"name": name, "count": count} for name, count in piece.speakers
            ],
            "keywords": [
                {"word": word, "count": count} for word, count in piece.keywords
            ],
            "highlights": [
                message.as_json() for message in piece.highlights(highlights)
            ],
        }
        for piece in slices
    ]


def as_text(slices: Iterable[Slice], zone: timezone) -> str:
    """Render slices as plain text, for handing to a model or to a file."""
    lines: list[str] = []
    for piece in slices:
        start = piece.start.astimezone(zone)
        end = piece.end.astimezone(zone)
        lines.append(f"## {start:%Y-%m-%d %H:%M} – {end:%H:%M}  ({piece.count} messages)")
        for message in piece.messages:
            instant = _instant(message)
            stamp = instant.astimezone(zone).strftime("%H:%M") if instant else "--:--"
            body = (message.text or "").replace("\n", " ")
            lines.append(f"{stamp} {message.sender}: {body}")
        lines.append("")
    return "\n".join(lines)
