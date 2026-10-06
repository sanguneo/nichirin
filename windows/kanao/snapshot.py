"""Turn a live encrypted database into a stable, queryable plaintext snapshot.

Three properties matter here, and they pull against each other.

*Consistency.*  KakaoTalk writes while we read.  A snapshot is accepted only if
the source is byte-for-byte what was read, and is otherwise retried rather than
returned half-old.  That comparison is deliberately exact rather than a stat
check: a rewrite of the same length inside a single file-time tick leaves
``st_mtime_ns`` identical, so a timestamp alone can miss a change and hand back
a torn database.  An appended log is caught by comparing the prefix we read.

Change *detection* for caching and following is a different question with a
different answer, and there ``os.stat`` is the right tool: it is what actually
observes a live SQLite writer on Windows, where directory change notifications
(``ReadDirectoryChangesW``, and therefore C#'s ``FileSystemWatcher``) reported
nothing at all across a 30-second observation of the running client, because
NTFS defers directory-entry updates while SQLite holds the log handle open.  A
stale answer there costs one extra poll rather than a wrong result.

*Cost.*  Decryption is whole-file, so it is the expensive operation in this
program.  A snapshot is therefore cached against that same stat fingerprint:
repeat reads of an unchanged conversation cost one stat instead of one
decryption, which is what makes following a conversation affordable.

A conversation that *did* change is the case that used to cost the most and
did not have to.  A new message appends committed frames to ``-wal`` and leaves
the ``.edb`` file untouched, so the cache keeps the decrypted base pages beside
the snapshot and lays only the log over them when the fingerprint says the base
file stood still.  That turns an arrival from a whole-database decryption into
a few dozen pages.  The overlay is given up on rather than defended: a log that
moved again, or one that describes a database these pages cannot assemble, just
falls back to a full decryption.

*Exposure.*  With SQLite's deserialize support (normally Python 3.11+) the
plaintext never reaches disk. Otherwise a temporary file is required and
removed after use. On Windows, abrupt termination can leave a tagged file
until the next fallback, which removes only verified same-user orphans. Legacy
untagged files and unverifiable candidates are left alone. Deletion is not
secure erasure; upgrading Python avoids this plaintext disk exposure.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import tempfile
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional, Protocol

from . import PAGE_SIZE, RESERVE_SIZE, page as pages, wal as wal_format
from .cng import PreparedKey
from .errors import ErrorCode, KanaoError

_READ_ATTEMPTS: Final = 4
_BACKOFF_SECONDS: Final = (0.05, 0.15, 0.4)
_DEFAULT_BUDGET_BYTES: Final = 256 * 1024 * 1024

_BUDGET_HEADROOM: Final = 3
"""How many copies of the largest image a cache without a stated budget holds.

One entry costs roughly two: the snapshot, and the base pages kept so a log
that moved can be laid over them instead of decrypting the file again. The
third leaves room for the other databases a reader opens beside it. A fixed
256 MiB therefore starts shedding base pages at a database of about 128 MiB,
and this account already holds a 73.0 MB room beside a 56.6 MB one — two
entries, 259 MB charged, over the fixed budget before the catalog is counted.
This bounds what the cache retains, not what the process allocates.
"""

_MEMORY_SHARE: Final = 0.25
"""The share of physical memory a derived budget may claim.

