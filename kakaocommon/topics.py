"""Cut the last two hours of a conversation into the topics it moved through.

This is extractive for the same reason `/sum` is: there is no model in this
process, so a topic is never named by anything but words its messages really
used, and never described by anything but messages somebody really sent.

The cut is TextTiling adapted to a chat. Every gap between two consecutive
messages is scored by how long nobody spoke, by how little the vocabulary on
one side shares with the other, and by whether somebody on the far side was
answering somebody on the near side. A silence of `SILENCE` is a boundary
outright; a reply across a gap is the strongest evidence there is that it is
not one, because the client recorded it rather than this module guessing.

A topic that was already running when the window opened is followed back to
where it actually started, up to `TOPIC_EXTENSION_MAX` earlier. The window is
two hours because that is what the reader asked about; cutting a conversation
at that instant would hand them the second half of an argument. The cut is
scored over the wider range exactly as it is everywhere else, so nothing is
forced at the window's edge — it only decides which topics are kept. A reply
pulls a topic's start back only the way it holds any gap together: from
within `REPLY_REACH`. An answer to something hours old is a topic coming back,
and following it all the way would swallow everything said in between.

Everything here is a pure function of the messages and the clock it is
handed, so the panel and the tests see the same answer for the same input.

Both builds run this file unchanged. A message is anything shaped like
`FlowMessage` -- the fields of `kanao.model.Message` and `kamado.model.Message`
this reads, which carry the same names on both -- so nothing here imports
either package.
"""

from __future__ import annotations

import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final, Mapping, Optional, Protocol, Sequence, Union

from .words import PLACEHOLDERS as _PLACEHOLDERS
from .words import STOPWORDS as _STOPWORDS
from .words import WORD as _WORD

FEED_MESSAGE: Final = 0
"""The client's type code for a feed row; `model.FEED_MESSAGE` in both builds."""
ELLIPSIS: Final = "…"
"""What a cut label ends with; `layout.ELLIPSIS` in both builds."""


class FlowQuote(Protocol):
    """The one thing a cut reads of a reply: which message it answered."""

    @property
    def message_id(self) -> Optional[int]: ...


class FlowMessage(Protocol):
    """What a cut reads of a message, and nothing more.

    Structural, so either build's frozen `Message` dataclass is one as it
    stands. `covered`, `photo` and `video` are only ever compared with None.
    """

    @property
    def id(self) -> int: ...
    @property
    def sent_at(self) -> Optional[int]: ...
    @property
    def text(self) -> str: ...
    @property
    def sender(self) -> str: ...
    @property
    def from_me(self) -> bool: ...
    @property
    def type(self) -> int: ...
    @property
    def erased(self) -> bool: ...
    @property
    def covered(self) -> Optional[str]: ...
    @property
    def photo(self) -> Optional[object]: ...
    @property
    def video(self) -> Optional[object]: ...
    @property
    def reply(self) -> Optional[FlowQuote]: ...
    @property
    def mentions(self) -> Sequence[object]: ...
    @property
    def mentions_all(self) -> bool: ...
    @property
    def mentions_me(self) -> bool: ...


TOPIC_WINDOW: Final = timedelta(hours=2)
"""How far back the panel looks: the "최근 2시간" the reader asked for."""
TOPIC_EXTENSION_MAX: Final = timedelta(hours=2)
"""How far before the window a topic already running at its start is followed.

A hard cap: a room that never fell silent would otherwise be followed back to
its first message, and the panel is about the last two hours, not the day.
"""
SILENCE: Final = timedelta(minutes=15)
"""A pause this long ends a topic whatever the words on either side say."""
COHESION_SPAN: Final = 6
"""How many messages each side of a gap contributes to the vocabulary compared.

Chat lines are short — two or three content words — so one message per side
compares noise; six is roughly a turn each for a three-way exchange.
"""
REPLY_REACH: Final = 12
"""How many messages back a reply may reach and still hold a topic together.

A reply to something an hour ago is a topic coming back, not one topic
spanning everything in between; that is what `Topic.resumes` reports instead.
"""
BOUNDARY_SCORE: Final = 0.45
"""The combined depth and pause a gap needs before it is called a boundary."""
TIME_WEIGHT: Final = 0.35
"""What a pause just short of `SILENCE` adds to a gap's score."""
SAME_TOPIC: Final = 0.6
"""Neighbours whose vocabularies are at least this alike are one topic."""
MIN_TOPIC_MESSAGES: Final = 4
MAX_TOPICS: Final = 8
KEYWORDS_SHOWN: Final = 5
LABEL_WORDS: Final = 3
LABEL_COLUMNS: Final = 24
"""How wide a label made from a message, rather than from keywords, may be."""

