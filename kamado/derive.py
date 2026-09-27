"""Recompute the passphrase the macOS client hands to SQLCipher.

The Windows client kept its key in memory and nowhere else, so that build went
looking for it in the process heap.  The macOS client does not need to: it
rebuilds the passphrase on every launch out of two values that are already on
the machine — the hardware's ``IOPlatformUUID`` and the signed-in account's
``userId`` — and so can this program.

Two consequences follow, and both are load-bearing:

* **KakaoTalk does not have to be running.**  There is no heap to read and no
  window to open, so every conversation stored locally is readable at any time.
* **The database's own *filename* is derived from the same two values**, which
  turns discovery into a proof: when a file in the container is named exactly
  what a candidate account derives, that candidate is the account, and its
  passphrase is the one that opens the file.  Nothing is guessed.

The derivation itself is not this project's discovery; it is the published
scheme the client uses, transcribed here in the standard library's terms.
Both halves are PBKDF2-HMAC-SHA256 at 100,000 iterations over 128 bytes of
output, differing only in how they arrange the same two inputs.
"""

from __future__ import annotations

import base64
import hashlib
import multiprocessing
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Final, Iterator, Optional

from .errors import ErrorCode, KamadoError

_ITERATIONS: Final = 100_000
_DERIVED_BYTES: Final = 128

_NAME_START: Final = 28
_NAME_LENGTH: Final = 78
"""The database filename is one slice of the first derivation's hex digest."""

_IOREG: Final = "/usr/sbin/ioreg"
_IOREG_TIMEOUT_SECONDS: Final = 10

_UUID_PATTERN: Final = re.compile(
    r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"'
)

_TRANSPARENCY_PREFIX: Final = "FSChatWindowTransparency"
_FRAME_PREFIX: Final = "NSWindow Frame FSChatWindowFrame_"
_ACCOUNT_LIST_KEY: Final = "AlertKakaoIDsList"
_DIRECT_KEYS: Final = ("userId", "user_id", "userID", "KAKAO_USER_ID")


def platform_uuid(runner=subprocess.run) -> str:
    """Return this machine's ``IOPlatformUUID``.

    Read through ``ioreg`` rather than through a framework binding because it
    is the one interface that needs no permission and no dependency.
    """
    try:
        completed = runner(
            [_IOREG, "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=_IOREG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise KamadoError(
            ErrorCode.NO_USER_DATA,
            f"could not run {_IOREG} to read this machine's hardware identifier",
        ) from error

    found = _UUID_PATTERN.search(completed.stdout or "")
    if found is None:
        raise KamadoError(
            ErrorCode.NO_USER_DATA,
            "this machine reported no IOPlatformUUID, so no KakaoTalk key can be derived",
        )
    return found.group(1)


def _hashed_uuid(uuid: str) -> str:
    """The client's own fingerprint of the machine: SHA-1 then SHA-256, base64."""
    raw = uuid.encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest() + hashlib.sha256(raw).digest()).decode(
        "ascii"
    )


def _pbkdf2(password: str, salt: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS, _DERIVED_BYTES
    )


def database_name(user_id: int, uuid: str) -> str:
    """Return the bare filename the client stores this account's database under."""
    password = ".".join([".", "F", str(user_id), "A", "F", uuid[::-1], ".", "|"])
    salt = _hashed_uuid(uuid)[::-1]
    return _pbkdf2(password, salt).hex()[_NAME_START : _NAME_START + _NAME_LENGTH]


def passphrase(user_id: int, uuid: str) -> str:
    """Return the SQLCipher passphrase for this account's database.

    SQLCipher is handed this as a *string*, not as raw key material, so it runs
    its own PBKDF2 over it using the salt in the file; see :mod:`.cipher`.
    """
    parts = [
        "A", _hashed_uuid(uuid), "|", "F", uuid[:5], "H", str(user_id), "|", uuid[7:],
    ]
    password = "F".join(parts)[::-1]
    salt = uuid[int(len(uuid) * 0.3) :]
    return _pbkdf2(password, salt).hex()


def _longest_common_suffix(values: list[str]) -> str:
    """Return the tail every value shares, which is where the account id hides."""
    if not values:
        return ""
    shortest = min(len(value) for value in values)
    length = 0
    while length < shortest:
        character = values[0][-1 - length]
        if any(value[-1 - length] != character for value in values):
            break
        length += 1
    return values[0][len(values[0]) - length :] if length else ""


