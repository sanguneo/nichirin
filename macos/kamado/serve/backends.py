"""What the routes are wired to on a host that is actually running KakaoTalk.

The HTTP layer knows nothing about a heap scan, a window handle, or a name
ledger; it knows it was handed callables. These are those callables, so the
platform-specific half of the server is one import away from the rest of it.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from typing import Any, Callable, ContextManager, Iterator, Optional

from ..errors import ErrorCode, KamadoError


class SharedReads:
    """One snapshot cache and one account for every read the server answers.

    A reader built with neither decrypts the whole database on each request.
    On a measured 880 MB account that was minutes per ``GET /rooms`` once a
    few requests overlapped, each holding its own decrypted copy, until the
    read slots ran out and every later read was refused BUSY. Sharing the
    cache makes the first read pay for the decryption and every later one a
    stat, or a log overlay when only the log moved.

    ``SnapshotCache`` takes no lock, so every use of the shared one happens
    under :attr:`lock`. That is also what makes concurrent requests share the
    work: the first one decrypts while the rest wait on the lock, then find
    the snapshot already cached. Refreshing outside the lock was tried and
    kept several stale images alive at once, so it stays inside.
    """

    def __init__(self) -> None:
        from ..snapshot import SnapshotCache

        self.lock = threading.RLock()
        self._snapshots = SnapshotCache()
        self._account: dict[str, Any] = {}

    def open_reader(self) -> Any:
        """A fresh reader on the shared cache; use it only under :attr:`lock`.

        Like the reads it replaces it records no names: the ledger stays the
        send line's. The account is resolved on the first read, not at startup.
        """
        from ..reader import Reader

        if "keyring" not in self._account:
            from ..keyring import Keyring

            self._account["keyring"] = Keyring.discover()
        return Reader(cache=self._snapshots, keyring=self._account["keyring"])

    @contextlib.contextmanager
    def reading(self) -> Iterator[Any]:
        """Hold the line for one read; the reader itself is fresh each time."""
        with self.lock:
            reader = self.open_reader()
            try:
                yield reader
            finally:
                reader.close()

    def warm(self) -> None:
        """Decrypt once so the first request does not wait for it.

        Failure is only reported: an install that cannot read yet should still
        serve ``/health`` and explain itself on the request that needs it.
        """
        try:
            with self.reading() as reader:
                reader.rooms()
        except Exception as error:  # noqa: BLE001 - the request path retries
            print(
                f"warning: warming the snapshot failed: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )

    def warm_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.warm, name="kamado-warm", daemon=True)
        thread.start()
        return thread


def make_live_rooms_provider(reader_factory) -> Callable[[], list[Any]]:
    """Return the rooms a live server lists on each ``GET /rooms``."""

    def provider() -> list[Any]:
        with reader_factory() as reader:
            return list(reader.rooms())

    return provider


def make_live_context_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return the context a live server reads on each ``GET /context``."""

    def provider(room_id: int, limit: int, *, show_covered: bool = False) -> dict[str, Any]:
        from ..context import read_context

        with reader_factory() as reader:
            reader.reveal_covered = show_covered
            answer = read_context(reader, room_id, limit=limit)
        return {key: value for key, value in answer.items() if key != "ok"}

    return provider


def make_live_account_reader() -> Callable[[], Any]:
    """Open readers that share one snapshot cache and one account.

    For the send line only: ``SnapshotCache`` takes no lock, and texts and
    files are both handed over under the handler's ``send_lock``. See
    :func:`make_live_send` for what sharing them buys.
    """
    from ..names import NameLedger
    from ..reader import Reader
    from ..snapshot import SnapshotCache

    snapshots = SnapshotCache()
    account: dict[str, Any] = {}

    def keyring():
        """Resolve the account once, on the first send rather than at startup.

        Doing it when the server is built would make an unreadable install a
        server that refuses to start, instead of one that explains itself.
        """
        if "keyring" not in account:
            from ..keyring import Keyring

            account["keyring"] = Keyring.discover()
        return account["keyring"]

    def open_reader():
        return Reader(cache=snapshots, keyring=keyring(), ledger=NameLedger())

    return open_reader