# Particles that are almost never the last syllable of a noun, so they are
# taken off whenever two syllables remain: 회의는, 회의를 and 회의 are one word.
_CLEAR_JOSA: Final = ("이랑", "에서", "으로", "한테", "까지", "부터", "은", "는", "을", "를")
# Particles that also end ordinary nouns — 고양이, 결과, 포도 — so they are
# taken off only when the stem is attested on its own in the same stretch of
# conversation, or under two different particles.
_AMBIGUOUS_JOSA: Final = ("이", "가", "에", "의", "도", "로", "와", "과", "랑", "만")
# Polite and connective endings of a verb or adjective. 있어요, 좋아요 and
# 했습니다 fill a chat and say nothing about its subject; a noun of three
# syllables or more almost never ends this way, while 필요 and 수요 are two.
_PREDICATE_ENDINGS: Final = ("요", "니다", "는데", "지만")
# Who is speaking, with its particle already attached: in a chat these are as
# common as the particles `words.STOPWORDS` removes, and as empty of topic.
_PRONOUNS: Final = frozenset("저도 저는 제가 저희 나도 나는 내가 너도 너는 네가 다들".split())


@dataclass(frozen=True)
class Topic:
    """One contiguous stretch of a conversation that was about one thing."""

    start: datetime
    end: datetime
    messages: tuple[FlowMessage, ...]
    keywords: tuple[str, ...]
    """The words that set this topic apart from the others in the window."""
    label: str
    resumes: Optional[int] = None
    """Index of an earlier topic in the same flow that this one picks up again.

    Group chats interleave: a question from an hour ago comes back after two
    other things were discussed, and following the flow means seeing that.
    """
    extends_before: bool = False
    """Whether this topic started before the window and was followed back."""
    truncated: bool = False
    """Whether following it back stopped at the cap rather than at its start."""
    replies: Mapping[int, int] = field(default_factory=dict, repr=False, compare=False)
    """How many messages in the flow answered each message here, by id."""

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def speakers(self) -> list[tuple[str, int]]:
        """Who carried the topic, most first; the reader is "나" as on screen."""
        counts = Counter("나" if one.from_me else one.sender for one in self.messages)
        return counts.most_common()

    def key_messages(self, limit: int = 5) -> list[FlowMessage]:
        """The messages most likely to carry the point, in the order they were sent.

        `digest.Slice.highlights` ranks by length alone, so a wall of text
        always wins and a short decisive line never does. Here a line earns its
        place by naming what the topic was about, by being answered, and by
        addressing somebody; length is a tie-breaker capped at one keyword's
        worth, so it cannot outweigh content. Ties go to the earlier message,
        so the same conversation always yields the same lines.
        """
        wanted = set(self.keywords)
        scored: list[tuple[float, int, FlowMessage]] = []
        for message in self.messages:
            if not _scorable(message) or not message.text.strip():
                continue
            # Keywords are normalised stems, so 회의는 counts for 회의.
            tokens = _raw_terms(message.text)
            coverage = sum(1 for word in wanted if any(token.startswith(word) for token in tokens))
            answered = min(3, self.replies.get(message.id, 0))
            addressing = 1.0 if message.mentions_me else 0.5 if message.mentions or message.mentions_all else 0.0
            length = min(1.0, len(message.text.strip()) / 60)
            score = coverage + 1.5 * answered + addressing + length
            scored.append((score, message.id, message))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return sorted((entry[2] for entry in scored[:limit]), key=lambda message: message.id)


@dataclass(frozen=True)
class TopicFlow:
    """The topics of one window, oldest first, and the window they belong to."""

    start: datetime
    end: datetime
    """The nominal window: two hours ending now, or ending at the last message."""
    topics: list[Topic]
    count: int
    """How many messages inside the nominal window the topics were cut from."""
    fallback: bool = False
    """Whether nothing was said in the last two hours, so the window ends at
    the newest message instead and the panel has to say so."""


