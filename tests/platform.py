"""Why a test module cannot run here, said once and said precisely.

This tool is macOS-only, but the work on it is not: most of the shared code —
the reader, the watcher, the digest, the HTTP server — is platform-neutral and
gets written and run wherever the author happens to be, including on the
Windows sibling's machine. A suite that answers `FAILED (errors=62)` there
tells a developer nothing except that they are not on a Mac, and it buries the
one real failure they might have just introduced.

So a module that genuinely needs the platform says which part of it, and is
skipped rather than broken. The check is on the capability, not the operating
system name, so a Linux box that has a POSIX console runs those tests instead
of being told it is not a Mac.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest


def require_posix_console() -> None:
    """Skip unless this host has the terminal machinery ``tui.console`` needs.

    `fcntl` and `termios` are what the interface uses to read the window size
    and put the terminal into raw mode. Without them the module cannot even be
    imported, which is an import error for every test in the file.
    """
    if importlib.util.find_spec("fcntl") is None:
        raise unittest.SkipTest(
            "the terminal interface needs a POSIX console (fcntl is missing here)"
        )


def import_app():
    """Return ``tui.app.App``, or skip if this host has no POSIX console.

    Some modules reach for the interface from inside a test rather than at the
    top, so the guard has to travel with the import.
    """
    require_posix_console()
    from kamado.tui.app import App

    return App


def require_commoncrypto() -> None:
    """Skip unless Apple's crypto library can be loaded.

    The fixtures encrypt their synthetic pages through CommonCrypto — the same
    library the product decrypts with — so on anything that is not a Mac they
    cannot build the data they are about to test. That is the single reason 44
    of these fail off-platform, and it is worth saying rather than counting.
    """
    if sys.platform != "darwin":
        raise unittest.SkipTest(
            "the fixtures encrypt through CommonCrypto, which is Apple's"
        )


def require_unix_sockets() -> None:
    """Skip unless the resident daemon's transport exists here.

    The daemon listens on a Unix domain socket under the user's home. Windows
    has had `AF_UNIX` since 10, but not the rest of what this assumes, and the
    daemon is a POSIX feature rather than a portable one.
    """
    if os.name != "posix":
        raise unittest.SkipTest("the daemon listens on a Unix domain socket")
