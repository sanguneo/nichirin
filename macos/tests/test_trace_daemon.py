"""Daemon trace transport with only synthetic sockets and dispatch."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import kakaocommon.trace as trace
import kakaocommon.trace_transport as trace_transport
from kamado import daemon
from kamado.macauto import Sender
from tests.test_send import FakeWindows
from tests.fixtures import DIRECT_ROOM, build_reader
from kamado.macauto import ChatWindow
from types import SimpleNamespace


IDENTITY = trace.Identity("macos", "kamado", "test")


class DaemonTraceTests(unittest.TestCase):
    def test_forwarded_confirmation_dispatches_once_and_retains_local_trace_result(self):
        reader = build_reader()
        self.addCleanup(reader.close)
        server = daemon.DaemonServer()
        server._reader = reader
        backend = FakeWindows()
        backend.windows = [ChatWindow(42, "Peer Renamed")]
        rows = []
        original_submit = backend.submit

        def submit(control):
            original_submit(control)
            rows.append(SimpleNamespace(id=2, text="synthetic", from_me=True))

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "daemon.sock"
            socket_path.touch()
            connection = MagicMock()
            connection.__enter__.return_value = connection

            def dispatch(payload):
                request = json.loads(payload)
                answer = server._dispatch(request["command"], request["args"])
                connection.recv.return_value = (json.dumps(answer) + "\n").encode()

            connection.sendall.side_effect = dispatch
            with patch("kamado.daemon.socket_path", return_value=socket_path), \
                 patch("kamado.daemon.socket.socket", return_value=connection), \
                 patch("kamado.macauto.make_sender", return_value=Sender(backend, running=lambda: True)), \
                 patch.object(backend, "submit", side_effect=submit), \
                 patch.object(reader, "newest_log_id", return_value=1), \
                 patch.object(reader, "messages", side_effect=lambda _, **kw: [
                     row for row in rows if row.id > kw["after_id"]
                 ]):
                with trace.attempt(directory, IDENTITY, "cli"):
                    answer = daemon.send_to_daemon("send", {
                        "room_id": DIRECT_ROOM, "text": "synthetic", "confirm": 1,
                    }, repeatable=False)
            assert answer is not None
            self.assertEqual((answer["logged"], answer["messageId"]), (True, "2"))
            self.assertEqual(answer["disposition"], {"delivery": "submitted", "retry": "unsafe"})
            events = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            self.assertEqual(events[-1]["conditions"]["L"], "pass")
            self.assertEqual(backend.actions, ["fill", "submit"])

    def test_lost_response_is_correlated_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a daemon that receives the request but loses the answer.
            socket_path = Path(directory) / "daemon.sock"
            socket_path.touch()
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.recv.return_value = b""
            with patch("kamado.daemon.socket_path", return_value=socket_path):
                with patch("kamado.daemon.socket.AF_UNIX", 1, create=True):
                    with patch("kamado.daemon.socket.socket", return_value=connection):
                        # When a nonrepeatable request is forwarded.
                        with trace.attempt(directory, IDENTITY, "cli"):
                            answer = daemon.send_to_daemon("send", {"text": "PRIVATE"}, repeatable=False)
            # Then the caller gets unknown, not the fallback sentinel.
            self.assertIsNotNone(answer)
            assert answer is not None
            self.assertEqual(answer["error"]["disposition"]["delivery"], "unknown")
            rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            request = json.loads(connection.sendall.call_args.args[0])
            self.assertEqual(request["_trace"]["attempt_id"], rows[0]["attempt_id"])
            self.assertIn("forward", [row["stage"] for row in rows])
            self.assertEqual(rows[-1]["outcome"], "unknown")
            self.assertNotIn("PRIVATE", json.dumps(rows))
            connection.sendall.assert_called_once()

    def test_non_envelope_answer_does_not_request_a_local_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given a fully written send whose daemon returns the wrong JSON shape.
            socket_path = Path(directory) / "daemon.sock"
            socket_path.touch()
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.recv.return_value = b"[]\n"
            with patch("kamado.daemon.socket_path", return_value=socket_path):
                with patch("kamado.daemon.socket.AF_UNIX", 1, create=True):
                    with patch("kamado.daemon.socket.socket", return_value=connection):
                        # When the client parses the response.
                        answer = daemon.send_to_daemon("send", {}, repeatable=False)
            # Then it returns an unknown failure rather than the local-fallback sentinel.
            assert answer is not None
            self.assertEqual(answer["error"]["disposition"]["delivery"], "unknown")
            connection.sendall.assert_called_once()

    def test_daemon_native_stages_append_to_the_forwarded_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            # Given an exclusive caller-owned attempt and one synthetic server connection.
            backend = FakeWindows()
            server = daemon.DaemonServer()
            server.running = True
            connection = MagicMock()
            connection.__enter__.return_value = connection

            def accept():
                server.running = False
                return connection, None

            with trace.attempt(directory, IDENTITY, "cli"):
                descriptor = trace_transport.export()
                raw = json.dumps({"command": "send", "args": {}, "_trace": descriptor}).encode()
                with patch("kamado.daemon.socket.AF_UNIX", 1, create=True):
                    with patch("kamado.daemon.socket.socket") as socket:
                        socket.return_value.__enter__.return_value.accept.side_effect = accept
                        with patch("kamado.daemon._receive_line", return_value=raw):
                            with patch.object(server, "_dispatch", side_effect=lambda *_: {
                                "ok": True, **Sender(backend, running=lambda: True).send("Synthetic room", "PRIVATE")
                            }):
                                # When the real server dispatch boundary runs.
                                server._serve()
            # Then native stages share the attempt, not an unrelated file.
            files = list(Path(directory).glob("*.jsonl"))
            rows = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual(len(files), 1)
            self.assertEqual(len({row["attempt_id"] for row in rows}), 1)
            self.assertTrue(any(row["stage"] == "write" and row["route"] == "daemon" for row in rows))
            self.assertEqual(backend.actions, ["fill", "submit"])
            self.assertNotIn("PRIVATE", json.dumps(rows))