def _instant(message: FlowMessage) -> Optional[datetime]:
    if message.sent_at is None:
        return None
    return datetime.fromtimestamp(message.sent_at, timezone.utc)


def _as_instant(now: Union[datetime, float, int]) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(now, timezone.utc)


def part_of_flow(message: FlowMessage) -> bool:
    """Whether a message belongs to a topic at all.

    A message taken back was unsaid, and a feed row is the client's own
    bookkeeping; neither is part of what anybody talked about. A photo is: it
    is counted, it just contributes no words.
    """
    return message.sent_at is not None and not message.erased and message.type != FEED_MESSAGE


def _scorable(message: FlowMessage) -> bool:
    """Whether a message's text may say what a topic is about.

    A covered message's text is withheld unless revealed, and even revealed it
    is something SafeBot judged should not be read at a glance. An attachment's
    text is the client's placeholder (`사진`), not anything typed.
    """
    return (
        part_of_flow(message)
        and message.covered is None
        and message.photo is None
        and message.video is None
    )


def _raw_terms(text: str) -> list[str]:
    """Content words as written, fillers and placeholders gone, not yet normalised."""
    found: list[str] = []
    for word in _WORD.findall(text or ""):
        lowered = word.lower()
        if lowered in _STOPWORDS or lowered in _PRONOUNS or word in _PLACEHOLDERS or lowered.isdigit():
            continue
        if len(lowered) >= 3 and lowered.endswith(_PREDICATE_ENDINGS):
            continue
        found.append(lowered)
    return found


def _strip(token: str, particles: Sequence[str]) -> Optional[str]:
    for particle in particles:
        if token.endswith(particle) and len(token) - len(particle) >= 2:
            return token[: -len(particle)]
    return None


class _Normaliser:
    """Folds a word's particle-bearing forms into one, using the window as evidence.

    Korean attaches particles to the noun, so 회의는, 회의를 and 회의 would
    otherwise be three different words, and none of them would reach the
    count a keyword needs. Taking every trailing 이 off would turn 고양이 into
    고양, so the ambiguous particles are taken off only when the conversation
    itself shows the stem standing alone or under another particle.
    """

    def __init__(self, texts: Sequence[str]) -> None:
        self.known: set[str] = set()
        forms: dict[str, set[str]] = defaultdict(set)
        for text in texts:
            for token in _raw_terms(text):
                clear = _strip(token, _CLEAR_JOSA)
                self.known.add(clear or token)
                stem = _strip(token, _AMBIGUOUS_JOSA)
                if clear is None and stem is not None:
                    forms[stem].add(token)
        self.shared = {stem for stem, seen in forms.items() if len(seen) >= 2}

    def __call__(self, token: str) -> Optional[str]:
        clear = _strip(token, _CLEAR_JOSA)
        if clear is not None:
            token = clear
        else:
            stem = _strip(token, _AMBIGUOUS_JOSA)
            if stem is not None and (stem in self.known or stem in self.shared):
                token = stem
        if token in _STOPWORDS or token in _PLACEHOLDERS:
            return None
        return token


def _cosine(left: Counter[str], right: Counter[str]) -> Optional[float]:
    """How alike two vocabularies are, or None when either side has no words."""
    if not left or not right:
        return None
    dot = sum(count * right[word] for word, count in left.items() if word in right)
    norm = math.sqrt(sum(v * v for v in left.values())) * math.sqrt(sum(v * v for v in right.values()))
    return dot / norm if norm else None


