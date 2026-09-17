"""Binding the socket and serving until interrupted."""

from __future__ import annotations

import time
from http.server import ThreadingHTTPServer
from typing import Any, Callable, Optional

from .config import DEFAULT_MIN_INTERVAL_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS
from .handler import build_handler


def run(
    host: str,
    port: int,
    send: Callable[..., dict[str, Any]],
    *,
    token: str,
    min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    rooms_provider: Optional[Callable[[], list[Any]]] = None,
    context_provider: Optional[Callable[[int, int], dict[str, Any]]] = None,
    digest_provider: Optional[Callable[[int, str, int, int], dict[str, Any]]] = None,
    watcher_factory: Optional[Callable[[], Any]] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
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
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    print(f"kanao serve listening on {host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0