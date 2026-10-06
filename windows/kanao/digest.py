"""The digest itself, so both the command and the server ask the same thing.

The slicing lives in :mod:`kanao.tui.digest` because that is where the screen's
``/sum`` needs it. What was missing is the step above it — resolve a room, read
its newest messages, cut them into spans, and say what came out — which existed
only inside ``cli._digest``. The HTTP server wanted exactly that, and reaching
into the CLI for it meant building an ``argparse.Namespace`` to satisfy a
function that never wanted one, and pointing the dependency backwards into the
layer that is supposed to sit on top.

Extractive, not generative: there is no model in this process, so nothing here
is invented. What comes back is the material a model would want — how busy each
window was, who carried it, which words it was about, and the lines most likely
to carry the point — rather than a page of raw messages to chew through.
"""

from __future__ import annotations

from typing import Any, Union

from .errors import ErrorCode, KanaoError
from .reader import Reader
from .tui import digest as slicing

DEFAULT_SPAN = "30m"
DEFAULT_LIMIT = 400
"""How many of the newest messages a digest slices, matching the screen's /sum."""

DEFAULT_HIGHLIGHTS = 3

NOTE = (
    "Extractive, not generative: counts, speakers, keywords and "
    "verbatim highlights from the newest messages only. Nothing here is a "
    "summary; write one from it."
)


def build(
    reader: Reader,
    target: Union[int, str],
    *,
    span: str = DEFAULT_SPAN,
    limit: int = DEFAULT_LIMIT,
    highlights: int = DEFAULT_HIGHLIGHTS,
) -> dict[str, Any]:
    """Return the digest payload for one room, without the envelope.

    *target* is a room id or a title, resolved the way every other command
    resolves one. A span the slicer cannot read is an ``INVALID_REQUEST``
    rather than a traceback, because it is the caller's word that was wrong.
    """
    from .context import _resolve

    try:
        window = slicing.parse_duration(span)
    except slicing.DurationError as error:
        raise KanaoError(ErrorCode.INVALID_REQUEST, str(error)) from error
    room = _resolve(reader, target)
    found = reader.messages(room.id, limit=limit)
    slices = slicing.slice_messages(found, window)
    return {
        "room": room.as_json(),
        "span": span,
        "considered": len(found),
        "slices": slicing.as_json(slices, highlights),
        "note": NOTE,
    }