def _gap_scores(
    messages: Sequence[FlowMessage], terms: Sequence[Counter[str]]
) -> tuple[list[float], list[bool]]:
    """Score every gap between consecutive messages, and say which are silences.

    Gap *i* sits between message *i* and *i + 1*. The lexical part is
    TextTiling's depth score: how far the similarity at this gap falls below
    the peaks on either side, so a gradual drift is not mistaken for a turn.
    A reply reaching across a gap from within `REPLY_REACH` clears both parts,
    silence included — somebody answering after twenty minutes is continuing.
    """
    count = len(messages)
    gaps = max(0, count - 1)
    similarity: list[Optional[float]] = []
    for gap in range(gaps):
        left: Counter[str] = Counter()
        for one in terms[max(0, gap - COHESION_SPAN + 1) : gap + 1]:
            left.update(one)
        right: Counter[str] = Counter()
        for one in terms[gap + 1 : gap + 1 + COHESION_SPAN]:
            right.update(one)
        similarity.append(_cosine(left, right))

    depth = [0.0] * gaps
    for gap, value in enumerate(similarity):
        if value is None:
            continue
        peak_left = value
        index = gap - 1
        while index >= 0 and similarity[index] is not None and similarity[index] >= peak_left:
            peak_left = similarity[index]  # type: ignore[assignment]
            index -= 1
        peak_right = value
        index = gap + 1
        while index < gaps and similarity[index] is not None and similarity[index] >= peak_right:
            peak_right = similarity[index]  # type: ignore[assignment]
            index += 1
        depth[gap] = (peak_left - value) + (peak_right - value)

    bridged = [False] * gaps
    position = {message.id: index for index, message in enumerate(messages)}
    for index, message in enumerate(messages):
        target = message.reply.message_id if message.reply is not None else None
        origin = position.get(target) if target is not None else None
        if origin is None or origin >= index or index - origin > REPLY_REACH:
            continue
        for gap in range(origin, index):
            bridged[gap] = True

    scores: list[float] = []
    silences: list[bool] = []
    for gap in range(gaps):
        if bridged[gap]:
            scores.append(0.0)
            silences.append(False)
            continue
        pause = messages[gap + 1].sent_at - messages[gap].sent_at  # type: ignore[operator]
        silent = pause >= SILENCE.total_seconds()
        silences.append(silent)
        scores.append(depth[gap] + TIME_WEIGHT * min(1.0, max(0.0, pause) / SILENCE.total_seconds()))
    return scores, silences


def _boundaries(scores: Sequence[float], silences: Sequence[bool]) -> list[int]:
    """Pick the gaps that end a topic: every silence, and lexical turns that stand out.

    A lexical boundary must also be the strongest gap within half a minimum
    topic either side, or one turn of subject spread over three gaps would
    cut three slivers out of the conversation.
    """
    reach = MIN_TOPIC_MESSAGES // 2
    chosen: list[int] = []
    for gap, score in enumerate(scores):
        if silences[gap]:
            chosen.append(gap)
            continue
        if score < BOUNDARY_SCORE:
            continue
        nearby = scores[max(0, gap - reach) : gap + reach + 1]
        # The earliest of equal peaks wins, so a tie never cuts twice.
        if score == max(nearby) and scores.index(score, max(0, gap - reach)) == gap:
            chosen.append(gap)
    return chosen


@dataclass
class _Span:
    """A run of message indexes [first, last] while topics are being decided."""

    first: int
    last: int


def _vector(span: _Span, terms: Sequence[Counter[str]]) -> Counter[str]:
    total: Counter[str] = Counter()
    for one in terms[span.first : span.last + 1]:
        total.update(one)
    return total


def _merge_small(spans: list[_Span], scores: Sequence[float], silences: Sequence[bool]) -> list[_Span]:
    """Fold any topic shorter than `MIN_TOPIC_MESSAGES` into a neighbour.

    It joins across the weaker of its two boundaries, so a scrap after a long
    silence goes back to what it followed only if what follows is further
    away still. A silence counts as stronger than any lexical score.
    """

    def strength(gap: int) -> float:
        return math.inf if silences[gap] else scores[gap]

    while len(spans) > 1:
        small = next(
            (index for index, span in enumerate(spans) if span.last - span.first + 1 < MIN_TOPIC_MESSAGES),
            None,
        )
        if small is None:
            break
        span = spans[small]
        before = strength(span.first - 1) if small > 0 else None
        after = strength(span.last) if small < len(spans) - 1 else None
        if after is None or (before is not None and before <= after):
            spans[small - 1].last = span.last
        else:
            spans[small + 1].first = span.first
        del spans[small]
    return spans


