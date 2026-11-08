"""Opt-in, allowlisted send diagnostics. No user content crosses this boundary."""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, final


Stage = Literal[
    "attempt", "validation", "catalog", "discovery", "input", "write", "submit", "confirm",
    "ax_button", "focused_return", "focus_guard", "button_guard", "forward", "daemon",
    "queue", "execution", "queue_full", "shutdown", "log",
]
Outcome = Literal["pass", "rejected", "unknown", "error"]
Route = Literal["cli", "daemon", "http"]
_STAGES = frozenset(Stage.__args__)
_CODES = frozenset((
    "KAKAO_NOT_RUNNING", "NO_USER_DATA", "KEY_UNAVAILABLE", "CHAT_NOT_FOUND",
    "MESSAGE_NOT_FOUND", "FILE_EXPIRED", "AMBIGUOUS_CHAT", "BUSY", "READ_FAILED",
    "INVALID_REQUEST", "WINDOW_NOT_FOUND", "INPUT_NOT_FOUND", "SEND_FAILED",
    "UNAUTHORIZED", "RATE_LIMITED", "PERMISSION_REQUIRED", "UNEXPECTED",
))
current_attempt: contextvars.ContextVar[Attempt | None] = contextvars.ContextVar("send_trace", default=None)


@dataclass(frozen=True)
class Identity:
    """Trusted installed build identity, shared by every event in an attempt."""

    platform: Literal["windows", "macos"]
    build: Literal["kanao", "kamado"]
    version: str


class TraceDestinationError(OSError):
    """The requested trace could not be opened before any UI mutation."""

    def __init__(self) -> None:
        super().__init__("trace destination is not writable; no message was submitted")


@final
class Attempt:
    """Mutable per-attempt accumulator; its descriptor is closed by its owner."""

    def __init__(self, fd: int, identity: Identity, route: Route) -> None:
        self.fd = fd
        self.identity = identity
        self.route = route
        self.id = uuid.uuid4().hex
        self.origin = time.monotonic()
        self.path: Path | None = None
        self.lock = threading.Lock()
        self.enabled = True
        self.outcome: Outcome = "pass"
        self.code: str | None = None
        self.targets: dict[str, str] = {}
        self.aliases: dict[tuple[str, str], str] = {}
        self.conditions = dict.fromkeys(("E", "D", "U", "W", "S", "L", "B"), "not_checked")

    def emit(self, name: Stage, event: Literal["start", "end"], started: float) -> bool:
        """One bounded JSON write; failure disables logging, never submission."""
        now = started if event == "start" else time.monotonic()
        with self.lock:
            if not self.enabled:
                return False
            row = {
                "schema": 1, "attempt_id": self.id,
                "platform": self.identity.platform, "build": self.identity.build,
                "version": self.identity.version, "route": self.route,
                "http_route": "POST /send" if self.route == "http" else None,
                "stage": name, "event": event,
                "start_ms": max(0.0, (started - self.origin) * 1000),
                "end_ms": max(0.0, (now - self.origin) * 1000),
                "elapsed_ms": max(0.0, (now - started) * 1000),
                "outcome": self.outcome, "code": self.code,
                "conditions": dict(self.conditions), **self.targets,
            }
            payload = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                if os.write(self.fd, payload) != len(payload):
                    self.enabled = False
            except OSError:
                self.enabled = False
            return self.enabled

    def failure(self, error: BaseException, name: Stage) -> None:
        """Read only known error codes and disposition, never exception text."""
        if self.outcome != "pass":
            return
        code = getattr(error, "code", None)
        self.code = code if code in _CODES else ("SEND_FAILED" if isinstance(error, OSError) else "UNEXPECTED")
        disposition: Mapping[str, str] | None = getattr(error, "disposition", None)
        if isinstance(disposition, dict) and disposition.get("delivery") == "not_delivered":
            self.outcome = "rejected"
        elif name in ("catalog", "discovery", "input", "write", "focus_guard", "button_guard"):
            self.outcome = "rejected"
        elif name in ("submit", "confirm", "forward", "ax_button", "focused_return"):
            self.outcome = "unknown"
        else:
            self.outcome = "error" if self.route == "cli" else "unknown"


def validate_directory(directory: str | None) -> None:
    """Probe an explicitly requested host destination before work begins."""
    if directory is None:
        return
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / (uuid.uuid4().hex + ".probe")
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        probe.unlink()
    except (OSError, ValueError):
        raise TraceDestinationError() from None


@contextmanager
def attempt(directory: str | None, identity: Identity, route: Route) -> Generator[Attempt | None, None, None]:
    """Own one exclusively created attempt file, or do nothing when disabled."""
    if current_attempt.get() is not None:
        yield current_attempt.get()
        return
    if directory is None:
        yield None
        return
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        identifier = uuid.uuid4().hex
        path = (Path(directory) / (identifier + ".jsonl")).absolute()
        fd = os.open(path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    except (OSError, ValueError):
        raise TraceDestinationError() from None
    value = Attempt(fd, identity, route)
    value.id = identifier
    value.path = path
    token = current_attempt.set(value)
    try:
        if not value.emit("attempt", "start", value.origin):
            raise TraceDestinationError()
        try:
            yield value
        except BaseException as error:
            # Diagnostic boundary: preserve and re-raise the original failure.
            value.failure(error, "attempt")
            raise
        finally:
            _ = value.emit("attempt", "end", value.origin)
    finally:
        current_attempt.reset(token)
        try:
            os.close(fd)
        except OSError:
            value.enabled = False


@contextmanager
def stage(name: Stage) -> Generator[None, None, None]:
    """Record a real boundary with monotonic timing and categorical failure."""
    value = current_attempt.get()
    if value is None:
        yield
        return
    if name not in _STAGES:
        raise ValueError("unknown trace stage")
    started = time.monotonic()
    _ = value.emit(name, "start", started)
    try:
        yield
    except BaseException as error:
        value.failure(error, name)
        raise
    finally:
        _ = value.emit(name, "end", started)


def refused(name: Stage, code: str) -> None:
    """Record a handled refusal without replacing an earlier failed boundary."""
    value = current_attempt.get()
    if value is not None and value.outcome == "pass":
        value.outcome = "rejected"
        value.code = code if code in _CODES else "UNEXPECTED"
        with stage(name):
            pass


def report_failure(error: Mapping[str, str | Mapping[str, str]]) -> None:
    """A daemon envelope is a boundary: copy only code and delivery category."""
    value = current_attempt.get()
    if value is not None:
        code = error.get("code")
        value.code = code if isinstance(code, str) and code in _CODES else "UNEXPECTED"
        disposition = error.get("disposition", {})
        value.outcome = (
            "rejected" if isinstance(disposition, dict) and disposition.get("delivery") == "not_delivered"
            else "unknown"
        )


def identify(kind: Literal["room", "window", "control"], identifier: str | int) -> None:
    """Assign an opaque alias valid only inside this attempt."""
    value = current_attempt.get()
    if value is not None:
        key = (kind, str(identifier))
        if key not in value.aliases:
            value.aliases[key] = f"{value.id[:8]}-{kind}-{len(value.aliases) + 1}"
        value.targets[kind] = value.aliases[key]


def observed(condition: Literal["E", "D", "U", "W", "S"]) -> None:
    """Mark only a predicate that the caller actually observed."""
    value = current_attempt.get()
    if value is not None:
        value.conditions[condition] = "pass"


def logged(found: bool) -> None:
    """L records local observation, never recipient delivery or send failure."""
    value = current_attempt.get()
    if value is not None:
        value.conditions["L"] = "pass" if found else "unknown"
