"""Daemon error envelopes without sockets, processes or account access."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import kamado.daemon as daemon


class DaemonErrorTests(unittest.TestCase):
    def test_dispatch_failures_preserve_writing_context_and_bad_frames_stay_typed(self) -> None:
        for raw, code, disposition in (
            (b'{"command":"send"}', "SEND_FAILED", {"delivery": "unknown", "retry": "after_check"}),
            (b'{"command":"rooms"}', "READ_FAILED", {"retry": "safe"}),
            (b'{"command":', "INVALID_REQUEST", {"retry": "safe"}),
        ):
            with self.subTest(raw=raw):
                server = daemon.DaemonServer()
                server.running = True
                connection = MagicMock()
                connection.__enter__.return_value = connection

                def accept():
                    server.running = False
                    return connection, None

                with patch("kamado.daemon.socket.AF_UNIX", 1, create=True):
                    with patch("kamado.daemon.socket.socket") as socket:
                        socket.return_value.__enter__.return_value.accept.side_effect = accept
                        with patch("kamado.daemon._receive_line", return_value=raw):
                            with patch.object(server, "_dispatch", side_effect=RuntimeError("synthetic")):
                                server._serve()
                answer = json.loads(connection.sendall.call_args.args[0])
                self.assertEqual(answer["error"]["code"], code)
                self.assertEqual(answer["error"]["disposition"], disposition)

    def test_a_lost_send_response_requires_a_check_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daemon.sock"
            path.touch()
            with patch("kamado.daemon.socket_path", return_value=path):
                with patch("kamado.daemon.socket.AF_UNIX", 1, create=True):
                    with patch("kamado.daemon.socket.socket") as socket:
                        socket.return_value.__enter__.return_value.recv.return_value = b""
                        answer = daemon.send_to_daemon("send", {}, repeatable=False)
        assert answer is not None
        self.assertEqual(answer["error"]["code"], "SEND_FAILED")
        self.assertEqual(
            answer["error"]["disposition"], {"delivery": "unknown", "retry": "after_check"}
        )

    def test_stopping_a_missing_daemon_has_a_safe_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("kamado.daemon.socket_path", return_value=Path(directory) / "daemon.sock"):
                with patch("kamado.daemon.pid_path", return_value=Path(directory) / "daemon.pid"):
                    server = daemon.DaemonServer()
            with patch("kamado.daemon.send_to_daemon", return_value=None):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(server.stop(), 1)
        answer = json.loads(output.getvalue())
        self.assertEqual(answer["error"]["code"], "NOT_RUNNING")
        self.assertEqual(answer["error"]["disposition"], {"retry": "safe"})

    def test_start_timeout_has_a_safe_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("kamado.daemon.socket_path", return_value=Path(directory) / "daemon.sock"):
                with patch("kamado.daemon.pid_path", return_value=Path(directory) / "daemon.pid"):
                    server = daemon.DaemonServer()
            with patch("kamado.daemon.is_daemon_running", return_value=False):
                with patch("kamado.daemon.os.fork", return_value=123, create=True):
                    with patch("kamado.daemon.time.time", side_effect=[0.0, 6.0]):
                        with patch("sys.stdout", new_callable=io.StringIO) as output:
                            self.assertEqual(server.start(), 1)
        answer = json.loads(output.getvalue())
        self.assertEqual(answer["error"]["code"], "START_TIMEOUT")
        self.assertEqual(answer["error"]["disposition"], {"retry": "safe"})
