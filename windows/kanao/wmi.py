"""Run the CLI outside the caller's Job Object, through WMI (issue #111).

Some agents start every process inside a Job Object with a CPU rate cap.
Each command decrypts its databases afresh, so under a 1% cap ``rooms`` took
seconds where it took one, and minutes on a large account.  A process that
``Win32_Process.Create`` starts belongs to no job of ours, so this wrapper
starts a hidden child that way and hands back what the CLI produced: the same
stdout and stderr bytes and the same exit status.

Nothing the caller typed reaches a command line.  The arguments, working
directory and environment travel in a JSON file inside a private temporary
directory; the child's command line names only this interpreter, the launcher
and that directory.  The child runs the ordinary CLI with its output going to
files there, under the timeout, and signals a named event when it is done,
which is what the caller waits on.  The directory is removed afterwards.

``follow``, ``serve`` and ``tui`` never finish or need the caller's console,
so they run directly, as if the wrapper were not there.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Protocol, Sequence

from .errors import ErrorCode, KanaoError
from .wmicom import LaunchFailed, WmiBoundary, set_event

DIRECT_COMMANDS = frozenset({"follow", "serve", "tui"})
"""Commands that run until stopped or draw on the caller's own console."""

CHILD_FLAG = "--kanao-wmi-child"
TIMEOUT_VARIABLE = "KANAO_WMI_TIMEOUT"
DEFAULT_TIMEOUT = 600.0
GRACE_SECONDS = 60.0
"""How much longer than the child's own timeout the caller waits for its signal.

The child enforces the timeout itself and kills the CLI when it passes, so the
caller's wait only ends unsignalled when the child never started or died.
"""

REQUEST, STDOUT, STDERR, STATUS = "request.json", "stdout", "stderr", "status.json"

class Signal(Protocol):
    def wait(self, seconds: float) -> bool:
        """Return True once the child signalled, False when ``seconds`` passed first."""
        ...


class Boundary(Protocol):
    """Where this module meets Windows; tests replace it."""

    def signal(self, name: str) -> ContextManager[Signal]: ...

    def create(self, argv: Sequence[str], cwd: str) -> None:
        """Start ``argv`` through WMI, hidden, in ``cwd``; raise ``LaunchFailed``."""
        ...


@dataclass(frozen=True)
class Entry:
    cli: Path
    launcher: Path


def main(argv: Sequence[str], entry: Entry, boundary: Boundary | None = None) -> int:
    if len(argv) == 2 and argv[0] == CHILD_FLAG:
        child(Path(argv[1]))
        return 0
    command = _command(argv)
    if command in DIRECT_COMMANDS:
        return _direct(entry.cli, argv)
    try:
        timeout = _timeout(os.environ)
        return _relaunch(argv, entry, boundary or WmiBoundary(), timeout)
    except KanaoError as error:
        _emit(error.envelope(writing=command == "send"))
        return 1


def _command(argv: Sequence[str]) -> str | None:
    return next((argument for argument in argv if not argument.startswith("-")), None)


def _direct(cli: Path, argv: Sequence[str]) -> int:
    process = subprocess.Popen([sys.executable, str(cli), *argv])
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The console delivered the interrupt to the CLI as well; let it
            # finish and report its own status, as it would without us.
            continue


def _timeout(environment: Mapping[str, str]) -> float:
    text = environment.get(TIMEOUT_VARIABLE)
    if text is None:
        return DEFAULT_TIMEOUT
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not 0 < value < 86_400:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST,
            f"{TIMEOUT_VARIABLE} must be a number of seconds between 0 and 86400, not {text!r}",
        )
    return value


def _relaunch(argv: Sequence[str], entry: Entry, boundary: Boundary, timeout: float) -> int:
    writing = _command(argv) == "send"
    work = Path(tempfile.mkdtemp(prefix="kanao-wmi-"))
    try:
        name = f"Local\\kanao-wmi-{uuid.uuid4().hex}"
        request = {
            "cli": str(entry.cli),
            "argv": list(argv),
            "cwd": os.getcwd(),
            "env": dict(os.environ),
            "timeout": timeout,
            "signal": name,
        }
        (work / REQUEST).write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        with boundary.signal(name) as signal:
            try:
                boundary.create(
                    [sys.executable, str(entry.launcher), CHILD_FLAG, str(work)],
                    os.getcwd(),
                )
            except LaunchFailed as error:
                raise KanaoError(
                    ErrorCode.SEND_FAILED if writing else ErrorCode.READ_FAILED,
                    f"kanao-wmi could not start the command through Win32_Process.Create: {error}",
                    disposition={"delivery": "not_delivered", "retry": "safe"} if writing else None,
                ) from error
            signalled = signal.wait(timeout + GRACE_SECONDS)
        return _deliver(work, signalled, timeout, writing)
    finally:
        try:
            shutil.rmtree(work)
        except OSError as error:
            sys.stderr.write(f"kanao-wmi: could not remove {work}: {error}\n")


def _deliver(work: Path, signalled: bool, timeout: float, writing: bool) -> int:
    code = ErrorCode.SEND_FAILED if writing else ErrorCode.READ_FAILED
    status_path = work / STATUS
    if not signalled or not status_path.is_file():
        raise KanaoError(
            code,
            f"kanao-wmi: the command started through WMI gave no answer within "
            f"{timeout + GRACE_SECONDS:.0f} seconds",
        )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if "returncode" in status:
        sys.stdout.flush()
        sys.stdout.buffer.write((work / STDOUT).read_bytes())
        sys.stdout.buffer.flush()
        sys.stderr.flush()
        sys.stderr.buffer.write((work / STDERR).read_bytes())
        sys.stderr.buffer.flush()
        return int(status["returncode"])
    if status.get("timedOut"):
        raise KanaoError(
            code,
            f"kanao-wmi: the command did not finish within {timeout:.0f} seconds and was "
            f"stopped; raise {TIMEOUT_VARIABLE} for a larger account",
        )
    raise KanaoError(code, f"kanao-wmi: the child failed: {status.get('error', status)}")


def child(work: Path, run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> None:
    """Run the CLI for the request in ``work`` and leave its result beside it."""
    request = json.loads((work / REQUEST).read_text(encoding="utf-8"))
    status: dict[str, Any]
    try:
        with open(work / STDOUT, "wb") as out, open(work / STDERR, "wb") as err:
            try:
                completed = run(
                    [sys.executable, request["cli"], *request["argv"]],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    cwd=request["cwd"],
                    env=request["env"],
                    timeout=request["timeout"],
                    check=False,
                )
                status = {"returncode": completed.returncode}
            except subprocess.TimeoutExpired:
                status = {"timedOut": True}
    except Exception as error:  # noqa: BLE001 - reported to the caller, never swallowed
        status = {"error": f"{type(error).__name__}: {error}"}
    partial = work / (STATUS + ".tmp")
    partial.write_text(json.dumps(status), encoding="utf-8")
    os.replace(partial, work / STATUS)
    set_event(request["signal"])


def _emit(payload: dict[str, Any]) -> None:
    # The same rendering as ``cli._emit``; importing the CLI would load the
    # reader, which is the slow start this wrapper exists to avoid.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
