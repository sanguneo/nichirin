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
# The second alternative is an OSC 8 hyperlink's opener or its terminator.
# Both are zero-width, and a measurement that counts them shifts every column
# after a link by the length of its target.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)")
# A control character, or the picture :func:`clean` leaves in its place,
# ends an address: neither belongs in a target handed to the terminal.
_URL: Final = re.compile(r"https?://[^\s<>\"'()\[\]\x00-\x1f\x7f-\x9f␀-␿]+")
_WIDE: Final = frozenset({"W", "F"})

_CONTROL_PICTURE_BASE: Final = 0x2400
"""U+2400 onwards stand for C0 in order, so ESC is shown as U+241B."""
_DELETE_PICTURE: Final = "␡"
_REPLACEMENT: Final = "�"


def clean(text: str, keep_newlines: bool = False) -> str:
    """Make text somebody else wrote safe to put on the terminal.

    A message, a nickname or a room title is data, and every escape on the
    screen has to be one the renderer wrote. Measuring past an escape is not
    disarming it: ``truncate_to_width`` carries escapes through so colour
    survives a cut, which is exactly how a message holding ``ESC[2J`` would
    reach the terminal whole. So control characters are replaced before any
    styling is applied, with something that can be seen - C0 by its control
    picture, DEL by its own, C1 by the replacement character - rather than
    dropped, because a message that quietly lost characters would read as if
    it had said something else.

    One character in, one character out, so an index into the original is an
    index into the result. Tab and carriage return become spaces: they are
    zero columns to the ruler and a jump to the terminal. A newline is kept
    only where the caller lays out paragraphs itself.
    """
    if text.isprintable():
        return text
    cleaned: list[str] = []
    for character in text:
        code = ord(character)
        if character == "\n":
            cleaned.append("\n" if keep_newlines else " ")
        elif character in "\t\r":
            cleaned.append(" ")
        elif code < 0x20:
            cleaned.append(chr(_CONTROL_PICTURE_BASE + code))
        elif code == 0x7F:
            cleaned.append(_DELETE_PICTURE)
        elif 0x80 <= code <= 0x9F:
            cleaned.append(_REPLACEMENT)
        else:
            cleaned.append(character)
    return "".join(cleaned)


_JOINERS: Final = re.compile("[‍︎️\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f]")


def settle(text: str) -> str:
    """Drop the code points that make an emoji's width a guess.

    A joiner, a variation selector, a skin tone or a flag tag is one column
    or two to ``east_asian_width`` and none to a terminal that fuses the
    sequence - or two to one that does not - so a line holding them is drawn
    wider or narrower than it was measured. Where a column has to hold still
    (the rule beside the topic panel, the screen's right edge) the sequence
    is shown as its separate pictures instead, each as wide as it is measured.
    """
    return _JOINERS.sub("", text)


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


def _hyperlink(url: str, text: str) -> str:
    """Underlined *text* that a terminal will open *url* for when clicked.

    The target sits inside an OSC sequence, where one ESC or BEL would end it
    early and hand the rest to the terminal as instructions. The pattern that
    finds addresses already refuses them; a target that got here anyway is
    shown unlinked rather than trusted.
    """
    if not url.isprintable():
        return text
    return f"\x1b]8;;{url}\x1b\\\x1b[4m{text}\x1b[24m\x1b]8;;\x1b\\"


def wrap_with_links(text: str, width: int) -> list[str]:
    """Wrap *text*, making every URL clickable on each line it reaches.

    A URL is routinely wider than the body, so it is split, and each piece has
    to carry the *whole* target or clicking the tail of a link opens something
    that does not exist. Each line therefore opens and closes its own link.

    The pieces are matched back to the source character by character rather
    than by counting: wrapping drops the spaces it breaks on and rebuilds the
    rest, so length arithmetic drifts by however many it dropped and binds the
    target to the wrong letters — which looks right until a message has two
    spaces in it.
    """
    lines = wrap_to_width(text, width)
    spans = [(match.start(), match.end(), match.group()) for match in _URL.finditer(text)]
    if not spans:
        return lines

    target: list[Optional[str]] = [None] * len(text)
    for start, end, url in spans:
        target[start:end] = [url] * (end - start)

    linked: list[str] = []
    cursor = 0
    for line in lines:
        pieces: list[str] = []
        run = ""
        run_url: Optional[str] = None
        for character in line:
            while cursor < len(text) and text[cursor] != character:
                cursor += 1  # A space the wrapper broke on and did not keep.
            url = target[cursor] if cursor < len(text) else None
            cursor += 1
            if run and url != run_url:
                pieces.append(_hyperlink(run_url, run) if run_url else run)
                run = ""
            run_url = url
            run += character
        if run:
            pieces.append(_hyperlink(run_url, run) if run_url else run)
        linked.append("".join(pieces))
    return linked


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


TAB_SEPARATOR: Final = " │ "
"""What goes between two tabs, when the strip holds more than one."""

