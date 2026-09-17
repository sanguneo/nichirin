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

from ..model import Message

MAX_SPAN: Final = timedelta(hours=12)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mh])\s*$", re.IGNORECASE)
_WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")

# Particles and fillers carry no topic, and in Korean they dominate any raw
# frequency count, so they are removed before ranking.
_STOPWORDS: Final = frozenset(
    """
    그리고 그래서 그런데 하지만 그러면 그러니까 근데 진짜 정말 아니 우리 저기 여기 거기
    이거 그거 저거 뭐가 뭔가 이런 저런 그런 하는 하고 해서 하면 한데 있는 있다 없는 없다
    같은 같이 때문 지금 오늘 내일 어제 그냥 조금 많이 다시 아직 이제 계속 혹시 일단
    the and for you are not but with this that have from was were will can all
    네네 ㅇㅇ ㅋㅋ ㅋㅋㅋ ㅎㅎ ㅎㅎㅎ ㅠㅠ ㅜㅜ
    """.split()
)

# The client writes a placeholder word into the text column for attachments, so
# without this a photo-heavy hour reports "사진" as what it was about.
_PLACEHOLDERS: Final = frozenset(
    "사진 동영상 이모티콘 파일 음성메시지 보이스톡 페이스톡 선물 링크 삭제된 지도 연락처".split()
)


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