def _suffix_ids(plist: dict, prefix: str) -> Iterator[int]:
    """Yield the account id shared by a family of per-window preference keys.

    The client writes one key per chat window, each ending in the signed-in
    account's id; a single window says nothing, but two windows share exactly
    that suffix.
    """
    keys = [key for key in plist if key.startswith(prefix)]
    if len(keys) < 2:
        return
    suffix = _longest_common_suffix([key[len(prefix) :] for key in keys])
    digits = re.search(r"\d+$", suffix)
    if digits is not None and digits.group():
        yield int(digits.group())


def candidate_user_ids(plist: dict) -> list[int]:
    """Return every account id this preferences file suggests, best first.

    More than one is normal on a machine that has had more than one sign-in,
    which is why these are candidates rather than an answer: the database's
    filename decides between them.
    """
    found: list[int] = []

    def offer(value: object) -> None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if number > 0 and number not in found:
            found.append(number)

    for prefix in (_TRANSPARENCY_PREFIX, _FRAME_PREFIX):
        for identifier in _suffix_ids(plist, prefix):
            offer(identifier)
    for key in _DIRECT_KEYS:
        if key in plist:
            offer(plist[key])
    listed = plist.get(_ACCOUNT_LIST_KEY)
    if isinstance(listed, (list, tuple)):
        for entry in listed:
            offer(entry)
    return found


def read_preferences(path: Path) -> dict:
    """Load one preferences plist, treating a missing or damaged one as empty.

    A refusal is not either of those. ``PermissionError`` is left to rise, so
    the guard around the read can name the permission rather than the tool
    quietly concluding that no account is signed in.
    """
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except PermissionError:
        raise
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# -- recovering an account id the preferences only hash ----------------------

_REVISION_PREFIXES: Final = ("DESIGNATEDFRIENDSREVISION:", "PROFILELISTREVISION:")
"""Preference keys whose suffix is SHA-512 of the account they belong to."""

EMPTY_ACCOUNT_HASH: Final = hashlib.sha512(b"0").hexdigest()
"""SHA-512("0"), which the client writes for the absent account."""

DEFAULT_SEARCH_LIMIT: Final = 2_000_000_000
"""How far a recovery searches before giving up.

The account observed on the machine this was written against is eight digits,
and KakaoTalk ids have not reached ten; two billion covers that with room, and
is about a minute spread across a modern machine's cores.
"""

_CHUNK: Final = 5_000_000


def account_hashes(plist: dict) -> set[str]:
    """Return the SHA-512 account hashes this preferences file vouches for.

    The client never writes the account id down, but it does write keys whose
    *suffix* is the id's hash, and it gives the live account a non-zero
    revision.  So the id is not stored, yet it is pinned: any candidate can be
    confirmed against these in a single hash.
    """
    found: set[str] = set()
    for key, value in plist.items():
        if not key.startswith(_REVISION_PREFIXES):
            continue
        suffix = key.split(":", 1)[1]
        if suffix and suffix != EMPTY_ACCOUNT_HASH and value:
            found.add(suffix)
    return found


def confirms(user_id: int, hashes: set[str]) -> bool:
    """Whether an id is the account those hashes name. One hash, no guessing."""
    return hashlib.sha512(str(user_id).encode("ascii")).hexdigest() in hashes


def _scan(bounds: tuple[int, int, frozenset]) -> list[int]:
    start, end, targets = bounds
    digest = hashlib.sha512
    found = []
    for number in range(start, end):
        if digest(str(number).encode("ascii")).hexdigest() in targets:
            found.append(number)
    return found


def recover_user_ids(
    hashes: set[str], limit: int = DEFAULT_SEARCH_LIMIT, workers: Optional[int] = None
) -> list[int]:
    """Find the account ids whose hashes these are, by searching for them.

    This is the one expensive thing in the program, and it exists because the
    client stopped writing its account id anywhere in the clear: what is left
    is the hash, so the id is recovered by hashing candidates until one
    matches.  It is a *search*, not a break — the space is small because
    account ids are small, and every answer it returns is confirmed by the
    hash rather than assumed.

    The result is meant to be remembered; see :mod:`.keyring`, which stores it
    so that this runs once on a machine rather than once per command.
    """
    if not hashes:
        return []
    targets = frozenset(hashes)
    pending = len(targets)
    found: list[int] = []

    # Fork rather than spawn: the children re-execute nothing, which keeps a
    # command-line program from re-entering its own entry point, and the work
    # is pure arithmetic with no inherited state to go wrong.
    context = multiprocessing.get_context("fork")
    chunks = [
        (start, min(start + _CHUNK, limit), targets) for start in range(0, limit, _CHUNK)
    ]
    with context.Pool(workers or os.cpu_count() or 1) as pool:
        for hits in pool.imap_unordered(_scan, chunks):
            found.extend(hits)
            if len(found) >= pending:
                pool.terminate()
                break
    return sorted(found)
