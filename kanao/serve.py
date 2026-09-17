"""Remote access to the send + read + stream paths over HTTP.

Windows hosts this process next to the KakaoTalk client. A Linux or remote
machine connects with a bearer token and can then:

- ``POST /send`` — hand a message to the same ``winauto.Sender`` the ``send``
  command drives, with the two-seconds-between-sends rule enforced server-side.
- ``GET /rooms`` — list readable rooms in the same shape as the CLI command.
- ``GET /context?roomId=…`` — read one room's recent messages.
- ``GET /stream`` — Server-Sent Events carrying new messages as ``watch.Watcher``
  finds them. ``EventSource`` both in browsers and in curl consumes this, and
  the same channel works for a remote build that forwards chat into an LLM.

SSE, not WebRTC: this is a one-way push of small JSON payloads, not latency
sensitive media. WebRTC would add a signalling server, SDP exchange, and
STUN/TURN traversal for zero benefit here; SSE rides plain HTTP, passes
through any proxy a ``POST`` would, and needs no stdlib-external package.
``curl -N`` alone reads the stream, which is exactly the surface a remote
Linux caller has.

Security posture: a shared bearer token (``--token`` or ``KANAO_TOKEN``),
loopback binding by default, no TLS of its own. Reach it from another machine
over SSH port forwarding, a VPN, or a reverse proxy that terminates TLS —
never by binding ``0.0.0.0`` on an open network with a weak token.

Sends are serialized under one lock and spaced ``min_interval`` seconds apart,
because the client's input box is a single shared resource.
"""

from __future__ import annotations

import argparse
import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional, Type

from .errors import ErrorCode, KanaoError, as_envelope

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MIN_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
MAX_BODY_BYTES = 1024 * 1024

_STATUS_BY_CODE = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.AMBIGUOUS_CHAT: 409,
    ErrorCode.CHAT_NOT_FOUND: 404,
    ErrorCode.WINDOW_NOT_FOUND: 404,
    ErrorCode.KEY_UNAVAILABLE: 503,
    ErrorCode.KAKAO_NOT_RUNNING: 503,
    ErrorCode.BUSY: 503,
}


def _status_for(error: KanaoError) -> int:
    if error.code is ErrorCode.UNAUTHORIZED:
        return 401
    if error.code is ErrorCode.RATE_LIMITED:
        return 429
    return _STATUS_BY_CODE.get(error.code, 500)


