"""The request/response half of the server: auth, routing, and the envelope.

Every route that answers and closes lives here. ``GET /stream`` does not — it
holds its socket for as long as the caller stays, so its lifetime is
:mod:`kanao.serve.stream`'s problem and this module only hands off to it.
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

from ..errors import ErrorCode, KanaoError, as_envelope
from ..model import ATTACHMENT_KINDS
from .config import (
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    LINGER_BYTES,
    LINGER_SECONDS,
    MAX_BODY_BYTES,
    MAX_HIGHLIGHTS,
    MAX_LIMIT,
    MAX_QUEUED_SENDS,
    MAX_STREAMS,
    _status_for,
    validate_count,
    validate_flag,
    validate_cursor,
    validate_max_streams,
    validate_message_id,
    validate_min_interval,
    validate_poll_interval,
    validate_request_timeout,
    validate_room_id,
    validate_token,
)
from .stream import serve_stream


_FILE_CHUNK = 64 * 1024


def _disposition(name: str) -> str:
    """Name a download for the caller, whatever characters the sender used.

    The name is somebody else's text, so it never reaches a header raw: the
    plain ``filename`` carries only printable ASCII, and ``filename*`` the
    whole name percent-encoded, which is how RFC 6266 names a Hangul file.
    """
    fallback = "".join(
        character if 32 <= ord(character) < 127 and character not in '"\\' else "_"
        for character in name
    ) or "file"
    encoded = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


class _BodyTimeout(Exception):
    """The caller promised a body in ``Content-Length`` and then stopped."""


class _ShuttingDown(Exception):
    """The server stopped while this send was still waiting its turn."""


class _SendQueue:
    """Space sends apart by making them wait, not by throwing them away.

    What ``min_interval`` protects is one input box in one client, and a
    caller told "too soon" can do nothing but sleep and try again — which is
    every client writing the queue that belongs here. So a second send waits
    for its turn and then goes. ``429 RATE_LIMITED`` is kept for the case it
    is actually needed: a queue that never refuses anything would only make
    pouring a thousand messages into someone's conversation take longer.

    The pacing is measured from when a send *started*. From when it finished,
    the next caller is charged again for time the last send already spent, so
    the period becomes ``send + min_interval``; from the start it is
    ``max(send, min_interval)``, which is the spacing that was asked for and
    nothing else. This is what the interval being shorter than a send used to
    hide: the old limiter recorded the start too, but it recorded it *before*
    the send, so with a three-second send and a two-second interval it never
    once fired.

    A zero interval disables all of it — no reservation, no clock, no wait —
    which is what ``--min-interval 0`` asks for.
    """

    def __init__(
        self,
        min_interval: float,
        capacity: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = min_interval
        self._capacity = capacity
        self._clock = clock
        self._state = threading.Condition()
        self._queued = 0
        self._next_start: Optional[float] = None
        self._closed = False

    def reserve(self) -> Optional[int]:
        """Take a place in line, or say how many are already in it.

        The count is the answer rather than a number of seconds because
        nobody knows how far into its three seconds the send at the front is.
        A number that cannot be kept is worse than no number.
        """
        if self._min_interval == 0.0:
            return None
        with self._state:
            if self._queued >= self._capacity:
                return self._queued
            self._queued += 1
            return None

    def release(self) -> None:
        """Give the place back, however the send ended."""
        if self._min_interval == 0.0:
            return
        with self._state:
            if self._queued > 0:
                self._queued -= 1

    def wait_for_turn(self) -> None:
        """Block until the interval since the last send has passed."""
        if self._min_interval == 0.0:
            return
        with self._state:
            while True:
                if self._closed:
                    raise _ShuttingDown()
                if self._next_start is None:
                    return
                remaining = self._next_start - self._clock()
                if remaining <= 0:
                    return
                self._wait(remaining)

    def begin(self) -> float:
        """Read the time this send is starting at, without recording it yet."""
        return self._clock()

    def commit(self, started: float) -> None:
        """Having handed the text over, book the next turn from *started*."""
        if self._min_interval == 0.0:
            return
        with self._state:
            self._next_start = started + self._min_interval
            self._state.notify_all()

    def close(self) -> None:
        """Wake everyone still in line so nothing dies mid-wait in silence."""
        with self._state:
            self._closed = True
            self._state.notify_all()

    def _wait(self, remaining: float) -> None:
        """The only place this ever really waits, so a test can replace it."""
        self._state.wait(remaining)


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
    max_queued_sends: int = MAX_QUEUED_SENDS,
    room_opener: Optional[Callable[[int], None]] = None,
    notice_provider: Optional[Callable[[int], dict[str, Any]]] = None,
    attachments_provider: Optional[Callable[..., dict[str, Any]]] = None,
    revisions_provider: Optional[Callable[[int, int], dict[str, Any]]] = None,
    file_provider: Optional[Callable[[int, int], Any]] = None,
) -> Type[BaseHTTPRequestHandler]:
    """Build a request handler around injected backends.

    *send* takes ``(room, text, dry_run=..., open_if_needed=..., aliases=...,
    hint=...)`` and returns the sender envelope or raises :class:`KanaoError`.
    *rooms* is a static list of serialized rooms (tests); *rooms_provider*
    returns model rooms; *context_provider* takes ``(room_id, limit)`` and
    returns the ``context.read_context`` payload minus its envelope; and
    *room_opener* takes a room id and has the client open that room so its
    key is loaded, raising :class:`KanaoError` if it cannot; it is what
    ``open=true`` on ``/context`` and ``/digest`` calls, one at a time under
    the lock sends take, because both drive the one client.
    *watcher_factory* returns an object with ``prime()`` and ``poll()``
    (the two halves of ``watch.Watcher`` kept separate precisely so streaming
    can be tested here without ever sleeping).

    Raises :class:`KanaoError` for a bad token, interval, or poll interval.
    """
    secret = validate_token(token)
    spacing = validate_min_interval(min_interval)
    period = validate_poll_interval(poll_interval)
    patience = validate_request_timeout(request_timeout)
    streams_allowed = validate_max_streams(max_streams)
    queue = _SendQueue(spacing, max_queued_sends, clock=clock)
    send_lock = threading.Lock()
    # Not a lock: a permit per stream, so the ninth caller is refused rather
    # than queued behind eight connections that intend to stay all day.
    stream_slots = threading.Semaphore(streams_allowed)

    class Handler(BaseHTTPRequestHandler):
        server_version = "kanao-serve"
        timeout = patience
        _body_read = False
        """Whether this request's body was read to its declared end."""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
            pass

        def finish(self) -> None:
            try:
                if self._body_left_unread():
                    self._linger()
            finally:
                super().finish()

        def _body_left_unread(self) -> bool:
            """Whether the caller may still be sending a body nothing here read.

            A refusal is written before the body is read whenever the body is
            what was wrong - no length, a bad one, too much - and so is a 401
            or 404 for a POST. A GET without a length leaves nothing behind.
            """
            headers = getattr(self, "headers", None)
            if self._body_read or headers is None:
                return False
            if headers.get("Transfer-Encoding") is not None:
                return True
            declared = headers.get("Content-Length")
            if declared is None:
                return self.command not in ("GET", "HEAD")
            return declared.strip() != "0"

        def _linger(self) -> None:
            """Read what the caller is still sending, so the close is not a reset.

            See ``LINGER_SECONDS``. Writing is finished first, so the caller
            sees the whole answer and then its end, and closes in turn.
            """
            connection = self.connection
            try:
                connection.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + LINGER_SECONDS
                discarded = 0
                while discarded < LINGER_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    connection.settimeout(remaining)
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    discarded += len(chunk)
            except OSError:
                # Hung up or reset first: there is no answer left to save.
                return

        # -- small helpers ------------------------------------------------

        def _read_json_body(self) -> Any:
            declared = self.headers.get("Content-Length")
            if declared is None:
                raise KanaoError(ErrorCode.INVALID_REQUEST, "Content-Length is required")
            try:
                length = int(declared)
            except ValueError:
                raise KanaoError(ErrorCode.INVALID_REQUEST, "invalid Content-Length")
            if length < 0:
                raise KanaoError(ErrorCode.INVALID_REQUEST, "negative Content-Length")
            if length > MAX_BODY_BYTES:
                raise KanaoError(ErrorCode.INVALID_REQUEST, "request body too large")
            encoding = self.headers.get("Transfer-Encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise KanaoError(
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
                raise KanaoError(
                    ErrorCode.INVALID_REQUEST, "request body ended before Content-Length"
                )
            self._body_read = True
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
                as_envelope(KanaoError(ErrorCode.UNAUTHORIZED, "missing or invalid token")),
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
            elif parsed.path == "/notice":
                self._handle_notice(parsed)
            elif parsed.path == "/attachments":
                self._handle_attachments(parsed)
            elif parsed.path == "/revisions":
                self._handle_revisions(parsed)
            elif parsed.path == "/file":
                self._handle_file(parsed)
            else:
                self._reply(
                    404, as_envelope(KanaoError(ErrorCode.INVALID_REQUEST, "not found"))
                )

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if not self._require_auth():
                return
            if urllib.parse.urlparse(self.path).path != "/send":
                self._reply(
                    404, as_envelope(KanaoError(ErrorCode.INVALID_REQUEST, "not found"))
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
                as_envelope(KanaoError(ErrorCode.INVALID_REQUEST, "method not allowed")),
            )

        # -- endpoints ----------------------------------------------------

        def _handle_rooms(self) -> None:
            try:
                if rooms_provider is not None:
                    entries = [r.as_json() for r in rooms_provider()]
                else:
                    raise KanaoError(
                        ErrorCode.READ_FAILED, "no room source is configured on this server"
                    )
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, "rooms": entries})

        def _read_opening(self, room_id: int, wanted: bool, read: Callable[[], Any]) -> Any:
            """Read; if the room's key is missing and *wanted*, open it and read again.

            A room the client never opened has no key in memory, so the read
            is refused with ``KEY_UNAVAILABLE``. The TUI and ``messages --open``
            already had the client open it; a remote caller had no way to ask.
            Opening marks the room read in the client, so it happens only when
            the caller says ``open=true`` — never as a side effect of reading.
            """
            try:
                return read()
            except KanaoError as error:
                if not wanted or room_opener is None or error.code != ErrorCode.KEY_UNAVAILABLE:
                    raise
            # The same lock sends take: opening a room drives the one client,
            # and two things driving it at once is how the wrong window gets
            # the words or the click.
            with send_lock:
                room_opener(room_id)
            return read()

        def _handle_context(self, parsed) -> None:
            if context_provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KanaoError(
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
                wanted = validate_flag(query.get("open", ["false"])[0], name="open")
            except KanaoError as error:
                # Refused before the reader is opened: a bad number should cost
                # the caller a round trip, not a decryption.
                self._reply(_status_for(error), as_envelope(error))
                return
            try:
                payload = self._read_opening(
                    room_id, wanted, lambda: context_provider(room_id, limit)
                )
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
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
                        KanaoError(
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
                wanted = validate_flag(query.get("open", ["false"])[0], name="open")
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            try:
                payload = self._read_opening(
                    room_id, wanted, lambda: digest_provider(room_id, span, limit, highlights)
                )
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **payload})

        def _answer_read(
            self, provider: Optional[Callable[..., Any]], label: str, read: Callable[[], Any]
        ) -> None:
            """Run one read route's provider and answer in the usual envelope."""
            if provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KanaoError(
                            ErrorCode.READ_FAILED,
                            f"no {label} source is configured on this server",
                        )
                    ),
                )
                return
            try:
                payload = read()
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **payload})

        def _handle_notice(self, parsed) -> None:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            provider = notice_provider
            self._answer_read(provider, "notice", lambda: provider(room_id))  # type: ignore[misc]

        def _handle_attachments(self, parsed) -> None:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
                limit = validate_count(
                    query.get("limit", ["50"])[0], name="limit", maximum=MAX_LIMIT
                )
                kind = query.get("kind", [""])[0] or None
                if kind is not None and kind not in ATTACHMENT_KINDS:
                    raise KanaoError(
                        ErrorCode.INVALID_REQUEST,
                        f"kind must be one of {', '.join(ATTACHMENT_KINDS)}",
                    )
                before = query.get("beforeId", [""])[0]
                before_id = validate_message_id(before, name="beforeId") if before else None
                wanted = validate_flag(query.get("open", ["false"])[0], name="open")
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            provider = attachments_provider
            self._answer_read(
                provider,
                "attachment",
                lambda: self._read_opening(
                    room_id, wanted, lambda: provider(room_id, kind, limit, before_id)  # type: ignore[misc]
                ),
            )

        def _handle_revisions(self, parsed) -> None:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
                message_id = validate_message_id(query.get("messageId", [""])[0])
                wanted = validate_flag(query.get("open", ["false"])[0], name="open")
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            provider = revisions_provider
            self._answer_read(
                provider,
                "revision",
                lambda: self._read_opening(
                    room_id, wanted, lambda: provider(room_id, message_id)  # type: ignore[misc]
                ),
            )

        def _handle_file(self, parsed) -> None:
            """Stream one file message's bytes, never buffering the whole file.

            The provider says where they come from — the client's saved copy
            or its address — and hands over something to read and close.
            Failures before the first byte answer in the usual JSON envelope;
            one after it can only end the connection early, which is what a
            client sees as a short ``Content-Length``.
            """
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
                message_id = validate_message_id(query.get("messageId", [""])[0])
                wanted = validate_flag(query.get("open", ["false"])[0], name="open")
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            if file_provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KanaoError(
                            ErrorCode.READ_FAILED, "no file source is configured on this server"
                        )
                    ),
                )
                return
            try:
                download = self._read_opening(
                    room_id, wanted, lambda: file_provider(room_id, message_id)
                )
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KanaoError(ErrorCode.READ_FAILED, str(error))))
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", _disposition(download.name))
                self.send_header("X-Kanao-Source", download.source)
                if download.size is not None:
                    self.send_header("Content-Length", str(download.size))
                else:
                    self.close_connection = True
                self.end_headers()
                while True:
                    chunk = download.stream.read(_FILE_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
            finally:
                download.close()

        def _handle_stream(self, parsed) -> None:
            try:
                cursor = validate_cursor(
                    urllib.parse.parse_qs(parsed.query).get("since", [])
                )
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            if not stream_slots.acquire(blocking=False):
                self._reply(
                    503,
                    as_envelope(
                        KanaoError(
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
            except KanaoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except _BodyTimeout:
                self._reply(
                    408,
                    as_envelope(
                        KanaoError(
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
            for name, flag in (("dryRun", dry_run), ("openRoom", open_room)):
                if not isinstance(flag, bool):
                    self._reply(
                        400,
                        as_envelope(
                            KanaoError(
                                ErrorCode.INVALID_REQUEST, f"{name} must be a boolean"
                            )
                        ),
                    )
                    return
            # The place in line is taken *before* the lock, so the line has a
            # length to report; waiting for the turn happens under it, where
            # the order a caller is given is the order the input box serves.
            if not dry_run:
                in_line = queue.reserve()
                if in_line is not None:
                    self._reply(
                        429,
                        as_envelope(
                            KanaoError(
                                ErrorCode.RATE_LIMITED,
                                f"the send queue is full ({in_line} already in "
                                "line); retry once they have gone through",
                            )
                        ),
                    )
                    return
            try:
                with send_lock:
                    if not dry_run:
                        try:
                            queue.wait_for_turn()
                        except _ShuttingDown:
                            self._reply(
                                503,
                                as_envelope(
                                    KanaoError(
                                        ErrorCode.BUSY,
                                        "the server is shutting down; this "
                                        "message was not sent",
                                    )
                                ),
                            )
                            return
                    started = queue.begin()
                    try:
                        result = send(
                            room, text, dry_run=dry_run, open_if_needed=open_room
                        )
                    except KanaoError as error:
                        self._reply(_status_for(error), as_envelope(error))
                        return
                    except OSError as error:
                        self._reply(
                            500,
                            as_envelope(KanaoError(ErrorCode.SEND_FAILED, str(error))),
                        )
                        return
                    if not dry_run:
                        # Only a send that was handed over spends the pacing.
                        # The failures above put text nowhere, and there is no
                        # reason to hold back somebody retrying a room they
                        # have just gone and opened.
                        queue.commit(started)
            finally:
                if not dry_run:
                    queue.release()
            self._reply(200, {"ok": True, **result})

    # Bind the validated secret and validated tunables onto the class so a
    # reviewer can see what each connection closes over. The queue is bound
    # because shutdown has to reach it: the threads holding a place in line
    # are daemons, and Ctrl-C would otherwise kill them without an answer.
    Handler._serve_secret = secret  # type: ignore[attr-defined]
    Handler._send_queue = queue  # type: ignore[attr-defined]
    return Handler
