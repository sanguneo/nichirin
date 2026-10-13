"""Windows recovery of plaintext files abandoned by the SQLite fallback.

Only this versioned namespace is eligible. A name is not proof of ownership:
recovery checks the file's owner SID against the current token, refuses links,
and deletes through the same exclusive handle it inspected (not by pathname).
A live, inaccessible or unknown PID is never grounds for deletion; a PID now
held by a process with a different creation time is, because the tagged
process must have exited for the number to be handed out again.

Windows only: this module reads ``ctypes.wintypes`` at import, which raises
elsewhere, so ``snapshot`` imports it inside the Windows branch.

This is next-fallback recovery, not crash-proof erasure. Legacy untagged files
and unverifiable candidates are deliberately left alone. No process is changed.
"""

from __future__ import annotations

import ctypes
import os
import re
import tempfile
import warnings
from ctypes import wintypes as w
from pathlib import Path

_NAME = re.compile(r"kanao-snapshot-v1-([1-9][0-9]{0,9})-([0-9a-f]{16})-([a-z0-9_]{8})\.sqlite3")
_QUERY_AND_WAIT = 0x1000 | 0x100000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
_FILE_ACCESS = 0x10000 | 0x20000 | 0x80  # DELETE | READ_CONTROL | READ_ATTRIBUTES


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME), ("accessed", w.FILETIME), ("written", w.FILETIME),
        ("volume", w.DWORD), ("size_high", w.DWORD), ("size_low", w.DWORD),
        ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD),
    ]


class _WindowsRecovery:
    """Own the native bindings; no Windows DLL is loaded on the memory path."""

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        dword_pointer = ctypes.POINTER(w.DWORD)
        time_pointer = ctypes.POINTER(w.FILETIME)
        for library, name, arguments, result in (
            (self.kernel, "GetCurrentProcess", [], w.HANDLE),
            (self.kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.kernel, "CloseHandle", [w.HANDLE], w.BOOL),
            (self.kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            (self.kernel, "GetProcessTimes", [w.HANDLE] + [time_pointer] * 4, w.BOOL),
            (self.kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, pointer,
                                        w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (self.kernel, "GetFileInformationByHandle",
             [w.HANDLE, ctypes.POINTER(_FileInfo)], w.BOOL),
            (self.kernel, "SetFileInformationByHandle",
             [w.HANDLE, ctypes.c_int, pointer, w.DWORD], w.BOOL),
            (self.kernel, "LocalFree", [pointer], pointer),
            (self.security, "OpenProcessToken",
             [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
            (self.security, "GetTokenInformation",
             [w.HANDLE, ctypes.c_int, pointer, w.DWORD, dword_pointer], w.BOOL),
            (self.security, "GetLengthSid", [pointer], w.DWORD),
            (self.security, "EqualSid", [pointer, pointer], w.BOOL),
            (self.security, "GetSecurityInfo",
             [w.HANDLE, ctypes.c_int, w.DWORD] + [pointer] * 5, w.DWORD),
        ):
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
        # A file this account's processes wrote is owned by the token's user,
        # or, from an elevated process, by the token's default owner (the
        # Administrators group), so both identify a file of ours.
        self.owners: tuple[bytes, ...] = (self._token_sid(1), self._token_sid(4))  # TokenUser, TokenOwner

    def _token_sid(self, information_class: int) -> bytes:
        token = w.HANDLE()
        if not self.security.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = w.DWORD()
            self.security.GetTokenInformation(
                token, information_class, None, 0, ctypes.byref(size)
            )
            if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
                raise ctypes.WinError(ctypes.get_last_error())
            data = ctypes.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(
                token, information_class, data, size, ctypes.byref(size)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # TOKEN_USER (SID_AND_ATTRIBUTES) and TOKEN_OWNER both start with a PSID.
            sid = ctypes.c_void_p.from_buffer(data).value
            if not sid:
                raise ctypes.WinError(1337)  # ERROR_INVALID_SID
            return ctypes.string_at(sid, self.security.GetLengthSid(sid))
        finally:
            self.kernel.CloseHandle(token)

    def _birth(self, process: int) -> int:
        times = [w.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def _exited(self, pid: int, birth: int) -> bool:
        """Whether the process that tagged a file is provably gone.

        ERROR_INVALID_PARAMETER for a positive DWORD PID means no such process.
        Every other OpenProcess failure (especially access denied) is unknown.
        A PID held by a process born at another time is a reused number, so
        the tagged process exited; the same birth needs a signalled object.
        Never use os.kill(pid, 0): on Windows that terminates the process.
        """
        if not 0 < pid <= 0xFFFFFFFF or birth == 0:
            return False
        process = self.kernel.OpenProcess(_QUERY_AND_WAIT, False, pid)
        if not process:
            return ctypes.get_last_error() == 87
        try:
            try:
                current = self._birth(process)
            except OSError:
                return False  # Unqueryable is not proof of the tagged process's exit.
            if current != birth:
                return True
            return self.kernel.WaitForSingleObject(process, 0) == 0  # WAIT_OBJECT_0
        finally:
            self.kernel.CloseHandle(process)

    def _owned(self, handle: int) -> bool:
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self.security.GetSecurityInfo(
            handle, 1, 1, ctypes.byref(owner), None, None, None,
            ctypes.byref(descriptor),  # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION
        )
        if result != 0:
            return False
        try:
            return bool(owner.value) and any(
                self.security.EqualSid(owner, sid) for sid in self.owners
            )
        finally:
            self.kernel.LocalFree(descriptor)

    def _remove(self, path: Path) -> bool:
        # No sharing pins the inspected object against rename/replacement and
        # excludes files still open elsewhere. OPEN_REPARSE_POINT avoids targets.
        handle = self.kernel.CreateFileW(
            str(path), _FILE_ACCESS, 0, None, 3, 0x00200000, None,
        )  # OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT
        if handle == ctypes.c_void_p(-1).value:
            return False  # Missing, busy or inaccessible: leave it alone.
        try:
            info = _FileInfo()
            if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                return False
            if info.attributes & (0x10 | 0x400) or info.links != 1:
                return False  # Directory, any reparse point, or hard-linked file.
            if not self._owned(handle):
                return False
            delete = w.BOOL(True)  # FILE_DISPOSITION_INFO.DeleteFile
            if not self.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete),
                                                         ctypes.sizeof(delete)):
                warnings.warn(
                    f"Could not remove abandoned plaintext snapshot {path}: "
                    f"{ctypes.WinError(ctypes.get_last_error())}", RuntimeWarning,
                )
                return False
            return True
        finally:
            self.kernel.CloseHandle(handle)

    def cleanup(self, directory: Path) -> None:
        for path in directory.iterdir():
            match = _NAME.fullmatch(path.name)
            if match is not None and self._exited(int(match[1]), int(match[2], 16)):
                self._remove(path)

    def prefix(self) -> str:
        birth = self._birth(self.kernel.GetCurrentProcess())
        return f"kanao-snapshot-v1-{os.getpid()}-{birth:016x}-"


def create() -> tuple[int, str]:
    """Recover only tagged orphans, then exclusively create a new tagged file."""
    recovery = _WindowsRecovery()
    recovery.cleanup(Path(tempfile.gettempdir()))
    return tempfile.mkstemp(prefix=recovery.prefix(), suffix=".sqlite3")
