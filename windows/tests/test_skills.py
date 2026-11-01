"""Offline documentation must run before any live backend is acquired."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kakaocommon.skills import get_document
from kanao import __version__, cli


class SkillsTests(unittest.TestCase):
    def test_root_redirect_finds_the_same_bundled_document(self) -> None:
        # Given: the documented checkout-root entry point uses a redirect package.
        root = Path(__file__).resolve().parents[2]
        # When: a fresh CLI process requests only its bundled documentation.
        result = subprocess.run(
            [sys.executable, "-m", "kanao", "skills", "get", "read"],
            cwd=root, capture_output=True, timeout=30, check=False,
        )
        # Then: the redirect reaches the real package's versioned resource.
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8"))
        answer = json.loads(result.stdout)
        self.assertEqual(answer["version"], __version__)
        self.assertEqual(answer["topic"], "read")

    def test_topic_selection_keeps_common_and_repeated_sections(self) -> None:
        # Given: synthetic resource tokens, with no natural-language assertions.
        source = (
            "HEADER\n<!-- skills:read -->READ\n<!-- skills:send -->SEND\n"
            "<!-- skills:serve -->SERVE\n<!-- skills:send -->SEND2\n"
            "<!-- skills:common -->COMMON\n"
        )
        with tempfile.TemporaryDirectory(prefix="kanao-skills-") as directory:
            package = Path(directory)
            (package / "agent_docs").mkdir()
            (package / "agent_docs/SKILL.md").write_text(source, encoding="utf-8")
            for topic, expected in (
                ("read", "HEADER\nREAD\nCOMMON\n"),
                ("send", "HEADER\nSEND\nSEND2\nCOMMON\n"),
                ("serve", "HEADER\nSERVE\nCOMMON\n"),
                (None, source),
            ):
                with self.subTest(topic=topic), \
                        patch("kakaocommon.skills.files", return_value=package / "agent_docs"):
                    # When: the real resource reader selects a topic.
                    answer = get_document("fixture", "9.8.7", topic)
                    # Then: only that topic and common sections survive.
                    self.assertEqual(answer["content"], expected)

    def test_get_returns_versioned_documents_without_live_access(self) -> None:
        # Given: live readers and sockets must never be acquired.
        for topic in ("send", "read", "serve"):
            with self.subTest(topic=topic):
                output = io.StringIO()
                with patch.object(cli, "Reader", side_effect=AssertionError("live reader")), \
                        patch("socket.socket", side_effect=AssertionError("network")), \
                        redirect_stdout(output):
                    # When: documentation is requested through the real entry point.
                    code = cli.main(["skills", "get", topic])
                # Then: the topic and header describe this runtime's version.
                self.assertEqual(code, 0)
                answer = json.loads(output.getvalue())
                self.assertEqual(set(answer), {"ok", "topic", "version", "content"})
                self.assertTrue(answer["ok"])
                self.assertEqual(answer["topic"], topic)
                self.assertEqual(answer["version"], __version__)
                header = dict(
                    line.split(": ", 1)
                    for line in answer["content"].split("---", 2)[1].strip().splitlines()
                )
                self.assertEqual(header["version"], __version__)
                self.assertGreater(len(answer["content"].split("---", 2)[2].strip()), 0)

    def test_get_returns_the_full_skill_when_topic_is_omitted(self) -> None:
        # Given: no topic is selected.
        output = io.StringIO()
        with patch.object(cli, "Reader", side_effect=AssertionError("live reader")), \
                redirect_stdout(output):
            # When: the default document is requested.
            code = cli.main(["skills", "get"])
        # Then: the default topic is explicit.
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["topic"], "all")

    def test_invalid_skill_invocations_exit_two_without_live_access(self) -> None:
        # Given: missing actions, unknown actions and unknown topics.
        for args in (["skills"], ["skills", "list"], ["skills", "get", "unknown"]):
            with self.subTest(args=args), \
                    patch.object(cli, "Reader", side_effect=AssertionError("live reader")), \
                    redirect_stderr(io.StringIO()):
                # When: argparse handles malformed input.
                with self.assertRaises(SystemExit) as stopped:
                    cli.main(args)
                # Then: the existing argument-error contract is preserved.
                self.assertEqual(stopped.exception.code, 2)
