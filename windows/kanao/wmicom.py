"""The Windows side of ``kanao.wmi``: a named event, ``Win32_Process.Create``,
the caller's CPU rate cap, and the interpreters registered under PEP 514.

``Create`` is called through WMI's COM interfaces from this process rather
than through PowerShell. Inside a hard-capped job everything this process
starts is capped too, and PowerShell's own start did not finish within 25
seconds under a 1% cap on the machine this was written on, where a bare
Python start took 0.38 seconds. The COM call leaves the work to the WMI
service, which runs outside the job.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, byref, c_long, c_ushort, c_void_p, c_wchar_p
from typing import Sequence
import subprocess

# Win32_Process.Create return values, from its documentation.
CREATE_RESULTS = {
    2: "access denied",
    3: "insufficient privilege",
    8: "unknown failure",
    9: "path not found",
    21: "invalid parameter",
}

CLSID_WBEM_LOCATOR = "{4590F811-1D3A-11D0-891F-00AA004B2E24}"
IID_IWBEM_LOCATOR = "{DC12A687-737F-11CF-884D-00AA004B2E24}"
VT_I4, VT_BSTR, VT_UNKNOWN = 3, 8, 13
RPC_E_CHANGED_MODE = -2147417850
SW_HIDE = 0
"""Without it the child's console window appears and takes the focus."""

# Vtable slots, counted from IUnknown's three, in the order wbemcli.h declares.
LOCATOR_CONNECT_SERVER = 3
SERVICES_GET_OBJECT, SERVICES_EXEC_METHOD = 6, 24
OBJECT_GET, OBJECT_PUT, OBJECT_SPAWN_INSTANCE, OBJECT_GET_METHOD = 4, 5, 15, 19
RELEASE = 2


class LaunchFailed(Exception):
    pass


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]


class _Variant(ctypes.Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("reserved", c_ushort * 3),
        ("value", c_void_p),
        ("record", c_void_p),
    ]


def _call(interface: c_void_p, slot: int, *arguments: object) -> None:
    """Call a COM method; a failing HRESULT raises ``OSError``."""
    table = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *[c_void_p] * len(arguments))
    prototype(table[slot])(interface, *arguments)


class _Com:
    """Owns the COM references and strings of one ``Create`` call."""

    def __init__(self) -> None:
        self.ole32 = ctypes.WinDLL("ole32")
        self.oleaut32 = ctypes.WinDLL("oleaut32")
        self.oleaut32.SysAllocString.restype = c_void_p
        self.oleaut32.SysAllocString.argtypes = (c_wchar_p,)
        self.oleaut32.SysFreeString.argtypes = (c_void_p,)
        self.references: list[c_void_p] = []
        self.strings: list[int] = []
        result = self.ole32.CoInitializeEx(None, 0)
        if result < 0 and result != RPC_E_CHANGED_MODE:
            raise OSError(result, "CoInitializeEx failed")
        self.initialized = result >= 0

    def __enter__(self) -> _Com:
        return self

    def __exit__(self, *_exc: object) -> None:
        for reference in reversed(self.references):
            _call(reference, RELEASE)
        for string in self.strings:
            self.oleaut32.SysFreeString(string)
        if self.initialized:
            self.ole32.CoUninitialize()

    def bstr(self, text: str) -> int:
        string = self.oleaut32.SysAllocString(text)
        self.strings.append(string)
        return string

    def out(self) -> c_void_p:
        reference = c_void_p()
        self.references.append(reference)
        return reference

    def guid(self, text: str) -> _Guid:
        value = _Guid()
        self.ole32.CLSIDFromString(c_wchar_p(text), byref(value))
        return value

    def put(self, instance: c_void_p, name: str, vt: int, value: int | None) -> None:
        variant = _Variant(vt=vt, value=value)
        _call(instance, OBJECT_PUT, c_wchar_p(name), 0, byref(variant), 0)

    def create(self, command_line: str, cwd: str) -> int:
        """Return ``Win32_Process.Create``'s ReturnValue."""
        locator, services = self.out(), self.out()
        self.ole32.CoCreateInstance(
            byref(self.guid(CLSID_WBEM_LOCATOR)), None, 1,
            byref(self.guid(IID_IWBEM_LOCATOR)), byref(locator),
        )
        if not locator:
            raise OSError("CoCreateInstance(WbemLocator) failed")
        _call(locator, LOCATOR_CONNECT_SERVER, self.bstr("ROOT\\CIMV2"),
              None, None, None, 0, None, None, byref(services))
        # Create needs the impersonate level; the default proxy only identifies.
        self.ole32.CoSetProxyBlanket(services, 10, 0, None, 3, 3, None, 0)

        process, signature, arguments = self.out(), self.out(), self.out()
        _call(services, SERVICES_GET_OBJECT, self.bstr("Win32_Process"), 0, None, byref(process), None)
        _call(process, OBJECT_GET_METHOD, c_wchar_p("Create"), 0, byref(signature), None)
        _call(signature, OBJECT_SPAWN_INSTANCE, 0, byref(arguments))

        startup_class, startup = self.out(), self.out()
        _call(services, SERVICES_GET_OBJECT, self.bstr("Win32_ProcessStartup"), 0, None,
              byref(startup_class), None)
        _call(startup_class, OBJECT_SPAWN_INSTANCE, 0, byref(startup))
        self.put(startup, "ShowWindow", VT_I4, SW_HIDE)

        self.put(arguments, "CommandLine", VT_BSTR, self.bstr(command_line))
        self.put(arguments, "CurrentDirectory", VT_BSTR, self.bstr(cwd))
        self.put(arguments, "ProcessStartupInformation", VT_UNKNOWN, startup.value)

        result, answer = self.out(), _Variant()
        _call(services, SERVICES_EXEC_METHOD, self.bstr("Win32_Process"), self.bstr("Create"),
              0, None, arguments, byref(result), None)
        _call(result, OBJECT_GET, c_wchar_p("ReturnValue"), 0, byref(answer), None, None)
        return c_long((answer.value or 0) & 0xFFFFFFFF).value


