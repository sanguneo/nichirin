"""Width-aware text handling and the footer layout rule.

Everything here exists because a Korean conversation is mostly double-width
text wrapped in zero-width escape sequences, so ``len(text)`` is wrong twice
over. Every measurement goes through :func:`display_width`, which ignores
escapes and counts wide characters as the two columns they occupy.

The footer planner is a port of OmO's layout: the right-hand label is pinned,
the anchor stays visible, middle segments drop one at a time from the right,
and only then does the anchor lose its head to an ellipsis.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Iterable, Optional, Sequence

ELLIPSIS: Final = "…"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WIDE: Final = frozenset({"W", "F"})


def strip_ansi(text: str) -> str:
    """Return *text* without escape sequences."""
    return _ANSI.sub("", text)


def char_width(character: str) -> int:
    """Columns one character occupies, treating combining marks as zero."""
    if unicodedata.combining(character):
        return 0
    if character in ("\x00", "\n", "\r"):
        return 0
    return 2 if unicodedata.east_asian_width(character) in _WIDE else 1


def display_width(text: str) -> int:
    """Columns *text* occupies once escapes are ignored."""
    return sum(char_width(character) for character in strip_ansi(text))


def truncate_to_width(text: str, width: int, ellipsis: str = "") -> str:
    """Cut *text* to *width* columns, preserving escapes that are still open.

    Escape sequences are carried through rather than counted, so a truncated
    coloured string keeps its colour instead of bleeding into the next cell.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text

    marker_width = display_width(ellipsis)
    budget = max(0, width - marker_width)
    result: list[str] = []
    used = 0
    index = 0
    while index < len(text):
        match = _ANSI.match(text, index)
        if match:
            result.append(match.group())
            index = match.end()
            continue
        character = text[index]
        step = char_width(character)
        if used + step > budget:
            break
        result.append(character)
        used += step
        index += 1
    return "".join(result) + ellipsis


def pad_to_width(text: str, width: int, fill: str = " ") -> str:
    """Right-pad *text* with *fill* until it occupies *width* columns."""
    missing = width - display_width(text)
    return text + fill * missing if missing > 0 else text


def elide_head(text: str, max_width: int) -> str:
    """Drop the head of *text* so the identifying tail survives."""
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    if max_width == 1:
        return ELLIPSIS
    budget = max_width - display_width(ELLIPSIS)
    kept: list[str] = []
    used = 0
    for character in reversed(text):
        step = char_width(character)
        if used + step > budget:
            break
        kept.append(character)
        used += step
    return ELLIPSIS + "".join(reversed(kept))


def window_around(text: str, caret: int, width: int) -> tuple[str, int]:
    """Return the part of *text* that fits *width*, and the caret's column.

    A composer is narrower than the thing being written into it, so what is
    on screen has to follow the caret rather than the end of the text. The
    caret's own column is reserved at the right edge, or a caret at the very
    end would sit one column past the box.
    """
    if width <= 0:
        return "", 0
    caret = max(0, min(caret, len(text)))
    if display_width(text) <= width:
        return text, display_width(text[:caret])

    start = 0
    while start < caret and display_width(text[start:caret]) > width - 1:
        start += 1
    end = start
    used = 0
    while end < len(text):
        step = char_width(text[end])
        if used + step > width:
            break
        used += step
        end += 1
    return text[start:end], display_width(text[start:caret])


def wrap_to_width(text: str, width: int) -> list[str]:
    """Break *text* into lines of at most *width* columns, splitting words late.

    Korean runs without spaces, so wrapping cannot rely on whitespace alone; a
    word longer than the line is broken rather than allowed to overflow.
    """
    if width <= 0:
        return [""]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in _chunks(paragraph, width):
            candidate = f"{current} {word}" if current and not word.startswith(" ") else current + word
            if display_width(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word.lstrip() if display_width(word) <= width else ""
            if not current:
                remainder = word.lstrip()
                while display_width(remainder) > width:
                    head = truncate_to_width(remainder, width)
                    lines.append(head)
                    remainder = remainder[len(head):]
                current = remainder
        lines.append(current)
    return lines or [""]


def _chunks(paragraph: str, width: int) -> Iterable[str]:
    for piece in paragraph.split(" "):
        yield piece


@dataclass(frozen=True)
class Segment:
    """One footer item, measured plain and rendered coloured."""

    plain: str
    coloured: str


@dataclass(frozen=True)
class FooterPlan:
    """How much of the footer survived the available width."""

    kept_middle: int
    show_marker: bool
    anchor_plain: Optional[str] = None
    left_plain: Optional[str] = None
    right_plain: Optional[str] = None


def _segments_width(segments: Sequence[Segment], separator: str) -> int:
    if not segments:
        return 0
    return display_width(separator) * (len(segments) - 1) + sum(
        display_width(segment.plain) for segment in segments
    )


def plan_footer(
    anchor: Sequence[Segment],
    middle: Sequence[Segment],
    tail: Segment,
    right: Segment,
    width: int,
    separator: str = " • ",
    min_padding: int = 2,
) -> FooterPlan:
    """Choose the richest footer that fits, matching OmO's preference order.

    The anchor is the first segment and is the last thing to be sacrificed; the
    right-hand label is never dropped, only the left side gives way.
    """
    if not anchor:
        raise ValueError("a footer needs an anchor segment")
    head = anchor[0]
    rest = list(anchor[1:])

    def fits(left: Sequence[Segment]) -> bool:
        return _segments_width(left, separator) + min_padding + display_width(right.plain) <= width

    candidates = [(len(middle), False)]
    candidates += [(len(middle) - index - 1, True) for index in range(len(middle))]
    candidates.append((0, False))

    marker = Segment(ELLIPSIS, ELLIPSIS)
    for kept, show_marker in candidates:
        retained = list(middle[:kept])
        extra = [marker] if show_marker else []
        left = [*anchor, *retained, *extra, tail]
        if fits(left):
            return FooterPlan(kept_middle=kept, show_marker=show_marker)

    # Nothing fit whole, so shorten the anchor's head before giving up on it.
    for kept, show_marker in candidates:
        retained = list(middle[:kept])
        extra = [marker] if show_marker else []
        others = [*rest, *retained, *extra, tail]
        budget = (
            width
            - min_padding
            - display_width(right.plain)
            - display_width(separator)
            - _segments_width(others, separator)
        )
        if budget >= 2:
            return FooterPlan(
                kept_middle=kept, show_marker=show_marker, anchor_plain=elide_head(head.plain, budget)
            )

    left_budget = width - min_padding - display_width(right.plain)
    if left_budget >= 1:
        joined = separator.join(segment.plain for segment in [*anchor, tail])
        return FooterPlan(kept_middle=0, show_marker=False, left_plain=elide_head(joined, left_budget))
    return FooterPlan(
        kept_middle=0, show_marker=False, right_plain=truncate_to_width(right.plain, width)
    )