MIN_TAB_COLUMNS: Final = 8
"""The narrowest a tab's label may be before the strip shows fewer of them.

Four Hangul characters: as short as a name can be and still name something.
"""

MAX_TAB_COLUMNS: Final = 24
"""The widest a tab's label may grow.

A room named after a sentence must not push every other tab off the strip,
and past this a longer title is no easier to recognise than a cut one.
"""


def _tab_badge(unseen: int) -> str:
    """What a tab adds to its label while arrivals are waiting in it."""
    return f" +{unseen}" if unseen else ""


def _tab_marker(hidden: int) -> int:
    """Columns the count of tabs off one edge takes, or none."""
    return 0 if hidden <= 0 else display_width(f"{ELLIPSIS}{hidden}") + 1


def _cut_tab(title: str, unseen: int, budget: int) -> tuple[str, int]:
    """Cut a title to *budget* columns with room left for its badge.

    The badge is part of the tab: a count pushing the title past its columns
    would run into the next tab instead of shortening this one.
    """
    badge = _tab_badge(unseen)
    label = truncate_to_width(title, max(0, budget - display_width(badge)), ELLIPSIS)
    return label, display_width(label) + display_width(badge)


@dataclass(frozen=True)
class TabEntry:
    """One tab as the strip draws it."""

    index: int
    """Which conversation, counted in the order they were opened."""
    label: str
    """The title, cut to the columns it was given."""
    columns: int
    """How many columns the tab takes, badge included."""
    unseen: int
    """Arrivals waiting in it, drawn after the label."""


@dataclass(frozen=True)
class TabStrip:
    """What the strip shows, and what it had to leave out."""

    entries: list[TabEntry]
    hidden_left: int
    """Tabs off the left edge, counted in a marker rather than drawn."""
    hidden_right: int
    """Tabs off the right edge, likewise."""
    hidden_unseen: int
    """Arrivals held by tabs that are not drawn, so the marker can say so."""


def plan_tabs(tabs: Sequence[tuple[str, int]], active: int, width: int) -> TabStrip:
    """Lay out the tab strip: every title as wide as it earns, cut where needed.

    Three rules, in order. Everything that fits at its own width is drawn
    whole. Failing that, the focused tab keeps its width and the rest share
    what is left, down to a floor. Failing that, the strip becomes a window
    around the focused tab and the others are counted at its edges - a strip
    that cannot show the tab you are on is worse than one admitting it holds
    more, because the tab you are on is the one title that must stay readable.

    *tabs* is ``(title, unseen)`` per conversation. The titles are expected to
    be display text already; nothing here cleans them.
    """
    count = len(tabs)
    if count == 0:
        return TabStrip([], 0, 0, 0)
    active = max(0, min(active, count - 1))
    natural = [
        min(display_width(title) + display_width(_tab_badge(unseen)), MAX_TAB_COLUMNS)
        for title, unseen in tabs
    ]
    separator = display_width(TAB_SEPARATOR)

    def entries(indices: Iterable[int], budgets: dict[int, int]) -> list[TabEntry]:
        built = []
        for index in indices:
            title, unseen = tabs[index]
            label, columns = _cut_tab(title, unseen, budgets[index])
            built.append(TabEntry(index=index, label=label, columns=columns, unseen=unseen))
        return built

    if sum(natural) + separator * (count - 1) <= width:
        return TabStrip(entries(range(count), dict(enumerate(natural))), 0, 0, 0)

    if count > 1:
        share = (width - natural[active] - separator * (count - 1)) // (count - 1)
        if share >= MIN_TAB_COLUMNS:
            budgets = {
                index: natural[index] if index == active else min(natural[index], share)
                for index in range(count)
            }
            return TabStrip(entries(range(count), budgets), 0, 0, 0)

    def window(size: int) -> tuple[int, int]:
        """Where a window of *size* tabs starts, and the columns it costs.

        The others are costed at what they would actually be drawn at, not at
        the floor they may be cut to: costing a strip of short names at the
        floor hides tabs there was room to show.
        """
        start = max(0, min(active - (size - 1) // 2, count - size))
        others = sum(
            min(natural[index], MIN_TAB_COLUMNS)
            for index in range(start, start + size)
            if index != active
        )
        cost = (
            natural[active]
            + others
            + separator * (size - 1)
            + _tab_marker(start)
            + _tab_marker(count - start - size)
        )
        return start, cost

    size = count
    while size > 1 and window(size)[1] > width:
        size -= 1
    start, _cost = window(size)
    shown = range(start, start + size)
    budgets = {
        index: natural[active] if index == active else min(natural[index], MIN_TAB_COLUMNS)
        for index in shown
    }
    hidden_unseen = sum(
        unseen for index, (_title, unseen) in enumerate(tabs) if index not in shown
    )
    return TabStrip(entries(shown, budgets), start, count - start - size, hidden_unseen)
