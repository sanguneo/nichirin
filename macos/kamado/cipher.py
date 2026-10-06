"""What shape of SQLCipher database this is, and which key opens it.

The Windows build never needed this module: it lifted a finished 32-byte key
out of the client's heap, and the heap also told it the page size and the
reserve.  On macOS nothing is lifted.  The client is handed a *passphrase* and
SQLCipher turns that into a key using the salt stored in the clear at the front
of the file, so this program has to perform the same derivation — and has to
know which of SQLCipher's two parameter sets was used, because they disagree
about the hash, the iteration count and how many bytes each page reserves.

Rather than trust a version number that is written down nowhere, the geometry
is *proved*: a candidate is accepted only when the page it decrypts declares
back exactly the page size and reserve that candidate assumed.  Two independent
fields have to agree, which a wrong key does not manage.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator, Optional

from .commoncrypto import AES_BLOCK_SIZE, AES_256_KEY_SIZE, PreparedKey

SALT_SIZE: Final = 16
"""Length of the plaintext key derivation salt at the start of page 1."""

_HEADER_PAGE_SIZE: Final = slice(0, 2)
"""Where the page size sits, counting from the start of *decrypted* page 1."""

_HEADER_RESERVE: Final = 4
"""Offset of the reserved-bytes-per-page field, likewise."""

_MAX_PAGE_SIZE: Final = 65536
"""SQLite's largest page, which the header encodes as 1 rather than 65536."""


@dataclass(frozen=True)
class CipherProfile:
    """One of SQLCipher's parameter sets, named by the version that ships it."""

    version: int
    kdf_hash: str
    iterations: int
    reserve: int

    def derive(self, passphrase: str, salt: bytes) -> bytes:
        """Turn a passphrase and a database's salt into its AES key.

        SQLCipher treats a plain string key as a passphrase and runs PBKDF2
        over it; only the ``x'...'`` form is taken as raw key material.  The
        passphrase KakaoTalk uses is a long hex string, but it is still a
        passphrase, so it is hashed here exactly as its own bytes.
        """
        if len(salt) != SALT_SIZE:
            raise ValueError(f"a SQLCipher salt is {SALT_SIZE} bytes")
        return hashlib.pbkdf2_hmac(
            self.kdf_hash, passphrase.encode("utf-8"), salt, self.iterations, AES_256_KEY_SIZE
        )


PROFILES: Final = (
    CipherProfile(version=4, kdf_hash="sha512", iterations=256_000, reserve=80),
    CipherProfile(version=3, kdf_hash="sha1", iterations=64_000, reserve=48),
)
"""Newest first, because a current client is the common case."""

PAGE_SIZES: Final = (4096, 1024, 2048, 8192, 512, 16384, 32768, 65536)
"""Candidate page sizes, likeliest first: 4096 is SQLCipher 4's default and
1024 is SQLCipher 3's."""


@dataclass(frozen=True)
class Geometry:
    """A proven way to read one database: which parameters, which page size."""

    profile: CipherProfile
    page_size: int
    key: bytes

    @property
    def reserve(self) -> int:
        return self.profile.reserve

    def prepared(self) -> PreparedKey:
        """Open an AES key for this database's pages."""
        return PreparedKey(self.key)


def validate_geometry(page_size: int, reserve: int) -> None:
    """Reject page geometry that could not hold a payload and an IV."""
    if page_size <= 0 or page_size % AES_BLOCK_SIZE != 0:
        raise ValueError("page_size must be a positive multiple of the AES block size")
    if reserve < AES_BLOCK_SIZE or reserve >= page_size:
        raise ValueError("reserve must leave room for data and contain a 16-byte IV")


def read_salt(page: bytes) -> bytes:
    """Return the plaintext salt that opens this database, given its first page."""
    if len(page) < SALT_SIZE:
        raise ValueError(f"a salt needs the first {SALT_SIZE} bytes of page 1")
    return page[:SALT_SIZE]


def _declares(plaintext: bytes, page_size: int, reserve: int) -> bool:
    """Whether a decrypted page 1 describes the geometry used to decrypt it.

    *plaintext* starts 16 bytes into the logical page, because SQLCipher keeps
    the salt where SQLite keeps its magic string, so the two header fields sit
    at offsets 0 and 4 rather than 16 and 20.
    """
    if len(plaintext) < _HEADER_RESERVE + 1:
        return False
    declared = int.from_bytes(plaintext[_HEADER_PAGE_SIZE], "big")
    if declared == 1:
        declared = _MAX_PAGE_SIZE
    return declared == page_size and plaintext[_HEADER_RESERVE] == reserve


def candidates(
    profiles: tuple[CipherProfile, ...] = PROFILES,
    page_sizes: tuple[int, ...] = PAGE_SIZES,
) -> Iterator[tuple[CipherProfile, int]]:
    """Yield every (profile, page size) pair worth trying, likeliest first."""
    for profile in profiles:
        for page_size in page_sizes:
            if page_size > profile.reserve + AES_BLOCK_SIZE:
                yield profile, page_size


def detect(header: bytes, passphrase: str) -> Optional[Geometry]:
    """Work out how to read a database from its first bytes, or return None.

    *header* must hold at least one whole page; the largest candidate page size
    bounds how much that is.  A returned :class:`Geometry` has been proved
    against the file, so a wrong passphrase yields None rather than nonsense.
    """
    if len(header) < SALT_SIZE:
        return None
    salt = read_salt(header)

    derived: dict[int, bytes] = {}
    for profile, page_size in candidates():
        if len(header) < page_size:
            continue
        key = derived.get(profile.version)
        if key is None:
            key = profile.derive(passphrase, salt)
            derived[profile.version] = key

        payload_end = page_size - profile.reserve
        with PreparedKey(key) as prepared:
            plaintext = prepared.decrypt(
                header[payload_end : payload_end + AES_BLOCK_SIZE],
                header[SALT_SIZE:payload_end],
            )
        if _declares(plaintext, page_size, profile.reserve):
            return Geometry(profile=profile, page_size=page_size, key=key)
    return None


def detect_file(database: Path, passphrase: str) -> Optional[Geometry]:
    """Read just enough of a database to work out how to decrypt it."""
    try:
        with database.open("rb") as handle:
            header = handle.read(max(PAGE_SIZES))
    except OSError:
        return None
    return detect(header, passphrase)
