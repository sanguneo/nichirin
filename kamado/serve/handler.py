"""The request/response half of the server: auth, routing, and the envelope.

Every route that answers and closes lives here. ``GET /stream`` does not — it
holds its socket for as long as the caller stays, so its lifetime is
:mod:`kamado.serve.stream`'s problem and this module only hands off to it.
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Optional, Type

from ..errors import ErrorCode, KamadoError, as_envelope
from .config import (
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    MAX_BODY_BYTES,
    MAX_HIGHLIGHTS,
    MAX_LIMIT,
    MAX_STREAMS,
    _status_for,
    validate_count,
    validate_cursor,
    validate_max_streams,
    validate_min_interval,
    validate_poll_interval,
    validate_request_timeout,
    validate_room_id,
    validate_token,
)
from .stream import serve_stream


class _BodyTimeout(Exception):
    """The caller promised a body in ``Content-Length`` and then stopped."""


class _RateLimiter:
    """Refuse sends that arrive sooner than *min_interval* after the last one.

    A zero interval disables the limiter entirely: no clock call, no state.
    """

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    def check(self) -> Optional[float]:
        """Return seconds to wait when too soon, else record now and return None."""
        if self._min_interval == 0.0:
            return None
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
    rooms_provider: Optional[Callable[[], list[Any]]] = None,
    context_provider: Optional[Callable[[int, int], dict[str, Any]]] = None,
    digest_provider: Optional[Callable[[int, str, int, int], dict[str, Any]]] = None,
    watcher_factory: Optional[Callable[[], Any]] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    max_streams: int = MAX_STREAMS,
) -> Type[BaseHTTPRequestHandler]:
    """Build a request handler around injected backends.

    *send* takes ``(room, text, dry_run=..., open_if_needed=..., aliases=...,
    hint=...)`` and returns the sender envelope or raises :class:`KamadoError`.
    *rooms* is a static list of serialized rooms (tests); *rooms_provider*
    returns model rooms; *context_provider* takes ``(room_id, limit)`` and
    returns the ``context.read_context`` payload minus its envelope; and
    *watcher_factory* returns an object with ``prime()`` and ``poll()``
    (the two halves of ``watch.Watcher`` kept separate precisely so streaming
    can be tested here without ever sleeping).

    Raises :class:`KamadoError` for a bad token, interval, or poll interval.
    """
    secret = validate_token(token)
    spacing = validate_min_interval(min_interval)
    period = validate_poll_interval(poll_interval)
    patience = validate_request_timeout(request_timeout)
    streams_allowed = validate_max_streams(max_streams)
    limiter = _RateLimiter(spacing, clock=clock)
    send_lock = threading.Lock()
    # Not a lock: a permit per stream, so the ninth caller is refused rather
    # than queued behind eight connections that intend to stay all day.
    stream_slots = threading.Semaphore(streams_allowed)

    class Handler(BaseHTTPRequestHandler):
        server_version = "kamado-serve"
        timeout = patience

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802 - stdlib name
            pass

        # -- small helpers ------------------------------------------------

        def _read_json_body(self) -> Any:
            declared = self.headers.get("Content-Length")
            if declared is None:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "Content-Length is required")
            try:
                length = int(declared)
            except ValueError:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "invalid Content-Length")
            if length < 0:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "negative Content-Length")
            if length > MAX_BODY_BYTES:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "request body too large")
            encoding = self.headers.get("Transfer-Encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise KamadoError(
                    ErrorCode.INVALID_REQUEST,
                    "chunked request bodies are not supported",
                )
            try:
                raw = self.rfile.read(length) if length else b""
            except (socket.timeout, TimeoutError):
                # Promised a body and then went quiet. Without the connection
                # timeout this held a thread for as long as the caller liked.
                raise _BodyTimeout()
            if len(raw) < length:
                raise KamadoError(
                    ErrorCode.INVALID_REQUEST, "request body ended before Content-Length"
                )
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise KamadoError(ErrorCode.INVALID_REQUEST, "request body is not JSON")

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _authorized(self) -> bool:
            presented = self.headers.get("Authorization")
            if presented is None:
                return False
            scheme, _, rest = presented.partition(" ")
            if scheme.lower() != "bearer":
                return False
            # partition consumed the single separating space; strip() then
            # tolerates clients that send more than one or trail one.
            candidate = rest.strip()
            if not candidate or any(not 33 <= ord(c) <= 126 for c in candidate):
                return False
            try:
                return hmac.compare_digest(
                    candidate.encode("ascii"), secret.encode("ascii")
                )
            except (UnicodeEncodeError, ValueError):
                return False

        def _is_health(self) -> bool:
            return (
                self.command == "GET"
                and urllib.parse.urlparse(self.path).path == "/health"
            )

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._reply(
                401,
                as_envelope(KamadoError(ErrorCode.UNAUTHORIZED, "missing or invalid token")),
            )
            return False

        # -- routing ------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if self._is_health():
                self._reply(200, {"ok": True})
                return
            if not self._require_auth():
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/rooms":
                self._handle_rooms()
            elif parsed.path == "/context":
                self._handle_context(parsed)
            elif parsed.path == "/digest":
                self._handle_digest(parsed)
            elif parsed.path == "/stream":
                self._handle_stream(parsed)
            else:
                self._reply(
                    404, as_envelope(KamadoError(ErrorCode.INVALID_REQUEST, "not found"))
                )

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if not self._require_auth():
                return
            if urllib.parse.urlparse(self.path).path != "/send":
                self._reply(
                    404, as_envelope(KamadoError(ErrorCode.INVALID_REQUEST, "not found"))
                )
                return
            self._handle_send()

        def do_PUT(self) -> None:  # noqa: N802 - stdlib name
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib name
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib name
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            if not self._require_auth():
                return
            self._reply(
                405,
                as_envelope(KamadoError(ErrorCode.INVALID_REQUEST, "method not allowed")),
            )

        # -- endpoints ----------------------------------------------------

        def _handle_rooms(self) -> None:
            try:
                if rooms_provider is not None:
                    entries = [r.as_json() for r in rooms_provider()]
                else:
                    raise KamadoError(
                        ErrorCode.READ_FAILED, "no room source is configured on this server"
                    )
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KamadoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, "rooms": entries})

        def _handle_context(self, parsed) -> None:
            if context_provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KamadoError(
                            ErrorCode.READ_FAILED,
                            "no context source is configured on this server",
                        )
                    ),
                )
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
                limit = validate_count(
                    query.get("limit", ["40"])[0], name="limit", maximum=MAX_LIMIT
                )
            except KamadoError as error:
                # Refused before the reader is opened: a bad number should cost
                # the caller a round trip, not a decryption.
                self._reply(_status_for(error), as_envelope(error))
                return
            try:
                payload = context_provider(room_id, limit)
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KamadoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **payload})

        def _handle_digest(self, parsed) -> None:
            """Hand over the extractive digest rather than the raw window.

            A remote caller asked to summarise a busy room otherwise pages the
            messages themselves: measured on this account, the same newest 400
            were 116,269 bytes through `messages` and 3,989 through `digest`.
            Over a link that is the whole point of this server, that gap is the
            feature.
            """
            if digest_provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KamadoError(
                            ErrorCode.READ_FAILED,
                            "no digest source is configured on this server",
                        )
                    ),
                )
                return
            query = urllib.parse.parse_qs(parsed.query)
            span = query.get("span", ["30m"])[0]
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
                limit = validate_count(
                    query.get("limit", ["400"])[0], name="limit", maximum=MAX_LIMIT
                )
                highlights = validate_count(
                    query.get("highlights", ["3"])[0],
                    name="highlights",
                    maximum=MAX_HIGHLIGHTS,
                )
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            try:
                payload = digest_provider(room_id, span, limit, highlights)
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KamadoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **payload})

        def _handle_stream(self, parsed) -> None:
            try:
                cursor = validate_cursor(
                    urllib.parse.parse_qs(parsed.query).get("since", [])
                )
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            if not stream_slots.acquire(blocking=False):
                self._reply(
                    503,
                    as_envelope(
                        KamadoError(
                            ErrorCode.BUSY,
                            f"this server holds at most {streams_allowed} streams; "
                            "close one and reconnect",
                        )
                    ),
                )
                return
            try:
                serve_stream(
                    self,
                    watcher_factory=watcher_factory,
                    poll_interval=period,
                    reply=self._reply,
                    cursor=cursor,
                )
            finally:
                stream_slots.release()

        def _handle_send(self) -> None:
            try:
                body = self._read_json_body()
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except _BodyTimeout:
                self._reply(
                    408,
                    as_envelope(
                        KamadoError(
                            ErrorCode.INVALID_REQUEST,
                            "the request body did not arrive in time",
                        )
                    ),
                )
                self.close_connection = True
                return
            room = body.get("room", "") if isinstance(body, dict) else ""
            text = body.get("text", "") if isinstance(body, dict) else ""
            dry_run = body.get("dryRun", False) if isinstance(body, dict) else False
            open_room = body.get("openRoom", False) if isinstance(body, dict) else False
            if not isinstance(room, str) or not room.strip():
                self._reply(
                    400,
                    as_envelope(
                        KamadoError(ErrorCode.INVALID_REQUEST, "a nonempty room is required")
                    ),
                )
                return
            if not isinstance(text, str) or not text.strip():
                self._reply(
                    400,
                    as_envelope(
                        KamadoError(ErrorCode.INVALID_REQUEST, "a nonempty text is required")
                    ),
                )
                return
            for name, flag in (("dryRun", dry_run), ("openRoom", open_room)):
                if not isinstance(flag, bool):
                    self._reply(
                        400,
                        as_envelope(
                            KamadoError(
                                ErrorCode.INVALID_REQUEST, f"{name} must be a boolean"
                            )
                        ),
                    )
                    return
            # Quota and execution share one lock so the spacing a caller is
            # charged matches the order the input box actually serves.
            with send_lock:
                if not dry_run:
                    wait = limiter.check()
                    if wait is not None:
                        self._reply(
                            429,
                            as_envelope(
                                KamadoError(
                                    ErrorCode.RATE_LIMITED,
                                    f"another send ran recently; retry in {wait:.1f}s",
                                )
                            ),
                        )
                        return
                try:
                    result = send(room, text, dry_run=dry_run, open_if_needed=open_room)
                except KamadoError as error:
                    self._reply(_status_for(error), as_envelope(error))
                    return
                except OSError as error:
                    self._reply(
                        500, as_envelope(KamadoError(ErrorCode.SEND_FAILED, str(error)))
                    )
                    return
            self._reply(200, {"ok": True, **result})

    # Bind the validated secret and validated tunables onto the class so a
    # reviewer can see what each connection closes over.
    Handler._serve_secret = secret  # type: ignore[attr-defined]
    return Handler
