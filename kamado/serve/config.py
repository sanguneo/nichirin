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

_STATUS_BY_CODE = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.AMBIGUOUS_CHAT: 409,
    ErrorCode.CHAT_NOT_FOUND: 404,
    ErrorCode.WINDOW_NOT_FOUND: 404,
    ErrorCode.KEY_UNAVAILABLE: 503,
    ErrorCode.KAKAO_NOT_RUNNING: 503,
    ErrorCode.BUSY: 503,
}


def _status_for(error: KamadoError) -> int:
    if error.code is ErrorCode.UNAUTHORIZED:
        return 401
    if error.code is ErrorCode.RATE_LIMITED:
        return 429
    return _STATUS_BY_CODE.get(error.code, 500)


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
