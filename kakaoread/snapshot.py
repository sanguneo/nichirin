"""Turn a live encrypted database into a stable, queryable plaintext snapshot.

Three properties matter here, and they pull against each other.

*Consistency.*  KakaoTalk writes while we read.  A snapshot is accepted only if
the source is byte-for-byte what was read, and is otherwise retried rather than
returned half-old.  That comparison is deliberately exact rather than a stat
check: a rewrite of the same length inside a single file-time tick leaves
``st_mtime_ns`` identical, so a timestamp alone can miss a change and hand back
a torn database.  An appended log is caught by comparing the prefix we read.

Change *detection* for caching and following is a different question with a
different answer, and there ``os.stat`` is the right tool: a stale answer costs
one extra poll rather than a wrong result, so the cheap signal is enough.

*Cost.*  Decryption is whole-file, and the macOS client keeps one large
database (measured 119 MB) that it rewrites constantly, so reading it in place
loses the consistency race again and again.  :func:`clone_snapshot` takes an
APFS copy-on-write clone first — instant and frozen against the writer — and
the decrypted image is cached against the stat fingerprint so an unchanged
conversation costs one stat rather than one decryption.

*Exposure.*  On Python 3.11 and newer the plaintext never reaches the disk at
all, because the image is handed straight to SQLite in memory.  On 3.9 and 3.10
a temporary file is still required; it is deleted, but deletion is not secure
erasure and that limitation is documented rather than hidden.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from . import page as pages, wal as wal_format
from .cipher import Geometry
from .errors import ErrorCode, KakaoReadError

_READ_ATTEMPTS: Final = 4
_BACKOFF_SECONDS: Final = (0.05, 0.15, 0.4)
_DEFAULT_BUDGET_BYTES: Final = 256 * 1024 * 1024

_JOURNAL_MODE_OFFSET: Final = 18
_ROLLBACK_JOURNAL: Final = b"\x01\x01"


@dataclass(frozen=True)
class Fingerprint:
    """The cheap signal that a database may have changed underneath us."""

    database: tuple[int, int]
    log: tuple[int, int]

    @staticmethod
    def _stat(path: Path) -> tuple[int, int]:
        try:
            status = path.stat()
        except OSError:
            return (0, 0)
        return (status.st_mtime_ns, status.st_size)

    @classmethod
    def of(cls, database: Path) -> "Fingerprint":
        """Sample a database and its write-ahead log in one go."""
        return cls(
            database=cls._stat(database),
            log=cls._stat(database.with_name(database.name + "-wal")),
        )


def _matches(database: Path, expected: bytes) -> bool:
    """Compare a file against bytes already read, without holding a second copy."""
    position = 0
    try:
        with database.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                end = position + len(chunk)
                if end > len(expected) or expected[position:end] != chunk:
                    return False
                position = end
    except OSError:
        return False
    return position == len(expected)


def _read_log(database: Path) -> bytes:
    try:
        return database.with_name(database.name + "-wal").read_bytes()
    except FileNotFoundError:
        return b""


def clone_snapshot(database: Path, into: Path) -> Path:
    """Copy a database and its log into *into*, frozen against the live writer.

    This is the macOS answer to the one thing the Windows build never had to
    solve.  There, conversations were one file per room, small enough that the
    byte-for-byte consistency check almost always caught a clean moment.  Here
    the whole account is one file — measured at 119 MB with a live client — and
    a client that writes continuously changes it faster than it can be read, so
    the consistency check kept rejecting read after read (112 s to catch a
    clean one, once).

    APFS makes this free: ``cp -c`` asks for a copy-on-write clone, which is
    O(1) regardless of size and which the client cannot subsequently modify —
    a later write allocates new blocks and leaves the clone's blocks alone.  So
    the read runs against a still file and the consistency check passes the
    first time.  A filesystem without clone support falls back to an ordinary
    copy, which is merely fast rather than instant and is still far steadier
    than reading in place.
    """
    into.mkdir(parents=True, exist_ok=True)
    destination = into / database.name
    for source in (database, database.with_name(database.name + "-wal")):
        if not source.exists():
            continue
        target = into / source.name
        clone = subprocess.run(
            ["/bin/cp", "-c", str(source), str(target)],
            capture_output=True,
        )
        if clone.returncode != 0:
            # -c is refused off APFS; a plain copy still freezes far better
            # than reading a live file in place.
            shutil.copyfile(source, target)
    return destination


def build_image(database: Path, geometry: Geometry) -> bytes:
    """Decrypt one database plus its committed log into a standalone SQLite image.

    Raises :class:`ConsistencyError` when the source moved during the read, so
    that the caller can retry rather than trust a torn result.
    """
    page_size, reserve = geometry.page_size, geometry.reserve
    pages.validate_geometry(page_size, reserve)

    log = _read_log(database)
    encrypted = database.read_bytes()

    if not encrypted:
        raise KakaoReadError(ErrorCode.READ_FAILED, f"{database.name} is empty")
    if len(encrypted) % page_size:
        raise KakaoReadError(
            ErrorCode.READ_FAILED,
            f"{database.name} ends with a partial page ({len(encrypted) % page_size} bytes)",
        )

    base_pages = len(encrypted) // page_size
    committed = wal_format.parse(log, page_size, base_pages)
    total_pages = committed.database_pages

    missing = [
        number
        for number in range(base_pages + 1, total_pages + 1)
        if number not in committed.pages
    ]
    if missing:
        raise KakaoReadError(
            ErrorCode.READ_FAILED,
            f"the committed log for {database.name} leaves pages {missing[:4]} undefined",
        )

    image = bytearray(total_pages * page_size)
    with geometry.prepared() as prepared:
        for number in range(1, min(base_pages, total_pages) + 1):
            start = (number - 1) * page_size
            image[start : start + page_size] = pages.reconstruct_page(
                prepared, number, encrypted[start : start + page_size], page_size, reserve
            )
        for number, raw in committed.pages.items():
            if number > total_pages:
                continue  # A later commit truncated the database past this page.
            start = (number - 1) * page_size
            image[start : start + page_size] = pages.reconstruct_page(
                prepared, number, raw, page_size, reserve
            )

    # The image is a standalone file with no log beside it, so it must not
    # claim write-ahead logging mode.
    image[_JOURNAL_MODE_OFFSET : _JOURNAL_MODE_OFFSET + 2] = _ROLLBACK_JOURNAL

    if not _matches(database, encrypted) or not _read_log(database).startswith(log):
        raise ConsistencyError(f"{database.name} changed while it was being read")
    return bytes(image)


class ConsistencyError(RuntimeError):
    """The source moved mid-read; the partial result was discarded."""


def _connect(image: bytes) -> sqlite3.Connection:
    """Open an image as an in-memory database, avoiding disk where possible."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    deserialize = getattr(connection, "deserialize", None)
    if deserialize is not None:
        try:
            deserialize(image)
            return connection
        except BaseException:
            connection.close()
            raise
    connection.close()

    # Python 3.10 has no deserialize, so the image has to reach SQLite as a file.
    handle, name = tempfile.mkstemp(suffix=".sqlite3")
    try:
        with os.fdopen(handle, "wb") as plaintext:
            plaintext.write(image)
        on_disk = sqlite3.connect(f"{Path(name).as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            memory = sqlite3.connect(":memory:", check_same_thread=False)
            on_disk.backup(memory)
            return memory
        finally:
            on_disk.close()
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def open_image(image: bytes, label: str) -> sqlite3.Connection:
    """Open a decrypted image and refuse it unless SQLite reports it intact.

    A wrong key produces plausible-looking bytes rather than an error, so this
    check is what separates "decrypted" from "decrypted correctly".
    """
    connection = _connect(image)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise KakaoReadError(
                ErrorCode.READ_FAILED, f"{label} did not decrypt to a valid database"
            )
    except BaseException:
        connection.close()
        raise
    return connection


class SnapshotCache:
    """Decrypted databases, reused until their files change.

    The cache owns every connection it returns; callers read and must not
    close them.  Eviction is least-recently-used against a byte budget.
    """

    def __init__(self, budget_bytes: int = _DEFAULT_BUDGET_BYTES) -> None:
        self._budget = budget_bytes
        self._entries: OrderedDict[Path, tuple[Fingerprint, sqlite3.Connection, int]] = (
            OrderedDict()
        )
        self._bytes = 0

    def fingerprint(self, database: Path) -> Fingerprint:
        """Sample a database without decrypting anything."""
        return Fingerprint.of(database)

    def unchanged(self, database: Path, since: Fingerprint) -> bool:
        """Whether a database still looks exactly as it did at *since*."""
        return Fingerprint.of(database) == since

    def connect(self, database: Path, geometry: Geometry) -> sqlite3.Connection:
        """Return a queryable snapshot, decrypting only when the source moved."""
        current = Fingerprint.of(database)
        cached = self._entries.get(database)
        if cached is not None and cached[0] == current:
            self._entries.move_to_end(database)
            return cached[1]

        image = self._decrypt_with_retry(database, geometry)
        connection = open_image(image, database.name)
        self._store(database, Fingerprint.of(database), connection, len(image))
        return connection

    def invalidate(self, database: Path) -> None:
        """Drop one cached snapshot, closing it."""
        entry = self._entries.pop(database, None)
        if entry is not None:
            self._bytes -= entry[2]
            entry[1].close()

    def close(self) -> None:
        """Close every cached snapshot."""
        for _fingerprint, connection, _size in self._entries.values():
            connection.close()
        self._entries.clear()
        self._bytes = 0

    def __enter__(self) -> "SnapshotCache":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def _decrypt_with_retry(self, database: Path, geometry: Geometry) -> bytes:
        last: Optional[ConsistencyError] = None
        for attempt in range(_READ_ATTEMPTS):
            staging = Path(tempfile.mkdtemp(prefix="kakaoread-"))
            try:
                # Read a copy-on-write clone rather than the live file: the
                # account is one large database a running client rewrites
                # faster than it can be read in place, and the clone is frozen
                # the instant it is taken. See clone_snapshot.
                frozen = clone_snapshot(database, staging)
                return build_image(frozen, geometry)
            except ConsistencyError as error:
                last = error
                if attempt < len(_BACKOFF_SECONDS):
                    time.sleep(_BACKOFF_SECONDS[attempt])
            except FileNotFoundError as error:
                raise KakaoReadError(
                    ErrorCode.READ_FAILED, f"{database.name} no longer exists"
                ) from error
            except wal_format.WalFormatError as error:
                raise KakaoReadError(
                    ErrorCode.READ_FAILED, f"{database.name}: {error}"
                ) from error
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        raise KakaoReadError(
            ErrorCode.BUSY,
            f"KakaoTalk kept writing {database.name} during "
            f"{_READ_ATTEMPTS} read attempts; try again in a moment",
            database=database.name,
        ) from last

    def _store(
        self, database: Path, fingerprint: Fingerprint, connection: sqlite3.Connection, size: int
    ) -> None:
        previous = self._entries.pop(database, None)
        if previous is not None:
            self._bytes -= previous[2]
            previous[1].close()
        self._entries[database] = (fingerprint, connection, size)
        self._bytes += size
        while self._bytes > self._budget and len(self._entries) > 1:
            _evicted, (_fingerprint, stale, stale_size) = self._entries.popitem(last=False)
            self._bytes -= stale_size
            stale.close()
