"""The repository root keeps the old commands, and test modules import alone."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
REPOSITORY = PLATFORM.parent
BUILD = "kamado" if sys.platform == "darwin" else "kanao"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )


class RootEntryTests(unittest.TestCase):
    def test_nichirin_runs_the_build_for_this_machine(self) -> None:
        result = run("-m", "nichirin", "--version", cwd=REPOSITORY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(BUILD), result.stdout)

    def test_the_build_name_still_runs_from_the_root(self) -> None:
        result = run("-m", "kanao", "--version", cwd=REPOSITORY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("kanao"), result.stdout)

    def test_an_unknown_option_is_refused_through_the_redirect(self) -> None:
        result = run("-m", "nichirin", "--no-such-option", cwd=REPOSITORY)
        self.assertEqual(result.returncode, 2)

    def test_the_root_installer_passes_its_arguments_on(self) -> None:
        result = run("install.py", "--help", cwd=REPOSITORY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--target", result.stdout)


class IsolatedImportTests(unittest.TestCase):
    def test_a_shared_module_test_imports_on_its_own(self) -> None:
        result = run("-m", "unittest", "tests.test_wal", cwd=PLATFORM)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_platform_package_is_the_real_one_not_the_redirect(self) -> None:
        probe = (
            "import tests, kakaocommon.wal, kanao, pathlib, sys;"
            "print(pathlib.Path(kanao.__file__).resolve().parent.parent)"
        )
        result = run("-c", probe, cwd=PLATFORM)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), PLATFORM)


if __name__ == "__main__":
    unittest.main()
