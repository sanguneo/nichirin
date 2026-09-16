"""Decide which account this machine is signed in as, and prove it.

The Windows build matched a database to a key by the 16-byte salt they shared,
because it had a pile of keys and a pile of files and no other way to pair
them.  Here the pairing runs the other way and is stronger.  A machine may
carry preferences from several sign-ins, so there may be several candidate
account ids; but the *filename* of a database is derived from the account id
and the hardware id together, so a candidate that reproduces a filename present
in the container is not a guess, it is the account.

The passphrase is then derived from the same pair and proved against the file
itself by :func:`cipher.detect`, which accepts a parameter set only when the
page it decrypts describes itself correctly.  Two independent proofs — the name
and the header — stand between a candidate and being believed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import cipher, derive, paths
from .cipher import Geometry
from .errors import ErrorCode, KakaoReadError

_ACCOUNT_CACHE = "account.json"
"""Where a resolved account id is remembered, so recovery runs once.

Only the account id is stored, and only alongside the hardware id it was
resolved against, so a cache copied to another Mac is ignored rather than
trusted.  No passphrase, no key, and no salt is ever written — the id is
public in the sense that it is already recoverable from the machine, and it is
kept solely to avoid recovering it again.
"""


@dataclass(frozen=True)
class Account:
    """The signed-in account, its database, and how that database is encrypted."""

    user_id: int
    database: Path
    geometry: Geometry

    @property
    def user(self) -> str:
        """The account id as it crosses the boundary: a string, always."""
        return str(self.user_id)


def _candidate_ids(environment=None) -> list[int]:
    """Every account id the preferences suggest, best first, without repeats."""
    found: list[int] = []
    for path in paths.preference_paths(environment):
        for identifier in derive.candidate_user_ids(derive.read_preferences(path)):
            if identifier not in found:
                found.append(identifier)
    return found


def _stems(databases: Iterable[Path]) -> dict[str, Path]:
    """Index the container's databases by filename without the optional suffix."""
    return {database.name.split(".", 1)[0]: database for database in databases}


def _cache_path(environment=None) -> Path:
    return paths.state_directory(environment) / _ACCOUNT_CACHE


def _remembered_id(environment, uuid: str) -> Optional[int]:
    """Return the account id last resolved on this machine, if still this machine."""
    try:
        stored = json.loads(_cache_path(environment).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("uuid") != uuid:
        return None
    identifier = stored.get("userId")
    return int(identifier) if isinstance(identifier, int) and identifier > 0 else None


def _remember_id(environment, uuid: str, user_id: int) -> None:
    """Record the resolved id. Failing to write only costs a future recovery."""
    path = _cache_path(environment)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"uuid": uuid, "userId": user_id}), encoding="utf-8")
    except OSError:
        pass


@dataclass(frozen=True)
class Keyring:
    """What can be read on this machine right now."""

    container: Path
    databases: list[Path]
    account: Optional[Account]

    @property
    def readable(self) -> bool:
        """Whether a database was found *and* proved to decrypt."""
        return self.account is not None

    @classmethod
    def discover(cls, environment=None, recover: bool = True) -> "Keyring":
        """Resolve the signed-in account, or explain why none could be.

        Three sources of a candidate id are tried in rising order of cost: the
        remembered id from a previous run, the ids the preferences name in the
        clear, and — only when *recover* is set and the cheaper two fail — the
        id recovered from the account hash by search.  The first that decrypts
        a present database is remembered so the search is paid at most once per
        machine.
        """
        container = paths.container_directory(environment)
        databases = paths.database_paths(container)
        if not databases:
            return cls(container=container, databases=[], account=None)

        uuid = derive.platform_uuid()
        by_stem = _stems(databases)

        cached = _remembered_id(environment, uuid)
        cheap = [identifier for identifier in (cached, *_candidate_ids(environment)) if identifier]
        account = cls._resolve(cheap, databases, by_stem, uuid)

        if account is None and recover:
            # Nothing cheap worked, so fall back to the one expensive path: the
            # account id lives only as a hash, and this searches for its
            # preimage. It is bounded and confirmed, and its result is cached
            # below so it does not run twice.
            hashes: set[str] = set()
            for path in paths.preference_paths(environment):
                hashes |= derive.account_hashes(derive.read_preferences(path))
            recovered = [i for i in derive.recover_user_ids(hashes) if i not in cheap]
            account = cls._resolve(recovered, databases, by_stem, uuid)

        if account is not None:
            _remember_id(environment, uuid, account.user_id)
        return cls(container=container, databases=databases, account=account)

    @staticmethod
    def _resolve(
        candidates: Iterable[int],
        databases: list[Path],
        by_stem: dict[str, Path],
        uuid: str,
    ) -> Optional[Account]:
        """Return the account among *candidates* that opens a database, or None.

        A candidate whose derived filename is actually present is proof enough
        to try alone, so when any candidate names a file, only those are
        attempted and only against their own file — one derivation and one
        decryption, no guessing.  Only when no candidate reproduces a filename
        does discovery fall back to trying each candidate against every
        database, which is the same proof by header without the name to point
        the way.
        """
        ordered = list(candidates)
        # Check candidates one-by-one: each database_name derivation is a 100k-iteration
        # PBKDF2 calculation, so if the first candidate (e.g. cached id) matches,
        # return immediately without deriving the remaining candidates.
        named: list[tuple[int, Path]] = []
        for identifier in ordered:
            stem = derive.database_name(identifier, uuid)
            matched = by_stem.get(stem)
            if matched is not None:
                geometry = cipher.detect_file(matched, derive.passphrase(identifier, uuid))
                if geometry is not None:
                    return Account(user_id=identifier, database=matched, geometry=geometry)
                named.append((identifier, matched))

        # Fall back to trying each candidate against every database if no stem matched.
        if not named:
            for identifier in ordered:
                passphrase = derive.passphrase(identifier, uuid)
                for database in databases:
                    geometry = cipher.detect_file(database, passphrase)
                    if geometry is not None:
                        return Account(user_id=identifier, database=database, geometry=geometry)
        return None

    def require(self) -> Account:
        """Return the account, turning every way of having none into an error."""
        if self.account is not None:
            return self.account
        if not self.databases:
            raise KakaoReadError(
                ErrorCode.NO_USER_DATA,
                "no KakaoTalk database was found in this machine's container; "
                "sign in to the KakaoTalk desktop client at least once",
                container=str(self.container),
            )
        raise KakaoReadError(
            ErrorCode.KEY_UNAVAILABLE,
            "a KakaoTalk database is present but no account on this machine derives "
            "a passphrase that opens it; the database may belong to a different "
            "macOS user or a different Mac",
            databases=len(self.databases),
        )
