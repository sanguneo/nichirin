"""Match each encrypted database to the live key that opens it.

SQLCipher stores a database's key derivation salt in the clear, as the first 16
bytes of the file, and the same salt appears in the in-memory context that
holds its key.  Matching on that salt means a database is never guessed at by
filename: a key either provably belongs to a file or is not used at all.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import paths
from .errors import ErrorCode, KanaoError
from .page import SALT_SIZE
from .process import CodecContext, find_pid, resolve_addresses, scan_process


def _cache_path() -> Path:
    """Where the addresses of a previous scan are remembered, outside the repo."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(root) / "kanao-cli" / "contexts.json"


def _remembered(pid: int) -> tuple[list[int], int]:
    """Return the addresses last seen in *pid*, and how many keys they paired.

    Only addresses are kept. A key never reaches the disk, which is why the
    count is stored beside them: it is what tells a stale cache apart from a
    good one without having anything to compare a key against.
    """
    try:
        stored = json.loads(_cache_path().read_text("utf-8"))
    except (OSError, ValueError):
        return [], 0
    if not isinstance(stored, dict) or stored.get("pid") != pid:
        return [], 0
    addresses = stored.get("addresses")
    expected = stored.get("keys")
    if not isinstance(addresses, list) or not isinstance(expected, int):
        return [], 0
    return [int(address) for address in addresses if isinstance(address, int)], expected


def _remember(pid: int, addresses: Iterable[int], keys: int) -> None:
    """Record where the contexts were. Failing to write only costs a rescan."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "addresses": list(addresses), "keys": keys}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _pair(
    contexts: Iterable[CodecContext],
    databases: dict[bytes, list[Path]],
    user_directory: Path,
) -> dict[str, bytes]:
    """Map every database to the key whose salt it carries."""
    keys: dict[str, bytes] = {}
    for context in contexts:
        for database in databases.get(context.salt, ()):
            relative = database.relative_to(user_directory).as_posix()
            keys[relative] = context.key
            keys[database.name] = context.key
    return keys


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

        # Searching the whole heap measured 2.8 s, and every command paid it
        # again. Addresses from the last scan are re-read first; they are not
        # trusted, only tried, and a short answer sends us back to the scan.
        remembered, expected = _remembered(pid)
        if remembered and expected > 0:
            keys = _pair(resolve_addresses(pid, remembered), databases, user_directory)
            if len(keys) >= expected:
                return cls(user_directory=user_directory, pid=pid, keys=keys)

        contexts = scan_process(pid)
        keys = _pair(contexts, databases, user_directory)
        _remember(pid, [context.address for context in contexts], len(keys))
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
            raise KanaoError(
                ErrorCode.KAKAO_NOT_RUNNING,
                "KakaoTalk is not running, so no decryption key is available; "
                "start and sign in to the desktop client",
            )
        raise KanaoError(
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
