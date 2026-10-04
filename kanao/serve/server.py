"""Binding the socket and serving until interrupted."""

from __future__ import annotations

import sys
import time
from http.server import ThreadingHTTPServer
from typing import Any, Callable, Optional

from .config import (
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    MAX_STREAMS,
)
from .handler import build_handler

_ORDINARY = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
    TimeoutError,
)


class QuietDisconnects:
    """Treat a client leaving as an event, not as a server error.

    ``socketserver`` prints a full traceback for anything raised out of a
    request, and a reader closing ``curl -N`` raises ``ConnectionResetError``
    on the next write. That is the *normal* end of an SSE connection, so the
    stock behaviour means the one long-lived route in this server cannot be
    used without filling the log with tracebacks for its own success case.
    Anything else still gets the stock treatment, because an unexpected
    exception on a socket is worth seeing.
    """

    def handle_error(self, request, client_address) -> None:  # noqa: D102 - stdlib name
        if isinstance(sys.exc_info()[1], _ORDINARY):
            return
        super().handle_error(request, client_address)  # type: ignore[misc]


class ServeHTTPServer(QuietDisconnects, ThreadingHTTPServer):
    """The server the command actually runs, and the one the tests drive.

    Threaded because ``GET /stream`` holds its connection for as long as the
    caller stays: on a single-threaded server one SSE reader is the whole
    server, and nothing else could be answered until they left.
    """

    daemon_threads = True


def run(
    host: str,
    port: int,
    send: Callable[..., dict[str, Any]],
    *,
    token: str,
    min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    rooms_provider: Optional[Callable[[], list[Any]]] = None,
    context_provider: Optional[Callable[..., dict[str, Any]]] = None,
    digest_provider: Optional[Callable[..., dict[str, Any]]] = None,
    watcher_factory: Optional[Callable[[], Any]] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    max_streams: int = MAX_STREAMS,
    room_opener: Optional[Callable[[int], None]] = None,
    notice_provider: Optional[Callable[[int], dict[str, Any]]] = None,
    attachments_provider: Optional[Callable[..., dict[str, Any]]] = None,
    revisions_provider: Optional[Callable[[int, int], dict[str, Any]]] = None,
    file_provider: Optional[Callable[[int, int], Any]] = None,
) -> int:
    """Serve until interrupted; return 130 on a clean Ctrl-C like the CLI."""
    handler = build_handler(
        send,
        token=token,
        min_interval=min_interval,
        clock=clock,
        rooms_provider=rooms_provider,
        context_provider=context_provider,
        digest_provider=digest_provider,
        watcher_factory=watcher_factory,
        poll_interval=poll_interval,
        request_timeout=request_timeout,
        max_streams=max_streams,
        room_opener=room_opener,
        notice_provider=notice_provider,
        attachments_provider=attachments_provider,
        revisions_provider=revisions_provider,
        file_provider=file_provider,
    )
    server = ServeHTTPServer((host, port), handler)
    print(f"kanao serve listening on {host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Request threads are daemons, so Ctrl-C would otherwise kill a send
        # that was accepted and still waiting its turn without ever answering
        # it. Queueing sends instead of refusing them is what creates that
        # state; closing the queue is what keeps it from being a silent drop.
        handler._send_queue.close()  # type: ignore[attr-defined]
        return 130
    finally:
        server.server_close()
    return 0
