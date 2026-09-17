"""Read the committed prefix of a SQLite write-ahead log.

KakaoTalk writes continuously and checkpoints rarely, so the newest messages
usually live in ``<database>.edb-wal`` rather than in the ``.edb`` file.  A
reader that ignores the WAL silently reports stale conversations, which is
worse than reporting none.

This follows SQLite's own recovery rules.  Frames are accumulated but only
become visible at a commit frame, a frame carrying a different salt pair
belongs to an earlier generation and ends the current one, and a trailing
partial frame is simply the writer mid-flight.  A complete frame whose
checksum does not match is corruption and is refused rather than returned.

SQLCipher's per-page HMAC is deliberately *not* verified here: these checksums
detect torn writes, not tampering, and the distinction is documented for users
rather than papered over.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

WAL_HEADER_SIZE: Final = 32
FRAME_HEADER_SIZE: Final = 24
_BIG_ENDIAN_MAGIC: Final = 0x377F0683
_LITTLE_ENDIAN_MAGIC: Final = 0x377F0682
_SUPPORTED_VERSION: Final = 3007000


class WalFormatError(ValueError):
    """The write-ahead log could not be trusted to describe a consistent database."""


@dataclass(frozen=True)
class WalSnapshot:
    """Committed WAL content as of one read."""

    pages: dict[int, bytes]
    """Newest committed image of every page the WAL overrides, by page number."""

    database_pages: int
    """Page count of the database after the last commit, which may truncate it."""


def _checksummer(order: str):
    unpack = struct.Struct(order + "II").iter_unpack

    def checksum(data: bytes, state: tuple[int, int]) -> tuple[int, int]:
        first, second = state
        for left, right in unpack(data):
            first = (first + left + second) & 0xFFFFFFFF
            second = (second + right + first) & 0xFFFFFFFF
        return first, second

    return checksum


def parse(wal: bytes, page_size: int, base_pages: int) -> WalSnapshot:
    """Return the committed pages in *wal* for a database of *base_pages* pages.

    An empty or header-only log contributes nothing, which is the normal state
    right after a checkpoint.
    """
    if not wal:
        return WalSnapshot({}, base_pages)
    if len(wal) < WAL_HEADER_SIZE:
        raise WalFormatError("write-ahead log header is truncated")

    magic, version, declared_page_size, _sequence, salt1, salt2, check1, check2 = struct.unpack(
        ">8I", wal[:WAL_HEADER_SIZE]
    )
    if magic not in (_LITTLE_ENDIAN_MAGIC, _BIG_ENDIAN_MAGIC):
        raise WalFormatError(f"unrecognized write-ahead log magic 0x{magic:08X}")
    if version != _SUPPORTED_VERSION:
        raise WalFormatError(f"unsupported write-ahead log version {version}")
    if declared_page_size != page_size:
        raise WalFormatError(
            f"write-ahead log page size {declared_page_size} does not match database page size {page_size}"
        )

    # The low bit of the magic selects the byte order used by the checksums.
    checksum = _checksummer(">" if magic & 1 else "<")
    state = checksum(wal[:24], (0, 0))
    if state != (check1, check2):
        raise WalFormatError("write-ahead log header checksum mismatch")

    frame_size = page_size + FRAME_HEADER_SIZE
    page_limit = base_pages + (len(wal) - WAL_HEADER_SIZE) // frame_size
    committed: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}
    database_pages = base_pages

    for offset in range(WAL_HEADER_SIZE, len(wal) - frame_size + 1, frame_size):
        header = wal[offset : offset + FRAME_HEADER_SIZE]
        number, commit_size, frame_salt1, frame_salt2, frame_check1, frame_check2 = struct.unpack(
            ">6I", header
        )
        if (frame_salt1, frame_salt2) != (salt1, salt2):
            # A reused log can still hold intact frames from an earlier
            # generation; they are not part of this one.
            break

        page = wal[offset + FRAME_HEADER_SIZE : offset + frame_size]
        state = checksum(header[:8] + page, state)
        if state != (frame_check1, frame_check2):
            raise WalFormatError(f"write-ahead log frame checksum mismatch at page {number}")
        if not 1 <= number <= page_limit or commit_size > page_limit:
            raise WalFormatError(f"write-ahead log frame declares an impossible page {number}")

        pending[number] = page
        if commit_size:
            committed.update(pending)
            pending.clear()
            database_pages = commit_size

    # Frames after the last commit are a transaction still in progress, even
    # when they overwrite a page an earlier transaction already committed.
    return WalSnapshot(committed, database_pages)
