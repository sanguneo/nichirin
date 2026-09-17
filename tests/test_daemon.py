from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.platform import require_unix_sockets

require_unix_sockets()

from kamado import daemon
from kamado.reader import Reader
from tests.fixtures import StubCache, build_database, build_keyring


class DaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_daemon_socket_and_dispatch(self) -> None:
        server = daemon.DaemonServer(environment=None)
        server.sock_path = self.root / "test.sock"
        server.p_path = self.root / "test.pid"

        def serve_worker():
            cache = StubCache(build_database())
            server._reader = Reader(cache=cache, keyring=build_keyring())
            server._serve()

        server.running = True
        thread = threading.Thread(target=serve_worker, daemon=True)
        thread.start()

        deadline = time.time() + 2.0
        while time.time() < deadline and not server.sock_path.exists():
            time.sleep(0.01)

        try:
            with patch("kamado.daemon.socket_path", return_value=server.sock_path):
                ping_res = daemon.send_to_daemon("ping", {})
                self.assertEqual(ping_res, {"ok": True, "status": "pong"})

                rooms_res = daemon.send_to_daemon("rooms", {"limit": 2})
                self.assertTrue(rooms_res["ok"])
                self.assertEqual(len(rooms_res["rooms"]), 2)

                stop_res = daemon.send_to_daemon("stop", {})
                self.assertEqual(stop_res, {"ok": True, "status": "stopping"})
        finally:
            server.running = False
            thread.join(timeout=1.0)
            server._cleanup()
