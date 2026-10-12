"""Constants, request validation, and the error-to-status map.

Nothing here opens a socket or touches a database: these are the values every
other module in the package agrees on, and the checks a caller's input has to
survive before any of it is trusted.
"""

from __future__ import annotations

import math
import re

from ..errors import ErrorCode, KanaoError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MIN_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
MAX_BODY_BYTES = 1024 * 1024
MIN_TOKEN_LENGTH = 16
_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-._~+/=]+$")

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
"""How long one request may take to arrive before its connection is given up.

Without this a client that opens a socket, promises a body in
``Content-Length`` and then says nothing holds a thread for as long as it
likes. Thirty seconds is far more than a request over a tunnel needs and far
less than forever.
"""

LINGER_SECONDS = 2.0
"""How long a body that was refused unread is read and thrown away before closing.

A body that lands on a socket already closed is answered with a reset, and on
Windows the reset destroys the answer the caller has not read yet: it saw
``WinError 10053`` where it was owed a 400. So after a refusal the server says
it is done writing, reads whatever is still coming until the caller closes,
and gives up after this long.
"""

LINGER_BYTES = MAX_BODY_BYTES
"""The most of a refused body that is read and thrown away."""

MAX_STREAMS = 8
"""How many SSE connections may be open at once.

Each one owns a reader and a watcher for its whole life, which is the most
expensive thing a caller can ask this server to hold. Refusing the ninth is
how the eighth keeps working.
"""

MAX_QUEUED_SENDS = 4
"""How many sends may be in line at once, counting the one being sent.

What ``min_interval`` protects is a single input box, not a server resource,
and a caller refused for being early can only sleep and try again — which is
every client reimplementing a queue. So they wait their turn instead. A queue
with no end, though, would merely slow down pouring a thousand messages into
somebody's conversation, so ``429 RATE_LIMITED`` still exists and now means
*the line is full*.
"""

MAX_LIMIT = 1000
"""Most messages one request may ask a room for.

The local commands cap nothing because a person typing one is the cap. A
remote caller is not: ``limit`` reaches SQLite, where a negative number means
*no limit at all*, and one observed room holds 445,714 messages. Somewhere
above a thousand the answer stops being a window onto a conversation and
becomes a way to make this process read a whole history into memory.
"""

MAX_HIGHLIGHTS = 20
"""Most representative lines a digest slice may be asked for."""

MAX_CONCURRENT_READS = 4
"""Read requests answered at once. They share one snapshot and take turns on
it (``SharedReads``), so this bounds how many wait rather than how many
decrypt; streams are limited separately by ``MAX_STREAMS``."""

MAX_CONCURRENT_UPLOADS = 2
"""Upload bodies held at once. A body is kept whole until its file is handed
over, and the send queue bounds only how many wait for the client."""

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
"""Largest file ``POST /upload`` takes, and what ``/health`` declares.

The body is held in memory while it is parsed and then written once to the
folder the client reads it from, so this is a bound on this process rather than
on the client, which takes larger files from a person. Fifty megabytes covers a
phone's photos and short videos, which is what a remote send is for.
"""

MULTIPART_OVERHEAD_BYTES = 64 * 1024
"""What the parts around the file may add to an upload's body."""

UPLOAD_KEEP_SECONDS = 3600
"""How long a sent file's copy is kept for the client to finish reading it.

Pressing send hands the client a path, and it uploads from that path after the
confirmation has closed; deleting the copy then could cut the upload short.
Copies older than this are swept by the next upload.
"""

_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_MAX_NAME_LENGTH = 120


def upload_name(filename: object) -> str:
    """The name a sent file is written under, which is the name the room sees.

    The name is the caller's text, so it is reduced to one Windows file name:
    no folders, none of the characters Windows refuses, no device names, and
    a length the open-file dialog's box holds whole.
    """
    if not isinstance(filename, str):
        raise KanaoError(ErrorCode.INVALID_REQUEST, "the file part needs a file name")
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        "_" if character in '<>:"|?*' or ord(character) < 32 or ord(character) == 127 else character
        for character in base
    ).strip(" .")
    if not cleaned:
        raise KanaoError(ErrorCode.INVALID_REQUEST, "the file part needs a file name")
    stem, dot, extension = cleaned.rpartition(".")
    if (stem if dot else cleaned).upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned) > _MAX_NAME_LENGTH:
        if dot and 0 < len(extension) <= 16:
            cleaned = cleaned[: _MAX_NAME_LENGTH - len(extension) - 1].rstrip(" .") + "." + extension
        else:
            cleaned = cleaned[:_MAX_NAME_LENGTH].rstrip(" .")
    return cleaned

_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1

_STATUS_BY_CODE = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.AMBIGUOUS_CHAT: 409,
    ErrorCode.CHAT_NOT_FOUND: 404,
    ErrorCode.MESSAGE_NOT_FOUND: 404,
    # The file existed and its address no longer answers: gone, not missing.
    ErrorCode.FILE_EXPIRED: 410,
    ErrorCode.WINDOW_NOT_FOUND: 404,
    # The window was found and its input box was not: the caller asked for
    # something that is not there, which is the same answer as a missing
    # window rather than a fault in this server.
    ErrorCode.INPUT_NOT_FOUND: 404,
    ErrorCode.KEY_UNAVAILABLE: 503,
    ErrorCode.KAKAO_NOT_RUNNING: 503,
    # No signed-in profile is a machine that is not ready, exactly like a
    # client that is not running; 500 would say this server broke.
    ErrorCode.NO_USER_DATA: 503,
    ErrorCode.BUSY: 503,
}