def _merge_alike(
    spans: list[_Span], terms: Sequence[Counter[str]], silences: Sequence[bool]
) -> list[_Span]:
    """Join neighbours that turned out to be about the same thing.

    Not across a silence: the same words after a long pause are a topic coming
    back, and the panel says so with `resumes` rather than hiding the pause.
    """
    index = 0
    while index < len(spans) - 1:
        left, right = spans[index], spans[index + 1]
        alike = _cosine(_vector(left, terms), _vector(right, terms))
        if not silences[left.last] and alike is not None and alike >= SAME_TOPIC:
            left.last = right.last
            del spans[index + 1]
            continue
        index += 1
    return spans


def _merge_to_cap(spans: list[_Span], terms: Sequence[Counter[str]]) -> list[_Span]:
    """Join the most alike neighbours until no more than `MAX_TOPICS` remain.

    Eight entries is about what a side panel shows without scrolling; a
    two-hour window cut finer than that reads as a list of messages again.
    """
    while len(spans) > MAX_TOPICS:
        best = 0
        best_score = -1.0
        for index in range(len(spans) - 1):
            alike = _cosine(_vector(spans[index], terms), _vector(spans[index + 1], terms)) or 0.0
            if alike > best_score:
                best, best_score = index, alike
        spans[best].last = spans[best + 1].last
        del spans[best + 1]
    return spans


def _keywords(vectors: Sequence[Counter[str]]) -> list[tuple[str, ...]]:
    """The words each topic used that the other topics of the window did not.

    TF-IDF over the topics of one window: a word everybody used all evening
    says nothing about any one stretch of it. A word must be used at least
    in two of its topic's messages, or a stray noun would label a stretch it
    barely touched.
    """
    total = len(vectors)
    spread: Counter[str] = Counter()
    for vector in vectors:
        spread.update(vector.keys())
    result: list[tuple[str, ...]] = []
    for vector in vectors:
        ranked = sorted(
            ((count * (math.log((1 + total) / (1 + spread[word])) + 1.0), count, word)
             for word, count in vector.items() if count >= 2),
            key=lambda entry: (-entry[0], -entry[1], entry[2]),
        )
        result.append(tuple(word for _score, _count, word in ranked[:KEYWORDS_SHOWN]))
    return result


def _label(keywords: Sequence[str], messages: Sequence[FlowMessage]) -> str:
    """Name a topic by its keywords, or by its first line when it has none."""
    if keywords:
        return " · ".join(keywords[:LABEL_WORDS])
    for message in messages:
        if _scorable(message) and message.text.strip():
            said = " ".join(message.text.split())
            return _shorten(said, LABEL_COLUMNS)
    return "사진·파일"


def _columns(character: str) -> int:
    """Columns one character takes, by the rule both builds' `layout.char_width` use."""
    if unicodedata.combining(character) or character in ("\x00", "\n", "\r"):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def _shorten(text: str, columns: int) -> str:
    """Cut *text* to *columns* terminal columns, marking the cut with `ELLIPSIS`.

    A label is plain text — the panel cleans and styles it afterwards — so
    unlike `layout.truncate_to_width` there are no escapes to carry through.
    """
    if sum(_columns(character) for character in text) <= columns:
        return text
    budget = max(0, columns - _columns(ELLIPSIS))
    kept: list[str] = []
    used = 0
    for character in text:
        step = _columns(character)
        if used + step > budget:
            break
        kept.append(character)
        used += step
    return "".join(kept) + ELLIPSIS


def window_of(
    messages: Sequence[FlowMessage],
    now: Union[datetime, float, int],
    window: timedelta = TOPIC_WINDOW,
) -> Optional[tuple[datetime, datetime, bool]]:
    """The nominal window for *messages*: its start, its end, and whether it fell back.

    Two hours ending *now*; or, when nothing was said in them, two hours
    ending at the newest message, so a quiet room still shows where it was
    left rather than an empty panel. None when there is nothing to show.
    """
    instants = [instant for message in messages if part_of_flow(message) and (instant := _instant(message))]
    if not instants:
        return None
    end = _as_instant(now)
    start = end - window
    if any(instant >= start for instant in instants):
        return start, end, False
    end = max(instants)
    return end - window, end, True


def lookback_start(
    messages: Sequence[FlowMessage],
    now: Union[datetime, float, int],
    window: timedelta = TOPIC_WINDOW,
    extension: timedelta = TOPIC_EXTENSION_MAX,
) -> Optional[datetime]:
    """How far back history must be loaded for a flow to be cut honestly.

    The window plus the extension; the caller should load one message past
    it, so the gap before the oldest candidate can be scored too.
    """
    found = window_of(messages, now, window)
    if found is None:
        return None
    return found[0] - extension


