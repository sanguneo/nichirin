"""Find KakaoTalk's container, and survive the way macOS guards it.

Everything is resolved from the running machine's environment, so an installed
copy works for whoever runs it rather than for whoever built it.

The one surprise here is how a missing permission behaves.  A process without
Full Disk Access does not get ``PermissionError`` when it enumerates another
application's container — it gets **nothing at all**.  The call sits in the
kernel waiting on a consent decision that no one is going to make, and a tool
that simply called ``os.listdir`` would hang with no output and no explanation,
which is exactly how this was first met.  Every first touch of the container is
therefore made on a throwaway thread with a deadline, so the answer is a typed
error naming the permission instead of a program that never returns.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Final, Optional, TypeVar

from .errors import ErrorCode, KamadoError

BUNDLE_ID: Final = "com.kakao.KakaoTalkMac"

_CONTAINER_ROOT: Final = f"Library/Containers/{BUNDLE_ID}/Data"
_APPLICATION_SUPPORT: Final = f"{_CONTAINER_ROOT}/Library/Application Support/{BUNDLE_ID}"
_CONTAINER_PREFERENCES: Final = f"{_CONTAINER_ROOT}/Library/Preferences"
_GLOBAL_PREFERENCES: Final = f"Library/Preferences/{BUNDLE_ID}.plist"

DATABASE_NAME_PATTERN: Final = re.compile(r"^[0-9a-f]{78}(\.db)?$")
"""What the client names its database: the derivation's hex digest slice."""

ACCESS_TIMEOUT_SECONDS: Final = 5.0
"""How long a container access may take before it is called a refusal."""

_T = TypeVar("_T")

_PERMISSION_MESSAGE: Final = (
    "macOS is withholding access to KakaoTalk's container. Grant Full Disk Access "
    "to the terminal application you are running this from — System Settings › "
    "Privacy & Security › Full Disk Access — then run the command again."
)


_abandoned: list[threading.Thread] = []
"""Workers given up on that may still be stuck in the kernel."""


def guarded(operation: Callable[[], _T], what: str) -> _T:
    """Run a container access with a deadline, reporting a refusal as an error.

    The worker is a daemon so that a call still stuck in the kernel cannot keep
    the process alive; it is abandoned rather than cancelled, because there is
    no way to cancel it.

    While one abandoned worker is still stuck, the next access is refused at
    once instead of starting another. Every access here waits on the same
    permission, so a second worker would only be stuck beside the first, and
    a long-lived process asking again and again would pile them up. Once the
    stuck call returns - the permission was granted or refused - accesses go
    through again.
    """
    _abandoned[:] = [worker for worker in _abandoned if worker.is_alive()]
    if _abandoned:
        raise KamadoError(
            ErrorCode.PERMISSION_REQUIRED, _PERMISSION_MESSAGE, permission="fullDiskAccess"
        )
    result: list[_T] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(operation())
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
            failure.append(error)

    worker = threading.Thread(target=run, daemon=True, name=f"kamado-{what}")
    worker.start()
    worker.join(ACCESS_TIMEOUT_SECONDS)

    if worker.is_alive():
        _abandoned.append(worker)
        raise KamadoError(
            ErrorCode.PERMISSION_REQUIRED, _PERMISSION_MESSAGE, permission="fullDiskAccess"
        )
    if failure:
        error = failure[0]
        if isinstance(error, PermissionError):
            raise KamadoError(
                ErrorCode.PERMISSION_REQUIRED, _PERMISSION_MESSAGE, permission="fullDiskAccess"
            ) from error
        raise error
    return result[0]


def home(environment: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Return the home directory the container hangs beneath."""
    env = os.environ if environment is None else environment
    return Path(env.get("HOME") or os.path.expanduser("~"))


def container_directory(
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> Path:
    """Return the directory holding the client's databases."""
    return home(environment) / _APPLICATION_SUPPORT


def preference_paths(
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> list[Path]:
    """Return every preferences file that may name the signed-in account.

    The container's own copy is richer than the global one and is listed first;
    its name carries a hex suffix that varies per install, so it is discovered
    rather than spelled out.
    """
    base = home(environment)
    found: list[Path] = []
    directory = base / _CONTAINER_PREFERENCES
    plain = directory / f"{BUNDLE_ID}.plist"

    def scan() -> list[Path]:
        try:
            return sorted(directory.iterdir())
        except (OSError, NotADirectoryError):
            return []

    for entry in guarded(scan, "preferences"):
        if entry.name.startswith(f"{BUNDLE_ID}.") and entry.suffix == ".plist" and entry != plain:
            found.append(entry)
    found.append(plain)
    found.append(base / _GLOBAL_PREFERENCES)
    return found


def database_paths(container: Path) -> list[Path]:
    """Return every file in the container that is named like a database.

    Sorted so a machine with more than one signed-in account answers the same
    way twice; which of them is *this* account is settled by the derivation,
    never by position.
    """

    def scan() -> list[Path]:
        try:
            entries = list(os.scandir(container))
        except FileNotFoundError:
            return []
        # The container also holds cache directories whose names are hex of
        # exactly this shape, so being named like a database is not enough to
        # be one; only regular files are offered to the key derivation.
        return sorted(
            Path(entry.path)
            for entry in entries
            if DATABASE_NAME_PATTERN.match(entry.name) and entry.is_file()
        )

    return guarded(scan, "container")


def find_database(container: Path, name: str) -> Optional[Path]:
    """Return the database a derived *name* refers to, with or without suffix."""
    for candidate in (container / name, container / f"{name}.db"):
        if guarded(candidate.is_file, "database"):
            return candidate
    return None


def state_directory(
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> Path:
    """Where this tool keeps its own small state, outside the repository.

    Application Support rather than Caches: the name ledger is the one thing
    here that cannot be rebuilt by re-reading the client, so it must not be a
    file the system is entitled to delete.
    """
    return home(environment) / "Library" / "Application Support" / "kamado-cli"


def wal_path(database: Path) -> Path:
    """Return the write-ahead log that belongs to *database*."""
    return database.with_name(database.name + "-wal")
