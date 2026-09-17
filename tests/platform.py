"""Why a test module cannot run here, said once and said precisely.

This tool is Windows-only, but the work on it is not: the reader, the watcher,
the digest and the HTTP server are platform-neutral and get written and run
wherever the author happens to be, including on the macOS sibling's machine.
A suite that answers `FAILED (errors=…)` there tells a developer nothing except
that they are not on Windows, and it buries the one real failure they might
have just introduced.

So a module that genuinely needs the platform says which part of it, and is
skipped rather than broken. The check is on the capability rather than the
operating system's name, because what these tests actually need is `WinDLL` —
the door to CNG, to the console API and to `user32`.
"""

from __future__ import annotations

import ctypes
import unittest


def _has_windows_libraries() -> bool:
    return hasattr(ctypes, "WinDLL")


def require_windows_console() -> None:
    """Skip unless the Windows console API is reachable.

    `tui.console` reads `ctypes.wintypes` at import time and drives `kernel32`
    for the screen buffer, the code page and the input queue. Off Windows the
    import alone raises, which is an error for every test in the file.
    """
    if not _has_windows_libraries():
        raise unittest.SkipTest(
            "the terminal interface drives the Windows console API (no WinDLL here)"
        )


def require_windows_crypto() -> None:
    """Skip unless CNG can be called.

    The fixtures encrypt their synthetic pages through the same `bcrypt.dll`
    the product decrypts with, so a host without it cannot build the data it is
    about to test.
    """
    if not _has_windows_libraries():
        raise unittest.SkipTest(
            "the fixtures encrypt through Windows CNG (bcrypt.dll is unreachable here)"
        )


def import_app():
    """Return ``tui.app.App``, or skip if the console API is unreachable.

    Some modules reach for the interface from inside a test rather than at the
    top, so the guard has to travel with the import.
    """
    require_windows_console()
    from kanao.tui.app import App

    return App
