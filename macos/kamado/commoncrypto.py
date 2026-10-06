"""AES-256-CBC decryption through CommonCrypto, with the key schedule reused.

A SQLCipher database is decrypted one page at a time, so how much setup each
page costs decides how long a read takes.  CommonCrypto separates the two
halves of that cost: ``CCCryptorCreate`` builds the key schedule, and
``CCCryptorReset`` points an existing cryptor at a new IV.  A database is
therefore one create and one reset per page rather than a create per page.

This is the macOS counterpart of the Windows build's CNG layer, and it exists
for the same reason that one did: AES is not in the standard library, and this
project takes no third-party dependencies.  CommonCrypto ships inside
``libSystem``, so ``ctypes`` reaches it with nothing to install.

Nothing here is KakaoTalk-specific; it is the AES layer the page codec uses.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Final

AES_BLOCK_SIZE: Final = 16
AES_256_KEY_SIZE: Final = 32

_kCCDecrypt: Final = 1
_kCCAlgorithmAES: Final = 0
_kCCOptionNone: Final = 0
_kCCSuccess: Final = 0

_STATUS_NAMES: Final = {
    -4300: "kCCParamError",
    -4301: "kCCBufferTooSmall",
    -4302: "kCCMemoryFailure",
    -4303: "kCCAlignmentError",
    -4304: "kCCDecodeError",
    -4305: "kCCUnimplemented",
}

_library: ctypes.CDLL | None = None


def _commoncrypto() -> ctypes.CDLL:
    """Load and configure libSystem once, lazily so imports work off macOS."""
    global _library
    if _library is not None:
        return _library

    name = ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib"
    library = ctypes.CDLL(name, use_errno=True)

    library.CCCryptorCreate.argtypes = (
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    library.CCCryptorCreate.restype = ctypes.c_int32
    library.CCCryptorUpdate.argtypes = (
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    )
    library.CCCryptorUpdate.restype = ctypes.c_int32
    library.CCCryptorReset.argtypes = (ctypes.c_void_p, ctypes.c_char_p)
    library.CCCryptorReset.restype = ctypes.c_int32
    library.CCCryptorRelease.argtypes = (ctypes.c_void_p,)
    library.CCCryptorRelease.restype = ctypes.c_int32

    _library = library
    return library


def _check(status: int, operation: str) -> None:
    if status != _kCCSuccess:
        name = _STATUS_NAMES.get(status, "unknown")
        raise OSError(f"{operation} failed with CCCryptorStatus {status} ({name})")


class PreparedKey:
    """An open CommonCrypto cryptor usable for many independent CBC decryptions.

    Each :meth:`decrypt` call supplies its own IV and does not depend on the
    previous call, which is what lets one key serve every page of a database.
    """

    __slots__ = ("_handle", "_output")

    def __init__(self, key: bytes) -> None:
        if len(key) != AES_256_KEY_SIZE:
            raise ValueError(f"AES-256 requires a {AES_256_KEY_SIZE}-byte key")

        library = _commoncrypto()
        self._handle = ctypes.c_void_p()
        self._output: ctypes.Array[ctypes.c_char] | None = None
        _check(
            library.CCCryptorCreate(
                _kCCDecrypt, _kCCAlgorithmAES, _kCCOptionNone,
                key, len(key), bytes(AES_BLOCK_SIZE),
                ctypes.byref(self._handle),
            ),
            "CCCryptorCreate",
        )

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

        library = _commoncrypto()
        # Reset carries the key schedule over and only re-seeds the chaining
        # state, which is the whole reason one cryptor can serve every page.
        _check(library.CCCryptorReset(self._handle, iv), "CCCryptorReset")

        if self._output is None or len(self._output) < len(ciphertext):
            self._output = ctypes.create_string_buffer(len(ciphertext))

        moved = ctypes.c_size_t(0)
        _check(
            library.CCCryptorUpdate(
                self._handle, ciphertext, len(ciphertext),
                self._output, len(ciphertext), ctypes.byref(moved),
            ),
            "CCCryptorUpdate",
        )
        if moved.value != len(ciphertext):
            raise OSError(
                f"CCCryptorUpdate returned {moved.value} bytes for {len(ciphertext)} bytes of input"
            )
        return self._output.raw[: moved.value]

    def close(self) -> None:
        """Release the cryptor; further decryption is refused."""
        if self._handle:
            _commoncrypto().CCCryptorRelease(self._handle)
            self._handle = ctypes.c_void_p()

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
