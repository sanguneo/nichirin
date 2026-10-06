"""Installer tests: ship exactly the payload, and keep working once moved.

The installed copy is what users actually run, so these drive the real script
in a subprocess against a throwaway home rather than importing it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT.parent / "kakaocommon"
LAUNCHER = Path("scripts/kamado-cli.py")
LEGACY_LAUNCHER = Path("scripts/kakaoread.py")
KOREAN = "\ud55c\uae00"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="kamado-cli-test-")
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.cwd = self.work / "unrelated cwd"
        self.cwd.mkdir()
        self.home = self.work / f"home {KOREAN}"
        self.home.mkdir()
        self.target = self.work / f"skill {KOREAN}"

        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            PYTHONIOENCODING="ascii",
            PYTHONDONTWRITEBYTECODE="1",
        )

    def python(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, *args],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            check=False,
            timeout=120,
        )

    def install(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return self.python(str(ROOT / "install.py"), *args)

    def installed(self) -> Path:
        result = self.install("--target", str(self.target))
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.target


class PayloadTests(InstallerTests):
    def test_installs_to_the_standard_agent_directory_by_default(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".agents/skills/kamado-cli" / LAUNCHER).is_file())

    def test_installs_exactly_where_asked_without_appending_a_subdirectory(self) -> None:
        target = self.home / ".claude/skills/kamado-cli"
        result = self.install("--target", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / LAUNCHER).is_file())
        self.assertFalse((target / "kamado").exists())

    def test_ships_the_documented_payload_and_nothing_else(self) -> None:
        target = self.installed()
        installed = {
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file()
        }
        expected = {
            "SKILL.md",
            "LICENSE",
            LAUNCHER.as_posix(),
            LEGACY_LAUNCHER.as_posix(),
        } | {
            f"scripts/kamado/{source.relative_to(ROOT / 'kamado').as_posix()}"
            for source in (ROOT / "kamado").rglob("*.py")
        } | {
            f"scripts/kakaocommon/{source.relative_to(COMMON).as_posix()}"
            for source in COMMON.rglob("*.py")
        }
        self.assertEqual(installed, expected)

    def test_never_copies_compiled_caches(self) -> None:
        target = self.installed()
        self.assertEqual([p for p in target.rglob("__pycache__")], [])

    def test_shipped_files_are_copied_byte_for_byte(self) -> None:
        target = self.installed()
        self.assertEqual(
            (target / "SKILL.md").read_bytes(), (ROOT / "skill/SKILL.md").read_bytes()
        )
        self.assertEqual((target / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())


class SafetyTests(InstallerTests):
    def test_refuses_an_existing_directory_without_consent(self) -> None:
        self.target.mkdir()
        keep = self.target / "SKILL.md"
        keep.write_bytes(b"USER_CONTENT")

        result = self.install("--target", str(self.target))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(keep.read_bytes(), b"USER_CONTENT")
        self.assertFalse((self.target / LAUNCHER).exists())

    def test_force_replaces_shipped_files_and_leaves_others_alone(self) -> None:
        target = self.installed()
        (target / LAUNCHER).write_bytes(b"STALE")
        extras = (target / "notes.txt", target / "scripts/kamado/custom.py")
        for path in extras:
            path.write_bytes(b"USER_CONTENT")

        result = self.install("--target", str(target), "--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (target / LAUNCHER).read_bytes(), (ROOT / "skill" / LAUNCHER).read_bytes()
        )
        for path in extras:
            self.assertEqual(path.read_bytes(), b"USER_CONTENT")

    def test_refuses_to_replace_a_directory_that_shares_a_shipped_name(self) -> None:
        conflict = self.target / "SKILL.md"
        conflict.mkdir(parents=True)
        (conflict / "notes.txt").write_bytes(b"USER_CONTENT")

        result = self.install("--target", str(self.target), "--force")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((conflict / "notes.txt").read_bytes(), b"USER_CONTENT")


class PortabilityTests(InstallerTests):
    def moved(self) -> Path:
        destination = self.work / f"moved {KOREAN} skill"
        shutil.move(str(self.installed()), destination)
        return destination

    def test_a_moved_installation_still_runs_without_the_checkout(self) -> None:
        result = self.python("-I", "-S", str(self.moved() / LAUNCHER), "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"follow", result.stdout)
        self.assertEqual(result.stderr, b"")

    def test_a_moved_installation_imports_only_its_own_runtime(self) -> None:
        moved = self.moved()
        probe = (
            "import runpy, sys; sys.argv = [sys.argv[1], '--help']\n"
            "try: runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "except SystemExit as stop: assert stop.code == 0\n"
            "for name, module in sys.modules.items():\n"
            " if name == 'kamado' or name.startswith('kamado.'):\n"
            "  print('ORIGIN', name, module.__file__, sep='\\t')\n"
        )
        result = self.python("-S", "-c", probe, str(moved / LAUNCHER))
        self.assertEqual(result.returncode, 0, result.stderr)

        origins = [
            line.split("\t")
            for line in result.stdout.decode("utf-8").splitlines()
            if line.startswith("ORIGIN\t")
        ]
        self.assertTrue(any(name == "kamado.cli" for _tag, name, _file in origins))
        for _tag, _name, filename in origins:
            # macOS aliases /var through /private/var; compare canonical paths
            # because the launcher resolves its own directory before importing.
            self.assertTrue(
                Path(filename).resolve().is_relative_to((moved / "scripts/kamado").resolve()),
                filename,
            )

    def test_output_is_utf8_even_when_the_environment_asks_for_ascii(self) -> None:
        """The launcher must not mangle Korean text on a legacy console."""
        launcher = self.installed() / LAUNCHER
        sentinel = f"{KOREAN}_SENTINEL"
        result = self.python(str(launcher), sentinel)
        self.assertEqual(result.returncode, 2)
        self.assertIn(sentinel, result.stderr.decode("utf-8"))

    def test_the_installer_reports_a_unicode_destination_as_utf8(self) -> None:
        result = self.install("--target", str(self.target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(KOREAN, result.stdout.decode("utf-8"))


class PartialCheckoutTests(InstallerTests):
    def test_a_checkout_without_the_shared_runtime_installs_nothing(self) -> None:
        """Copied without the repository around it, the installer used to succeed
        and leave a launcher that could not import ``kakaocommon``."""
        copy = self.work / "platform only" / ROOT.name
        shutil.copytree(
            ROOT, copy, ignore=shutil.ignore_patterns("__pycache__", "tests", "demo", "assets")
        )
        result = self.python(str(copy / "install.py"), "--target", str(self.target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"shared runtime", result.stderr)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
