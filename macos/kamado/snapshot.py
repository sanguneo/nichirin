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
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional, Protocol, TextIO

from . import page as pages, wal as wal_format
from .cipher import Geometry
from .commoncrypto import PreparedKey
from .errors import ErrorCode, KamadoError

_READ_ATTEMPTS: Final = 4
_BACKOFF_SECONDS: Final = (0.05, 0.15, 0.4)
_DEFAULT_BUDGET_BYTES: Final = 256 * 1024 * 1024

_BUDGET_HEADROOM: Final = 3
"""How many copies of the largest image a cache with no stated budget may hold.

One entry costs roughly two copies: the snapshot and the base pages kept for
log-only updates. The third leaves room for growth and smaller cached images.
The fixed 256 MiB budget sheds base pages past a roughly 128 MiB database.
This limits retained cache entries, not temporary allocations or process RSS.
"""

_MEMORY_SHARE: Final = 0.25
"""The share of physical memory the budget may claim.

Following the database is only safe while the machine can hold it, so the
scaled budget is capped here rather than being allowed to grow without one.
On a machine too small for that cap the base pages are shed exactly as they
were before, which is the behaviour this cap exists to keep.
"""

_physical_memory: "list[Optional[int]]" = []
"""Memoised answer from :func:`physical_memory_bytes`; empty until first asked."""

_JOURNAL_MODE_OFFSET: Final = 18
_ROLLBACK_JOURNAL: Final = b"\x01\x01"

_PARALLEL_PAGE_THRESHOLD: Final = 4000
"""Databases with fewer pages decrypt in a few milliseconds and do not pay
the multiprocessing overhead; larger live databases decrypt across CPU cores."""

_PARALLEL_DEADLINE_SECONDS: Final = 120.0
"""How long the forked decryption may take before it is abandoned for the serial pass.

Far past what a measured 119 MB account needs; it exists so a child that never
returns costs a slow answer rather than a command that never ends."""

_REAP_SECONDS: Final = 2.0
"""How long a terminated child is given to go before it is killed."""


def _decrypt_pages_worker(
    shm_name: str,
    key_bytes: bytes,
    page_size: int,
    reserve: int,
    start_page: int,
    end_page: int,
    encrypted: bytes,
) -> None:
    """Decrypt one span of pages in a forked child.

    Only ever reached from a process that was single-threaded when it forked;
    see :func:`_forkable` for why that is the condition rather than a matter
    of what this function is careful about.
    """
    from multiprocessing import shared_memory

    shm = shared_memory.SharedMemory(name=shm_name)
    buffer = shm.buf
    if buffer is None:  # the segment was closed under this worker
        raise RuntimeError(f"shared memory {shm_name} is already closed")
    try:
        with PreparedKey(key_bytes) as prepared:
            for number in range(start_page, end_page + 1):
                start = (number - 1) * page_size
                page_data = pages.reconstruct_page(
                    prepared, number, encrypted[start : start + page_size], page_size, reserve
                )
                buffer[start : start + page_size] = page_data
    finally:
        shm.close()


def _decrypt_serially(
    encrypted: bytes,
    geometry: Geometry,
    base_pages: int,
    page_size: int,
    reserve: int,
) -> bytes:
    """Decrypt every base page on this thread, in order."""
    base_image = bytearray(len(encrypted))
    with geometry.prepared() as prepared:
        for number in range(1, base_pages + 1):
            start = (number - 1) * page_size
            base_image[start : start + page_size] = pages.reconstruct_page(
                prepared, number, encrypted[start : start + page_size], page_size, reserve
            )
    return bytes(base_image)


def _forkable() -> bool:
    """Whether this process may fork children to decrypt with.

    Only a process that is alone in itself. ``fork`` gives the child one
    thread — the one that called it — and everything every other thread held
    at that instant: a mutex inside libc's allocator, a lock some library took
    on the way into a native call, the interpreter's own bookkeeping. The
    child inherits the locked state without the threads that would unlock it,
    and what that produces is not an error but a child that never returns.

    Making the child careful is not a fix, because the child does not choose
    what it inherits. So the fork is confined to the one case where there is
    nothing to inherit: a command-line run, on its own thread, with no other
    thread alive. The terminal interface reads its database on a thread of its
    own and therefore decrypts serially — slower on the first read of a large
    account, and correct.
    """
    return (
        threading.current_thread() is threading.main_thread()
        and threading.active_count() == 1
    )


