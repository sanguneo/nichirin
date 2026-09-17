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
from typing import Any, Callable, Optional


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
) -> None:
    """Stream what the watcher finds until the caller leaves.

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
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    try:
        try:
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
        if not emit(handler, "ready", {"ok": True}):
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
            for message in fresh:
                payload = (
                    message.as_json() if hasattr(message, "as_json") else dict(message)
                )
                if not emit(handler, "message", {"ok": True, "message": payload}):
                    return
            if not emit(handler, "ping", {"ok": True}):
                return
            time.sleep(poll_interval)
    finally:
        close = getattr(watcher, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
