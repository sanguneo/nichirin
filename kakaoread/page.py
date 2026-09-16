"""Turn SQLCipher pages back into the plain SQLite pages SQLite expects.

A SQLCipher page is ``[payload][iv][hmac]`` where the trailing ``reserve``
bytes hold the IV and authentication tag.  Page 1 additionally keeps its first
16 bytes in the clear: that is the key derivation salt, which is what
:mod:`.cipher` turns the account's passphrase into a key with.

Reconstruction rewrites the well-known SQLite header onto page 1 and zero-fills
the reserve area, because SQLite itself only needs to see a consistent
``reserved bytes per page`` value, not the original IV or tag.  The tag is not
checked: a wrong key is caught instead by SQLite's own integrity check over the
assembled image, which no plausible-looking garbage survives.
"""

from __future__ import annotations

from .cipher import SALT_SIZE, validate_geometry
from .commoncrypto import AES_BLOCK_SIZE, PreparedKey

SQLITE_HEADER = b"SQLite format 3\x00"
"""The 16 bytes SQLCipher replaces with the salt on page 1."""

__all__ = ["SALT_SIZE", "SQLITE_HEADER", "reconstruct_page", "validate_geometry"]


def reconstruct_page(
    prepared: PreparedKey,
    number: int,
    page: bytes,
    page_size: int,
    reserve: int,
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
    plaintext = prepared.decrypt(
        page[payload_end : payload_end + AES_BLOCK_SIZE], page[start:payload_end]
    )
    header = SQLITE_HEADER if number == 1 else b""
    return header + plaintext + bytes(reserve)