def _decrypt_base_pages(
    encrypted: bytes,
    geometry: Geometry,
    base_pages: int,
    page_size: int,
    reserve: int,
) -> bytes:
    """Decrypt all base pages, across CPU cores when this process may fork."""
    num_cpus = os.cpu_count() or 4
    num_workers = min(num_cpus, 8)

    if base_pages < _PARALLEL_PAGE_THRESHOLD or num_workers <= 1 or not _forkable():
        return _decrypt_serially(encrypted, geometry, base_pages, page_size, reserve)

    try:
        return _decrypt_in_parallel(
            encrypted, geometry, base_pages, page_size, reserve, num_workers
        )
    except Exception:
        # Every child has been reaped and the segment released by now, so the
        # serial pass is the only work left running.
        return _decrypt_serially(encrypted, geometry, base_pages, page_size, reserve)


def _decrypt_in_parallel(
    encrypted: bytes,
    geometry: Geometry,
    base_pages: int,
    page_size: int,
    reserve: int,
    num_workers: int,
) -> bytes:
    """Decrypt across forked children, or raise with every child already gone.

    The children are started one at a time, and any of them can fail to
    start, fail, or never finish; ``join`` without a limit turned the last of
    those into a command that never returned, with no fallback reached. So
    the wait has one overall deadline, and on the way out - success or not -
    every child that was started is terminated if it is still there and
    collected, *before* the shared segment it writes into is released and
    before the caller begins decrypting the same pages serially.
    """
    from multiprocessing import get_context, shared_memory

    total_size = len(encrypted)
    shm = None
    processes: list = []
    try:
        shm = shared_memory.SharedMemory(create=True, size=total_size)
        ctx = get_context("fork")
        chunk_size = (base_pages + num_workers - 1) // num_workers
        for w in range(num_workers):
            start_page = w * chunk_size + 1
            end_page = min(base_pages, (w + 1) * chunk_size)
            if start_page > end_page:
                break
            proc = ctx.Process(
                target=_decrypt_pages_worker,
                args=(
                    shm.name,
                    geometry.key,
                    page_size,
                    reserve,
                    start_page,
                    end_page,
                    encrypted,
                ),
            )
            proc.start()
            processes.append(proc)

        deadline = time.monotonic() + _PARALLEL_DEADLINE_SECONDS
        for proc in processes:
            proc.join(max(0.0, deadline - time.monotonic()))
        if any(proc.is_alive() for proc in processes):
            raise RuntimeError("parallel page decryption did not finish in time")
        if any(proc.exitcode != 0 for proc in processes):
            raise RuntimeError("parallel page decryption worker failed")

        view = shm.buf
        if view is None:  # as above: only reachable if it was closed
            raise RuntimeError("shared memory closed during decryption")
        return bytes(view[:total_size])
    finally:
        _reap(processes)
        if shm is not None:
            try:
                shm.close()
                shm.unlink()
            except Exception:
                pass


def _reap(processes: list) -> None:
    """Make sure no started child outlives the call that started it."""
    for proc in processes:
        if proc.is_alive():
            proc.terminate()
            proc.join(_REAP_SECONDS)
        if proc.is_alive():
            proc.kill()
            proc.join(_REAP_SECONDS)


