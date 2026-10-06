"""Turn SQLCipher pages back into the plain SQLite pages SQLite expects.

A SQLCipher 4 page is ``[payload][iv][hmac]`` where the trailing ``reserve``
bytes hold the IV and authentication tag.  Page 1 additionally keeps its first
16 bytes in the clear: that is the key derivation salt, which is also how a
database is matched to a key found in KakaoTalk's memory.

Reconstruction rewrites the well-known SQLite header onto page 1 and zero-fills
the reserve area, because SQLite itself only needs to see a consistent
``reserved bytes per page`` value, not the original IV or tag.
"""

from __future__ import annotations

from . import PAGE_SIZE, RESERVE_SIZE
from .cng import AES_BLOCK_SIZE, PreparedKey

SQLITE_HEADER = b"SQLite format 3\x00"
"""The 16 bytes SQLCipher replaces with the salt on page 1."""

SALT_SIZE = 16
"""Length of the plaintext key derivation salt at the start of page 1."""


def validate_geometry(page_size: int, reserve: int) -> None:
    """Reject page geometry that could not hold a payload and an IV."""
    if page_size <= 0 or page_size % AES_BLOCK_SIZE != 0:
        raise ValueError("page_size must be a positive multiple of the AES block size")
    if reserve < AES_BLOCK_SIZE or reserve >= page_size:
        raise ValueError("reserve must leave room for data and contain a 16-byte IV")


def reconstruct_page(
    prepared: PreparedKey,
    number: int,
    page: bytes,
    page_size: int = PAGE_SIZE,
    reserve: int = RESERVE_SIZE,
) -> bytes:
    """Decrypt one page and return it as a plain SQLite page of the same length.

    *number* is the one-based page number, because page 1 is encrypted from a
    different offset than every other page.
    """
    validate_geometry(page_size, reserve)
    if len(page) < page_size:
        raise ValueError(f"page must contain at least {page_size} bytes")
    if number < 1:
        raise ValueError("page numbers are one-based")

    payload_end = page_size - reserve
    start = SALT_SIZE if number == 1 else 0
    plaintext = prepared.decrypt(page[payload_end : payload_end + AES_BLOCK_SIZE], page[start:payload_end])
    header = SQLITE_HEADER if number == 1 else b""
    return header + plaintext + bytes(reserve)


def read_salt(page: bytes) -> bytes:
    """Return the plaintext salt that identifies which key opens this database."""
    if len(page) < SALT_SIZE:
        raise ValueError(f"a salt needs the first {SALT_SIZE} bytes of page 1")
    return page[:SALT_SIZE]
