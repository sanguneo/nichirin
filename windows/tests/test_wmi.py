"""The WMI wrapper hands back exactly what the CLI produced (issue #111).

Only ``Win32_Process.Create`` is faked: the fake runs the real child routine
in-process instead of starting it through WMI. The request file, the CLI
subprocess, the output files and the named event are all real, and the CLI is
a small synthetic script whose output can be compared byte for byte with a
direct run of the same script.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

from kanao import wmi
from tests.platform import require_windows_libraries

FAKE_CLI = r"""
import json, os, sys
record = os.environ.get("FAKE_CLI_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd()}, f, ensure_ascii=False)
if not os.environ.get("FAKE_CLI_QUIET"):
    sys.stdout.buffer.write(b'{"ok": true, "text": "\xed\x95\x9c"}\r\n\xff raw\n')
    sys.stderr.buffer.write(b"warning \xea\xb8\x80\r\n")
sys.exit(int(os.environ.get("FAKE_CLI_STATUS", "0")))
"""

ARGUMENTS = [
    "search",
    'say "hi"',
    "$env:USERPROFILE $(calc)",
    "\ud55c\uae00 \U0001f600",
    "line one\nline two",
    "",
    "trailing\\",
    '\\"',
    "& | < > ^ %PATH%",
]


def setUpModule() -> None:
    require_windows_libraries()


class _RecordingSignal:
    def __init__(self, event: wmi.Signal) -> None:
        self.event = event
        self.waited: list[float] = []

    def wait(self, seconds: float) -> bool:
        # The bound is recorded and the real event is asked without blocking,
        # so an unsignalled wait answers at once instead of after minutes.
        self.waited.append(seconds)
        return self.event.wait(0)


class FakeBoundary:
    def __init__(
        self,
        *,
        run=subprocess.run,
        failure: str | None = None,
        starts: bool = True,
        capped: bool = True,
        pythons: list[tuple[tuple[int, int], str]] | None = None,
    ):
        self.run = run
        self.failure = failure
        self.starts = starts
        self.is_capped = capped
        self.registered = pythons or []
        self.created: list[tuple[list[str], str]] = []
        self.work: Path | None = None
        self.signals: list[_RecordingSignal] = []

    @contextmanager
    def signal(self, name: str) -> Iterator[wmi.Signal]:
        with wmi.WmiBoundary().signal(name) as event:
            recording = _RecordingSignal(event)
            self.signals.append(recording)
            yield recording

    def capped(self) -> bool:
        return self.is_capped

    def pythons(self) -> list[tuple[tuple[int, int], str]]:
        return self.registered

    def create(self, argv, cwd) -> None:
        self.created.append((list(argv), cwd))
        self.work = Path(argv[-1])
        if self.failure is not None:
            raise wmi.LaunchFailed(self.failure)
        if self.starts:
            wmi.child(self.work, run=self.run)


class WmiTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="kanao-wmi-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cli = self.root / "fake-cli.py"
        self.cli.write_text(FAKE_CLI, encoding="utf-8")
        self.record = self.root / "record.json"
        self.entry = wmi.Entry(cli=self.cli, launcher=self.root / "kanao-wmi.py")
        environment = patch.dict(os.environ, {"FAKE_CLI_RECORD": str(self.record)})
        environment.start()
        self.addCleanup(environment.stop)
        for variable in (wmi.TIMEOUT_VARIABLE, wmi.MODE_VARIABLE, wmi.PYTHON_VARIABLE):
            os.environ.pop(variable, None)

    def wrapped(self, argv: list[str], boundary: FakeBoundary) -> tuple[int, bytes, bytes]:
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
            code = wmi.main(argv, self.entry, boundary)
        stdout.flush()
        stderr.flush()
        return code, stdout.buffer.getvalue(), stderr.buffer.getvalue()

    def envelope(self, argv: list[str], boundary: FakeBoundary) -> dict[str, Any]:
        code, stdout, _stderr = self.wrapped(argv, boundary)
        self.assertEqual(code, 1)
        answer = json.loads(stdout)
        self.assertFalse(answer["ok"])
        return answer["error"]

    def assert_cleaned(self, boundary: FakeBoundary) -> None:
        work = boundary.work
        assert work is not None
        self.assertFalse(work.exists())

    def test_arguments_and_cwd_reach_the_cli_unchanged(self) -> None:
        boundary = FakeBoundary()
        self.wrapped(ARGUMENTS, boundary)
        recorded = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(recorded["argv"], ARGUMENTS)
        self.assertEqual(Path(recorded["cwd"]), Path.cwd())
        (argv, cwd), = boundary.created
        self.assertEqual(argv, [sys.executable, str(self.entry.launcher), wmi.CHILD_FLAG, str(boundary.work)])
        self.assertEqual(cwd, os.getcwd())
        self.assert_cleaned(boundary)

    def test_output_and_status_are_byte_for_byte_those_of_a_direct_run(self) -> None:
        for status in ("0", "1", "2", "7"):
            with self.subTest(status=status), patch.dict(os.environ, {"FAKE_CLI_STATUS": status}):
                direct = subprocess.run(
                    [sys.executable, str(self.cli), "rooms"], capture_output=True, timeout=60, check=False
                )
                code, stdout, stderr = self.wrapped(["rooms"], FakeBoundary())
                self.assertEqual((code, stdout, stderr), (direct.returncode, direct.stdout, direct.stderr))

    def test_long_running_commands_run_directly_without_wmi(self) -> None:
        for command in ("follow", "serve", "tui"):
            with self.subTest(command=command), \
                    patch.dict(os.environ, {"FAKE_CLI_QUIET": "1", "FAKE_CLI_STATUS": "5"}):
                boundary = FakeBoundary()
                code = wmi.main([command, "--interval", "1"], self.entry, boundary)
                self.assertEqual(code, 5)
                self.assertEqual(boundary.created, [])
                recorded = json.loads(self.record.read_text(encoding="utf-8"))
                self.assertEqual(recorded["argv"], [command, "--interval", "1"])

    def test_a_failed_create_is_reported_in_the_envelope_and_never_run_locally(self) -> None:
        for argv, code, disposition in (
            (["rooms"], "READ_FAILED", {"retry": "safe"}),
            (["send", "--room", "x", "hi"], "SEND_FAILED", {"delivery": "not_delivered", "retry": "safe"}),
        ):
            with self.subTest(command=argv[0]):
                boundary = FakeBoundary(failure="ReturnValue 2 (access denied)")
                error = self.envelope(argv, boundary)
                self.assertEqual(error["code"], code)
                self.assertEqual(error["disposition"], disposition)
                self.assertIn("ReturnValue 2", error["message"])
                self.assertFalse(self.record.exists())
                self.assert_cleaned(boundary)

    def test_the_child_enforces_the_timeout_and_the_caller_reports_it(self) -> None:
        seen: list[float] = []

        def expire(command, **options):
            seen.append(options["timeout"])
            raise subprocess.TimeoutExpired(command, options["timeout"])

        with patch.dict(os.environ, {wmi.TIMEOUT_VARIABLE: "42"}):
            for argv, code, disposition in (
                (["rooms"], "READ_FAILED", {"retry": "safe"}),
                (["send", "--room", "x", "hi"], "SEND_FAILED", {"delivery": "unknown", "retry": "after_check"}),
            ):
                with self.subTest(command=argv[0]):
                    boundary = FakeBoundary(run=expire)
                    error = self.envelope(argv, boundary)
                    self.assertEqual((error["code"], error["disposition"]), (code, disposition))
                    self.assertIn(wmi.TIMEOUT_VARIABLE, error["message"])
                    self.assert_cleaned(boundary)
        self.assertEqual(seen, [42.0, 42.0])

    def test_a_child_that_never_answers_is_bounded_and_cleaned_up(self) -> None:
        with patch.dict(os.environ, {wmi.TIMEOUT_VARIABLE: "30"}):
            boundary = FakeBoundary(starts=False)
            error = self.envelope(["rooms"], boundary)
        self.assertEqual(error["code"], "READ_FAILED")
        self.assertEqual(boundary.signals[0].waited, [30 + wmi.GRACE_SECONDS])
        self.assert_cleaned(boundary)

    def test_without_a_cap_the_cli_runs_directly_and_wmi_is_never_asked(self) -> None:
        boundary = FakeBoundary(capped=False)
        code, stdout, stderr = self.wrapped(["rooms"], boundary)
        self.assertEqual(code, 0)
        self.assertEqual(boundary.created, [])
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["argv"], ["rooms"])

    def test_the_mode_variable_overrides_the_cap_check(self) -> None:
        for mode, capped, relaunched in (
            ("always", False, True),
            ("ALWAYS", False, True),
            ("never", True, False),
            ("auto", True, True),
            ("auto", False, False),
        ):
            with self.subTest(mode=mode, capped=capped), patch.dict(os.environ, {wmi.MODE_VARIABLE: mode}):
                boundary = FakeBoundary(capped=capped)
                with patch.dict(os.environ, {"FAKE_CLI_QUIET": "1"}):
                    code = wmi.main(["rooms"], self.entry, boundary)
                self.assertEqual(code, 0)
                self.assertEqual(bool(boundary.created), relaunched)

    def test_an_unknown_mode_is_refused_before_anything_starts(self) -> None:
        with patch.dict(os.environ, {wmi.MODE_VARIABLE: "sometimes"}):
            boundary = FakeBoundary()
            error = self.envelope(["rooms"], boundary)
        self.assertEqual(error["code"], "INVALID_REQUEST")
        self.assertIn(wmi.MODE_VARIABLE, error["message"])
        self.assertEqual(boundary.created, [])
        self.assertFalse(self.record.exists())

    def test_an_older_caller_runs_the_cli_on_the_newest_registered_311_or_later(self) -> None:
        old, newer, newest = (self.root / name for name in ("old.exe", "newer.exe", "newest.exe"))
        for path in (old, newer, newest):
            path.write_bytes(b"")
        missing = str(self.root / "missing.exe")
        registered = [
            ((3, 10), str(old)),
            ((3, 13), str(newer)),
            ((3, 14), str(newest)),
            ((3, 15), missing),
        ]
        boundary = FakeBoundary(pythons=registered)
        with patch.object(sys, "version_info", (3, 10, 11)):
            self.assertEqual(wmi._python(os.environ, boundary), str(newest))
            self.assertEqual(wmi._python(os.environ, FakeBoundary(pythons=registered[:1])), sys.executable)
        with patch.object(sys, "version_info", (3, 12, 0)):
            self.assertEqual(wmi._python(os.environ, boundary), sys.executable)
        with patch.object(sys, "version_info", (3, 10, 11)):
            self.assertEqual(wmi._python({wmi.PYTHON_VARIABLE: str(newer)}, boundary), str(newer))

    def test_the_chosen_interpreter_starts_the_child_and_the_direct_run(self) -> None:
        boundary = FakeBoundary(starts=False)
        with patch.object(wmi, "_python", return_value="C:/chosen/python.exe"):
            self.wrapped(["rooms"], boundary)
        (argv, _cwd), = boundary.created
        self.assertEqual(argv[0], "C:/chosen/python.exe")
        with patch.object(wmi.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            with patch.object(wmi, "_python", return_value="C:/chosen/python.exe"):
                wmi.main(["rooms"], self.entry, FakeBoundary(capped=False))
        self.assertEqual(popen.call_args.args[0][0], "C:/chosen/python.exe")

    def test_a_kanao_python_that_names_no_file_is_refused(self) -> None:
        with patch.dict(os.environ, {wmi.PYTHON_VARIABLE: str(self.root / "absent.exe")}):
            boundary = FakeBoundary()
            error = self.envelope(["rooms"], boundary)
        self.assertEqual(error["code"], "INVALID_REQUEST")
        self.assertEqual(boundary.created, [])

    def test_an_unusable_timeout_is_refused_before_anything_starts(self) -> None:
        for value in ("abc", "0", "-5", "nan"):
            with self.subTest(value=value), patch.dict(os.environ, {wmi.TIMEOUT_VARIABLE: value}):
                boundary = FakeBoundary()
                error = self.envelope(["rooms"], boundary)
                self.assertEqual(error["code"], "INVALID_REQUEST")
                self.assertEqual(boundary.created, [])


if __name__ == "__main__":
    unittest.main()