def _read_physical_memory() -> Optional[int]:
    """Ask the kernel how much RAM this machine has, or give up quietly.

    ``sysconf`` answers from the already-loaded libc with no process to
    spawn, and on macOS it reports exactly what ``hw.memsize`` does.  The
    ``sysctl`` call behind it is the fallback for a platform where the
    variable is not defined, and a machine that answers neither is treated as
    unknown rather than guessed at.
    """
    try:
        page_count = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        page_count = page_size = -1
    if page_count > 0 and page_size > 0:
        return page_count * page_size

    try:
        reported = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        total = int(reported.stdout.strip())
    except ValueError:
        return None
    return total if total > 0 else None


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

    ``_BUDGET_HEADROOM`` copies of the largest image held, with the fixed
    default as a floor unless the physical-memory cap is lower. A
    machine that will not say how much memory it has gets the fixed default,
    which is what it got before this scaled at all.
    """
    memory = physical_memory_bytes()
    if memory is None:
        return _DEFAULT_BUDGET_BYTES
    wanted = max(_DEFAULT_BUDGET_BYTES, largest_image * _BUDGET_HEADROOM)
    return min(wanted, int(memory * _MEMORY_SHARE))


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


def _log_header(database: Path) -> bytes:
    """The live log's header, which names its generation; empty when there is none."""
    try:
        with database.with_name(database.name + "-wal").open("rb") as handle:
            return handle.read(wal_format.WAL_HEADER_SIZE)
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

    Two clones are not one, though.  Each file is frozen at its own instant,
    and a checkpoint that *restarts* the log between them pairs a database
    from before it with a log from after it: pages the old log carried are
    in neither, and the image is a mix of two states that each passes every
    structural check.  A restart always writes a new salt pair into the log
    header, so the header is read once before cloning and the cloned log
    must still carry it; a log that appeared or vanished in between fails
    the same comparison.  A checkpoint that does not restart is harmless,
    because it copies frames the log still holds and the overlay applies
    them anyway.  A plain copy is not frozen at any instant, so it is
    compared with the live file afterwards instead of being trusted.
    Raises :class:`ConsistencyError` so the caller retries.
    """
    into.mkdir(parents=True, exist_ok=True)
    destination = into / database.name
    generation = _log_header(database)
    copied: list[tuple[Path, Path]] = []
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
            copied.append((source, target))
    if _log_header(destination) != generation:
        raise ConsistencyError(f"the {database.name} log restarted while it was cloned")
    for source, target in copied:
        if source == database:
            still = _matches(source, target.read_bytes())
        else:
            # The log only grows within a generation, so a copy that is a
            # prefix of the live log under the same header is a whole one.
            still = _log_header(database) == generation and _read_log(database).startswith(
                target.read_bytes()
            )
        if not still:
            raise ConsistencyError(f"{source.name} changed while it was copied")
    return destination


@dataclass(frozen=True)
class BasePages:
    """The decrypted database file, and the proof it is still complete.

    *generation* is the log header these pages were decrypted against, which
    carries the salt pair naming the current run of the log.  That is what
    licenses reusing them, and a stat cannot do the job: a rewrite of equal
    length inside one file-time tick leaves ``st_mtime_ns`` identical.

    The argument is short.  In write-ahead logging mode the only thing that
    ever writes the database file is a checkpoint, and a checkpoint copies
    frames the log still holds.  So while the log runs in the same generation,
    every page a checkpoint may have written underneath us is a page the
    overlay applies anyway, and the assembled image comes out the same either
    way.  A checkpoint that *restarts* the log is the one case that would
    strand pages in a file we are no longer reading — and restarting is
    exactly what changes the salt in this header.
    """

    image: bytes
    generation: bytes

    def __len__(self) -> int:
        return len(self.image) + len(self.generation)


class DecryptedImage(bytes):
    """A decrypted SQLite image that also carries the base file it came from.

    The log is what moves when a message arrives, so keeping the base pages
    lets the next read overlay the log alone instead of decrypting the whole
    file again.  It is an attribute rather than a second return value because
    these images travel through code that only wants the bytes.
    """

    base: Optional[BasePages] = None


def _undefined_pages(committed: wal_format.WalSnapshot, base_pages: int) -> list[int]:
    """Pages the log grew the database to but never wrote."""
    return [
        number
        for number in range(base_pages + 1, committed.database_pages + 1)
        if number not in committed.pages
    ]


def _overlay_log(
    base_image: bytes,
    committed: wal_format.WalSnapshot,
    prepared: PreparedKey,
    page_size: int,
    reserve: int,
) -> bytearray:
    """Lay the committed log over decrypted base pages, growing or truncating.

    Both the full decryption and the log-only update assemble their result
    this way, so a commit that shrank the database is handled once rather than
    remembered in two places.
    """
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


def build_image(database: Path, geometry: Geometry) -> DecryptedImage:
    """Decrypt one database plus its committed log into a standalone SQLite image.

    Raises :class:`ConsistencyError` when the source moved during the read, so
    that the caller can retry rather than trust a torn result.
    """
    page_size, reserve = geometry.page_size, geometry.reserve
    pages.validate_geometry(page_size, reserve)

    log = _read_log(database)
    encrypted = database.read_bytes()

    if not encrypted:
        raise KamadoError(ErrorCode.READ_FAILED, f"{database.name} is empty")
    if len(encrypted) % page_size:
        raise KamadoError(
            ErrorCode.READ_FAILED,
            f"{database.name} ends with a partial page ({len(encrypted) % page_size} bytes)",
        )

    base_pages = len(encrypted) // page_size
    committed = wal_format.parse(log, page_size, base_pages)

    missing = _undefined_pages(committed, base_pages)
    if missing:
        raise KamadoError(
            ErrorCode.READ_FAILED,
            f"the committed log for {database.name} leaves pages {missing[:4]} undefined",
        )

    base_image_bytes = _decrypt_base_pages(
        encrypted, geometry, base_pages, page_size, reserve
    )
    with geometry.prepared() as prepared:
        image = _overlay_log(base_image_bytes, committed, prepared, page_size, reserve)

    if not _matches(database, encrypted) or not _read_log(database).startswith(log):
        raise ConsistencyError(f"{database.name} changed while it was being read")
    result = DecryptedImage(image)
    result.base = BasePages(base_image_bytes, log[: wal_format.WAL_HEADER_SIZE])
    return result


def update_image_with_wal(
    base: BasePages, database: Path, geometry: Geometry
) -> bytes:
    """Lay a freshly read log over base pages decrypted earlier, in ~10ms.

    Only the log is read, which is the whole point: a message that arrives
    appends committed frames and leaves the database file alone.  Anything
    saying these base pages no longer describe this database — a log that
    started a new generation, a log that moved again mid-read, a page the log
    grew the database to but never wrote — raises rather than assembling
    something plausible, and the caller decrypts the file properly instead.
    """
    page_size, reserve = geometry.page_size, geometry.reserve
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

    with geometry.prepared() as prepared:
        image = _overlay_log(base.image, committed, prepared, page_size, reserve)

    if not _read_log(database).startswith(log):
        raise ConsistencyError(f"{database.name} log changed while it was being read")
    return bytes(image)


class ConsistencyError(RuntimeError):
    """The source moved mid-read; the partial result was discarded."""


class LogRestarted(ConsistencyError):
    """The log began a new generation, so the cached base pages no longer apply.

    Separate from its parent because retrying cannot help: waiting does not
    put the stranded pages back into a log that has moved past them, so this
    one goes straight to a full decryption instead of backing off first.
    """


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
    # The name carries our pid so a run killed before ``finally`` leaves a file
    # the next run can tell is orphaned; see sweep_orphans.
    handle, name = tempfile.mkstemp(prefix=_temp_prefix("plain"), suffix=".sqlite3")
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


_TEMP_NAME: Final = re.compile(r"kamado-(?:plain|clone|wal)-([0-9]+)-")
"""Temporary files and directories this module names after its own pid."""

_swept: "list[bool]" = []
_sweep_lock = threading.Lock()
_warned: "list[bool]" = []


def _temp_prefix(kind: str) -> str:
    return f"kamado-{kind}-{os.getpid()}-"


def _pid_alive(pid: int) -> bool:
    """Whether *pid* names a process; one we may not signal counts as alive."""
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_orphans(directory: Optional[Path] = None) -> list[Path]:
    """Remove temporary copies left behind by a kamado run that was killed.

    ``finally`` does not run under SIGKILL or a launchd restart, so the
    plaintext file of the 3.9/3.10 path and the clone staging directories can
    outlive their process. Only names carrying a pid that no longer exists are
    removed, and only entries this user owns; a symlink is never followed.
    Returns what was removed.
    """
    root = Path(directory) if directory is not None else Path(tempfile.gettempdir())
    removed: list[Path] = []
    try:
        names = os.listdir(root)
    except OSError:
        return removed
    uid = os.getuid()
    for name in names:
        match = _TEMP_NAME.match(name)
        if match is None or _pid_alive(int(match.group(1))):
            continue
        path = root / name
        try:
            status = os.lstat(path)
            if status.st_uid != uid:
                continue
            if path.is_symlink() or not path.is_dir():
                os.unlink(path)
            else:
                shutil.rmtree(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def _sweep_once() -> None:
    with _sweep_lock:
        if _swept:
            return
        _swept.append(True)
    sweep_orphans()


def plaintext_reaches_disk() -> bool:
    """Whether this interpreter's SQLite lacks ``deserialize``."""
    probe = sqlite3.connect(":memory:")
    try:
        return getattr(probe, "deserialize", None) is None
    finally:
        probe.close()


