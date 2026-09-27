"""Server-Sent Events: the one route that outlives its own request.

Every other route answers and closes. ``GET /stream`` holds the socket for as
long as the caller stays, which makes it the only place in the server that owns
a watcher — and therefore the only place that has to give one back, whether the
caller disconnects, the poll fails, or the connection is torn down under it.
That lifetime is why this is a module rather than two more methods on the
handler: the request path should not have to reason about it.

The handler is passed in rather than subclassed. What is needed from it is
small and explicit — ``wfile``, the response headers, and its ``_reply`` for
the failures that happen before the stream has started — so the seam stays
readable from both sides.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional


from ..errors import ErrorCode, KanaoError, as_envelope


def emit(handler, kind: str, payload: dict[str, Any]) -> bool:
    """Write one SSE event, returning False once the client has gone.

    A disconnect is ordinary here — a reader closes ``curl`` — so it is a
    return value rather than an exception to be handled at every call site.
    """
    data = json.dumps(payload, ensure_ascii=False)
    try:
        handler.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def serve_stream(
    handler,
    *,
    watcher_factory: Optional[Callable[[], Any]],
    poll_interval: float,
    reply: Callable[[int, dict[str, Any]], None],
    cursor: Optional[Mapping[str, str]] = None,
) -> None:
    """Stream what the watcher finds until the caller leaves.

    *cursor* maps room id to the last message id that caller received. Without
    one the stream reports from the moment it connects; with one it pages each
    named room forward from there first, which is how a reconnection stops
    being a hole in what a follower saw.

    *reply* is used only before the event stream has begun; once the headers
    are out, a failure is an ``event: error`` because there is no status code
    left to send.
    """
    if watcher_factory is None:
        reply(
            500,
            as_envelope(
                KanaoError(
                    ErrorCode.READ_FAILED,
                    "no watcher is configured on this server",
                )
            ),
        )
        return
    try:
        watcher = watcher_factory()
    except Exception:
        reply(
            500,
            as_envelope(KanaoError(ErrorCode.READ_FAILED, "the watcher failed to start")),
        )
        return
    resume = getattr(watcher, "resume", None)
    if cursor and not callable(resume):
        # Refused rather than quietly downgraded to a fresh stream: a caller
        # that asked to be resumed and was silently started from now would
        # believe it had missed nothing.
        reply(
            500,
            as_envelope(
                KanaoError(
                    ErrorCode.READ_FAILED, "this server cannot resume a stream"
                )
            ),
        )
        _release(watcher)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    seen: dict[str, str] = dict(cursor or {})
    try:
        try:
            if cursor:
                resume(seen)  # type: ignore[misc] - checked above
            else:
                watcher.prime()
        except Exception:
            emit(
                handler,
                "error",
                as_envelope(
                    KanaoError(ErrorCode.READ_FAILED, "the watcher failed to prime")
                ),
            )
            return
        if not emit(
            handler, "ready", {"ok": True, "resumed": bool(cursor), "cursor": dict(seen)}
        ):
            return
        while True:
            try:
                fresh = watcher.poll()
            except Exception:
                emit(
                    handler,
                    "error",
                    as_envelope(
                        KanaoError(
                            ErrorCode.READ_FAILED, "the watcher stopped responding"
                        )
                    ),
                )
                return
            advanced = False
            for message in fresh:
                payload = (
                    message.as_json() if hasattr(message, "as_json") else dict(message)
                )
                if not emit(handler, "message", {"ok": True, "message": payload}):
                    return
                room = str(payload.get("roomId", ""))
                identifier = str(payload.get("id", ""))
                if room and identifier and _later(identifier, seen.get(room)):
                    # Compared as numbers: "9" sorts after "10" as text, and
                    # a cursor that moves backwards hands the caller repeats.
                    seen[room] = identifier
                    advanced = True
            # The cursor is published *after* the messages it covers, so a
            # client that stores it has already received everything it claims.
            if advanced and not emit(
                handler, "cursor", {"ok": True, "cursor": dict(seen)}
            ):
                return
            if not emit(handler, "ping", {"ok": True}):
                return
            time.sleep(poll_interval)
    finally:
        _release(watcher)


def _later(identifier: str, previous: Optional[str]) -> bool:
    """Whether *identifier* moves a room's cursor forward.

    The cursor only ever advances. A room's messages arrive in id order, so
    this is the ordinary case restated - but a published cursor is a promise
    that everything up to it has been delivered, and a batch that ever went
    backwards would quietly withdraw part of that promise.
    """
    if previous is None:
        return True
    try:
        return int(identifier) > int(previous)
    except ValueError:
        return identifier != previous


def _release(watcher: Any) -> None:
    """Give a watcher back, whatever it was that ended the stream."""
    close = getattr(watcher, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
