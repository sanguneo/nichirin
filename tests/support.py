"""Synthetic fixtures for the test suite.

Nothing here reads the machine's real KakaoTalk data.  Encryption uses the same
Windows CNG provider the reader uses, so a round-trip test proves the real code
path rather than a mock of it.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Iterable, Sequence

from kanao import cng, page as page_codec

PAGE_SIZE = 4096
RESERVE = 80
KEY = bytes(range(32))
IV = bytes(range(16))
SALT = b"synthetic-salt!!"


def encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt with AES-256-CBC so fixtures are decryptable by the real code."""
    library = cng._bcrypt()
    library.BCryptEncrypt.argtypes = library.BCryptDecrypt.argtypes
    library.BCryptEncrypt.restype = wintypes.LONG

    algorithm = ctypes.c_void_p()
    handle = ctypes.c_void_p()
    cng._check(
        library.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "AES", None, 0), "open"
    )
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        cng._check(
            library.BCryptSetProperty(
                algorithm, "ChainingMode", ctypes.cast(mode, cng._PUCHAR), ctypes.sizeof(mode), 0
            ),
            "mode",
        )
        length = wintypes.ULONG()
        written = wintypes.ULONG()
        cng._check(
            library.BCryptGetProperty(
                algorithm, "ObjectLength", ctypes.cast(ctypes.byref(length), cng._PUCHAR),
                ctypes.sizeof(length), ctypes.byref(written), 0,
            ),
            "length",
        )
        key_object = (ctypes.c_ubyte * length.value)()
        cng._check(
            library.BCryptGenerateSymmetricKey(
                algorithm, ctypes.byref(handle), key_object, length.value,
                (ctypes.c_ubyte * len(key)).from_buffer_copy(key), len(key), 0,
            ),
            "key",
        )
        output = (ctypes.c_ubyte * len(plaintext))()
        iv_buffer = (ctypes.c_ubyte * len(iv)).from_buffer_copy(iv)
        cng._check(
            library.BCryptEncrypt(
                handle, plaintext, len(plaintext), None, iv_buffer, len(iv),
                output, len(output), ctypes.byref(written), 0,
            ),
            "encrypt",
        )
        return bytes(output[: written.value])
    finally:
        if handle.value:
            library.BCryptDestroyKey(handle)
        library.BCryptCloseAlgorithmProvider(algorithm, 0)


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


def codec_context(
    salt_pointer: int = 0x1000,
    read_context: int = 0x2000,
    iterations: int = 256000,
    key_size: int = 32,
    page_size: int = 4096,
    reserve: int = 80,
) -> bytes:
    """Build a synthetic SQLCipher codec_ctx with the layout the scanner expects."""
    blob = bytearray(0x68)
    struct.pack_into("<I", blob, 0x04, iterations)
    struct.pack_into("<I", blob, 0x10, key_size)
    struct.pack_into("<I", blob, 0x1C, page_size)
    struct.pack_into("<I", blob, 0x24, reserve)
    struct.pack_into("<Q", blob, 0x40, salt_pointer)
    struct.pack_into("<Q", blob, 0x60, read_context)
    return bytes(blob)


def memory_reader(mapping: dict[tuple[int, int], bytes]):
    """Return a read_memory callable backed by an explicit address table."""

    def read(address: int, size: int):
        return mapping.get((address, size))

    return read


def flatten(blocks: Iterable[tuple[int, bytes]]) -> bytes:
    return b"".join(data for _address, data in blocks)


def reserve_pages(raw: bytes) -> list[bytes]:
    """Rewrite a plain SQLite file so each page keeps 80 bytes reserved.

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
        connection.execute("PRAGMA page_size=4096")
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