def warn_if_plaintext_fallback(stream: Optional[TextIO] = None) -> bool:
    """Say once, on stderr, that this interpreter takes the slow disk path.

    A long-running ``serve`` refreshes its snapshot on every commit, so the
    temporary-file path costs far more there than in a single command.
    """
    if _warned or not plaintext_reaches_disk():
        return False
    _warned.append(True)
    version = ".".join(str(part) for part in sys.version_info[:3])
    print(
        f"warning: Python {version} lacks sqlite3 deserialize; snapshot refresh "
        "writes plaintext temp files and is several times slower. Use Python 3.11+.",
        file=stream if stream is not None else sys.stderr,
        flush=True,
    )
    return True


def open_image(image: bytes, label: str) -> sqlite3.Connection:
    """Open a decrypted image and refuse it unless SQLite reports it intact.

    A wrong key produces plausible-looking bytes rather than an error, so this
    check is what separates "decrypted" from "decrypted correctly".
    Using quick_check verifies page structure and B-trees without the heavy
    cost of checking index sort order on large databases.
    """
    connection = _connect(image)
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise KamadoError(
                ErrorCode.READ_FAILED, f"{label} did not decrypt to a valid database"
            )
    except BaseException:
        connection.close()
        raise
    return connection


@dataclass
class _CachedSnapshot:
    """One decrypted database, and the base pages the next read can reuse."""

    fingerprint: Fingerprint
    connection: sqlite3.Connection
    size: int
    base: Optional[BasePages]
    image_bytes: int
    """The snapshot alone, which is what the adaptive budget scales against.

    ``size`` is what the entry is charged and shrinks when its base pages are
    shed, so it cannot stand in for the size of the database being read.
    """


