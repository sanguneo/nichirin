"""Synthetic fixtures for the test suite.

Nothing here reads the machine's real KakaoTalk data.  Encryption goes through
the same CommonCrypto provider the reader decrypts with, and the fixture key is
derived from a passphrase exactly as :mod:`kamado.cipher` derives a real
one, so a round-trip test exercises the real code path rather than a mock of
it — including the geometry detection, which only accepts a database whose own
header agrees with the parameters used to read it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
from typing import Sequence

from kamado import cipher, page as page_codec

PROFILE = cipher.PROFILES[0]
"""SQLCipher 4 parameters: SHA-512, 256,000 iterations, 80 reserved bytes."""

PAGE_SIZE = 4096
RESERVE = PROFILE.reserve
SALT = b"synthetic-salt!!"
PASSPHRASE = "0123456789abcdef" * 4
KEY = PROFILE.derive(PASSPHRASE, SALT)
IV = bytes(range(16))

_kCCEncrypt = 0
_kCCAlgorithmAES = 0


def encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt with AES-256-CBC so fixtures are decryptable by the real code."""
    library = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")
    library.CCCrypt.argtypes = (
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    )
    library.CCCrypt.restype = ctypes.c_int32

    output = ctypes.create_string_buffer(len(plaintext) + 16)
    moved = ctypes.c_size_t(0)
    status = library.CCCrypt(
        _kCCEncrypt, _kCCAlgorithmAES, 0,
        key, len(key), iv,
        plaintext, len(plaintext),
        output, len(output), ctypes.byref(moved),
    )
    if status != 0:
        raise OSError(f"CCCrypt failed with status {status}")
    return output.raw[: moved.value]


def encrypted_page(plaintext: bytes, number: int, key: bytes = KEY, iv: bytes = IV) -> bytes:
    """Build one encrypted page whose reconstruction yields *plaintext*.

    *plaintext* must be a full page; page 1 keeps its first 16 bytes as the
    plaintext salt, exactly as SQLCipher stores it.
    """
    if len(plaintext) != PAGE_SIZE:
        raise ValueError("fixture pages must be a full page long")
    start = page_codec.SALT_SIZE if number == 1 else 0
    body = encrypt(key, iv, plaintext[start : PAGE_SIZE - RESERVE])
    prefix = SALT if number == 1 else b""
    return prefix + body + iv + bytes(RESERVE - len(iv))


def geometry(key: bytes = KEY) -> cipher.Geometry:
    """The geometry a fixture database is written with."""
    return cipher.Geometry(profile=PROFILE, page_size=PAGE_SIZE, key=key)


def checksum(data: bytes, state: tuple[int, int], order: str) -> tuple[int, int]:
    """SQLite's write-ahead log checksum, in the byte order the header selects."""
    first, second = state
    for left, right in struct.iter_unpack(order + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def make_wal(
    frames: Sequence[tuple[int, int, bytes]],
    order: str = "<",
    page_size: int = PAGE_SIZE,
    salt: tuple[int, int] = (17, 29),
    version: int = 3007000,
) -> bytes:
    """Build a valid log from ``(page_number, commit_size, page_bytes)`` frames."""
    magic = 0x377F0682 if order == "<" else 0x377F0683
    header = struct.pack(">6I", magic, version, page_size, 0, salt[0], salt[1])
    state = checksum(header, (0, 0), order)
    result = header + struct.pack(">2I", *state)
    for number, commit_size, body in frames:
        prefix = struct.pack(">2I", number, commit_size)
        state = checksum(prefix + body, state, order)
        result += prefix + struct.pack(">2I", *salt) + struct.pack(">2I", *state) + body
    return result


def reserve_pages(raw: bytes) -> list[bytes]:
    """Rewrite a plain SQLite file so each page keeps its reserved tail.

    SQLite fills a page to its end, but SQLCipher needs the tail for an IV and
    tag, so the client's databases carry ``reserved bytes per page = 80``.
    Python cannot ask SQLite to create such a file, so the cell content of each
    leaf page is shifted down and its pointers corrected — the same
    transformation the real format implies, applied after the fact.
    """
    pages: list[bytes] = []
    for offset in range(0, len(raw), PAGE_SIZE):
        page = bytearray(raw[offset : offset + PAGE_SIZE])
        header = 100 if offset == 0 else 0
        if page[header] != 13:
            raise ValueError(
                f"fixture page {offset // PAGE_SIZE + 1} is type {page[header]}, "
                "expected a table leaf; keep fixture databases small and simple"
            )
        count, content_start = struct.unpack_from(">HH", page, header + 3)
        page[content_start - RESERVE : PAGE_SIZE - RESERVE] = page[content_start:]
        page[PAGE_SIZE - RESERVE :] = bytes(RESERVE)
        struct.pack_into(">H", page, header + 5, content_start - RESERVE)
        for cell in range(count):
            pointer = header + 8 + 2 * cell
            address = struct.unpack_from(">H", page, pointer)[0]
            struct.pack_into(">H", page, pointer, address - RESERVE)
        if offset == 0:
            page[18] = page[19] = 2  # Write-ahead logging mode.
            page[20] = RESERVE
        pages.append(bytes(page))
    return pages


def build_database(path, rows: Sequence[tuple[int, str]]) -> list[bytes]:
    """Create a tiny SQLite database and return its reserve-adjusted pages."""
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA page_size={PAGE_SIZE}")
        connection.execute("CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT)")
        connection.executemany("INSERT INTO note VALUES (?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
    with open(path, "rb") as handle:
        raw = handle.read()
    return reserve_pages(raw)


def encrypt_pages(pages: Sequence[bytes]) -> bytes:
    """Encrypt a whole database image, page 1 keeping its plaintext salt."""
    return b"".join(
        encrypted_page(body, number=index + 1) for index, body in enumerate(pages)
    )