def _status_for(error: KanaoError) -> int:
    if error.code is ErrorCode.UNAUTHORIZED:
        return 401
    if error.code is ErrorCode.RATE_LIMITED:
        return 429
    return _STATUS_BY_CODE.get(error.code, 500)


def validate_cursor(items: object) -> dict[str, str]:
    """Turn ``roomId:logId`` pairs into the map a stream resumes from.

    One id cannot stand for a whole account. A stream carries every room the
    caller follows, so the last message it saw says nothing about where the
    other rooms had reached — the cursor has to name each room it covers, and
    a room it does not name is one the server will start from now.
    """
    cursor: dict[str, str] = {}
    for item in items or ():  # type: ignore[union-attr]
        room, separator, log = str(item).partition(":")
        if not separator:
            raise KanaoError(
                ErrorCode.INVALID_REQUEST, "since must be given as roomId:logId"
            )
        room_id = validate_room_id(room)
        try:
            log_id = int(log)
        except (TypeError, ValueError):
            raise KanaoError(
                ErrorCode.INVALID_REQUEST, "since must be given as roomId:logId"
            )
        if not 0 <= log_id <= _SQLITE_INT_MAX:
            raise KanaoError(
                ErrorCode.INVALID_REQUEST, "the log id in since is out of range"
            )
        cursor[str(room_id)] = str(log_id)
    return cursor


def validate_request_timeout(value: object) -> float:
    """Accept a positive finite number of seconds, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KanaoError(ErrorCode.INVALID_REQUEST, "request-timeout must be a number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST, "request-timeout must be a positive number"
        )
    return seconds


def validate_max_streams(value: object) -> int:
    """Accept a positive count of concurrent SSE connections, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise KanaoError(ErrorCode.INVALID_REQUEST, "max-streams must be an integer")
    if value < 1:
        raise KanaoError(ErrorCode.INVALID_REQUEST, "max-streams must be at least 1")
    return value


def validate_room_id(raw: object) -> int:
    """Accept a room identifier SQLite could actually hold, else raise.

    Ids are carried as strings because they exceed 2^53, but they are still
    64-bit integers in the database. A larger one is not a room that will not
    be found — it is an ``OverflowError`` out of the driver, which would leave
    as a 500 for what is plainly a bad request.
    """
    try:
        room_id = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise KanaoError(ErrorCode.INVALID_REQUEST, "a numeric roomId is required")
    if not _SQLITE_INT_MIN <= room_id <= _SQLITE_INT_MAX:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST, "roomId is outside the range an id can have"
        )
    return room_id


def validate_message_id(raw: object, *, name: str = "messageId") -> int:
    """Accept a message id the way :func:`validate_room_id` accepts a room's."""
    try:
        log_id = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise KanaoError(ErrorCode.INVALID_REQUEST, f"a numeric {name} is required")
    if not 0 <= log_id <= _SQLITE_INT_MAX:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST, f"{name} is outside the range an id can have"
        )
    return log_id


def validate_count(raw: object, *, name: str, maximum: int) -> int:
    """Accept a positive integer no larger than *maximum*, else raise."""
    try:
        count = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise KanaoError(ErrorCode.INVALID_REQUEST, f"{name} must be an integer")
    if count < 1 or count > maximum:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST,
            f"{name} must be between 1 and {maximum}",
        )
    return count


def validate_flag(raw: object, *, name: str) -> bool:
    """Accept ``true``/``false`` (and ``1``/``0``), else raise.

    Spelled out rather than read as "present means yes", because the flag it
    carries today opens a room in the client and marks it read: a typo must be
    a 400, not a room opened by accident.
    """
    text = str(raw).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0", ""):
        return False
    raise KanaoError(ErrorCode.INVALID_REQUEST, f"{name} must be true or false")


def validate_token(token: object) -> str:
    """Accept an ASCII bearer secret and return it, else raise INVALID_REQUEST."""
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST,
            f"a bearer token of at least {MIN_TOKEN_LENGTH} characters is required",
        )
    if not _TOKEN_RE.fullmatch(token):
        raise KanaoError(
            ErrorCode.INVALID_REQUEST,
            "the bearer token must be ASCII letters, digits, or -._~+/=",
        )
    return token


def validate_min_interval(value: object) -> float:
    """Accept 0 (disabled) or a positive finite interval, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KanaoError(ErrorCode.INVALID_REQUEST, "min-interval must be a number")
    interval = float(value)
    if interval == 0.0:
        return 0.0
    if not math.isfinite(interval) or interval <= 0:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST, "min-interval must be 0 or a positive number"
        )
    return interval


def validate_poll_interval(value: object) -> float:
    """Accept a positive finite poll interval, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KanaoError(ErrorCode.INVALID_REQUEST, "poll-interval must be a number")
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST, "poll-interval must be a positive number"
        )
    return interval
