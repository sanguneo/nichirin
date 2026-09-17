"""The old ``kakaoread`` name keeps working after the rename to ``kamado-cli``.

What matters is not that the old name imports *something* — it is that it
imports the *same* objects.  A shim that loaded a second copy would give the
two names separate ``KamadoError`` classes, so an ``except`` written against one
would not catch the other, and the failure would only show up in the field.

The imports here go through :func:`importlib.import_module` on purpose: the
alias is resolved by a meta path finder at run time, so a static import of
``kakaoread.errors`` is a name no type checker can follow.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> subprocess.CompletedProcess:
    """Drive a command the way a caller would, from outside the checkout."""
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class AliasedImportTests(unittest.TestCase):
    def test_a_module_under_the_old_name_is_the_module_under_the_new_one(self) -> None:
        self.assertIs(import_module("kakaoread.errors"), import_module("kamado.errors"))

    def test_a_nested_module_resolves_through_the_old_name(self) -> None:
        self.assertIs(
            import_module("kakaoread.tui.layout"), import_module("kamado.tui.layout")
        )

    def test_an_error_class_is_one_class_under_both_names(self) -> None:
        self.assertIs(
            import_module("kakaoread.errors").KamadoError,
            import_module("kamado.errors").KamadoError,
        )

    def test_an_error_raised_under_one_name_is_caught_under_the_other(self) -> None:
        old = import_module("kakaoread.errors")
        new = import_module("kamado.errors")

        with self.assertRaises(new.KamadoError):
            raise old.KamadoError(old.ErrorCode.READ_FAILED, "raised through the old name")

    def test_the_package_reads_the_new_packages_own_attributes(self) -> None:
        self.assertEqual(
            import_module("kakaoread").__version__, import_module("kamado").__version__
        )

    def test_importing_through_the_alias_leaves_the_real_spec_alone(self) -> None:
        """The alias must not leave :mod:`kamado` describing itself as ``kakaoread``."""
        import_module("kakaoread.keyring")
        keyring = import_module("kamado.keyring")

        self.assertEqual(keyring.__name__, "kamado.keyring")
        self.assertEqual(getattr(keyring.__spec__, "name", None), "kamado.keyring")


class LegacyCommandTests(unittest.TestCase):
    def test_the_old_module_command_still_runs(self) -> None:
        result = run("-m", "kakaoread", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kamado", result.stdout)

    def test_the_old_module_command_names_the_current_one(self) -> None:
        result = run("-m", "kakaoread", "--help")

        self.assertIn("kamado-cli", result.stderr)

    def test_the_old_launcher_still_runs(self) -> None:
        result = run(str(ROOT / "skill" / "scripts" / "kakaoread.py"), "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kamado-cli", result.stderr)

    def test_the_notice_stays_out_of_the_parsed_output(self) -> None:
        """stdout carries the JSON envelope, so the notice belongs on stderr."""
        result = run("-m", "kakaoread", "--help")

        self.assertNotIn("kamado-cli", result.stdout)


if __name__ == "__main__":
    unittest.main()