class _Event:
    def __init__(self, name: str) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateEventW.restype = c_void_p
        self._kernel32.WaitForSingleObject.argtypes = (c_void_p, ctypes.c_uint32)
        self._kernel32.CloseHandle.argtypes = (c_void_p,)
        self._handle = self._kernel32.CreateEventW(None, True, False, name)
        if not self._handle:
            raise LaunchFailed(f"CreateEventW failed: {ctypes.get_last_error()}")

    def wait(self, seconds: float) -> bool:
        milliseconds = min(int(seconds * 1000), 0xFFFFFFFE)
        return self._kernel32.WaitForSingleObject(self._handle, milliseconds) == 0

    def __enter__(self) -> _Event:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._kernel32.CloseHandle(self._handle)


def set_event(name: str) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.restype = c_void_p
    kernel32.SetEvent.argtypes = (c_void_p,)
    kernel32.CloseHandle.argtypes = (c_void_p,)
    event_modify_state = 0x0002
    handle = kernel32.OpenEventW(event_modify_state, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"OpenEventW failed for {name}")
    try:
        kernel32.SetEvent(handle)
    finally:
        kernel32.CloseHandle(handle)


JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
CPU_RATE_CONTROL_ENABLE = 0x1
CPU_RATE_CONTROL_HARD_CAP = 0x4
CPU_RATE_CONTROL_MIN_MAX_RATE = 0x10
"""A weight-based rate only shares the CPU under contention, so it is not a cap."""


def cpu_capped() -> bool:
    """Whether the job this process belongs to caps its CPU rate.

    A query that fails answers True: relaunching costs a fraction of a second,
    staying inside an unseen cap costs minutes. Only the immediate job is
    visible, so a cap set on an outer job of a nested one is not seen.
    """
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, POINTER(wintypes.BOOL))
    kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, c_void_p, wintypes.DWORD, POINTER(wintypes.DWORD),
    )
    in_job = wintypes.BOOL()
    if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, byref(in_job)):
        return True
    if not in_job.value:
        return False

    class _CpuRate(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    rate = _CpuRate()
    # A NULL handle asks about the job this process belongs to.
    if not kernel32.QueryInformationJobObject(
        None, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION, byref(rate), ctypes.sizeof(rate), None,
    ):
        return True
    flags = rate.ControlFlags
    return bool(flags & CPU_RATE_CONTROL_ENABLE) and bool(
        flags & (CPU_RATE_CONTROL_HARD_CAP | CPU_RATE_CONTROL_MIN_MAX_RATE)
    )


def registered_pythons() -> list[tuple[tuple[int, int], str]]:
    """``((major, minor), python.exe)`` for each CPython registered under PEP 514."""
    import winreg

    found: list[tuple[tuple[int, int], str]] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                core = winreg.OpenKey(hive, r"Software\Python\PythonCore", 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with core:
                index = 0
                while True:
                    try:
                        tag = winreg.EnumKey(core, index)
                    except OSError:
                        break
                    index += 1
                    entry = _registered(winreg, core, tag)
                    if entry is not None:
                        found.append(entry)
    return found


def _registered(winreg, core, tag: str) -> tuple[tuple[int, int], str] | None:  # noqa: ANN001
    try:
        with winreg.OpenKey(core, tag) as key:
            try:
                version = str(winreg.QueryValueEx(key, "SysVersion")[0])
            except OSError:
                version = tag
        with winreg.OpenKey(core, tag + r"\InstallPath") as install:
            try:
                executable = str(winreg.QueryValueEx(install, "ExecutablePath")[0])
            except OSError:
                executable = os.path.join(str(winreg.QueryValueEx(install, "")[0]), "python.exe")
    except OSError:
        return None
    parts = version.split("-")[0].split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return (int(parts[0]), int(parts[1])), executable


class WmiBoundary:
    def capped(self) -> bool:
        try:
            return cpu_capped()
        except (OSError, AttributeError):
            return True

    def pythons(self) -> list[tuple[tuple[int, int], str]]:
        try:
            return registered_pythons()
        except (OSError, ImportError):
            return []

    def signal(self, name: str) -> _Event:
        return _Event(name)

    def create(self, argv: Sequence[str], cwd: str) -> None:
        try:
            with _Com() as com:
                answer = com.create(subprocess.list2cmdline(argv), cwd)
        except OSError as error:
            raise LaunchFailed(str(error)) from error
        if answer != 0:
            meaning = CREATE_RESULTS.get(answer)
            raise LaunchFailed(f"ReturnValue {answer}{f' ({meaning})' if meaning else ''}")
