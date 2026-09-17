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

Its three streams are files rather than pipes or this terminal. A child
sharing stdin would eat the keystrokes meant for the composer, a child sharing
stdout would paint over the alternate screen, and a pipe nobody drains
deadlocks a process whose answer outgrows the buffer. A file has none of those
problems and costs one temporary directory.
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

from .errors import ErrorCode, KamadoError

AGENT_VARIABLE = "KAMADO_AGENT"
"""A command line, split the way a shell would: ``KAMADO_AGENT='omo -p'``."""

DEFAULT_AGENT = ("omo", "-p")
"""What to use when the variable is unset and this is installed.

``-p`` is what makes it answer once and exit instead of opening its own
interface, which would be a second program fighting for this terminal.
"""

TIMEOUT_SECONDS = 180.0
"""How long an unanswered question may stand before it is given up on.

Long enough for a slow model on a long conversation, short enough that a
command which never exits — an agent left interactive, say — is reported
rather than left running behind the screen.
"""

_REASON_WIDTH = 160
"""How much of a failed command's last word to quote back.

Its *last line*, not its last bytes: an agent that fails prints a stack of its
own diagnostics first, and cutting that by length lands in the middle of one
of them. Run against a real agent with no credentials, the tail quoted half a
JSON object while the line above it said plainly that nobody was logged in.
"""


def command() -> Optional[list[str]]:
    """The command to ask, or ``None`` when this machine has nobody to ask."""
    configured = os.environ.get(AGENT_VARIABLE, "").strip()
    if configured:
        try:
            parts = shlex.split(configured)
        except ValueError as error:
            raise KamadoError(
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
        printed = ""
        output = self._directory / "answer"
        try:
            printed = output.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            printed = ""
        self._discard()
        if code != 0:
            spoken = [line.strip() for line in printed.splitlines() if line.strip()]
            reason = spoken[-1][:_REASON_WIDTH] if spoken else ""
            raise KamadoError(
                ErrorCode.INVALID_REQUEST,
                f"{self.label} 이(가) {code} 로 끝났습니다"
                + (f": {reason}" if reason else ""),
            )
        return printed

    def _discard(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


def start(prompt: str, now: float) -> Ask:
    """Hand *prompt* to the configured command and return without waiting."""
    parts = command()
    if parts is None:
        raise KamadoError(
            ErrorCode.INVALID_REQUEST,
            f"물어볼 상대가 없습니다. {AGENT_VARIABLE} 에 명령을 지정하세요 "
            f"(예: {AGENT_VARIABLE}='omo -p')",
        )
    directory = Path(tempfile.mkdtemp(prefix="kamado-ask-"))
    question = directory / "question"
    question.write_text(prompt, encoding="utf-8")
    try:
        with question.open("rb") as stdin, (directory / "answer").open("wb") as stdout:
            process = subprocess.Popen(
                parts,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                # Run where it can invent nothing about this checkout: the
                # material it needs is on its standard input.
                cwd=str(directory),
            )
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise KamadoError(
            ErrorCode.INVALID_REQUEST, f"{parts[0]} 를 실행할 수 없습니다: {error}"
        ) from error
    return Ask(
        process=process,
        started_at=now,
        label=Path(parts[0]).name,
        _directory=directory,
    )
