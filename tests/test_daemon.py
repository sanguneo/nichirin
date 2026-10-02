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
from kamado.macauto import ChatWindow, Sender
from kamado.reader import Reader
from tests.fixtures import StubCache, build_database, build_keyring
from tests.test_send import FakeWindows


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


class _Connection:
    """One end of a socket that hands its bytes over in exactly the pieces given.

    A stream socket keeps no write boundaries, so what one ``recv`` returns is
    whatever arrived; scripting the pieces is how a split is made certain
    rather than hoped for.
    """

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = list(pieces)
        self.sent = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_error) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def connect(self, _path: str) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if not self.pieces:
            return b""
        piece = self.pieces.pop(0)
        if len(piece) > size:
            self.pieces.insert(0, piece[size:])
            piece = piece[:size]
        return piece


class _Listener:
    """A listening socket that accepts one scripted connection, then stops the server."""

    def __init__(self, server: daemon.DaemonServer, connection: _Connection) -> None:
        self.server = server
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_error) -> None:
        return None

    def bind(self, _path: str) -> None:
        return None

    def listen(self, _backlog: int) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def accept(self):
        self.server.running = False
        return self.connection, None


class FramingTests(unittest.TestCase):
    """A request is everything up to its newline, and one read is not one request."""

    def serve(self, pieces: list[bytes]) -> dict:
        import json

        server = daemon.DaemonServer(environment=None)
        connection = _Connection(pieces)
        server.running = True
        with patch("kamado.daemon.socket.socket", return_value=_Listener(server, connection)):
            server._serve()
        return json.loads(connection.sent.decode("utf-8"))

    def padded(self, size: int) -> bytes:
        prefix, suffix = b'{"command":"ping","padding":"', b'"}'
        return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix

    def test_a_request_split_across_two_reads_is_put_back_together(self) -> None:
        self.assertEqual(self.serve([b'{"command":"pi', b'ng"}\n']), {"ok": True, "status": "pong"})

    def test_a_request_exactly_at_the_limit_is_read(self) -> None:
        answer = self.serve([self.padded(daemon.REQUEST_MAX_BYTES) + b"\n"])
        self.assertEqual(answer, {"ok": True, "status": "pong"})

    def test_a_request_past_the_limit_is_refused(self) -> None:
        answer = self.serve([self.padded(daemon.REQUEST_MAX_BYTES + 1) + b"\n"])
        self.assertEqual(answer["error"]["code"], ErrorCode.INVALID_REQUEST.value)
        self.assertIn("too large", answer["error"]["message"])

    def test_a_request_cut_off_before_its_newline_is_incomplete(self) -> None:
        answer = self.serve([b'{"command":"ping"}'])
        self.assertEqual(answer["error"]["code"], ErrorCode.INVALID_REQUEST.value)
        self.assertIn("newline", answer["error"]["message"])

    def test_malformed_json_is_told_apart_from_an_incomplete_request(self) -> None:
        answer = self.serve([b'{"command":}\n'])
        self.assertEqual(answer["error"]["code"], ErrorCode.INVALID_REQUEST.value)
        self.assertIn("JSON", answer["error"]["message"])

    def client(self, pieces: list[bytes], command: str = "ping", repeatable: bool = True, connection=None):
        connection = _Connection(pieces) if connection is None else connection
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "daemon.sock"
        path.touch()
        with (
            patch("kamado.daemon.socket_path", return_value=path),
            patch("kamado.daemon.socket.socket", return_value=connection),
        ):
            return daemon.send_to_daemon(command, {}, repeatable=repeatable)

    def test_a_send_whose_answer_is_lost_is_not_run_again(self) -> None:
        """The daemon may already have typed it; None would send it twice."""
        answer = self.client([b'{"ok": true, "subm'], command="send", repeatable=False)
        assert answer is not None
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["code"], ErrorCode.SEND_FAILED.value)

    def test_a_send_the_daemon_never_received_runs_here(self) -> None:
        class Refused(_Connection):
            def connect(self, _path: str) -> None:
                raise ConnectionRefusedError("synthetic refusal")

        answer = self.client([], command="send", repeatable=False, connection=Refused([]))
        self.assertIsNone(answer)

    def test_an_answer_split_across_reads_is_put_back_together(self) -> None:
        self.assertEqual(self.client([b'{"ok": tr', b'ue}\n']), {"ok": True})

    def test_an_answer_cut_short_is_not_taken_as_whole(self) -> None:
        """None sends the caller to run the command itself, as with no daemon."""
        self.assertIsNone(self.client([b'{"ok": true, "rooms": [']))

    def test_an_answer_past_the_limit_is_not_held(self) -> None:
        with patch.object(daemon, "RESPONSE_MAX_BYTES", 64):
            self.assertIsNone(self.client([b'{"ok": true, "padding": "' + b"x" * 100 + b'"}\n']))


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

    def test_send_dry_run_dispatches_cleanly(self) -> None:
        backend = FakeWindows()
        backend.windows = [ChatWindow(42, "Peer Renamed")]
        sender = Sender(backend, running=lambda: True)
        with patch("kamado.macauto.make_sender", return_value=sender):
            answer = self.dispatch(
                "send", {"room": "Peer Renamed", "text": "hello", "dry_run": True}
            )
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["dryRun"])
        self.assertFalse(answer["submitted"])
        self.assertEqual(answer["room"], "Peer Renamed")
        self.assertIn("roomId", answer)

    def test_send_requires_nonempty_room(self) -> None:
        for payload in ({}, {"room": "   "}):
            with self.subTest(payload=payload):
                with self.assertRaises(KamadoError) as raised:
                    self.dispatch("send", payload)
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)