class _RateLimiter:
    """Refuse sends that arrive sooner than *min_interval* after the last one."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    def check(self) -> Optional[float]:
        """Return seconds to wait when too soon, else record now and return None."""
        with self._lock:
            now = self._clock()
            if self._last is not None:
                wait = self._min_interval - (now - self._last)
                if wait > 0:
                    return wait
            self._last = now
            return None


def build_handler(
    send: Callable[..., dict[str, Any]],
    *,
    token: str,
    min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    rooms: Optional[list[dict[str, Any]]] = None,
    rooms_provider: Optional[Callable[[], list[Any]]] = None,
    context_provider: Optional[Callable[[int, int], dict[str, Any]]] = None,
    watcher_factory: Optional[Callable[[], Any]] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> Type[BaseHTTPRequestHandler]:
    """Build a request handler around injected backends.

    *send* takes ``(room, text, dry_run=..., open_if_needed=..., aliases=...,
    hint=...)`` and returns the sender envelope or raises :class:`KanaoError`.
    *rooms* is a static list of serialized rooms (tests); *rooms_provider*
    returns model rooms; *context_provider* takes ``(room_id, limit)`` and
    returns the ``context.read_context`` payload minus its envelope; and
    *watcher_factory* returns an object with ``prime()`` and ``poll()``
    (the two halves of ``watch.Watcher`` kept separate precisely so streaming
    can be tested here without ever sleeping).
    """
    limiter = _RateLimiter(min_interval, clock=clock)
    send_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "kanao-serve"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802 - stdlib name
            pass

        # -- small helpers ------------------------------------------------

        def _body(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise KanaoError(ErrorCode.INVALID_REQUEST, "invalid Content-Length")
            if length > MAX_BODY_BYTES:
                raise KanaoError(ErrorCode.INVALID_REQUEST, "request body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise KanaoError(ErrorCode.INVALID_REQUEST, "request body is not JSON")

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            presented = self.headers.get("Authorization") or ""
            if not presented.startswith("Bearer "):
                return False
            return hmac.compare_digest(presented[7:], token)

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._reply(
                401,
                as_envelope(KanaoError(ErrorCode.UNAUTHORIZED, "missing or invalid token")),
            )
            return False

        # -- GET ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._reply(200, {"ok": True})
                return
            if parsed.path in ("/rooms", "/context", "/stream"):
                if not self._require_auth():
                    return
            if parsed.path == "/rooms":
                self._handle_rooms()
                return
            if parsed.path == "/context":
                self._handle_context(parsed)
                return
            if parsed.path == "/stream":
                self._handle_stream()
                return
            self._reply(404, as_envelope(KanaoError(ErrorCode.INVALID_REQUEST, "not found")))

        def _handle_rooms(self) -> None:
            try:
                entries = rooms if rooms is not None else [r.as_json() for r in rooms_provider()]
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, "rooms": entries})

        def _handle_context(self, parsed) -> None:
            query = urllib.parse.parse_qs(parsed.query)
            raw_id = query.get("roomId", [query.get("room", [""])[0]])[0]
            try:
                room_id = int(raw_id)
            except (TypeError, ValueError):
                self._reply(
                    400,
                    as_envelope(
                        KanaoError(ErrorCode.INVALID_REQUEST, "a numeric roomId is required")
                    ),
                )
                return
            try:
                limit = int(query.get("limit", ["40"])[0])
            except (TypeError, ValueError):
                self._reply(
                    400,
                    as_envelope(KanaoError(ErrorCode.INVALID_REQUEST, "limit must be an integer")),
                )
                return
            try:
                payload = context_provider(room_id, limit)
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **payload})

        def _handle_stream(self) -> None:
            if watcher_factory is None:
                self._reply(
                    503,
                    as_envelope(
                        KanaoError(ErrorCode.BUSY, "no watcher is configured on this server")
                    ),
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(kind: str, data: str) -> bool:
                try:
                    chunk = f"event: {kind}\ndata: {data}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False

            watcher = watcher_factory()
            try:
                watcher.prime()
            except Exception:
                emit("error", json.dumps({"message": "watcher failed to prime"}))
                return
            if not emit("ready", "{}"):
                return
            while True:
                try:
                    fresh = watcher.poll()
                except Exception as error:
                    emit("error", json.dumps({"message": str(error)}))
                    return
                for message in fresh:
                    payload = (
                        message.as_json() if hasattr(message, "as_json") else dict(message)
                    )
                    if not emit("message", json.dumps(payload, ensure_ascii=False)):
                        return
                if not emit("ping", "{}"):
                    return
                time.sleep(poll_interval)

        # -- POST ---------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if self.path != "/send":
                self._reply(
                    404, as_envelope(KanaoError(ErrorCode.INVALID_REQUEST, "not found"))
                )
                return
            if not self._require_auth():
                return
            try:
                body = self._body()
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            room = body.get("room", "") if isinstance(body, dict) else ""
            text = body.get("text", "") if isinstance(body, dict) else ""
            dry_run = bool(body.get("dryRun", False)) if isinstance(body, dict) else False
            open_room = bool(body.get("openRoom", False)) if isinstance(body, dict) else False
            if not isinstance(room, str) or not room.strip():
                self._reply(
                    400,
                    as_envelope(
                        KanaoError(ErrorCode.INVALID_REQUEST, "a nonempty room is required")
                    ),
                )
                return
            if not isinstance(text, str) or not text.strip():
                self._reply(
                    400,
                    as_envelope(
                        KanaoError(ErrorCode.INVALID_REQUEST, "a nonempty text is required")
                    ),
                )
                return
            if not dry_run:
                wait = limiter.check()
                if wait is not None:
                    self._reply(
                        429,
                        as_envelope(
                            KanaoError(
                                ErrorCode.RATE_LIMITED,
                                f"another send ran recently; retry in {wait:.1f}s",
                            )
                        ),
                    )
                    return
            try:
                with send_lock:
                    result = send(room, text, dry_run=dry_run, open_if_needed=open_room)
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.SEND_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **result})

    return Handler


def make_live_rooms_provider(reader_factory) -> Callable[[], list[Any]]:
    """Return the rooms a live server lists on each ``GET /rooms``."""

    def provider() -> list[Any]:
        with reader_factory() as reader:
            return list(reader.rooms())

    return provider


def make_live_context_provider(reader_factory) -> Callable[[int, int], dict[str, Any]]:
    """Return the context a live server reads on each ``GET /context``."""

    def provider(room_id: int, limit: int) -> dict[str, Any]:
        from .context import read_context

        with reader_factory() as reader:
            answer = read_context(reader, room_id, limit=limit)
        return {key: value for key, value in answer.items() if key != "ok"}

    return provider


def make_live_send() -> Callable[..., dict[str, Any]]:
    """Build the send callable the server uses on a Windows host.

    Each call opens a fresh reader (so the catalog is never stale across
    requests), resolves the room exactly like the ``send`` command, refuses
    duplicate catalog titles, and hands the text to the real sender.
    """
    from .names import NameLedger
    from .reader import Reader

    def send(
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ) -> dict[str, Any]:
        from . import cli as cli_module
        from .winauto import make_sender

        with Reader(ledger=NameLedger()) as reader:
            namespace = argparse.Namespace(room=room, room_id=None)
            resolved = cli_module._selected_room(reader, namespace)
            catalogued = reader.rooms()
            duplicates = [
                r for r in catalogued if r.title.casefold() == resolved.title.casefold()
            ]
            if len(duplicates) != 1:
                raise KanaoError(
                    ErrorCode.AMBIGUOUS_CHAT,
                    "multiple catalog rooms share this window title; "
                    "give the target a unique title",
                    candidates=[r.as_json() for r in duplicates],
                )
            stored = reader.stored_title(resolved.id)
            window_aliases = [stored] if stored and stored != resolved.title else []
            position = next(
                (index for index, r in enumerate(catalogued) if r.id == resolved.id),
                None,
            )
            try:
                result = make_sender().send(
                    resolved.title,
                    text,
                    dry_run=dry_run,
                    open_if_needed=open_if_needed,
                    aliases=window_aliases,
                    hint=position,
                )
            except OSError as error:
                raise KanaoError(ErrorCode.SEND_FAILED, str(error)) from error
            return {**result, "roomId": str(resolved.id)}

    return send


def make_live_watcher_factory(reader_factory, interval: float) -> Callable[[], Any]:
    """Return one ``watch.Watcher`` per SSE connection on a live server."""

    def factory() -> Any:
        from .reader import Reader
        from .watch import Watcher

        reader = Reader()
        return Watcher(reader, interval=interval)

    return factory


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