def _send_target(reader, room: str):
    """Resolve *room* for a send: the room, its window aliases, its list hint."""
    from ..context import _resolve

    # cli._selected_room is this line plus an argparse namespace built
    # to carry two values; importing cli from here is a cycle, and the
    # cycle is the only thing that namespace was for.
    #
    # openapi.json calls `room` "the room's title, or its id as a
    # string", and a JSON body carries both as a string, while
    # `_resolve` takes the id path for an int alone. A client sending
    # back the id `/rooms` gave it was answered CHAT_NOT_FOUND. The id
    # is tried first and only when it names a room, so a room whose
    # title is a number still resolves by title.
    resolved = None
    if room.strip().isdigit():
        resolved = reader.room(int(room.strip()))
    if resolved is None:
        resolved = _resolve(reader, room)
    catalogued = reader.rooms()
    duplicates = [
        r for r in catalogued if r.title.casefold() == resolved.title.casefold()
    ]
    if len(duplicates) != 1:
        raise KamadoError(
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
    return resolved, window_aliases, position


def make_live_upload(open_reader: Callable[[], Any]) -> Callable[..., dict[str, Any]]:
    """Build the upload callable: the send's room resolution, then the file."""

    def upload(room: str, path: str, name: str, open_if_needed: bool = False) -> dict[str, Any]:
        from ..macupload import make_uploader

        with open_reader() as reader:
            resolved, window_aliases, position = _send_target(reader, room)
            try:
                result = make_uploader().upload(
                    resolved.title,
                    path,
                    name,
                    open_if_needed=open_if_needed,
                    aliases=window_aliases,
                    hint=position,
                )
            except OSError as error:
                raise KamadoError(
                    ErrorCode.SEND_FAILED, f"{error}; nothing was sent"
                ) from error
            return {**result, "roomId": str(resolved.id)}

    return upload


def make_live_send(
    open_reader: Optional[Callable[[], Any]] = None,
) -> Callable[..., dict[str, Any]]:
    """Build the send callable the server uses on a macOS host.

    Each call opens a fresh reader (so nothing a reader remembers on itself --
    names, columns -- survives into the next request), resolves the room
    exactly like the ``send`` command, refuses duplicate catalog titles, and
    hands the text to the real sender.

    The **snapshot cache and the account outlive the request**, and that is
    what makes a send quick. A reader built with neither decrypts the whole
    119 MB database and rediscovers the account every time: measured 2.56 s per
    request, every request, before anything was typed. Sharing both takes the
    second and later sends to **0.023 s** -- and it costs no freshness, because
    the cache re-reads the fingerprint on every ``connect`` and lays the log
    over the cached pages when only the log moved. Sharing the cache alone is
    not enough: rediscovering the account is itself a second of the cost.

    Sharing is safe here and nowhere else in this package: ``SnapshotCache``
    takes no lock, and every call to this function is made under the handler's
    ``send_lock``, so exactly one thread is ever inside it. The read paths
    share a different cache under a lock of their own (:class:`SharedReads`).
    """
    if open_reader is None:
        open_reader = make_live_account_reader()

    def send(
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ) -> dict[str, Any]:
        from ..macauto import make_sender

        with open_reader() as reader:
            resolved, window_aliases, position = _send_target(reader, room)
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
                raise KamadoError(ErrorCode.SEND_FAILED, str(error)) from error
            return {**result, "roomId": str(resolved.id)}

    return send


def make_live_digest_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return the digest a live server builds on each ``GET /digest``.

    The digest is ``kamado.digest.build``, the same call the command makes, so
    a remote caller and a local one are reading one implementation rather than
    two. Reaching into ``cli`` for it used to mean assembling an
    ``argparse.Namespace`` here and pointing this package at the layer above
    it.
    """

    def provider(
        room_id: int, span: str, limit: int, highlights: int, *, show_covered: bool = False
    ) -> dict[str, Any]:
        from ..digest import build

        with reader_factory() as reader:
            reader.reveal_covered = show_covered
            return build(
                reader, room_id, span=span, limit=limit, highlights=highlights
            )

    return provider


def _room_of(reader, room_id: int):
    room = reader.room(room_id)
    if room is None:
        raise KamadoError(ErrorCode.CHAT_NOT_FOUND, f"no room {room_id} in the catalog")
    return room


def make_live_notice_provider(reader_factory) -> Callable[[int], dict[str, Any]]:
    """Return the pinned notice a live server reads on each ``GET /notice``."""

    def provider(room_id: int) -> dict[str, Any]:
        with reader_factory() as reader:
            room = _room_of(reader, room_id)
            pinned = reader.notice(room_id)
            return {
                "room": room.as_json(),
                "notice": None if pinned is None else pinned.as_json(),
            }

    return provider


def make_live_attachments_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return what a live server lists on each ``GET /attachments``."""

    def provider(room_id: int, kind: Any, limit: int, before_id: Any) -> dict[str, Any]:
        with reader_factory() as reader:
            room = _room_of(reader, room_id)
            found = reader.attachments(room_id, kind=kind, limit=limit, before_id=before_id)
            return {
                "room": room.as_json(),
                "attachments": [item.as_json() for item in found],
                "count": len(found),
            }

    return provider


def make_live_revisions_provider(reader_factory) -> Callable[[int, int], dict[str, Any]]:
    """Return an edited message's versions on each ``GET /revisions``."""

    def provider(room_id: int, message_id: int) -> dict[str, Any]:
        with reader_factory() as reader:
            room = _room_of(reader, room_id)
            versions = reader.revisions(room_id, message_id)
            return {
                "room": room.as_json(),
                "messageId": str(message_id),
                "revisions": [version.as_json() for version in versions],
                "count": len(versions),
            }

    return provider


def make_live_file_provider(reader_factory) -> Callable[[int, int], Any]:
    """Return what ``GET /file`` streams: the client's saved copy, or the address.

    The reader is closed before a byte is sent; the download it found is a
    file or a response, and it is the handler's to close.
    """

    def provider(room_id: int, message_id: int):
        from ..files import open_file

        with reader_factory() as reader:
            _room_of(reader, room_id)
            attachment = reader.file_at(room_id, message_id)
        return open_file(attachment)

    return provider


def make_live_watcher_factory(
    reader_factory,
    interval: float,
    lock: Optional[Callable[[], ContextManager[Any]]] = None,
) -> Callable[[], Any]:
    """Return one ``watch.Watcher`` per SSE connection on a live server.

    *lock* is entered around every step that touches the database, for a
    reader whose cache is shared with other requests (see :class:`SharedReads`).
    """
    guard: Callable[[], ContextManager[Any]] = lock if lock is not None else contextlib.nullcontext

    class _LiveWatcher:
        """Own the reader alongside the watcher so disconnect closes both."""

        def __init__(self) -> None:
            self._reader = reader_factory()
            from ..watch import Watcher

            self._watcher = Watcher(self._reader, interval=interval)

        def prime(self) -> None:
            with guard():
                self._watcher.prime()

        def resume(self, cursor) -> None:
            with guard():
                self._watcher.resume(cursor)

        def poll(self):
            with guard():
                return self._watcher.poll()

        def close(self) -> None:
            try:
                with guard():
                    self._reader.close()
            except Exception:
                pass

    def factory() -> Any:
        return _LiveWatcher()

    return factory
