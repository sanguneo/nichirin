"""Find the live SQLCipher keys inside the running KakaoTalk process.

KakaoTalk derives its data encryption keys at run time, so there is nothing on
disk to derive them from: the only copy lives in the client's own heap while it
has the database open.  This module reads that heap, and nothing else — the
process is opened for query and read access only, is never written to, never
suspended, and never attached to as a debugger.

A candidate is recognized by the shape of SQLCipher's ``codec_ctx`` struct
rather than by a hard-coded address, so it survives the client being relocated
by ASLR.  The validated 64-bit layout is::

    0x04  u32   PBKDF2 iterations   == 256000
    0x10  u32   key size            == 32
    0x1C  u32   page size           == 4096
    0x24  u32   reserve size        == 80
    0x40  ptr   -> 16-byte salt
    0x60  ptr   -> read context, whose +0x08 points at the 32-byte key

The consequence worth knowing: a key exists only for a database the client has
actually opened.  A room KakaoTalk has never displayed has no key in memory and
therefore cannot be read at all.
"""

from __future__ import annotations

import ctypes
import struct
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Final, Iterable, Iterator, Optional

CONTEXT_SIZE: Final = 0x68
KDF_ITERATIONS: Final = 256000
KEY_SIZE: Final = 32
SALT_SIZE: Final = 16

_KDF_ITERATIONS_OFFSET: Final = 0x04
_KEY_SIZE_OFFSET: Final = 0x10
_PAGE_SIZE_OFFSET: Final = 0x1C
_RESERVE_OFFSET: Final = 0x24
_SALT_POINTER_OFFSET: Final = 0x40
_READ_CONTEXT_OFFSET: Final = 0x60
_KEY_POINTER_OFFSET: Final = 0x08

_SIGNATURE: Final = struct.pack("<I", KDF_ITERATIONS)
_EXPECTED_PAGE_SIZE: Final = 4096
_EXPECTED_RESERVE: Final = 80

_PROCESS_QUERY_INFORMATION: Final = 0x0400
_PROCESS_VM_READ: Final = 0x0010
_SNAPSHOT_PROCESSES: Final = 0x00000002
_MEM_COMMIT: Final = 0x1000
_MEM_PRIVATE: Final = 0x20000
_PAGE_GUARD: Final = 0x100
_PAGE_NOACCESS: Final = 0x01
_READABLE = frozenset({0x02, 0x04, 0x08, 0x20, 0x40, 0x80})
_WRITABLE = frozenset({0x04, 0x08, 0x40, 0x80})
_CHUNK_SIZE: Final = 1024 * 1024

_EXECUTABLE_NAME: Final = "kakaotalk.exe"

ReadMemory = Callable[[int, int], Optional[bytes]]
"""Reads *size* bytes at an address, or returns None when unreadable."""


@dataclass(frozen=True)
class CodecContext:
    """One open SQLCipher database, as seen from the client's heap."""

    address: int
    salt: bytes
    """The 16-byte salt, which also prefixes the database file it unlocks."""
    key: bytes
    """The 32-byte data encryption key."""


def decode_context(blob: bytes) -> Optional[tuple[int, int]]:
    """Return the salt and read-context pointers if *blob* looks like a codec_ctx.

    Every field is checked against the layout this client is known to use, so
    unrelated heap bytes that happen to contain the iteration count are
    rejected rather than followed.
    """
    if len(blob) < CONTEXT_SIZE:
        return None
    if (
        struct.unpack_from("<I", blob, _KDF_ITERATIONS_OFFSET)[0] != KDF_ITERATIONS
        or struct.unpack_from("<I", blob, _KEY_SIZE_OFFSET)[0] != KEY_SIZE
        or struct.unpack_from("<I", blob, _PAGE_SIZE_OFFSET)[0] != _EXPECTED_PAGE_SIZE
        or struct.unpack_from("<I", blob, _RESERVE_OFFSET)[0] != _EXPECTED_RESERVE
    ):
        return None

    salt_pointer = struct.unpack_from("<Q", blob, _SALT_POINTER_OFFSET)[0]
    read_context = struct.unpack_from("<Q", blob, _READ_CONTEXT_OFFSET)[0]
    if not salt_pointer or not read_context:
        return None
    return salt_pointer, read_context


