"""Account-wide nonblocking send ownership and shared start-time pacing.

The kernel releases flock ownership even when a sender is killed. A start is
booked before input and restored only for a definitely unsent KamadoError;
unknown outcomes retain their pacing charge. An unusable gate refuses input.
"""

from __future__ import annotations

import errno
import json
import math
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from kakaocommon.disposition import Disposition

from .errors import ErrorCode, KamadoError
from .paths import state_directory

LOCK_NAME = "send.lock"
RECORD_NAME = "send-pacing.json"
_NOT_SENT: Disposition = {"delivery": "not_delivered", "retry": "safe"}


class SendGate:
    """Serialize processes and pace starts, with injectable wall time and wait.

    Wall time is shared across processes. A future record after clock rollback
    costs at most one interval, rather than waiting for the old clock to return.
    """

    def __init__(
        self,
        directory: Path | None = None,
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
        """Hold one paced send turn; refuse competing processes immediately."""
        import fcntl

        directory_fd = self._open_directory()
        try:
            try:
                fd = self._open_private(LOCK_NAME, os.O_RDWR | os.O_CREAT, directory_fd)
            except OSError as error:
                raise self._unusable(error) from error
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EACCES, errno.EAGAIN):
                        raise KamadoError(
                            ErrorCode.BUSY,
                            "another kamado process is sending to KakaoTalk right now; "
                            "nothing was sent, so repeat once it has finished",
                            disposition=_NOT_SENT,
                        ) from error
                    raise self._unusable(error) from error
                try:
                    previous = self._read_record(directory_fd)
                    if previous is not None and min_interval > 0:
                        remaining = min(previous + min_interval - self._clock(), min_interval)
                        if remaining > 0:
                            self._wait(remaining)
                    self._write_record(self._clock(), directory_fd)
                    try:
                        yield
                    except KamadoError as error:
                        if error.disposition.get("delivery") == "not_delivered":
                            self._restore(previous, directory_fd)
                        raise
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)

    def _open_directory(self) -> int:
        """Pin an owned private directory; never follow its final symlink."""
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    raise PermissionError(errno.EACCES, "send directory belongs to another user")
                os.fchmod(fd, 0o700)
            except OSError:
                os.close(fd)
                raise
            return fd
        except OSError as error:
            raise self._unusable(error) from error

    def _open_private(self, name: str, flags: int, directory_fd: int) -> int:
        """Open only an owned regular file with no symlinks or hard links."""
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise PermissionError(errno.EACCES, "unsafe send state file")
                os.fchmod(fd, 0o600)
            except OSError:
                os.close(fd)
                raise
            return fd
        except FileNotFoundError:
            raise
        except OSError as error:
            raise self._unusable(error) from error

    @staticmethod
    def _unusable(error: OSError) -> KamadoError:
        return KamadoError(
            ErrorCode.SEND_FAILED,
            f"the send lock could not be taken ({error.strerror or error}); nothing was sent",
            disposition=_NOT_SENT,
        )

    def _read_record(self, directory_fd: int) -> float | None:
        """A missing record is fresh; malformed data costs one interval."""
        try:
            fd = self._open_private(RECORD_NAME, os.O_RDONLY, directory_fd)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, encoding="utf-8") as record:
                stored = json.load(record)
        except (OSError, ValueError):
            return self._clock()
        started = stored.get("startedAt") if isinstance(stored, dict) else None
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            try:
                value = float(started)
            except OverflowError:
                return self._clock()
            if math.isfinite(value):
                return value
        return self._clock()

    def _write_record(self, started: float, directory_fd: int) -> None:
        """Publish a private record atomically before touching the input."""
        scratch = RECORD_NAME + "." + secrets.token_hex(8) + ".tmp"
        try:
            fd = self._open_private(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory_fd)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as record:
                    json.dump({"startedAt": started}, record)
                os.replace(scratch, RECORD_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            finally:
                try:
                    os.unlink(scratch, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        except OSError as error:
            raise self._unusable(error) from error

    def _restore(self, previous: float | None, directory_fd: int) -> None:
        """Failure to restore only costs pacing; it never permits extra input."""
        try:
            if previous is None:
                os.unlink(RECORD_NAME, dir_fd=directory_fd)
            else:
                self._write_record(previous, directory_fd)
        except (OSError, KamadoError):
            pass
