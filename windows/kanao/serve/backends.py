"""What the routes are wired to on a host that is actually running KakaoTalk.

The HTTP layer knows nothing about a heap scan, a window handle, or a name
ledger; it knows it was handed callables. These are those callables, so the
platform-specific half of the server is one import away from the rest of it.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..errors import ErrorCode, KanaoError
from .config import UPLOAD_KEEP_SECONDS


def make_live_rooms_provider(reader_factory) -> Callable[[], list[Any]]:
    """Return the rooms a live server lists on each ``GET /rooms``."""

    def provider() -> list[Any]:
        with reader_factory() as reader:
            return list(reader.rooms())

    return provider


def make_live_context_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return the context a live server reads on each ``GET /context``.

    The reader is new per request, so revealing covered messages for one
    caller cannot leak into another's answer.
    """

    def provider(room_id: int, limit: int, *, show_covered: bool = False) -> dict[str, Any]:
        from ..context import read_context

        with reader_factory() as reader:
            reader.reveal_covered = show_covered
            answer = read_context(reader, room_id, limit=limit)
        return {key: value for key, value in answer.items() if key != "ok"}

    return provider


def make_live_room_opener(reader_factory) -> Callable[[int], None]:
    """Return what ``open=true`` calls: have the client open a room, wait for its key.

    The same path as ``messages --open``. Nothing is kept between calls: the
    rescan that finds the new key records its addresses, and the next reader
    the read routes build picks them up from there.
    """

    def opener(room_id: int) -> None:
        from ..opening import open_for_reading

        with reader_factory() as reader:
            room = reader.room(room_id)
            if room is None:
                raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"no room {room_id} in the catalog")
            if room.readable:
                return
            open_for_reading(reader, room)

    return opener


def _live_writer() -> Callable[..., dict[str, Any]]:
    """Build what the server's writes, text and files, go through on a Windows host.

    Each call opens a fresh reader, resolves the room exactly like the ``send``
    command, refuses duplicate catalog titles, and hands the room to the real
    sender — but the snapshot cache and the key ring outlive the request.

    Without that, every ``POST /send`` decrypted the whole catalog and scanned
    the client's heap again before a single character reached the input box,
    which is the rule ``AGENTS.md`` states as *a send must not re-read the
    catalog*: ``reader.rooms()`` measured 3.0 s against a stale snapshot, and
    sending is exactly what makes it stale. Sharing the cache alone is not
    enough — finding the account again is most of a second by itself.

    Freshness is not what is given up.  ``SnapshotCache.connect`` re-reads the
    fingerprint on every call and lays the log over cached pages when only the
    log moved, so a shared cache is the mechanism working rather than being
    bypassed; and the reader itself is still new each time, so nothing it
    memoises about names or columns carries into the next request.

    Only the send path shares these.  ``SnapshotCache`` takes no locks, and
    sends run one at a time under the handler's send lock; the read routes run
    concurrently, so they keep a cache per request.
    """
    from ..names import NameLedger
    from ..reader import Reader
    from ..snapshot import SnapshotCache

    snapshots = SnapshotCache()
    account: dict[str, Any] = {}
    """The key ring the last send found, kept for the next one.

    It is the reader that finds it, on the first send rather than at startup:
    scanning when the socket is bound would turn a closed client into a server
    that does not come up, instead of one that comes up and says so.
    """

    def write(room: str, act: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        from ..context import _resolve
        from ..model import SELF_CHAT
        from ..opening import search_rows
        from ..winauto import make_sender

        with Reader(
            cache=snapshots, keyring=account.get("keyring"), ledger=NameLedger()
        ) as reader:
            # Reading the property is what scans, and the next line would have
            # scanned anyway; taking it here is where the answer gets kept.
            account["keyring"] = reader.keyring
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
                result = act(
                    make_sender(),
                    resolved.title,
                    aliases=window_aliases,
                    hint=position,
                    # The client's search lists rooms that merely have a
                    # member by that name above the room itself, so the row
                    # to open is predicted from the catalog just read.
                    search_rows=search_rows(reader, resolved, window_aliases, catalogued),
                    self_chat=resolved.type == SELF_CHAT,
                )
            except OSError as error:
                raise KanaoError(ErrorCode.SEND_FAILED, str(error)) from error
            return {**result, "roomId": str(resolved.id)}

    return write


def make_live_send() -> Callable[..., dict[str, Any]]:
    """Build the send callable the server uses on a Windows host."""
    write = _live_writer()

    def send(
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ) -> dict[str, Any]:
        return write(
            room,
            lambda sender, title, **where: sender.send(
                title, text, dry_run=dry_run, open_if_needed=open_if_needed, **where
            ),
        )

    return send


def make_live_upload() -> Callable[..., dict[str, Any]]:
    """Build the file-send callable the server uses on a Windows host.

    The bytes are written once under the name the room will see, in a folder
    of their own, and the client is pointed at that path the way a person
    points it at a file. The copy outlives the request because the client
    uploads from it after its confirmation has closed.
    """
    write = _live_writer()

    def upload(
        room: str, name: str, data: bytes, open_if_needed: bool = False
    ) -> dict[str, Any]:
        path = stage_upload(_upload_folder(), name, data, now=time.time())
        return write(
            room,
            lambda sender, title, **where: sender.upload(
                title, str(path), open_if_needed=open_if_needed, **where
            ),
        )

    return upload


def _upload_folder() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "kanao-cli" / "uploads"


def stage_upload(folder: Path, name: str, data: bytes, *, now: float) -> Path:
    """Write *data* as *name* in a fresh folder under *folder*, sweeping old ones."""
    sweep_uploads(folder, now=now)
    target = folder / secrets.token_hex(8)
    target.mkdir(parents=True)
    path = target / name
    path.write_bytes(data)
    return path


def sweep_uploads(folder: Path, *, now: float) -> None:
    """Remove staged copies older than UPLOAD_KEEP_SECONDS.

    A copy the client still holds open cannot be removed on Windows; it is
    left for the next sweep rather than failing the upload that found it.
    """
    if not folder.is_dir():
        return
    for entry in folder.iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > UPLOAD_KEEP_SECONDS:
                shutil.rmtree(entry)
        except OSError:
            continue


def make_live_digest_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return the digest a live server builds on each ``GET /digest``.

    The digest is ``kanao.digest.build``, the same call the command makes, so
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
        raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"no room {room_id} in the catalog")
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

    def provider(
        room_id: int, kind: Any, limit: int, before_id: Any
    ) -> dict[str, Any]:
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


def make_live_watcher_factory(reader_factory, interval: float) -> Callable[[], Any]:
    """Return one ``watch.Watcher`` per SSE connection on a live server."""

    class _LiveWatcher:
        """Own the reader alongside the watcher so disconnect closes both."""

        def __init__(self) -> None:
            self._reader = reader_factory()
            from ..watch import Watcher

            self._watcher = Watcher(self._reader, interval=interval)

        def prime(self) -> None:
            self._watcher.prime()

        def resume(self, cursor) -> None:
            self._watcher.resume(cursor)

        def poll(self):
            return self._watcher.poll()

        def close(self) -> None:
            try:
                self._reader.close()
            except Exception:
                pass

    def factory() -> Any:
        return _LiveWatcher()

    return factory