def resolve_context(
    address: int, blob: bytes, read_memory: ReadMemory
) -> Optional[CodecContext]:
    """Follow a candidate's pointers and return it only if every read succeeds."""
    pointers = decode_context(blob)
    if pointers is None:
        return None
    salt_pointer, read_context = pointers

    salt = read_memory(salt_pointer, SALT_SIZE)
    if salt is None or len(salt) != SALT_SIZE:
        return None

    key_pointer_bytes = read_memory(read_context + _KEY_POINTER_OFFSET, 8)
    if key_pointer_bytes is None or len(key_pointer_bytes) != 8:
        return None
    key_pointer = struct.unpack("<Q", key_pointer_bytes)[0]
    if not key_pointer:
        return None

    key = read_memory(key_pointer, KEY_SIZE)
    if key is None or len(key) != KEY_SIZE:
        return None
    return CodecContext(address=address, salt=salt, key=key)


def resolve_many(
    addresses: Iterable[int], read_memory: ReadMemory
) -> list[CodecContext]:
    """Re-read contexts at addresses a previous scan found.

    Nothing here trusts an address: each one is decoded and followed exactly
    as a fresh candidate would be, so a heap that has moved on yields fewer
    contexts rather than a wrong key.
    """
    found: list[CodecContext] = []
    for address in addresses:
        blob = read_memory(address, CONTEXT_SIZE)
        if blob is None:
            continue
        context = resolve_context(address, blob, read_memory)
        if context is not None:
            found.append(context)
    return found


def scan_blocks(
    blocks: Iterable[tuple[int, bytes]], read_memory: ReadMemory
) -> Iterator[CodecContext]:
    """Find codec contexts across a stream of ``(address, data)`` memory blocks.

    Heaps are read in chunks, so a struct can straddle two blocks.  Carrying
    ``CONTEXT_SIZE - 1`` trailing bytes into the next contiguous block is
    exactly enough to catch those: a signature closer than that to the end of a
    block belongs to a struct that already fitted entirely in the block.
    """
    seen: set[int] = set()
    carry = b""
    carry_address = 0

    for address, data in blocks:
        if carry and carry_address + len(carry) == address:
            buffer = carry + data
            base = carry_address
        else:
            buffer = data
            base = address

        position = 0
        while True:
            hit = buffer.find(_SIGNATURE, position)
            if hit < 0:
                break
            start = hit - _KDF_ITERATIONS_OFFSET
            if start >= 0 and start + CONTEXT_SIZE <= len(buffer):
                candidate_address = base + start
                if candidate_address not in seen:
                    context = resolve_context(
                        candidate_address, buffer[start : start + CONTEXT_SIZE], read_memory
                    )
                    if context is not None:
                        seen.add(candidate_address)
                        yield context
            position = hit + 1

        carry = buffer[-(CONTEXT_SIZE - 1) :]
        carry_address = base + len(buffer) - len(carry)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _MemoryRegion(ctypes.Structure):
    _fields_ = [
        ("base_address", ctypes.c_void_p),
        ("allocation_base", ctypes.c_void_p),
        ("allocation_protect", wintypes.DWORD),
        ("partition_id", wintypes.WORD),
        ("region_size", ctypes.c_size_t),
        ("state", wintypes.DWORD),
        ("protect", wintypes.DWORD),
        ("type", wintypes.DWORD),
    ]


_kernel32: ctypes.CDLL | None = None