Following the database is only safe while the machine can hold it, so the
scaling is capped rather than allowed to grow with the file. On a machine too
small for the cap the base pages are shed exactly as they were before, which
is the behaviour the cap is there to keep.
"""

_physical_memory: "list[Optional[int]]" = []
"""Memoised answer from :func:`physical_memory_bytes`; empty until first asked."""

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


@dataclass(frozen=True)
class BasePages:
    """The decrypted ``.edb`` file, and the proof it is still complete.

    *generation* is the log header these pages were decrypted against, which
    carries the salt pair identifying the current run of the log.  It is what
    licenses reusing them, and a stat could not do the job: a rewrite of equal
    length inside one file-time tick leaves ``st_mtime_ns`` identical.

    The argument is short.  In write-ahead logging mode the only thing that
    ever writes the database file is a checkpoint, and a checkpoint copies
    frames the log still holds.  So while the log runs in the same generation,
    every page a checkpoint may have written underneath us is a page the log
    overlays anyway, and the assembled image is the same either way.  A
    checkpoint that *restarts* the log is the case that would strand pages in
    a file we are no longer reading — and restarting is exactly what changes
    the salt in this header.
    """

    image: bytes
    generation: bytes

    def __len__(self) -> int:
        return len(self.image) + len(self.generation)


class DecryptedImage(bytes):
    """A decrypted image that also carries the base file it was assembled from.

    The log is what moves when a message arrives, so keeping the base pages
    lets the next read overlay the log alone instead of decrypting the file
    again.  It is an attribute rather than a second return value because these
    images travel through code that only wants the bytes.
    """

    base: Optional[BasePages] = None


def _undefined_pages(committed: "wal_format.WalSnapshot", base_pages: int) -> list[int]:
    """Pages the log grew the database to but never wrote."""
    return [
        number
        for number in range(base_pages + 1, committed.database_pages + 1)
        if number not in committed.pages
    ]


def _overlay_log(
    base_image: bytes,
    committed: "wal_format.WalSnapshot",
    prepared: PreparedKey,
    page_size: int,
    reserve: int,
) -> bytearray:
    """Lay the committed log over decrypted base pages, growing or truncating."""
    base_pages = len(base_image) // page_size
    total_pages = committed.database_pages
    if total_pages < base_pages:
        image = bytearray(base_image[: total_pages * page_size])
    else:
        image = bytearray(base_image)
        image.extend(bytes((total_pages - base_pages) * page_size))

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
    return image


def build_image(
    database: Path,
    key: bytes,
    page_size: int = PAGE_SIZE,
    reserve: int = RESERVE_SIZE,
) -> DecryptedImage:
    """Decrypt one database plus its committed log into a standalone SQLite image.

    Raises :class:`ConsistencyError` when the source moved during the read, so
    that the caller can retry rather than trust a torn result.
    """
    pages.validate_geometry(page_size, reserve)

    log = _read_log(database)
    encrypted = database.read_bytes()

    if not encrypted:
        raise KanaoError(ErrorCode.READ_FAILED, f"{database.name} is empty")
    if len(encrypted) % page_size:
        raise KanaoError(
            ErrorCode.READ_FAILED,
            f"{database.name} ends with a partial page ({len(encrypted) % page_size} bytes)",
        )

    base_pages = len(encrypted) // page_size
    committed = wal_format.parse(log, page_size, base_pages)

    missing = _undefined_pages(committed, base_pages)
    if missing:
        raise KanaoError(
            ErrorCode.READ_FAILED,
            f"the committed log for {database.name} leaves pages {missing[:4]} undefined",
        )

    base_image = bytearray(len(encrypted))
    with PreparedKey(key) as prepared:
        for number in range(1, base_pages + 1):
            start = (number - 1) * page_size
            base_image[start : start + page_size] = pages.reconstruct_page(
                prepared, number, encrypted[start : start + page_size], page_size, reserve
            )
        image = _overlay_log(bytes(base_image), committed, prepared, page_size, reserve)

    if not _matches(database, encrypted) or not _read_log(database).startswith(log):
        raise ConsistencyError(f"{database.name} changed while it was being read")

    decrypted = DecryptedImage(image)
    decrypted.base = BasePages(bytes(base_image), log[: wal_format.WAL_HEADER_SIZE])
    return decrypted


def update_image_with_wal(
    base: BasePages,
    database: Path,
    key: bytes,
    page_size: int = PAGE_SIZE,
    reserve: int = RESERVE_SIZE,
) -> bytes:
    """Lay a freshly read log over base pages that were decrypted earlier.

    Only the log is read, which is the whole point: a message that arrives
    appends committed frames and leaves the database file alone, so a few
    dozen pages are decrypted instead of every page in the file.  Anything
    saying these base pages no longer describe this database — a log that
    started a new generation, a log that moved again mid-read, a page the log
    grew the database to but never wrote — raises :class:`ConsistencyError`,
    because the honest answer then is to decrypt the file properly rather than
    to assemble something plausible out of it.
    """
    pages.validate_geometry(page_size, reserve)

    log = _read_log(database)
    if log[: wal_format.WAL_HEADER_SIZE] != base.generation:
        raise LogRestarted(f"the {database.name} log restarted since it was read")

    base_pages = len(base.image) // page_size
    committed = wal_format.parse(log, page_size, base_pages)

    missing = _undefined_pages(committed, base_pages)
    if missing:
        raise ConsistencyError(
            f"the committed log for {database.name} leaves pages {missing[:4]} undefined"
        )

    with PreparedKey(key) as prepared:
        image = _overlay_log(base.image, committed, prepared, page_size, reserve)

    if not _read_log(database).startswith(log):
        raise ConsistencyError(f"{database.name} changed while its log was being read")
    return bytes(image)


class ConsistencyError(RuntimeError):
    """The source moved mid-read; the partial result was discarded."""


class LogRestarted(ConsistencyError):
    """The log began a new generation, so the cached base pages no longer apply.

    Separate from its parent because retrying cannot help: backing off does
    not put the stranded pages back into a log that has moved past them, so
    this one goes straight to a full decryption instead of waiting 0.60 s in
    ``_BACKOFF_SECONDS`` first. The name and the rule are the macOS build's.
    """


def _connect(image: bytes) -> sqlite3.Connection:
    """Open an image as an in-memory database, avoiding disk where possible.

    ``check_same_thread=False`` because a cached snapshot outlives the thread
    that decrypted it: the server hands one cache to the send path, and every
    request arrives on a thread of its own, so the second send reads a
    connection the first one opened. sqlite3 refuses that by default, and the
    refusal arrives as a request that dies without an answer — which is how it
    was found, by sending twice against a live client rather than by any test.
    Nothing here is concurrent: the send lock admits one send at a time, the
    read routes never share a cache, and these images are read-only snapshots
    of bytes already in memory.
    """
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
    if hasattr(ctypes, "WinDLL"):
        # Imported here: it reads ctypes.wintypes, which raises off Windows.
        from ._snapshot_temp import create as create_plaintext

        handle, name = create_plaintext()
    else:
        handle, name = tempfile.mkstemp(suffix=".sqlite3")
    try:
        with os.fdopen(handle, "wb") as plaintext:
            plaintext.write(image)
        on_disk = sqlite3.connect(f"{Path(name).as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            memory = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                on_disk.backup(memory)
                return memory
            except BaseException:
                memory.close()
                raise
        finally:
            on_disk.close()
    finally:
        try:
            os.unlink(name)
        except OSError as error:
            warnings.warn(
                f"Could not remove plaintext snapshot {name}: {error}", RuntimeWarning,
            )


def open_image(image: bytes, label: str) -> sqlite3.Connection:
    """Open a decrypted image and refuse it unless SQLite reports it intact.

    A wrong key produces plausible-looking bytes rather than an error, so this
    check is what separates "decrypted" from "decrypted correctly".

    ``quick_check`` rather than ``integrity_check``: both walk every page and
    every B-tree, which is what a wrong key fails, but the full check also
    verifies index sort order against the rows it indexes, and that is what
    made the check cost more than the decryption it was guarding.
    """
    connection = _connect(image)
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise KanaoError(
                ErrorCode.READ_FAILED, f"{label} did not decrypt to a valid database"
            )
    except BaseException:
        connection.close()
        raise
    return connection


def _read_physical_memory() -> Optional[int]:
    """Ask the OS how much RAM this machine has, or give up quietly.

    ``GlobalMemoryStatusEx`` is the answer on the platform this tool runs on,
    and it costs one call into a library already loaded.  ``sysconf`` is what
    the suite reaches on the macOS and Linux hosts the shared code is written
    from, so the scaling is exercised there rather than silently falling back.
    A machine that answers neither is treated as unknown instead of guessed
    at, and an unknown machine keeps the fixed budget it always had.

    ``ctypes.wintypes`` is deliberately not imported: reading it off Windows
    raises, and it would take this module — and everything that imports it —
    down with it. The three fields needed are plain C types.
    """
    library = getattr(ctypes, "WinDLL", None)
    if library is not None:
        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        try:
            if not library("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
        except (OSError, AttributeError):
            return None
        total = int(status.ullTotalPhys)
        return total if total > 0 else None

    try:
        page_count = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if page_count > 0 and page_size > 0:
        return page_count * page_size
    return None


def physical_memory_bytes() -> Optional[int]:
    """How much RAM this machine has, asked once and remembered.

    Lazy on purpose: a cache is built before anyone knows whether it will be
    used, and nothing expensive belongs in a constructor.
    """
    if not _physical_memory:
        _physical_memory.append(_read_physical_memory())
    return _physical_memory[0]


def _adaptive_budget(largest_image: int) -> int:
    """The budget for a cache that was not given one.

    ``_BUDGET_HEADROOM`` copies of the largest image being held, with the
    fixed default as a floor unless the memory cap is lower. A machine that
    will not say how much memory it has gets the fixed default, which is what
    it got before this scaled at all.
    """
    memory = physical_memory_bytes()
    if memory is None:
        return _DEFAULT_BUDGET_BYTES
    wanted = max(_DEFAULT_BUDGET_BYTES, largest_image * _BUDGET_HEADROOM)
    return min(wanted, int(memory * _MEMORY_SHARE))


class SnapshotSource(Protocol):
    """What a reader needs from a cache: a connection, and a way to let go.

    :class:`Reader` takes one of these rather than a :class:`SnapshotCache`
    because both real substitutions are worth being able to state. The server
    hands the send path a cache that outlives the request, and the suite hands
    a stub that serves prepared plaintext; naming the two methods a reader
    actually calls says so, where the concrete class would have made the stub
    a lie about the type.
    """

    def connect(
        self,
        database: Path,
        key: bytes,
        page_size: int = PAGE_SIZE,
        reserve: int = RESERVE_SIZE,
    ) -> sqlite3.Connection:
        ...

    def close(self) -> None:
        ...


@dataclass
class _CachedSnapshot:
    """One decrypted database, and the base pages the next read can reuse."""

    fingerprint: Fingerprint
    connection: sqlite3.Connection
    size: int
    base: Optional[BasePages]
    image_bytes: int = 0
    """The snapshot alone, which is what a derived budget scales against.

    ``size`` is what the entry is charged and it shrinks when the base pages
    are shed, so it cannot stand in for the size of the database being read.
    """


class SnapshotCache:
    """Decrypted databases, reused until their files change.

    The cache owns every connection it returns; callers read and must not
    close them.  Eviction is least-recently-used against a byte budget, which
    counts the base pages an entry keeps as well as the snapshot itself.

    *budget_bytes* is honoured exactly as given.  Left out, the budget follows
    the largest database the cache is actually holding rather than a constant
    chosen against a smaller account; see :func:`_adaptive_budget`.
    """

    def __init__(self, budget_bytes: Optional[int] = None) -> None:
        self._stated_budget = budget_bytes
        self._entries: OrderedDict[Path, _CachedSnapshot] = OrderedDict()
        self._bytes = 0

    @property
    def _budget(self) -> int:
        """What this cache may hold right now.

        A stated budget is returned untouched. Otherwise it is derived on each
        use rather than at construction, because the size it scales with is
        not known until a database has actually been read.
        """
        if self._stated_budget is not None:
            return self._stated_budget
        largest = max(
            (entry.image_bytes for entry in self._entries.values()), default=0
        )
        return _adaptive_budget(largest)

    def fingerprint(self, database: Path) -> Fingerprint:
        """Sample a database without decrypting anything."""
        return Fingerprint.of(database)

    def unchanged(self, database: Path, since: Fingerprint) -> bool:
        """Whether a database still looks exactly as it did at *since*."""
        return Fingerprint.of(database) == since

    def connect(
        self,
        database: Path,
        key: bytes,
        page_size: int = PAGE_SIZE,
        reserve: int = RESERVE_SIZE,
    ) -> sqlite3.Connection:
        """Return a queryable snapshot, decrypting only what actually moved.

        Three answers, cheapest first: nothing changed, so the cached snapshot
        stands; only the log changed, so its frames are laid over the base
        pages already decrypted; or the database file itself changed, which is
        the only case that pays for a whole decryption.
        """
        # Sampled once, before anything is read, and that is the fingerprint
        # the result is stored under. Sampled again afterwards, a commit that
        # landed during the read would be stored beside an image that does
        # not hold it, and the next call would find the files matching and
        # hand the older image back for as long as the room stayed quiet. A
        # commit that did make it into the read costs one extra read instead.
        current = Fingerprint.of(database)
        cached = self._entries.get(database)
        if cached is not None and cached.fingerprint == current:
            self._entries.move_to_end(database)
            return cached.connection

        if (
            cached is not None
            and cached.base is not None
            and cached.fingerprint.database == current.database
        ):
            connection = self._reuse_base_pages(
                database, key, cached.base, page_size, reserve, current
            )
            if connection is not None:
                return connection

        image = self._decrypt_with_retry(database, key, page_size, reserve)
        connection = open_image(image, database.name)
        self._store(
            database,
            current,
            connection,
            len(image),
            getattr(image, "base", None),
        )
        return connection

    def invalidate(self, database: Path) -> None:
        """Drop one cached snapshot, closing it."""
        entry = self._entries.pop(database, None)
        if entry is not None:
            self._bytes -= entry.size
            entry.connection.close()

    def close(self) -> None:
        """Close every cached snapshot."""
        for entry in self._entries.values():
            entry.connection.close()
        self._entries.clear()
        self._bytes = 0

    def __enter__(self) -> "SnapshotCache":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def _reuse_base_pages(
        self,
        database: Path,
        key: bytes,
        base: BasePages,
        page_size: int,
        reserve: int,
        sampled: Fingerprint,
    ) -> Optional[sqlite3.Connection]:
        """Try the log-only path, returning ``None`` when it must be given up on.

        *sampled* is the fingerprint taken before the log was read, and the
        only one the result may be stored under; see :meth:`connect`.

        The result is deliberately not put through :func:`open_image`.  Those
        base pages were checked when they were decrypted and every log frame
        carries its own checksum, so a whole-database check here would cost
        more than the decryption this path exists to avoid.  What is left — a
        log describing a database these pages cannot assemble — surfaces as
        SQLite refusing its own schema, so that one read is done, and any
        failure falls back to decrypting the file properly.
        """
        try:
            image = self._update_log_with_retry(
                base, database, key, page_size, reserve
            )
            connection = _connect(image)
            try:
                connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            except BaseException:
                connection.close()
                raise
        except (
            ConsistencyError,
            KanaoError,
            wal_format.WalFormatError,
            sqlite3.DatabaseError,
        ):
            return None
        self._store(database, sampled, connection, len(image), base)
        return connection

    def _update_log_with_retry(
        self,
        base: BasePages,
        database: Path,
        key: bytes,
        page_size: int,
        reserve: int,
    ) -> bytes:
        last: Optional[ConsistencyError] = None
        for attempt in range(_READ_ATTEMPTS):
            try:
                return update_image_with_wal(base, database, key, page_size, reserve)
            except LogRestarted:
                # Waiting cannot put the stranded pages back into a log that
                # has moved past them, so this goes straight to a full read.
                raise
            except ConsistencyError as error:
                last = error
                if attempt < len(_BACKOFF_SECONDS):
                    time.sleep(_BACKOFF_SECONDS[attempt])
        raise ConsistencyError(
            f"KakaoTalk kept writing the {database.name} log during "
            f"{_READ_ATTEMPTS} attempts"
        ) from last

    def _decrypt_with_retry(
        self, database: Path, key: bytes, page_size: int, reserve: int
    ) -> bytes:
        last: Optional[ConsistencyError] = None
        for attempt in range(_READ_ATTEMPTS):
            try:
                return build_image(database, key, page_size, reserve)
            except ConsistencyError as error:
                last = error
                if attempt < len(_BACKOFF_SECONDS):
                    time.sleep(_BACKOFF_SECONDS[attempt])
            except FileNotFoundError as error:
                raise KanaoError(
                    ErrorCode.READ_FAILED, f"{database.name} no longer exists"
                ) from error
            except wal_format.WalFormatError as error:
                raise KanaoError(
                    ErrorCode.READ_FAILED, f"{database.name}: {error}"
                ) from error
        raise KanaoError(
            ErrorCode.BUSY,
            f"KakaoTalk kept writing {database.name} during "
            f"{_READ_ATTEMPTS} read attempts; try again in a moment",
            database=database.name,
        ) from last

    def _store(
        self,
        database: Path,
        fingerprint: Fingerprint,
        connection: sqlite3.Connection,
        size: int,
        base: Optional[BasePages],
    ) -> None:
        # The retained base pages are charged to the budget too, or an entry
        # would quietly cost twice what the cache believes it does.
        held = size + (len(base) if base is not None else 0)
        previous = self._entries.pop(database, None)
        if previous is not None:
            self._bytes -= previous.size
            previous.connection.close()
        self._entries[database] = _CachedSnapshot(
            fingerprint, connection, held, base, image_bytes=size
        )
        self._bytes += held
        self._make_room()

    def _make_room(self) -> None:
        """Get back under budget, giving up the cheapest thing first.

        Base pages are a cache of a cache.  Dropping a set costs the next read
        of that database one decryption; closing a snapshot can cost a caller
        the connection it is holding.  So the pages go first, least recently
        used first, and a snapshot is only evicted once giving up every set of
        them was still not enough — which keeps the number of live connections
        exactly where it was before any pages were kept at all.
        """
        budget = self._budget
        for entry in self._entries.values():
            if self._bytes <= budget:
                return
            if entry.base is not None:
                self._bytes -= len(entry.base)
                entry.size -= len(entry.base)
                entry.base = None

        while self._bytes > budget and len(self._entries) > 1:
            _evicted, stale = self._entries.popitem(last=False)
            self._bytes -= stale.size
            stale.connection.close()
