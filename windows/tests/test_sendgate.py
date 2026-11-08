"""The account-wide send gate, across real processes and both entry points.

Each cross-process test registers its pipe reader before the child starts and
waits on the child's own line with a bounded timeout; nothing here sleeps.
"""

import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import kanao
from kanao import cli
from kanao.errors import ErrorCode, KanaoError
from kanao.sendgate import SendGate
from kanao.winauto import ChatWindow, Sender
from tests.fixtures import build_reader
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server
from tests.test_send import FakeWindows

WINDOWS = Path(kanao.__file__).resolve().parents[1]
HOLDER = """
import sys
from pathlib import Path
from kanao.sendgate import SendGate
with SendGate(Path(sys.argv[1]), clock=lambda: 1000.0).turn(0):
    print("HELD", flush=True)
    sys.stdin.readline()
print("RELEASED", flush=True)
"""


class Holder:
    """A second process holding the gate until told to let go."""

    def __init__(self, case: unittest.TestCase, directory: Path) -> None:
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(directory)],
            cwd=WINDOWS, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        case.addCleanup(self._reap)
        threading.Thread(target=self._pump, daemon=True).start()
        case.assertEqual(self.lines.get(timeout=30), "HELD")

    def _pump(self) -> None:
        assert self.child.stdout is not None
        for line in self.child.stdout:
            self.lines.put(line.strip())

    def release(self) -> str:
        assert self.child.stdin is not None
        self.child.stdin.write("go\n")
        self.child.stdin.flush()
        return self.lines.get(timeout=30)

    def kill(self) -> None:
        self.child.kill()
        self.child.wait(timeout=30)

    def _reap(self) -> None:
        if self.child.poll() is None:
            self.kill()
        for stream in (self.child.stdin, self.child.stdout):
            if stream is not None:
                stream.close()


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.now = [1000.0]
        self.waits: list[float] = []

    def gate(self) -> SendGate:
        def wait(seconds: float) -> None:
            self.waits.append(seconds)
            self.now[0] += seconds

        return SendGate(self.directory, clock=lambda: self.now[0], wait=wait)

    def test_a_write_waits_out_the_interval_since_the_last_start(self) -> None:
        with self.gate().turn(2.0):
            pass
        self.now[0] += 0.5
        with self.gate().turn(2.0):
            pass
        self.assertEqual(self.waits, [1.5])

    def test_a_record_from_a_clock_set_back_waits_at_most_one_interval(self) -> None:
        with self.gate().turn(2.0):
            pass
        self.now[0] -= 3600
        with self.gate().turn(2.0):
            pass
        self.assertEqual(self.waits, [2.0])

    def test_a_refused_write_gives_its_start_back_and_the_lock(self) -> None:
        with self.gate().turn(2.0):
            pass
        self.now[0] += 5
        with self.assertRaises(KanaoError):
            with self.gate().turn(2.0):
                raise KanaoError(ErrorCode.WINDOW_NOT_FOUND, "synthetic refusal")
        with self.gate().turn(2.0):
            pass
        self.assertEqual(self.waits, [])

    def test_an_uncertain_write_keeps_its_start(self) -> None:
        for failure in (KanaoError(ErrorCode.SEND_FAILED, "synthetic"), OSError("synthetic")):
            with self.subTest(failure=type(failure).__name__):
                self.now[0] += 5
                self.waits.clear()
                with self.assertRaises(type(failure)):
                    with self.gate().turn(2.0):
                        raise failure
                with self.gate().turn(2.0):
                    pass
                self.assertEqual(self.waits, [2.0])

    def test_the_gate_is_released_when_its_holder_dies(self) -> None:
        Holder(self, self.directory).kill()
        with self.gate().turn(0):
            pass


class CrossProcessCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.backend = FakeWindows()
        self.backend.windows = [ChatWindow(42, "Peer Renamed")]
        self.waits: list[float] = []
        gate = SendGate(self.directory, clock=lambda: 1000.5, wait=self.waits.append)
        for patcher in (
            patch("kanao.winauto.make_sender", return_value=Sender(self.backend, running=lambda: True)),
            patch("kanao.cli.send_gate", return_value=gate),
            patch("kanao.cli.Reader", side_effect=lambda **_: build_reader()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def send(self) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["send", "--room", "Peer Renamed", "synthetic"])
        return code, json.loads(output.getvalue())

    def test_a_command_is_refused_untouched_while_another_process_sends(self) -> None:
        holder = Holder(self, self.directory)
        code, answer = self.send()
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], "BUSY")
        self.assertEqual(
            answer["error"]["disposition"], {"delivery": "not_delivered", "retry": "safe"}
        )
        self.assertEqual(self.backend.actions, [])
        self.assertEqual(holder.release(), "RELEASED")
        code, _ = self.send()
        self.assertEqual(code, 0)
        # The other process started at 1000.0; this one is told it is 1000.5.
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(self.backend.actions, ["fill", "submit"])


class SharedServerGateTests(ServedCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.waits: list[float] = []
        self.sender = StubSender()

    def serve_with_gate(self) -> str:
        gate = SendGate(self.directory, clock=lambda: 1000.5, wait=self.waits.append)
        return self.serve(_start_server(self.sender, min_interval=2.0, gate=gate))

    def test_a_server_send_is_refused_while_another_process_sends(self) -> None:
        holder = Holder(self, self.directory)
        base = self.serve_with_gate()
        status, body, _ = _request(base, "POST", "/send", {"room": "Synthetic room", "text": "hi"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "BUSY")
        self.assertEqual(
            body["error"]["disposition"], {"delivery": "not_delivered", "retry": "safe"}
        )
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(holder.release(), "RELEASED")
        status, _, _ = _request(base, "POST", "/send", {"room": "Synthetic room", "text": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(len(self.sender.calls), 1)

    def test_a_dry_run_neither_waits_for_nor_holds_the_gate(self) -> None:
        holder = Holder(self, self.directory)
        base = self.serve_with_gate()
        status, body, _ = _request(
            base, "POST", "/send", {"room": "Synthetic room", "text": "hi", "dryRun": True}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["submitted"])
        holder.release()