def _library() -> ctypes.CDLL:
    """Load and configure kernel32 lazily so this module imports off Windows."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32

    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    library.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    library.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    library.Process32FirstW.restype = wintypes.BOOL
    library.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    library.Process32NextW.restype = wintypes.BOOL
    library.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    library.OpenProcess.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.CloseHandle.restype = wintypes.BOOL
    library.VirtualQueryEx.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MemoryRegion), ctypes.c_size_t,
    )
    library.VirtualQueryEx.restype = ctypes.c_size_t
    library.ReadProcessMemory.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    library.ReadProcessMemory.restype = wintypes.BOOL

    _kernel32 = library
    return library


def find_pid() -> Optional[int]:
    """Return a running KakaoTalk process id, or None when it is not running."""
    try:
        library = _library()
        snapshot = library.CreateToolhelp32Snapshot(_SNAPSHOT_PROCESSES, 0)
    except (AttributeError, OSError):
        return None
    if snapshot == ctypes.c_void_p(-1).value:
        return None

    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not library.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.casefold() == _EXECUTABLE_NAME:
                return int(entry.th32ProcessID)
            if not library.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        library.CloseHandle(snapshot)


def _is_candidate_region(region: _MemoryRegion) -> bool:
    """Only committed, private, readable *and* writable heap holds a live key."""
    protection = int(region.protect) & 0xFF
    return (
        int(region.state) == _MEM_COMMIT
        and int(region.type) == _MEM_PRIVATE
        and not int(region.protect) & (_PAGE_GUARD | _PAGE_NOACCESS)
        and protection in _READABLE
        and protection in _WRITABLE
    )


@contextmanager
def _opened(pid: int) -> Iterator[Optional[tuple[ctypes.CDLL, int, ReadMemory]]]:
    """Open a process for query and read access, and always close it again."""
    if pid <= 0:
        yield None
        return
    try:
        library = _library()
        handle = library.OpenProcess(
            _PROCESS_VM_READ | _PROCESS_QUERY_INFORMATION, False, pid
        )
    except (AttributeError, OSError):
        yield None
        return
    if not handle:
        yield None
        return

    def read_memory(address: int, size: int) -> Optional[bytes]:
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        ok = library.ReadProcessMemory(
            handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)
        )
        return buffer.raw if ok and read.value == size else None

    try:
        yield library, handle, read_memory
    finally:
        library.CloseHandle(handle)


def resolve_addresses(pid: int, addresses: Iterable[int]) -> list[CodecContext]:
    """Resolve remembered context addresses inside a running process."""
    with _opened(pid) as opened:
        if opened is None:
            return []
        _library_, _handle, read_memory = opened
        try:
            return resolve_many(addresses, read_memory)
        except OSError:
            return []


def scan_process(pid: int) -> list[CodecContext]:
    """Return every SQLCipher context found in a process's private heaps.

    An inaccessible or exited process yields an empty list rather than raising,
    because "cannot read" and "nothing to read" lead to the same next step.
    """
    with _opened(pid) as opened:
        if opened is None:
            return []
        library, handle, read_memory = opened
        return _scan_opened(library, handle, read_memory)


def _scan_opened(
    library: ctypes.CDLL, handle: int, read_memory: ReadMemory
) -> list[CodecContext]:
    def blocks() -> Iterator[tuple[int, bytes]]:
        address = 0
        limit = 0x7FFF_FFFF_FFFF if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x7FFF_FFFF
        while address < limit:
            region = _MemoryRegion()
            if not library.VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(region), ctypes.sizeof(region)
            ):
                return
            base = int(region.base_address or 0)
            size = int(region.region_size)
            if base + size <= address:
                return
            if _is_candidate_region(region):
                for offset in range(0, size, _CHUNK_SIZE):
                    data = read_memory(base + offset, min(_CHUNK_SIZE, size - offset))
                    if data is not None:
                        yield base + offset, data
            address = base + size

    try:
        return list(scan_blocks(blocks(), read_memory))
    except OSError:
        # The client may exit or revoke access part-way through a scan.
        return []
