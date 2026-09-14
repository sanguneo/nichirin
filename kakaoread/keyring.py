"""Match each encrypted database to the live key that opens it.

SQLCipher stores a database's key derivation salt in the clear, as the first 16
bytes of the file, and the same salt appears in the in-memory context that
holds its key.  Matching on that salt means a database is never guessed at by
filename: a key either provably belongs to a file or is not used at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths
from .errors import ErrorCode, KakaoReadError
from .page import SALT_SIZE
from .process import CodecContext, find_pid, scan_process


def _read_salt(database: Path) -> Optional[bytes]:
    """Return a database's plaintext salt, or None when it cannot be read."""
    try:
        with database.open("rb") as handle:
            salt = handle.read(SALT_SIZE)
    except OSError:
        return None
    return salt if len(salt) == SALT_SIZE else None


@dataclass(frozen=True)
class Keyring:
    """Every database on this machine that can currently be decrypted."""

    user_directory: Path
    pid: Optional[int]
    keys: dict[str, bytes] = field(default_factory=dict)
    """Keys by profile-relative posix path, and again by bare filename."""

    @property
    def running(self) -> bool:
        """Whether a KakaoTalk process was found while building this keyring."""
        return self.pid is not None

    @classmethod
    def discover(cls, user_directory: Path) -> "Keyring":
        """Scan the running client once and pair its keys with local databases."""
        pid = find_pid()
        if pid is None:
            return cls(user_directory=user_directory, pid=None, keys={})

        # A salt maps to every file carrying it, not just one: dropping a
        # database because another shares its salt would make a readable
        # conversation silently invisible.
        databases: dict[bytes, list[Path]] = {}
        try:
            for database in paths.database_paths(user_directory):
                salt = _read_salt(database)
                if salt is not None:
                    databases.setdefault(salt, []).append(database)
        except OSError:
            return cls(user_directory=user_directory, pid=pid, keys={})

        keys: dict[str, bytes] = {}
        for context in scan_process(pid):
            for database in databases.get(context.salt, ()):
                relative = database.relative_to(user_directory).as_posix()
                keys[relative] = context.key
                keys[database.name] = context.key
        return cls(user_directory=user_directory, pid=pid, keys=keys)

    def has(self, name: str) -> bool:
        """Whether *name* can be decrypted right now."""
        return self._lookup(name) is not None

    def key_for(self, name: str) -> bytes:
        """Return the key for a profile-relative path, explaining any absence.

        The two failure modes need different actions from the caller, so they
        are reported differently: the client is closed, or the client is open
        but has never loaded this particular database.
        """
        key = self._lookup(name)
        if key is not None:
            return key
        if not self.running:
            raise KakaoReadError(
                ErrorCode.KAKAO_NOT_RUNNING,
                "KakaoTalk is not running, so no decryption key is available; "
                "start and sign in to the desktop client",
            )
        raise KakaoReadError(
            ErrorCode.KEY_UNAVAILABLE,
            f"KakaoTalk has not opened {name}, so its key is not in memory; "
            "open that conversation in KakaoTalk and try again",
            database=name,
        )

    def path_for(self, name: str) -> Path:
        """Return the absolute path of a profile-relative database name."""
        return self.user_directory / name

    def chat_ids(self) -> list[int]:
        """Return the ids of every room whose message database can be read."""
        prefix = f"{paths.CHAT_DATA_DIRECTORY}/chatLogs_"
        found: set[int] = set()
        for name in self.keys:
            normalized = name.replace("\\", "/")
            if not normalized.startswith(prefix) or not normalized.endswith(".edb"):
                continue
            digits = normalized[len(prefix) : -len(".edb")]
            if digits.isdecimal():
                found.add(int(digits))
        return sorted(found)

    def _lookup(self, name: str) -> Optional[bytes]:
        normalized = name.replace("\\", "/")
        return self.keys.get(normalized) or self.keys.get(normalized.rsplit("/", 1)[-1])

    def __len__(self) -> int:
        """Number of distinct databases that can be decrypted."""
        return len({id(key) for key in self.keys.values()}) if self.keys else 0


def context_salts(contexts: list[CodecContext]) -> dict[bytes, bytes]:
    """Index scanned contexts by salt, keeping the first key seen for each."""
    indexed: dict[bytes, bytes] = {}
    for context in contexts:
        indexed.setdefault(context.salt, context.key)
    return indexed
