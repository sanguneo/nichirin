"""AES-256-CBC decryption through Windows CNG, with the key handle reused.

A SQLCipher database is decrypted one 4096-byte page at a time, so how much
setup each page costs decides how long a read takes.  Preparing the CNG
algorithm provider and symmetric key once per *database* rather than once per
*page* was measured at 4.5us per page against 182.2us for per-page setup: the
AES itself was never the expensive part, the eight CNG calls around it were.

Nothing here is KakaoTalk-specific; it is the AES layer the page codec uses.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Final

AES_BLOCK_SIZE: Final = 16
AES_256_KEY_SIZE: Final = 32

_ALGORITHM: Final = "AES"
_CHAINING_MODE: Final = "ChainingMode"
_CHAIN_MODE_CBC: Final = "ChainingModeCBC"
_OBJECT_LENGTH: Final = "ObjectLength"

_PUCHAR = ctypes.POINTER(ctypes.c_ubyte)
_HANDLE = ctypes.c_void_p

_library: ctypes.CDLL | None = None


def _bcrypt() -> ctypes.CDLL:
    """Load and configure bcrypt.dll once, lazily so imports work off Windows."""
    global _library
    if _library is not None:
        return _library

    library = ctypes.WinDLL("bcrypt")
    library.BCryptOpenAlgorithmProvider.argtypes = (
        ctypes.POINTER(_HANDLE), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.ULONG,
    )
    library.BCryptOpenAlgorithmProvider.restype = wintypes.LONG
    library.BCryptCloseAlgorithmProvider.argtypes = (_HANDLE, wintypes.ULONG)
    library.BCryptCloseAlgorithmProvider.restype = wintypes.LONG
    library.BCryptSetProperty.argtypes = (
        _HANDLE, wintypes.LPCWSTR, _PUCHAR, wintypes.ULONG, wintypes.ULONG,
    )
    library.BCryptSetProperty.restype = wintypes.LONG
    library.BCryptGetProperty.argtypes = (
        _HANDLE, wintypes.LPCWSTR, _PUCHAR, wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG), wintypes.ULONG,
    )
    library.BCryptGetProperty.restype = wintypes.LONG
    library.BCryptGenerateSymmetricKey.argtypes = (
        _HANDLE, ctypes.POINTER(_HANDLE), _PUCHAR, wintypes.ULONG,
        _PUCHAR, wintypes.ULONG, wintypes.ULONG,
    )
    library.BCryptGenerateSymmetricKey.restype = wintypes.LONG
    library.BCryptDestroyKey.argtypes = (_HANDLE,)
    library.BCryptDestroyKey.restype = wintypes.LONG
    # pbInput is read-only, so bytes may be passed straight through as c_char_p.
    # pbIV is written back by CBC chaining and must be a fresh mutable buffer.
    library.BCryptDecrypt.argtypes = (
        _HANDLE, ctypes.c_char_p, wintypes.ULONG, ctypes.c_void_p,
        _PUCHAR, wintypes.ULONG, _PUCHAR, wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG), wintypes.ULONG,
    )
    library.BCryptDecrypt.restype = wintypes.LONG

    _library = library
    return library


def _check(status: int, operation: str) -> None:
    if status != 0:
        raise OSError(f"{operation} failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}")


class PreparedKey:
    """An open CNG key usable for many independent CBC decryptions.

    Each :meth:`decrypt` call supplies its own IV and does not depend on the
    previous call, which is what lets one key serve every page of a database.
    """

    __slots__ = ("_algorithm", "_handle", "_key_object", "_iv", "_output")

    def __init__(self, key: bytes) -> None:
        if len(key) != AES_256_KEY_SIZE:
            raise ValueError(f"AES-256 requires a {AES_256_KEY_SIZE}-byte key")

        library = _bcrypt()
        self._algorithm = _HANDLE()
        self._handle = _HANDLE()
        self._iv = (ctypes.c_ubyte * AES_BLOCK_SIZE)()
        self._output: ctypes.Array[ctypes.c_ubyte] | None = None

        _check(
            library.BCryptOpenAlgorithmProvider(
                ctypes.byref(self._algorithm), _ALGORITHM, None, 0
            ),
            "BCryptOpenAlgorithmProvider",
        )
        try:
            mode = ctypes.create_unicode_buffer(_CHAIN_MODE_CBC)
            _check(
                library.BCryptSetProperty(
                    self._algorithm, _CHAINING_MODE,
                    ctypes.cast(mode, _PUCHAR), ctypes.sizeof(mode), 0,
                ),
                "BCryptSetProperty(ChainingMode)",
            )

            object_length = wintypes.ULONG()
            written = wintypes.ULONG()
            _check(
                library.BCryptGetProperty(
                    self._algorithm, _OBJECT_LENGTH,
                    ctypes.cast(ctypes.byref(object_length), _PUCHAR),
                    ctypes.sizeof(object_length), ctypes.byref(written), 0,
                ),
                "BCryptGetProperty(ObjectLength)",
            )

            # The key object buffer must outlive the handle that borrows it.
            self._key_object = (ctypes.c_ubyte * object_length.value)()
            _check(
                library.BCryptGenerateSymmetricKey(
                    self._algorithm, ctypes.byref(self._handle),
                    self._key_object, object_length.value,
                    (ctypes.c_ubyte * len(key)).from_buffer_copy(key), len(key), 0,
                ),
                "BCryptGenerateSymmetricKey",
            )
        except BaseException:
            self.close()
            raise

    def decrypt(self, iv: bytes, ciphertext: bytes) -> bytes:
        """Decrypt block-aligned CBC ciphertext that was encrypted under this key."""
        if len(iv) != AES_BLOCK_SIZE:
            raise ValueError(f"AES-CBC requires a {AES_BLOCK_SIZE}-byte IV")
        if len(ciphertext) % AES_BLOCK_SIZE != 0:
            raise ValueError("AES-CBC ciphertext length must be a multiple of the block size")
        if not ciphertext:
            return b""
        if not self._handle:
            raise ValueError("this key has been closed")

        # CBC writes the trailing ciphertext block back over the IV, so the
        # caller's bytes are copied into a scratch buffer rather than exposed.
        self._iv[:] = iv
        if self._output is None or len(self._output) < len(ciphertext):
            self._output = (ctypes.c_ubyte * len(ciphertext))()

        written = wintypes.ULONG()
        _check(
            _bcrypt().BCryptDecrypt(
                self._handle, ciphertext, len(ciphertext), None,
                self._iv, AES_BLOCK_SIZE,
                self._output, len(ciphertext), ctypes.byref(written), 0,
            ),
            "BCryptDecrypt",
        )
        if written.value != len(ciphertext):
            raise OSError(
                f"BCryptDecrypt returned {written.value} bytes for {len(ciphertext)} bytes of input"
            )
        return bytes(memoryview(self._output)[: written.value])

    def close(self) -> None:
        """Release the CNG key and provider; further decryption is refused."""
        library = _bcrypt()
        if self._handle:
            library.BCryptDestroyKey(self._handle)
            self._handle = _HANDLE()
        if self._algorithm:
            library.BCryptCloseAlgorithmProvider(self._algorithm, 0)
            self._algorithm = _HANDLE()

    def __enter__(self) -> "PreparedKey":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def decrypt_cbc(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Decrypt one buffer with a throwaway key; prefer :class:`PreparedKey` in loops."""
    with PreparedKey(key) as prepared:
        return prepared.decrypt(iv, ciphertext)
