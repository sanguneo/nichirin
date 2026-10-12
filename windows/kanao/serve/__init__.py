"""Remote access to the send + read + stream paths over HTTP.

Windows hosts this process next to the KakaoTalk client. A Linux or remote
machine connects with a bearer token and can then:

- ``POST /send`` — hand a message to the same ``winauto.Sender`` the ``send``
  command drives, with the two-seconds-between-sends rule enforced server-side
  by making the second send wait rather than by refusing it.
- ``POST /upload`` — send one file through the room's own file button, in the
  same line as ``/send``; ``GET /health`` declares it under ``features``.
- ``GET /rooms`` — list readable rooms in the same shape as the CLI command.
- ``GET /context?roomId=…`` — read one room's recent messages.
- ``GET /notice``, ``/attachments``, ``/revisions`` — a room's pinned notice,
  what its messages carried, and every version of an edited message.
- ``GET /file?roomId=…&messageId=…`` — a file message's bytes: the client's
  saved copy when there is one, otherwise fetched from the client's own hosts.
- ``GET /stream`` — Server-Sent Events carrying new messages as ``watch.Watcher``
  finds them. Programmable SSE clients and ``curl -N`` consume this, and the
  same channel works for a remote build that forwards chat into an LLM.

SSE, not WebRTC: this is a one-way push of small JSON payloads, not
latency-sensitive media. WebRTC would add a signalling server, SDP exchange,
and STUN/TURN traversal for zero benefit here; SSE rides plain HTTP, passes
through any proxy a ``POST`` would, and needs no stdlib-external package.
``curl -N`` alone reads the stream, which is exactly the surface a remote
Linux caller has.

Security posture: a shared bearer token (``--token`` or ``KANAO_TOKEN``, ASCII
letters/digits plus ``-._~+/=``, at least 16 characters), loopback binding by
default, no TLS of its own. Reach it from another machine over SSH port
forwarding, a VPN, or a reverse proxy that terminates TLS — never by binding a
wildcard address on an open network with a weak token.

Sends are serialized under one lock and spaced ``min_interval`` seconds apart,
because the client's input box is a single shared resource. A send that
arrives early waits its turn; ``429 RATE_LIMITED`` is what a caller gets when
``MAX_QUEUED_SENDS`` are already in line, so the queue has an end.

Where things live: :mod:`kanao.serve.config` holds the constants and the
validation every route agrees on, :mod:`kanao.serve.handler` the routes that
answer and close, :mod:`kanao.serve.stream` the one connection that outlives
its request, :mod:`kanao.serve.backends` the live Reader, Sender and Watcher
wiring, and :mod:`kanao.serve.server` the socket. What a caller imports is
re-exported here, so ``from kanao import serve`` still reaches all of it.
"""

from __future__ import annotations

from .backends import (
    SharedReads,
    make_live_attachments_provider,
    make_live_file_provider,
    make_live_notice_provider,
    make_live_revisions_provider,
    make_live_context_provider,
    make_live_room_opener,
    make_live_digest_provider,
    make_live_rooms_provider,
    make_live_send,
    make_live_upload,
    make_live_watcher_factory,
)
from .config import (
    DEFAULT_HOST,
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    MAX_BODY_BYTES,
    MAX_HIGHLIGHTS,
    MAX_LIMIT,
    MAX_QUEUED_SENDS,
    MAX_STREAMS,
    MAX_UPLOAD_BYTES,
    MIN_TOKEN_LENGTH,
    validate_min_interval,
    validate_poll_interval,
    validate_token,
)
from .handler import build_handler
from .server import QuietDisconnects, ServeHTTPServer, run

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_PORT",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_HIGHLIGHTS",
    "MAX_LIMIT",
    "MAX_QUEUED_SENDS",
    "MAX_STREAMS",
    "MAX_UPLOAD_BYTES",
    "MIN_TOKEN_LENGTH",
    "QuietDisconnects",
    "ServeHTTPServer",
    "SharedReads",
    "build_handler",
    "make_live_attachments_provider",
    "make_live_file_provider",
    "make_live_notice_provider",
    "make_live_revisions_provider",
    "make_live_context_provider",
    "make_live_room_opener",
    "make_live_digest_provider",
    "make_live_rooms_provider",
    "make_live_send",
    "make_live_upload",
    "make_live_watcher_factory",
    "run",
    "validate_min_interval",
    "validate_poll_interval",
    "validate_token",
]
