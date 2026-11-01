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


IDENTITY = trace.Identity("macos", "kamado", "test")


class DaemonTraceTests(unittest.TestCase):
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
