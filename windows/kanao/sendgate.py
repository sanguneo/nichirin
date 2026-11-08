"""One input box, one writer at a time, across every process on the account.

``serve`` already queues its own sends under a lock, but that lock lives in
one process: two agents each running ``kanao send``, or one of them beside a
running ``serve``, typed into the same client with nothing between them, and
the two-second spacing was only ever the caller's promise (issue #114).

The gate is a byte-range lock on a file under ``%LOCALAPPDATA%\\kanao-cli``,
taken with ``msvcrt.locking`` — ``LockFileEx`` underneath — so the operating
system releases it when the holder exits, however it exits. Beside it sits one
pacing record, the wall-clock time the last write *started*, which is read and
rewritten only while the lock is held, in the same critical section as the
input itself.

The policy is the smallest one the current contracts carry:

- **Contention is refused at once, never waited out.** A second writer gets
  ``BUSY`` with ``not_delivered`` / ``safe``: nothing was typed, so repeating
  it later is harmless, and nobody sits behind a send of unknown length.
- **Pacing waits.** Holding the lock, a writer waits until its own minimum
  interval since the recorded start has passed, as ``serve``'s queue does.
- **The start is booked before the input, and given back only when nothing
  was typed.** A record that cannot be written refuses the send before
  anything is touched. A write that ends ``not_delivered`` restores the
  previous record; one that ended submitted *or uncertain* keeps it, because
  an input that may have happened still occupied the box. Nothing is retried.
- **A gate that cannot be taken is a refusal.** There is no quiet fallback to
  an uncoordinated send.
"""

from __future__ import annotations

import errno
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from kakaocommon.disposition import Disposition

from .errors import ErrorCode, KanaoError

LOCK_NAME = "send.lock"
RECORD_NAME = "send-pacing.json"

_NOT_SENT: Disposition = {"delivery": "not_delivered", "retry": "safe"}
_HELD = frozenset({errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


def state_directory() -> Path:
    """Where the gate lives: beside the tool's other local state."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(root) / "kanao-cli"


def _try_lock(fd: int) -> bool:
    """Take the first byte of *fd* without waiting; False if another holds it."""
    try:
        import msvcrt
    except ImportError:  # the sibling's machine runs this suite too
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in _HELD or isinstance(error, BlockingIOError):
                return False
            raise
        return True
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as error:
        if error.errno in _HELD:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    try:
        import msvcrt
    except ImportError:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class SendGate:
    """The account-wide lock and pacing record every text and file send passes.

    *clock* is wall time, because the record is read by other processes and a
    monotonic clock means nothing outside the one that read it; a record from
    the future (the clock was set back) is honoured for at most one interval.
    *wait* is the only place this sleeps, so a test can replace both.
    """

    def __init__(
        self,
        directory: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.directory = state_directory() if directory is None else Path(directory)
        self._clock = clock
        self._wait = wait

    @property
    def record_path(self) -> Path:
        return self.directory / RECORD_NAME

    @contextmanager
    def turn(self, min_interval: float) -> Iterator[None]:
        """Hold the gate for one write, paced *min_interval* from the last start."""
        fd = self._open_lock()
        try:
            try:
                held = _try_lock(fd)
            except OSError as error:
                raise self._unusable(error) from error
            if not held:
                raise KanaoError(
                    ErrorCode.BUSY,
                    "another kanao process is sending to KakaoTalk right now; "
                    "nothing was sent, so this can be repeated once it has finished",
                    disposition=_NOT_SENT,
                )
            try:
                previous = self._read_record()
                self._pace(previous, min_interval)
                started = self._clock()
                self._write_record(started)
                try:
                    yield
                except KanaoError as error:
                    if error.disposition.get("delivery") == "not_delivered":
                        self._restore(previous)
                    raise
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

    def _open_lock(self) -> int:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            return os.open(self.directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise self._unusable(error) from error

    @staticmethod
    def _unusable(error: OSError) -> KanaoError:
        return KanaoError(
            ErrorCode.SEND_FAILED,
            f"the send lock could not be taken ({error.strerror or error}); nothing was sent",
            disposition=_NOT_SENT,
        )

    def _read_record(self) -> Optional[float]:
        """The last recorded start; a damaged record counts as one just now."""
        try:
            stored = json.loads(self.record_path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return self._clock()
        started = stored.get("startedAt") if isinstance(stored, dict) else None
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            return float(started)
        return self._clock()

    def _pace(self, previous: Optional[float], min_interval: float) -> None:
        if previous is None or min_interval <= 0:
            return
        remaining = min(previous + min_interval - self._clock(), min_interval)
        if remaining > 0:
            self._wait(remaining)

    def _write_record(self, started: float) -> None:
        """Book this start before the input; failing here sends nothing."""
        scratch = self.record_path.with_name(RECORD_NAME + ".tmp")
        try:
            scratch.write_text(json.dumps({"startedAt": started}), encoding="utf-8")
            os.replace(scratch, self.record_path)
        except OSError as error:
            raise self._unusable(error) from error

    def _restore(self, previous: Optional[float]) -> None:
        """Give a start back that typed nothing. Failing only costs a wait."""
        try:
            if previous is None:
                self.record_path.unlink(missing_ok=True)
            else:
                self._write_record(previous)
        except (OSError, KanaoError):
            pass