class SnapshotSource(Protocol):
    """What a reader needs from a cache: a connection, and a way to let go.

    :class:`Reader` takes one of these rather than a :class:`SnapshotCache`
    because both real substitutions are worth being able to state. The server
    hands the send path a cache that outlives the request, and the suite hands
    a stub that serves prepared plaintext; naming the two methods a reader
    actually calls says so, where the concrete class would have made the stub
    a lie about the type.
    """

    def connect(self, database: Path, geometry: Geometry) -> sqlite3.Connection:
        ...

    def invalidate(self, database: Path) -> None:
        ...

    def close(self) -> None:
        ...


class SnapshotCache:
    """Decrypted databases, reused until their files change.

    The cache owns every connection it returns; callers read and must not
    close them.  Eviction is least-recently-used against a byte budget, which
    counts the base pages an entry keeps as well as the snapshot itself.

    *budget_bytes* is honoured exactly when it is given.  Left out, the budget
    follows the largest database the cache is actually holding rather than a
    constant chosen against a smaller account; see :func:`_adaptive_budget`.
    """

    def __init__(self, budget_bytes: Optional[int] = None) -> None:
        self._stated_budget = budget_bytes
        self._entries: OrderedDict[Path, _CachedSnapshot] = OrderedDict()
        self._bytes = 0

    @property
    def _budget(self) -> int:
        """What this cache may hold right now.

        A stated budget is returned untouched.  Otherwise it is derived, on
        each use rather than at construction, because the size it scales with
        is not known until a database has been read.
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

    def connect(self, database: Path, geometry: Geometry) -> sqlite3.Connection:
        """Return a queryable snapshot, decrypting only when the source moved.

        When the base database file is unchanged and only the write-ahead log
        moved (the usual case for new messages), an incremental WAL-only update
        overlays new frames on top of the cached base image in ~10ms rather than
        re-decrypting the whole multi-hundred-megabyte database.

        The image is filed under the fingerprint taken *before* it was read.
        Sampling again afterwards put a commit that landed during the read
        beside an image that does not hold it, and the next call, finding the
        files unchanged since, handed the older image back for as long as the
        account stayed quiet.  A commit that did make it into the read costs
        one extra read instead.  Equal stats still say nothing about equal
        bytes; this does not claim to beat that.
        """
        current = Fingerprint.of(database)
        cached = self._entries.get(database)
        if cached is not None and cached.fingerprint == current:
            self._entries.move_to_end(database)
            return cached.connection

        _sweep_once()
        if (
            cached is not None
            and cached.base is not None
            and cached.fingerprint.database == current.database
        ):
            connection = self._reuse_base_pages(database, geometry, cached.base, current)
            if connection is not None:
                return connection

        image = self._decrypt_with_retry(database, geometry)
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
        self, database: Path, geometry: Geometry, base: BasePages, current: Fingerprint
    ) -> Optional[sqlite3.Connection]:
        """Try the log-only path, returning ``None`` when it must be given up on.

        The result is deliberately not put through :func:`open_image`.  Those
        base pages were checked when they were decrypted and every log frame
        carries its own checksum, so a whole-database check here would cost
        more than the decryption this path exists to avoid.  What is left — a
        log describing a database these pages cannot assemble — surfaces as
        SQLite refusing its own schema, so that one read is done, and any
        failure closes the connection and falls back to decrypting properly.
        """
        try:
            image = self._update_wal_with_retry(base, database, geometry)
            connection = _connect(image)
            try:
                connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            except BaseException:
                connection.close()
                raise
        except (
            ConsistencyError,
            KamadoError,
            wal_format.WalFormatError,
            sqlite3.DatabaseError,
        ):
            return None
        self._store(database, current, connection, len(image), base)
        return connection

    def _update_wal_with_retry(
        self, base: BasePages, database: Path, geometry: Geometry
    ) -> bytes:
        last: Optional[ConsistencyError] = None
        for attempt in range(_READ_ATTEMPTS):
            staging = Path(tempfile.mkdtemp(prefix=_temp_prefix("wal")))
            try:
                frozen = clone_snapshot(database, staging)
                return update_image_with_wal(base, frozen, geometry)
            except LogRestarted:
                # Backing off cannot undo a restart; decrypt the file instead.
                raise
            except ConsistencyError as error:
                last = error
                if attempt < len(_BACKOFF_SECONDS):
                    time.sleep(_BACKOFF_SECONDS[attempt])
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        raise ConsistencyError(
            f"KakaoTalk kept writing {database.name} log; incremental update retried"
        ) from last

    def _decrypt_with_retry(self, database: Path, geometry: Geometry) -> bytes:
        last: Optional[ConsistencyError] = None
        for attempt in range(_READ_ATTEMPTS):
            staging = Path(tempfile.mkdtemp(prefix=_temp_prefix("clone")))
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
                raise KamadoError(
                    ErrorCode.READ_FAILED, f"{database.name} no longer exists"
                ) from error
            except wal_format.WalFormatError as error:
                raise KamadoError(
                    ErrorCode.READ_FAILED, f"{database.name}: {error}"
                ) from error
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        raise KamadoError(
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
            fingerprint, connection, held, base, size
        )
        self._bytes += held
        self._make_room()

    def _make_room(self) -> None:
        """Get back under budget, giving up the cheapest thing first.

        Base pages are a cache of a cache.  Dropping a set costs the next read
        of that database one decryption; closing a snapshot can cost a caller
        the connection it is holding.  So the pages go first, least recently
        used first, and a snapshot is evicted only once giving up every set of
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
