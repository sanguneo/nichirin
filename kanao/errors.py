"""One typed failure contract shared by every command.

The predecessor returned typed JSON errors for a single command and bare
stderr text for the rest, so an agent had to branch on which command it had
run.  Here every command emits the same envelope, and every failure carries a
machine-readable code an agent can act on without parsing prose.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    """Why a request could not be answered, from an agent's point of view."""

    KAKAO_NOT_RUNNING = "KAKAO_NOT_RUNNING"
    """KakaoTalk is not running, so no decryption key is in memory."""

    NO_USER_DATA = "NO_USER_DATA"
    """No signed-in KakaoTalk profile directory exists on this computer."""

    KEY_UNAVAILABLE = "KEY_UNAVAILABLE"
    """KakaoTalk is running but has not opened this database, so its key is absent."""

    CHAT_NOT_FOUND = "CHAT_NOT_FOUND"
    """No catalogued room matched the requested name or identifier."""

    AMBIGUOUS_CHAT = "AMBIGUOUS_CHAT"
    """Several rooms matched; candidates are returned instead of a guess."""

    BUSY = "BUSY"
    """A read raced a database write, or a send found an existing draft."""

    READ_FAILED = "READ_FAILED"
    """Decryption or query execution failed for another reason."""

    INVALID_REQUEST = "INVALID_REQUEST"
    """The request itself was not answerable, such as unusable SQL."""

    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    """No open KakaoTalk chat window matched the room, so there is nowhere to type."""

    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
    """The chat window was found but its message input control was not."""

    SEND_FAILED = "SEND_FAILED"
    """The message could not be handed to the client's input for another reason."""

    UNAUTHORIZED = "UNAUTHORIZED"
    """The remote caller did not present a valid bearer token."""

    RATE_LIMITED = "RATE_LIMITED"
    """The send rate limiter refused a request because another send ran too recently."""


class KanaoError(Exception):
    """A failure that already knows how it should be reported to an agent."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def envelope(self) -> dict[str, Any]:
        """Render this failure as the standard JSON response body."""
        error: dict[str, Any] = {"code": self.code.value, "message": self.message}
        return {"ok": False, "error": error, **self.details}


def success(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Render a successful response with the same envelope shape as failures."""
    return {"ok": True, **payload}


def as_envelope(error: BaseException) -> dict[str, Any]:
    """Render any exception as a response body, preserving known codes."""
    if isinstance(error, KanaoError):
        return error.envelope()
    return KanaoError(ErrorCode.READ_FAILED, str(error) or type(error).__name__).envelope()
