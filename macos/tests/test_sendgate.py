"""Synthetic gate ownership, pacing, and HTTP/CLI integration across processes."""

from __future__ import annotations

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

from kamado.errors import ErrorCode, KamadoError
from kamado.macauto import ChatWindow, Sender
from kamado.sendgate import LOCK_NAME, RECORD_NAME, SendGate
from kamado.serve.upload import UploadStage

from kamado import cli
from tests.fixtures import build_reader
from tests.platform import require_posix_console
from tests.serve.helpers import (
    TOKEN,
    ServedCase,
    StubSender,
    _raw_request,
    _request,
    _start_server,
)
from tests.serve.test_upload import BOUNDARY, StubUploader, multipart
from tests.test_send import FakeWindows

require_posix_console()

HOLDER = """
import sys
from pathlib import Path
from kamado.sendgate import SendGate
with SendGate(Path(sys.argv[1]), clock=lambda: 1000.0).turn(0):
    print("HELD", flush=True)
    sys.stdin.readline()
print("RELEASED", flush=True)
"""


class Holder:
    """A child owns the real flock until its pipe is signalled or it is killed."""

    def __init__(self, case: unittest.TestCase, directory: Path) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(directory)],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        case.addCleanup(self.close)
        threading.Thread(target=self.pump, daemon=True).start()
        case.assertEqual(self.lines.get(timeout=30), "HELD")

    def pump(self) -> None:
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

    def close(self) -> None:
        if self.child.poll() is None:
            self.kill()
        for stream in (self.child.stdin, self.child.stdout):
            if stream is not None:
                stream.close()


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name) / "state"
        self.now = 1000.0
        self.waits: list[float] = []
        self.gate = SendGate(self.directory, clock=lambda: self.now, wait=self.wait)

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def test_starts_share_pacing_and_clock_rollback_is_bounded(self) -> None:
        with self.gate.turn(2):
            pass
        self.now += 0.5
        with self.gate.turn(2):
            pass
        self.now -= 3600
        with self.gate.turn(2):
            pass
        self.assertEqual(self.waits, [1.5, 2])

    def test_only_definitely_unsent_errors_restore_the_previous_start(self) -> None:
        for failure, waits in (
            (KamadoError(ErrorCode.WINDOW_NOT_FOUND, "refused"), []),
            (KamadoError(ErrorCode.SEND_FAILED, "unknown"), [2]),
            (OSError("unknown"), [2]),
        ):
            with self.subTest(failure=failure):
                self.now += 10
                self.waits.clear()
                with self.assertRaises(type(failure)), self.gate.turn(2):
                    raise failure
                with self.gate.turn(2):
                    pass
                self.assertEqual(self.waits, waits)

    def test_refusal_restores_an_existing_record(self) -> None:
        with self.gate.turn(2):
            pass
        self.now += 5
        with self.assertRaises(KamadoError), self.gate.turn(2):
            raise KamadoError(ErrorCode.WINDOW_NOT_FOUND, "refused")
        self.assertEqual(json.loads(self.gate.record_path.read_text())["startedAt"], 1000)

    def test_process_contention_refuses_then_release_or_kill_frees_the_gate(self) -> None:
        for killed in (False, True):
            with self.subTest(killed=killed):
                holder = Holder(self, self.directory)
                with self.assertRaises(KamadoError) as caught, self.gate.turn(0):
                    self.fail("contending input ran")
                self.assertEqual(caught.exception.code, ErrorCode.BUSY)
                self.assertEqual(caught.exception.disposition,
                                 {"delivery": "not_delivered", "retry": "safe"})
                if killed:
                    holder.kill()
                else:
                    self.assertEqual(holder.release(), "RELEASED")
                with self.gate.turn(0):
                    pass

    def test_directory_lock_and_record_are_private_even_when_preexisting(self) -> None:
        self.directory.mkdir(mode=0o755)
        for name in (LOCK_NAME, RECORD_NAME):
            (self.directory / name).write_text("{}")
            (self.directory / name).chmod(0o644)
        with self.gate.turn(0):
            pass
        for path, mode in ((self.directory, 0o700),
                           (self.directory / LOCK_NAME, 0o600),
                           (self.gate.record_path, 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_symlinks_are_refused_without_touching_their_target(self) -> None:
        target = self.directory.parent / "target"
        target.write_text("untouched")
        self.directory.mkdir()
        for name in (LOCK_NAME, RECORD_NAME):
            with self.subTest(name=name):
                path = self.directory / name
                path.symlink_to(target)
                with self.assertRaises(KamadoError) as caught, self.gate.turn(0):
                    self.fail("unsafe input ran")
                self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)
                self.assertEqual(target.read_text(), "untouched")
                path.unlink()

    def test_failed_record_publication_refuses_before_input(self) -> None:
        with patch("kamado.sendgate.os.replace", side_effect=PermissionError("synthetic")), \
                self.assertRaises(KamadoError) as caught, self.gate.turn(0):
            self.fail("unbooked input ran")
        self.assertEqual(caught.exception.disposition,
                         {"delivery": "not_delivered", "retry": "safe"})
        with self.gate.turn(0):
            pass

    def test_damaged_records_charge_one_interval(self) -> None:
        self.directory.mkdir()
        for content in ("broken", "[]", '{"startedAt": true}', '{"startedAt": NaN}'):
            with self.subTest(content=content):
                self.gate.record_path.write_text(content)
                self.waits.clear()
                with self.gate.turn(2):
                    pass
                self.assertEqual(self.waits, [2])

    def test_cli_is_untouched_on_contention_and_paces_after_release(self) -> None:
        holder = Holder(self, self.directory)
        backend = FakeWindows()
        backend.windows = [ChatWindow(42, "Peer Renamed")]
        reader = build_reader()
        self.addCleanup(reader.close)
        with patch("kamado.macauto.send_gate", return_value=self.gate), \
             patch("kamado.macauto.make_sender", return_value=Sender(backend, running=lambda: True)), \
             patch("kamado.cli.Reader", return_value=reader):
            for expected in (1, 0):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli.main(["send", "--room", "Peer Renamed", "synthetic"])
                self.assertEqual(code, expected)
                if expected:
                    self.assertEqual(json.loads(output.getvalue())["error"]["code"], "BUSY")
                    self.assertEqual(backend.actions, [])
                    holder.release()
                    self.now += 0.5
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(backend.actions, ["fill", "submit"])

    def test_daemon_dispatch_uses_the_same_gate_as_local_sends(self) -> None:
        from kamado.daemon import DaemonServer

        holder = Holder(self, self.directory)
        backend = FakeWindows()
        backend.windows = [ChatWindow(42, "Peer Renamed")]
        server = DaemonServer()
        server._reader = build_reader()
        self.addCleanup(server._reader.close)
        with patch("kamado.macauto.send_gate", return_value=self.gate), \
                patch("kamado.macauto.make_sender",
                      return_value=Sender(backend, running=lambda: True)):
            with self.assertRaises(KamadoError) as caught:
                server._dispatch("send", {"room": "Peer Renamed", "text": "synthetic"})
            self.assertEqual(caught.exception.code, ErrorCode.BUSY)
            self.assertEqual(caught.exception.disposition,
                             {"delivery": "not_delivered", "retry": "safe"})
            self.assertEqual(backend.actions, [])
            holder.release()
            answer = server._dispatch("send", {"room": "Peer Renamed", "text": "synthetic"})
        self.assertTrue(answer["submitted"])
        self.assertEqual(backend.actions, ["fill", "submit"])

    def test_sendfile_cannot_touch_the_client_while_another_process_sends(self) -> None:
        holder = Holder(self, self.directory)
        path = self.directory / "synthetic.txt"
        path.write_bytes(b"synthetic")
        reader = build_reader()
        self.addCleanup(reader.close)
        output = io.StringIO()
        with patch("kamado.macauto.send_gate", return_value=self.gate), \
                patch("kamado.cli.Reader", return_value=reader), \
                patch("kamado.macupload.make_uploader") as made, redirect_stdout(output):
            code = cli.main(["sendfile", "--room", "Peer Renamed", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "BUSY")
        made.assert_not_called()
        holder.release()


class HTTPGateTests(ServedCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.waits: list[float] = []
        self.gate = SendGate(self.directory, clock=lambda: 1000.5, wait=self.waits.append)
        self.sender = StubSender()
        self.uploader = StubUploader()
        self.serve(_start_server(
            self.sender, gate=self.gate, min_interval=2,
            upload=self.uploader, upload_stage=UploadStage(self.directory / "uploads"),
        ))

    def test_send_and_upload_refuse_other_process_then_send_paces(self) -> None:
        holder = Holder(self, self.directory)
        body = multipart([("room", b"Synthetic", None), ("file", b"data", "a.txt")])
        answers = [
            _request(self.base, "POST", "/send", {"room": "Synthetic", "text": "hi"})[:2],
            _raw_request(self.base, "POST", "/upload", body, {
                "Authorization": f"Bearer {TOKEN}", "Content-Length": str(len(body)),
                "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
            }),
        ]
        for status, answer in answers:
            self.assertEqual(status, 503)
            self.assertEqual(answer["error"]["code"], "BUSY")
            self.assertEqual(answer["error"]["disposition"],
                             {"delivery": "not_delivered", "retry": "safe"})
        self.assertEqual((self.sender.calls, self.uploader.calls), ([], []))
        holder.release()
        status, _, _ = _request(self.base, "POST", "/send", {"room": "Synthetic", "text": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(len(self.sender.calls), 1)

    def test_dry_run_bypasses_another_process_gate(self) -> None:
        holder = Holder(self, self.directory)
        status, answer, _ = _request(self.base, "POST", "/send",
                                    {"room": "Synthetic", "text": "hi", "dryRun": True})
        self.assertEqual(status, 200)
        self.assertFalse(answer["submitted"])
        self.assertEqual(self.waits, [])
        holder.release()

    def test_gate_is_held_until_backend_execution_finishes(self) -> None:
        answers: queue.Queue[int] = queue.Queue()
        thread = threading.Thread(target=lambda: answers.put(_request(
            self.base, "POST", "/send", {"room": "Block me", "text": "hi"},
        )[0]))
        thread.start()
        try:
            self.assertTrue(self.sender.entered.wait(timeout=5))
            with self.assertRaises(KamadoError) as caught, self.gate.turn(0):
                self.fail("gate released before execution finished")
            self.assertEqual(caught.exception.code, ErrorCode.BUSY)
        finally:
            self.sender.release.set()
            thread.join(timeout=5)
        self.assertEqual(answers.get(timeout=5), 200)
