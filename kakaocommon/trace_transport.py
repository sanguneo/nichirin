"""Local daemon trace handoff. File paths are IPC control data, never events."""

from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import TypedDict

import kakaocommon.trace as trace


class Descriptor(TypedDict):
    """One caller-created file shared with its local, same-user daemon."""

    file: str
    attempt_id: str
    origin: float


def export() -> Descriptor | None:
    value = trace.current_attempt.get()
    if value is None or not value.enabled or value.path is None:
        return None
    return {"file": str(value.path), "attempt_id": value.id, "origin": value.origin}


@contextmanager
def attach(
    descriptor: Mapping[str, str | float] | None, identity: trace.Identity
) -> Generator[None, None, None]:
    """Append to an existing attempt, never create a second attempt file."""
    if descriptor is None:
        yield
        return
    identifier = descriptor.get("attempt_id")
    filename = descriptor.get("file")
    origin = descriptor.get("origin")
    if (
        not isinstance(identifier, str) or re.fullmatch("[a-f0-9]{32}", identifier) is None
        or not isinstance(filename, str) or not isinstance(origin, (int, float))
    ):
        raise trace.TraceDestinationError()
    path = Path(filename)
    if path.name != identifier + ".jsonl":
        raise trace.TraceDestinationError()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise trace.TraceDestinationError() from None
    value = trace.Attempt(fd, identity, "daemon")
    value.id = identifier
    value.path = path
    value.origin = origin
    token = trace.current_attempt.set(value)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise trace.TraceDestinationError()
        with trace.stage("daemon"):
            yield
    finally:
        trace.current_attempt.reset(token)
        try:
            os.close(fd)
        except OSError:
            value.enabled = False
