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
from kamado.errors import ErrorCode, KamadoError
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
                assert rooms_res is not None, "the daemon did not answer"
                self.assertTrue(rooms_res["ok"])
                self.assertEqual(len(rooms_res["rooms"]), 2)

                stop_res = daemon.send_to_daemon("stop", {})
                self.assertEqual(stop_res, {"ok": True, "status": "stopping"})
        finally:
            server.running = False
            thread.join(timeout=1.0)
            server._cleanup()


class NamedRoomTests(unittest.TestCase):
    """`messages` given a title rather than an id.

    This path called `reader.rooms(name=…)`, which has no such parameter, so
    it raised `TypeError` — not a `KamadoError` the socket layer could turn
    into an envelope, and not anything a caller could act on. Nothing
    exercised it, so `_dispatch` is driven directly here rather than through
    the socket the other test uses; the envelope is `_serve`'s job.
    """

    def dispatch(self, command: str, args: dict):
        server = daemon.DaemonServer(environment=None)
        server._reader = Reader(cache=StubCache(build_database()), keyring=build_keyring())
        self.addCleanup(server._reader.close)
        return server._dispatch(command, args)

    def test_messages_accepts_a_room_title(self) -> None:
        answer = self.dispatch("messages", {"room": "Peer Renamed", "limit": 5})
        self.assertTrue(answer["ok"], answer)
        self.assertIn("messages", answer)

    def test_a_title_that_matches_nothing_says_which_title(self) -> None:
        with self.assertRaises(KamadoError) as raised:
            self.dispatch("messages", {"room": "no such room at all"})
        self.assertEqual(raised.exception.code, ErrorCode.CHAT_NOT_FOUND)

    def test_neither_a_room_nor_an_id_is_refused(self) -> None:
        for command in ("messages", "context"):
            with self.subTest(command=command):
                with self.assertRaises(KamadoError) as raised:
                    self.dispatch(command, {})
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)
