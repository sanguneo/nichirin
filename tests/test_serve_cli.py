"""CLI tests for the ``serve`` subcommand: arg parsing only.

The HTTP surface itself is covered by ``tests/serve/``. Here we pin that the
CLI exposes the right flags, accepts a long token via ``--token`` or
``KAMADO_TOKEN``, refuses too-short tokens, validates ``--min-interval``,
and falls through to the runtime handler when both pieces are present.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from kamado import cli
from kamado.errors import ErrorCode, KamadoError


class ServeArgumentTests(unittest.TestCase):
    def test_serve_defaults_match_the_documented_surface(self) -> None:
        arguments = cli.build_parser().parse_args(["serve"])
        self.assertEqual(arguments.host, "127.0.0.1")
        self.assertEqual(arguments.port, 8765)
        self.assertIsNone(arguments.token)
        self.assertEqual(arguments.min_interval, 2.0)
        self.assertEqual(arguments.poll_interval, 1.0)

    def test_serve_accepts_a_long_token_flag(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["serve", "--token", "long-enough-shared-secret-1234"]
        )
        self.assertEqual(arguments.token, "long-enough-shared-secret-1234")

    def test_serve_accepts_zero_min_interval(self) -> None:
        arguments = cli.build_parser().parse_args(["serve", "--min-interval", "0"])
        self.assertEqual(arguments.min_interval, 0.0)

    def test_serve_rejects_negative_and_infinite_min_interval(self) -> None:
        for bad in ("-1", "inf", "nan"):
            with self.assertRaises(SystemExit, msg=bad):
                cli.build_parser().parse_args(["serve", "--min-interval", bad])

    def test_serve_accepts_a_custom_host_port_and_intervals(self) -> None:
        arguments = cli.build_parser().parse_args(
            [
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                "9100",
                "--min-interval",
                "5",
                "--poll-interval",
                "0.5",
            ]
        )
        self.assertEqual(arguments.host, "0.0.0.0")
        self.assertEqual(arguments.port, 9100)
        self.assertEqual(arguments.min_interval, 5.0)
        self.assertEqual(arguments.poll_interval, 0.5)


class ServeRuntimeTests(unittest.TestCase):
    """End-to-end: parser -> run() with stubs covering the live side."""

    def setUp(self) -> None:
        # Starting sweeps the real temporary directory and may warn on stderr;
        # neither belongs to these tests unless one asks for it.
        for target, value in (("kamado.snapshot._warned", [True]),
                              ("kamado.snapshot.sweep_orphans", mock.Mock())):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _args(self, *extra):
        return cli.build_parser().parse_args(["serve", *extra])

    def test_missing_token_fails_fast_with_invalid_request(self) -> None:
        arguments = self._args()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KamadoError) as raised:  # main() wraps this in the envelope
                cli.run(arguments, mock.Mock())  # serve never touches the reader
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_short_token_fails_fast_with_invalid_request(self) -> None:
        arguments = self._args("--token", "too-short")
        with self.assertRaises(KamadoError) as raised:
            cli.run(arguments, mock.Mock())
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_kamado_token_env_var_is_used_when_flag_is_absent(self) -> None:
        arguments = self._args()
        captured: dict = {}

        def fake_run(host, port, send, **kwargs):
            captured["token"] = kwargs["token"]
            return 0

        with mock.patch.dict(os.environ,
                             {"KAMADO_TOKEN": "from-environment-1234567890"}), \
                mock.patch("kamado.serve.run", side_effect=fake_run):
            status = cli.run(arguments, mock.Mock())
        self.assertEqual(status, 0)  # run() returns int; main() owns the envelope
        self.assertEqual(captured["token"], "from-environment-1234567890")

    def test_token_flag_wins_over_environment(self) -> None:
        arguments = self._args("--token", "flag-token-1234567890")
        captured: dict = {}

        def fake_run(host, port, send, **kwargs):
            captured["token"] = kwargs["token"]
            return 0

        with mock.patch.dict(os.environ,
                             {"KAMADO_TOKEN": "from-environment-1234567890"}), \
                mock.patch("kamado.serve.run", side_effect=fake_run):
            status = cli.run(arguments, mock.Mock())
        self.assertEqual(status, 0)
        self.assertEqual(captured["token"], "flag-token-1234567890")

    def test_starting_warns_about_the_disk_path_and_sweeps_orphans(self) -> None:
        arguments = self._args("--token", "flag-token-1234567890")
        stderr = io.StringIO()
        with mock.patch("kamado.snapshot._warned", []), \
                mock.patch("kamado.snapshot.plaintext_reaches_disk", return_value=True), \
                mock.patch("kamado.snapshot.sweep_orphans") as sweep, \
                mock.patch("sys.stderr", stderr), \
                mock.patch("kamado.serve.run", return_value=0):
            status = cli.run(arguments, mock.Mock())
        self.assertEqual(status, 0)
        sweep.assert_called_once_with()
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("lacks sqlite3 deserialize", lines[0])


if __name__ == "__main__":
    unittest.main()
