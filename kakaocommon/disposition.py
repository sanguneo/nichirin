"""Delivery and retry decisions shared by both JSON frontends (Python 3.9+)."""

from typing import Final, Literal, TypedDict


class Disposition(TypedDict, total=False):
    delivery: Literal["not_delivered", "unknown", "submitted"]
    retry: Literal["safe", "unsafe", "after_check"]


# Explicit entries make a new ErrorCode an intentional contract decision.
DEFAULTS: Final[dict[str, Disposition]] = {
    "KAKAO_NOT_RUNNING": {"retry": "safe"},
    "NO_USER_DATA": {"retry": "safe"},
    "PERMISSION_REQUIRED": {"retry": "safe"},
    "KEY_UNAVAILABLE": {"retry": "safe"},
    "CHAT_NOT_FOUND": {"retry": "safe"},
    "MESSAGE_NOT_FOUND": {"retry": "safe"},
    "FILE_EXPIRED": {"retry": "safe"},
    "AMBIGUOUS_CHAT": {"retry": "safe"},
    "BUSY": {"retry": "safe"},
    "READ_FAILED": {"retry": "safe"},
    "INVALID_REQUEST": {"retry": "safe"},
    "WINDOW_NOT_FOUND": {"delivery": "not_delivered", "retry": "safe"},
    "INPUT_NOT_FOUND": {"delivery": "not_delivered", "retry": "safe"},
    "SEND_FAILED": {"delivery": "unknown", "retry": "after_check"},
    "UNAUTHORIZED": {"delivery": "not_delivered", "retry": "safe"},
    "RATE_LIMITED": {"delivery": "not_delivered", "retry": "safe"},
}


def default_disposition(code: str) -> Disposition:
    """Return a fresh decision; callers cannot mutate another error's default."""
    return DEFAULTS[code].copy()


def write_disposition(disposition: Disposition) -> Disposition:
    """Complete an already-safe write refusal without upgrading uncertainty."""
    result = disposition.copy()
    if result.get("retry") == "safe":
        _ = result.setdefault("delivery", "not_delivered")
    return result


def submission_disposition(submitted: bool) -> Disposition:
    """Submission is S, not log confirmation L; never resend a submitted write."""
    if submitted:
        return {"delivery": "submitted", "retry": "unsafe"}
    return {"delivery": "not_delivered", "retry": "safe"}