def topic_flow(
    messages: Sequence[FlowMessage],
    now: Union[datetime, float, int],
    complete: bool = True,
    window: timedelta = TOPIC_WINDOW,
    extension: timedelta = TOPIC_EXTENSION_MAX,
) -> TopicFlow:
    """Cut the window ending at *now* into topics, oldest first.

    *complete* says whether *messages* reach back as far as the conversation
    does, or hold a message that is `part_of_flow` from before
    `lookback_start`. When they do not — the caller stopped paging at its own
    limit — a topic reaching the oldest message given cannot claim to have
    found its start, and is marked truncated. A feed or deleted row past the
    cap is no evidence either way, which is why only countable rows count.
    """
    found = window_of(messages, now, window)
    if found is None:
        moment = _as_instant(now)
        return TopicFlow(start=moment - window, end=moment, topics=[], count=0)
    start, end, fallback = found
    horizon = start - extension

    ordered = sorted(
        (message for message in messages if part_of_flow(message)),
        key=lambda message: (message.sent_at, message.id),
    )
    earlier = [message for message in ordered if _instant(message) < horizon]  # type: ignore[operator]
    candidates = [message for message in ordered if _instant(message) >= horizon]  # type: ignore[operator]
    in_window = sum(1 for message in candidates if _instant(message) >= start)  # type: ignore[operator]

    normalise = _Normaliser([message.text for message in candidates if _scorable(message)])
    # A word counts once per message: how many lines used it, not how often
    # one line repeated it, or a single pasted wall would name its topic.
    terms: list[Counter[str]] = []
    for message in candidates:
        words: set[str] = set()
        if _scorable(message):
            for token in _raw_terms(message.text):
                word = normalise(token)
                if word is not None:
                    words.add(word)
        terms.append(Counter(words))

    scores, silences = _gap_scores(candidates, terms)
    cuts = _boundaries(scores, silences)
    spans: list[_Span] = []
    first = 0
    for gap in cuts:
        spans.append(_Span(first, gap))
        first = gap + 1
    if candidates:
        spans.append(_Span(first, len(candidates) - 1))

    # Topics that ended before the window are not what the reader asked for;
    # the one running across its start is, from wherever it really began —
    # including anything it answered within reach, since `_gap_scores`
    # already bridged those gaps.
    spans = [span for span in spans if _instant(candidates[span.last]) >= start]  # type: ignore[operator]
    spans = _merge_small(spans, scores, silences)
    spans = _merge_alike(spans, terms, silences)
    spans = _merge_to_cap(spans, terms)

    position = {message.id: index for index, message in enumerate(candidates)}
    answered: Counter[int] = Counter()
    for message in candidates:
        target = message.reply.message_id if message.reply is not None else None
        if target is not None and target in position:
            answered[target] += 1

    vectors = [_vector(span, terms) for span in spans]
    keywords = _keywords(vectors)
    topics: list[Topic] = []
    for index, span in enumerate(spans):
        members = tuple(candidates[span.first : span.last + 1])
        resumes = next(
            (
                earlier_index
                for earlier_index in range(index - 1, -1, -1)
                if len(set(keywords[index]) & set(keywords[earlier_index])) >= 2
            ),
            None,
        )
        opens = _instant(members[0])
        assert opens is not None
        reaches_back = index == 0 and span.first == 0
        if reaches_back and earlier:
            # The message before the cap was close enough to belong: the cap,
            # not a pause, is what ended the search for this topic's start.
            gap = members[0].sent_at - earlier[-1].sent_at  # type: ignore[operator]
            truncated = gap < SILENCE.total_seconds()
        else:
            truncated = reaches_back and not complete
        topics.append(
            Topic(
                start=opens,
                end=_instant(members[-1]),  # type: ignore[arg-type]
                messages=members,
                keywords=keywords[index],
                label=_label(keywords[index], members),
                resumes=resumes,
                extends_before=opens < start,
                truncated=truncated,
                replies={one.id: answered[one.id] for one in members if answered[one.id]},
            )
        )
    return TopicFlow(start=start, end=end, topics=topics, count=in_window, fallback=fallback)

