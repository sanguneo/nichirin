"""Hand material to a command outside this process, and take back what it says.

There is still no model in here. This starts a program the user named, gives
it material on standard input and reads what it printed, which is what keeps
"no model in this process" literally true — and what makes it plain that the
conversation goes to whichever program that is and nowhere else. Nothing is
configured by default beyond looking for ``omo`` on the PATH, and with neither
that nor the variable there is no one to ask.

The process is started and then polled rather than waited on. The interface is
a single loop and an agent answers in seconds to tens of seconds; blocking for
that long is the difference between a tool that is thinking and one that has
hung — the same mistake as decrypting between keystrokes, an order of
magnitude larger.

Its three streams are files rather than pipes or this console. A child sharing
stdin would eat the keystrokes meant for the composer, a child sharing stdout
would paint over the alternate screen, and a pipe nobody drains deadlocks a
process whose answer outgrows the buffer. A file has none of those problems
and costs one temporary directory.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import ErrorCode, KanaoError

AGENT_VARIABLE = "KANAO_AGENT"
"""A command line: ``set KANAO_AGENT=omo -p``, or a quoted path with spaces."""

DEFAULT_AGENT = ("omo", "-p")
"""What to use when the variable is unset and this is installed.

``-p`` is what makes it answer once and exit instead of opening its own
interface, which would be a second program fighting for this console.
"""

TIMEOUT_SECONDS = 180.0
"""How long an unanswered question may stand before it is given up on.

Long enough for a slow model on a long conversation, short enough that a
command which never exits — an agent left interactive, say — is reported
rather than left running behind the screen.
"""

_REASON_WIDTH = 160
"""How much of a failed command's last line to quote back.

Its *last line*, not its last bytes: an agent that fails prints a stack of its
own diagnostics first, and cutting that by length lands in the middle of one
of them.
"""


def split_command(configured: str) -> list[str]:
    """Split a configured command line the way this platform spells paths.

    ``shlex`` in its POSIX mode reads a backslash as an escape, so
    ``C:\\tools\\omo.exe`` comes back as ``C:toolsomo.exe`` — a path that
    does not exist, reported as though the program were missing. Windows
    paths are the ordinary case here, so the split keeps backslashes and the
    quotes that survive it are stripped instead.
    """
    parts = shlex.split(configured, posix=False)
    unquoted: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
            part = part[1:-1]
        if part:
            unquoted.append(part)
    return unquoted


def _decode(raw: bytes) -> str:
    """Read what a command printed: UTF-8 first, this machine's code page after.

    A child writing to a file on Windows gets the locale encoding unless it
    says otherwise, so an agent that prints Hangul can hand back cp949 bytes.
    Decoding those as UTF-8 does not raise anything the caller could act on —
    it quietly yields mojibake, and the draft would carry it into the client.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # ``mbcs`` is the ANSI code page itself. `locale.getpreferredencoding`
        # is not: under UTF-8 mode it answers "utf-8" whatever the machine is
        # set to, which turns this fallback back into the decoding that just
        # failed.
        return raw.decode("mbcs", errors="replace")


def command() -> Optional[list[str]]:
    """The command to ask, or ``None`` when this machine has nobody to ask."""
    configured = os.environ.get(AGENT_VARIABLE, "").strip()
    if configured:
        try:
            parts = split_command(configured)
        except ValueError as error:
            raise KanaoError(
                ErrorCode.INVALID_REQUEST,
                f"{AGENT_VARIABLE} 를 명령으로 읽을 수 없습니다: {error}",
            ) from error
        return parts or None
    found = shutil.which(DEFAULT_AGENT[0])
    return [found, *DEFAULT_AGENT[1:]] if found else None


@dataclass
class Ask:
    """One question handed over, still being answered."""

    process: subprocess.Popen
    started_at: float
    label: str
    """The program being asked, so the screen can say where the words went."""
    _directory: Path

    def finished(self) -> bool:
        return self.process.poll() is not None

    def cancel(self) -> None:
        """Give up on the answer and take the process down with it."""
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self._discard()

    def answer(self) -> str:
        """Return what the command printed, or explain why there is nothing.

        Called once: the temporary directory goes with the answer either way,
        so a question that was asked leaves nothing behind.
        """
        code = self.process.poll()
        printed = self._read("answer")
        complained = self._read("diagnostics")
        self._discard()
        if code != 0:
            spoken = [
                line.strip()
                for line in (complained or printed).splitlines()
                if line.strip()
            ]
            reason = spoken[-1][:_REASON_WIDTH] if spoken else ""
            raise KanaoError(
                ErrorCode.INVALID_REQUEST,
                f"{self.label} 이(가) {code} 로 끝났습니다"
                + (f": {reason}" if reason else ""),
            )
        return printed

    def _read(self, name: str) -> str:
        try:
            return _decode((self._directory / name).read_bytes()).strip()
        except OSError:
            return ""

    def _discard(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


def start(prompt: str, now: float) -> Ask:
    """Hand *prompt* to the configured command and return without waiting."""
    parts = command()
    if parts is None:
        raise KanaoError(
            ErrorCode.INVALID_REQUEST,
            f"물어볼 상대가 없습니다. {AGENT_VARIABLE} 에 명령을 지정하세요 "
            f'(예: set {AGENT_VARIABLE}=omo -p)',
        )
    directory = Path(tempfile.mkdtemp(prefix="kanao-ask-"))
    question = directory / "question"
    question.write_text(prompt, encoding="utf-8")
    try:
        with (
            question.open("rb") as stdin,
            (directory / "answer").open("wb") as stdout,
            (directory / "diagnostics").open("wb") as stderr,
        ):
            process = subprocess.Popen(
                parts,
                stdin=stdin,
                stdout=stdout,
                # Kept apart, not merged. An agent narrates on stderr while it
                # works — run live, omo printed two lines about skipped
                # continuations — and merging that into stdout puts it at the
                # top of the draft, which is a message about to be sent.
                stderr=stderr,
                # Run where it can invent nothing about this checkout: the
                # material it needs is on its standard input.
                cwd=str(directory),
            )
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise KanaoError(
            ErrorCode.INVALID_REQUEST, f"{parts[0]} 를 실행할 수 없습니다: {error}"
        ) from error
    return Ask(
        process=process,
        started_at=now,
        label=Path(parts[0]).name,
        _directory=directory,
    )
