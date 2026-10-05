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
    MAX_UPLOAD_BYTES,
    UPLOAD_FRAMING_BYTES,
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
from .upload import UploadStage, parse_form


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
    """Space sends apart without dropping any, and refuse only when full.

    Refusing the second of two messages was the wrong answer. What is being
    protected is not a scarce server resource, it is one input box, and a
    caller handed a 429 can do nothing with it except sleep and try again —
    which is the queue's job, written once here instead of once in every
    client. So a send that arrives during another one waits, and ``429`` is
    kept for the case it actually describes: the line is full.

    *Pacing runs from when a send starts, not when it finishes.* A send costs
    about three seconds here, and charging the interval on top of that would
    make every message wait for time the previous one already spent — the
    slower the machine, the worse the penalty. Measured from the start, the
    cadence is whichever is slower, the send itself or *min_interval*, so on
    this host a two-second interval costs nothing at all.

    A zero interval disables the queue entirely: sends still serialize on the
    lock outside, but nobody waits and nobody is refused.
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
        """Take a place in line, or return how many are already in it.

        A refused caller is told the depth rather than a time, because the
        honest estimate is not available: the send at the front may be halfway
        through three seconds of accessibility work, and nothing here knows how
        much of it is left.
        """
        if self._min_interval == 0.0:
            return None
        with self._state:
            if self._queued >= self._capacity:
                return self._queued
            self._queued += 1
            return None

    def release(self) -> None:
        """Give up a place, whether the send worked or not."""
        if self._min_interval == 0.0:
            return
        with self._state:
            if self._queued > 0:
                self._queued -= 1

    def wait_for_turn(self) -> None:
        """Block until this send may start, or raise if the server is stopping."""
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
        """Read the clock for a send that is about to start, recording nothing.

        Nothing is committed yet because the send may still fail, and a send
        that delivered nothing should not pace the next one.
        """
        return self._clock()

    def commit(self, started: float) -> None:
        """Now that a send has delivered, pace the next from when it started."""
        if self._min_interval == 0.0:
            return
        with self._state:
            self._next_start = started + self._min_interval
            self._state.notify_all()

    def close(self) -> None:
        """Wake whoever is still in line so they are answered, not killed.

        The server's threads are daemons, so without this a caller waiting
        their turn at Ctrl-C simply vanishes mid-request: preventing dropped
        messages would have bought a quieter way to drop them.
        """
        with self._state:
            self._closed = True
            self._state.notify_all()

    def _wait(self, remaining: float) -> None:
        """The one real pause, named so a test can replace it with a clock jump."""
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
    notice_provider: Optional[Callable[[int], dict[str, Any]]] = None,
    attachments_provider: Optional[Callable[..., dict[str, Any]]] = None,
    revisions_provider: Optional[Callable[[int, int], dict[str, Any]]] = None,
    file_provider: Optional[Callable[[int, int], Any]] = None,
    upload: Optional[Callable[..., dict[str, Any]]] = None,
    upload_stage: Optional[UploadStage] = None,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
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

    *upload* takes ``(room, path, name, open_if_needed=...)`` for a file staged
    in *upload_stage* and returns the ``UploadResult`` fields; without it
    ``POST /upload`` is not routed and ``GET /health`` names no feature.

    Raises :class:`KamadoError` for a bad token, interval, or poll interval.
    """
    if upload is not None and upload_stage is None:
        raise ValueError("an uploader needs a stage for its files")
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
        server_version = "kamado-serve"
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
            raw = self._read_body(MAX_BODY_BYTES)
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise KamadoError(ErrorCode.INVALID_REQUEST, "request body is not JSON")

        def _read_body(self, limit: int) -> bytes:
            declared = self.headers.get("Content-Length")
            if declared is None:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "Content-Length is required")
            try:
                length = int(declared)
            except ValueError:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "invalid Content-Length")
            if length < 0:
                raise KamadoError(ErrorCode.INVALID_REQUEST, "negative Content-Length")
            if length > limit:
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
            self._body_read = True
            return raw

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
                health: dict[str, Any] = {"ok": True}
                if upload is not None:
                    health["features"] = {"upload": {"maxBytes": max_upload_bytes}}
                self._reply(200, health)
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
                    404, as_envelope(KamadoError(ErrorCode.INVALID_REQUEST, "not found"))
                )

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if not self._require_auth():
                return
            route = urllib.parse.urlparse(self.path).path
            if route == "/send":
                self._handle_send()
            elif route == "/upload" and upload is not None:
                self._handle_upload()
            else:
                self._reply(
                    404, as_envelope(KamadoError(ErrorCode.INVALID_REQUEST, "not found"))
                )

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
                # Every room is readable here, so there is nothing to open.
                validate_flag(query.get("open", ["false"])[0], name="open")
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
                validate_flag(query.get("open", ["false"])[0], name="open")
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

        def _answer_read(
            self, provider: Optional[Callable[..., Any]], label: str, read: Callable[[], Any]
        ) -> None:
            """Run one read route's provider and answer in the usual envelope."""
            if provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KamadoError(
                            ErrorCode.READ_FAILED,
                            f"no {label} source is configured on this server",
                        )
                    ),
                )
                return
            try:
                payload = read()
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KamadoError(ErrorCode.READ_FAILED, str(error))))
                return
            self._reply(200, {"ok": True, **payload})

        def _handle_notice(self, parsed) -> None:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
            except KamadoError as error:
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
                    raise KamadoError(
                        ErrorCode.INVALID_REQUEST,
                        f"kind must be one of {', '.join(ATTACHMENT_KINDS)}",
                    )
                before = query.get("beforeId", [""])[0]
                before_id = validate_message_id(before, name="beforeId") if before else None
                # Every room is readable here, so there is nothing to open.
                validate_flag(query.get("open", ["false"])[0], name="open")
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            provider = attachments_provider
            self._answer_read(
                provider,
                "attachment",
                lambda: provider(room_id, kind, limit, before_id),  # type: ignore[misc]
            )

        def _handle_revisions(self, parsed) -> None:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                room_id = validate_room_id(query.get("roomId", [""])[0])
                message_id = validate_message_id(query.get("messageId", [""])[0])
                validate_flag(query.get("open", ["false"])[0], name="open")
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            provider = revisions_provider
            self._answer_read(
                provider, "revision", lambda: provider(room_id, message_id)  # type: ignore[misc]
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
                validate_flag(query.get("open", ["false"])[0], name="open")
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            if file_provider is None:
                self._reply(
                    500,
                    as_envelope(
                        KamadoError(
                            ErrorCode.READ_FAILED, "no file source is configured on this server"
                        )
                    ),
                )
                return
            try:
                download = file_provider(room_id, message_id)
            except KamadoError as error:
                self._reply(_status_for(error), as_envelope(error))
                return
            except OSError as error:
                self._reply(500, as_envelope(KamadoError(ErrorCode.READ_FAILED, str(error))))
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", _disposition(download.name))
                # The shared contract's header name, answered the same by both builds.
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
            except OSError:
                self.close_connection = True
            finally:
                download.close()

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
            self._in_line(
                lambda: send(room, text, dry_run=dry_run, open_if_needed=open_room),
                paced=not dry_run,
                what="message",
            )

        def _handle_upload(self) -> None:
            """Take one file and hand it to the client through the send line.

            The body is refused unread when its declared length is past the
            limit, read whole otherwise, and only staged once every part was
            accepted. Nothing is retried: a failure after the client's own
            send button was pressed has an unknown outcome.
            """
            try:
                body = self._read_body(max_upload_bytes + UPLOAD_FRAMING_BYTES)
                form = parse_form(
                    self.headers.get("Content-Type"), body, max_upload_bytes
                )
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
            assert upload is not None and upload_stage is not None

            def deliver() -> dict[str, Any]:
                upload_stage.sweep()
                try:
                    path = upload_stage.stage(form.name, form.data)
                except OSError as error:
                    raise KamadoError(
                        ErrorCode.SEND_FAILED,
                        f"could not stage the file: {error}; nothing was sent",
                    ) from error
                return upload(
                    form.room, str(path), form.name, open_if_needed=form.open_room
                )

            self._in_line(deliver, paced=True, what="file")

        def _in_line(
            self,
            action: Callable[[], dict[str, Any]],
            *,
            paced: bool,
            what: str,
        ) -> None:
            """Run *action* in the one line texts and files share, and answer."""
            # Take a place in line before the lock, so the queue can count who
            # is waiting rather than discover them already blocked on it.
            if paced:
                in_line = queue.reserve()
                if in_line is not None:
                    self._reply(
                        429,
                        as_envelope(
                            KamadoError(
                                ErrorCode.RATE_LIMITED,
                                f"the send queue is full ({in_line} already in "
                                "line); retry once they have gone through",
                            )
                        ),
                    )
                    return
            try:
                # Queue and execution share one lock so the spacing a caller is
                # charged matches the order the input box actually serves.
                with send_lock:
                    if paced:
                        try:
                            queue.wait_for_turn()
                        except _ShuttingDown:
                            self._reply(
                                503,
                                as_envelope(
                                    KamadoError(
                                        ErrorCode.BUSY,
                                        "the server is shutting down; "
                                        f"this {what} was not sent",
                                    )
                                ),
                            )
                            return
                    started = queue.begin()
                    try:
                        result = action()
                    except KamadoError as error:
                        self._reply(_status_for(error), as_envelope(error))
                        return
                    except OSError as error:
                        self._reply(
                            500,
                            as_envelope(KamadoError(ErrorCode.SEND_FAILED, str(error))),
                        )
                        return
                    if paced:
                        # Paced from when this send started, and only now that it
                        # delivered: the failures above put text nowhere, and
                        # charging them would hold up somebody's retry of a room
                        # they had simply not opened yet.
                        queue.commit(started)
            finally:
                if paced:
                    queue.release()
            self._reply(200, {"ok": True, **result})

    # Bind the validated secret and validated tunables onto the class so a
    # reviewer can see what each connection closes over.
    Handler._serve_secret = secret  # type: ignore[attr-defined]
    Handler._send_queue = queue  # type: ignore[attr-defined]
    return Handler
