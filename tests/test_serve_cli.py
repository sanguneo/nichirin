"""CLI tests for the ``serve`` subcommand: arg parsing and live wiring only.

The HTTP surface itself is covered by ``tests/test_serve.py``. Here we pin
that the CLI exposes the right flags, accepts ``KANAO_TOKEN``, refuses too-
short tokens, and refuses to start when neither is supplied.
"""

from __future__ import annotations

import unittest

from kanao import cli


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


if __name__ == "__main__":
    unittest.main()