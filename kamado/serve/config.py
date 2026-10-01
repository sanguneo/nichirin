"""Constants, request validation, and the error-to-status map.

Nothing here opens a socket or touches a database: these are the values every
other module in the package agrees on, and the checks a caller's input has to
survive before any of it is trusted.
"""

from __future__ import annotations

import math
import re

from ..errors import ErrorCode, KamadoError

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

Closing a socket the caller is still writing to resets the connection, and the
reset destroys the answer the caller has not read yet: measured on macOS 15,
``ConnectionResetError`` on every try when the body landed after the close,
whatever its size, and from 32 KB up when it had landed before. So after a
refusal the server says it is done writing, reads whatever is still coming
until the caller closes, and gives up after this long.
"""

LINGER_BYTES = MAX_BODY_BYTES
"""The most of a refused body that is read and thrown away."""

MAX_STREAMS = 8
"""How many SSE connections may be open at once.

Each one owns a reader and a watcher for its whole life, which is the most
expensive thing a caller can ask this server to hold. Refusing the ninth is
how the eighth keeps working.
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

MAX_QUEUED_SENDS = 4
"""How many sends may be in line at once, counting the one being made.

Somebody typing two short lines in a row is the normal shape of a
conversation, not abuse, so a send that arrives during another one waits its
turn instead of being thrown away. But a queue that never says no is only a
slower way to dump a thousand messages into someone's chat, so the line has an
end: past this, refusing is the honest answer and the caller is told when a
slot opens. Four keeps the worst wait to a few sends' worth of pacing, which
fits inside an ordinary client timeout.
"""

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
    ErrorCode.INPUT_NOT_FOUND: 404,
    ErrorCode.KEY_UNAVAILABLE: 503,
    ErrorCode.KAKAO_NOT_RUNNING: 503,
    ErrorCode.PERMISSION_REQUIRED: 503,
    ErrorCode.NO_USER_DATA: 503,
    ErrorCode.BUSY: 503,
}
"""The macOS-only codes are here because this table came from the Windows
branch, which has no `PERMISSION_REQUIRED`, `NO_USER_DATA` or
`INPUT_NOT_FOUND` — so all three left as a 500. `PERMISSION_REQUIRED` is the
one that mattered: it is the failure a user fixes in thirty seconds by
granting Accessibility or Full Disk Access, and calling it an internal server
error tells a remote caller to give up. It joins the other two 503s, which are
also 'the host cannot answer yet, and somebody could make it'.
`INPUT_NOT_FOUND` sits with `WINDOW_NOT_FOUND` because it is the same kind of
absence one level further in."""


def _status_for(error: KamadoError) -> int:
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
            raise KamadoError(
                ErrorCode.INVALID_REQUEST, "since must be given as roomId:logId"
            )
        room_id = validate_room_id(room)
        try:
            log_id = int(log)
        except (TypeError, ValueError):
            raise KamadoError(
                ErrorCode.INVALID_REQUEST, "since must be given as roomId:logId"
            )
        if not 0 <= log_id <= _SQLITE_INT_MAX:
            raise KamadoError(
                ErrorCode.INVALID_REQUEST, "the log id in since is out of range"
            )
        cursor[str(room_id)] = str(log_id)
    return cursor


def validate_request_timeout(value: object) -> float:
    """Accept a positive finite number of seconds, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KamadoError(ErrorCode.INVALID_REQUEST, "request-timeout must be a number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST, "request-timeout must be a positive number"
        )
    return seconds


def validate_max_streams(value: object) -> int:
    """Accept a positive count of concurrent SSE connections, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise KamadoError(ErrorCode.INVALID_REQUEST, "max-streams must be an integer")
    if value < 1:
        raise KamadoError(ErrorCode.INVALID_REQUEST, "max-streams must be at least 1")
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
        raise KamadoError(ErrorCode.INVALID_REQUEST, "a numeric roomId is required")
    if not _SQLITE_INT_MIN <= room_id <= _SQLITE_INT_MAX:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST, "roomId is outside the range an id can have"
        )
    return room_id


def validate_message_id(raw: object, *, name: str = "messageId") -> int:
    """Accept a message id the way :func:`validate_room_id` accepts a room's."""
    try:
        log_id = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise KamadoError(ErrorCode.INVALID_REQUEST, f"a numeric {name} is required")
    if not 0 <= log_id <= _SQLITE_INT_MAX:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST, f"{name} is outside the range an id can have"
        )
    return log_id


def validate_count(raw: object, *, name: str, maximum: int) -> int:
    """Accept a positive integer no larger than *maximum*, else raise."""
    try:
        count = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise KamadoError(ErrorCode.INVALID_REQUEST, f"{name} must be an integer")
    if count < 1 or count > maximum:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST,
            f"{name} must be between 1 and {maximum}",
        )
    return count


def validate_flag(raw: object, *, name: str) -> bool:
    """Accept ``true``/``false`` (and ``1``/``0``), else raise.

    ``open`` is in the shared contract because the Windows build needs it: a
    room's key there is in memory only once the client has opened it. Every
    room is readable here, so the flag opens nothing — but a value that is
    neither true nor false is refused the same way on both, or one caller
    would get two answers for one typo.
    """
    text = str(raw).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0", ""):
        return False
    raise KamadoError(ErrorCode.INVALID_REQUEST, f"{name} must be true or false")


def validate_token(token: object) -> str:
    """Accept an ASCII bearer secret and return it, else raise INVALID_REQUEST."""
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST,
            f"a bearer token of at least {MIN_TOKEN_LENGTH} characters is required",
        )
    if not _TOKEN_RE.fullmatch(token):
        raise KamadoError(
            ErrorCode.INVALID_REQUEST,
            "the bearer token must be ASCII letters, digits, or -._~+/=",
        )
    return token


def validate_min_interval(value: object) -> float:
    """Accept 0 (disabled) or a positive finite interval, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KamadoError(ErrorCode.INVALID_REQUEST, "min-interval must be a number")
    interval = float(value)
    if interval == 0.0:
        return 0.0
    if not math.isfinite(interval) or interval <= 0:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST, "min-interval must be 0 or a positive number"
        )
    return interval


def validate_poll_interval(value: object) -> float:
    """Accept a positive finite poll interval, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KamadoError(ErrorCode.INVALID_REQUEST, "poll-interval must be a number")
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST, "poll-interval must be a positive number"
        )
    return interval
