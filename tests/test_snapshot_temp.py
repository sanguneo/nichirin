"""Synthetic fallback snapshots only; never scan the user's temporary directory."""

from __future__ import annotations

import ctypes
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from kanao.snapshot import open_image

_CONNECT = sqlite3.connect


class _FileOnlyConnection(sqlite3.Connection):
    def __getattribute__(self, name):
        if name == "deserialize":
            raise AttributeError(name)
        return super().__getattribute__(name)


def _file_connect(database, *args, **kwargs):
    if database == ":memory:":
        kwargs["factory"] = _FileOnlyConnection
    return _CONNECT(database, *args, **kwargs)


_CHILD = """
import socket, sys, tempfile
from unittest.mock import patch
from tests.test_snapshot_temp import _file_connect
from kanao.snapshot import open_image
from pathlib import Path

tempfile.tempdir = sys.argv[1]
def connect(database, *args, **kwargs):
    connection = _file_connect(database, *args, **kwargs)
    if database != ':memory:':
        with socket.create_connection(('127.0.0.1', int(sys.argv[3])), timeout=10) as signal:
            signal.sendall(b'R')
        sys.stdin.buffer.read(1)
    return connection
with patch('kanao.snapshot.sqlite3.connect', side_effect=connect):
    open_image(Path(sys.argv[2]).read_bytes(), 'synthetic').close()
"""


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.temps = self.root / "temp"
        self.temps.mkdir()
        self.seed = self.root / "synthetic.sqlite3"
        with closing(_CONNECT(self.seed)) as connection:
            connection.execute("CREATE TABLE note(body TEXT)")
            connection.execute("INSERT INTO note VALUES ('synthetic plaintext')")
            connection.commit()
        self.image = self.seed.read_bytes()
        redirect = patch("tempfile.tempdir", str(self.temps))
        redirect.start()
        self.addCleanup(redirect.stop)

    @contextmanager
    def child(self):
        # Subscribe before launch; the signal arrives only after SQLite opens
        # the fully written fallback file. No sleeps or file polling.
        with socket.socket() as ready:
            ready.bind(("127.0.0.1", 0))
            ready.listen(1)
            ready.settimeout(10)
            with subprocess.Popen(
                [sys.executable, "-c", _CHILD, str(self.temps), str(self.seed),
                 str(ready.getsockname()[1])],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as process:
                try:
                    signal, _address = ready.accept()
                    with signal:
                        signal.settimeout(10)
                        self.assertEqual(signal.recv(1), b"R")
                    yield process
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)

    def open_fallback(self):
        with patch("kanao.snapshot.sqlite3.connect", side_effect=_file_connect):
            return open_image(self.image, "synthetic")


class FallbackTests(_Fixture):
    def test_normal_fallback_reads_the_image_and_removes_plaintext(self) -> None:
        with closing(self.open_fallback()) as connection:
            self.assertEqual(connection.execute("SELECT body FROM note").fetchall(),
                             [("synthetic plaintext",)])
        self.assertEqual(list(self.temps.iterdir()), [])

    def test_exception_fallback_removes_plaintext(self) -> None:
        with patch("kanao.snapshot.sqlite3.connect", side_effect=_file_connect):
            with self.assertRaises(sqlite3.DatabaseError):
                open_image(b"not a database", "synthetic")
        self.assertEqual(list(self.temps.iterdir()), [])

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows orphan recovery needs WinDLL")
    def test_next_fallback_cleans_plaintext_after_child_termination(self) -> None:
        with self.child() as child:
            orphan, = self.temps.iterdir()
            self.assertEqual(orphan.read_bytes(), self.image)
            child.terminate()
            child.communicate(timeout=10)
            self.assertTrue(orphan.exists(), "the abrupt exit must bypass finally")
            with closing(self.open_fallback()):
                pass
            self.assertEqual(list(self.temps.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
