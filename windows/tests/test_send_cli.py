"""Send CLI integration through synthetic databases and a fake window backend."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from kanao import cli
from kanao.errors import ErrorCode, KanaoError
from kanao.sendgate import SendGate
from kanao.winauto import ChatWindow, Sender
from tests.fixtures import DIRECT_ROOM, build_reader
from tests.test_send import FakeWindows
from tests.test_upload import FakeFileWindows


def isolate_gate(case: unittest.TestCase) -> list[float]:
    """Point the command's send gate at a temporary directory; return its waits.

    A test must never take the account's real lock or book its pacing.
    """
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    waits: list[float] = []
    gate = SendGate(Path(directory.name), clock=lambda: 1000.0, wait=waits.append)
    patcher = patch("kanao.cli.send_gate", return_value=gate)
    patcher.start()
    case.addCleanup(patcher.stop)
    return waits


class TypingWindows(FakeWindows):
    """A fake window that also keeps every text it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.typed: list[str] = []

    def set_text(self, control: int, text: str) -> None:
        self.typed.append(text)
        super().set_text(control, text)


class SendCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.backend = TypingWindows()
        self.backend.windows = [ChatWindow(42, "Peer Renamed")]
        self.sender = Sender(self.backend, running=lambda: True)
        self.factory = patch("kanao.winauto.make_sender", return_value=self.sender)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.waits = isolate_gate(self)

    def main(self, argv: list[str], stdin: bytes = b"") -> tuple[int, dict]:
        output = io.StringIO()
        piped = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        # main closes the reader it opened, so each invocation gets its own.
        with patch("kanao.cli.Reader", side_effect=lambda **_: build_reader()), \
                patch("sys.stdin", piped), \
                redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_text_from_standard_input_arrives_exactly_as_written(self) -> None:
        body = 'say "hi" to $env:USER\n둘째 줄 ✓\r\nlast line'
        code, answer = self.main(
            ["send", "--room", "Peer Renamed", "--text-file", "-"],
            stdin=b"\xef\xbb\xbf" + body.encode("utf-8"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.backend.typed, [body])
        self.assertEqual(answer["length"], len(body))
        self.assertEqual(answer["disposition"], {"delivery": "submitted", "retry": "unsafe"})

    def test_text_from_a_file_path_is_read_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "message.txt"
            source.write_bytes("합성 메시지\n".encode("utf-8"))
            code, _ = self.main(["send", "--room", "Peer Renamed", "--text-file", str(source)])
        self.assertEqual(code, 0)
        self.assertEqual(self.backend.typed, ["합성 메시지\n"])

    def test_exactly_one_text_source_is_accepted(self) -> None:
        argv = ["send", "--room", "Peer Renamed", "--text-file", "-", "synthetic"]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_an_unreadable_message_is_refused_before_anything_is_typed(self) -> None:
        for argv, stdin in (
            (["send", "--room", "Peer Renamed", "--text-file", "-"], b"PRIVATE\xff\xfe"),
            (["send", "--room", "Peer Renamed", "--text-file", "no-such-synthetic.txt"], b""),
        ):
            with self.subTest(argv=argv):
                code, answer = self.main(argv, stdin=stdin)
                self.assertEqual(code, 1)
                self.assertEqual(answer["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(
                    answer["error"]["disposition"], {"delivery": "not_delivered", "retry": "safe"}
                )
                self.assertNotIn("PRIVATE", json.dumps(answer))
        self.assertEqual(self.backend.actions, [])

    def test_a_send_books_the_shared_pacing_record(self) -> None:
        self.main(["send", "--room", "Peer Renamed", "synthetic"])
        self.main(["send", "--room", "Peer Renamed", "synthetic"])
        # The fixed clock never moves, so the second send waits the whole interval.
        self.assertEqual(self.waits, [2.0])
        self.assertEqual(self.backend.actions, ["fill", "submit", "fill", "submit"])

    def test_operation_id_replays_cli_output_without_second_input(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "--operation-id", "cli-synthetic", "synthetic"]
        )
        room = self.reader.room(DIRECT_ROOM)
        with tempfile.TemporaryDirectory() as root, \
                patch("kakaocommon.operations.directory", return_value=Path(root)), \
                patch.object(self.reader, "rooms", return_value=[room]):
            outputs = []
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.run(arguments, self.reader), 0)
                outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_operation_id_cannot_change_room_selector_from_id_to_title(self) -> None:
        by_id = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "--operation-id", "selector-id", "synthetic"]
        )
        by_title = cli.build_parser().parse_args(
            ["send", "--room", str(DIRECT_ROOM), "--operation-id", "selector-id", "synthetic"]
        )
        with tempfile.TemporaryDirectory() as root, \
                patch("kakaocommon.operations.directory", return_value=Path(root)), \
                redirect_stdout(io.StringIO()):
            cli.run(by_id, self.reader)
            with self.assertRaises(KanaoError) as caught:
                cli.run(by_title, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_dry_run_by_id_has_standard_envelope_and_string_id(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "--dry-run", "synthetic"]
        )
        output = io.StringIO()
        with redirect_stdout(output), patch(
            "kakaocommon.operations.directory", side_effect=AssertionError("no-ID send accessed receipts")
        ):
            result = cli.run(arguments, self.reader)
        body = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(body["ok"])
        self.assertTrue(body["dryRun"])
        self.assertEqual(body["roomId"], str(DIRECT_ROOM))
        self.assertFalse(body["submitted"])
        self.assertEqual(self.backend.actions, [])

    def test_unique_catalog_substring_resolves_to_exact_window(self) -> None:
        room = self.reader.room(DIRECT_ROOM)
        assert room is not None
        arguments = cli.build_parser().parse_args(["send", "--room", "Renamed", "synthetic"])
        with patch.object(self.reader, "rooms", return_value=[room]), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run(arguments, self.reader), 0)
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_explicit_target_and_message_are_required(self) -> None:
        for argv in (["send", "synthetic"], ["send", "--room", "Peer Renamed"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.build_parser().parse_args(argv)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.backend.actions, [])

    def test_duplicate_catalog_titles_refused_even_with_one_window(self) -> None:
        room = self.reader.room(DIRECT_ROOM)
        self.assertIsNotNone(room)
        assert room is not None
        duplicates = [room, replace(room, id=999999)]
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "synthetic"]
        )
        with patch.object(self.reader, "rooms", return_value=duplicates):
            with self.assertRaises(KanaoError) as caught:
                cli.run(arguments, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertEqual(len(caught.exception.details["candidates"]), 2)
        self.assertEqual(self.backend.actions, [])

    def test_ambiguous_substrings_have_candidates(self) -> None:
        room = self.reader.room(DIRECT_ROOM)
        assert room is not None
        rooms = [
            replace(room, title="Synthetic room A"),
            replace(room, id=999999, title="Synthetic room B"),
        ]
        arguments = cli.build_parser().parse_args(["send", "--room", "room", "synthetic"])
        with patch.object(self.reader, "rooms", return_value=rooms):
            with self.assertRaises(KanaoError) as caught:
                cli.run(arguments, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertGreater(len(caught.exception.details["candidates"]), 1)
        self.assertEqual(self.backend.actions, [])

    def test_native_failure_uses_send_error_not_read_error(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["send", "--room", "Peer Renamed", "synthetic"]
        )
        with patch.object(self.backend, "get_text", side_effect=OSError("timeout")):
            with self.assertRaises(KanaoError) as caught:
                cli.run(arguments, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)

    def test_main_emits_handled_error_json(self) -> None:
        output = io.StringIO()
        # A window whose message box cannot be found: a handled failure that
        # the envelope has to carry, now that a draft is simply overwritten.
        self.backend.control = 0
        with patch("kanao.cli.Reader", return_value=self.reader), redirect_stdout(output):
            result = cli.main(["send", "--room", "Peer Renamed", "synthetic"])
        body = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INPUT_NOT_FOUND")

    def test_a_draft_in_the_way_is_cleared_and_reported(self) -> None:
        output = io.StringIO()
        self.backend.draft = "unfinished synthetic draft"
        with patch("kanao.cli.Reader", return_value=self.reader), redirect_stdout(output):
            result = cli.main(["send", "--room", "Peer Renamed", "synthetic"])
        body = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(body["ok"])
        self.assertIs(body["clearedDraft"], True)
        # The unfinished draft is gone, and so is the message that replaced
        # it: the client emptying the box is what licensed reporting the send.
        self.assertEqual(self.backend.draft, "")


class SendFileCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.backend = FakeFileWindows()
        self.backend.windows = [ChatWindow(42, "Peer Renamed")]
        self.sender = Sender(self.backend, running=lambda: True, settle=lambda: None)
        self.factory = patch("kanao.winauto.make_sender", return_value=self.sender)
        self.made = self.factory.start()
        self.addCleanup(self.factory.stop)
        isolate_gate(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def main(self, argv: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with patch("kanao.cli.Reader", return_value=self.reader), redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_a_file_goes_through_the_room_window_with_a_native_path(self) -> None:
        source = self.folder / "synthetic.txt"
        source.write_bytes(b"synthetic bytes")
        code, answer = self.main(
            ["sendfile", "--room", "Peer Renamed", source.as_posix()]
        )
        native = os.path.normpath(str(source))
        self.assertEqual(code, 0)
        self.assertEqual(self.backend.chosen, native)
        self.assertEqual(answer["path"], native)
        self.assertEqual(answer["roomId"], str(DIRECT_ROOM))
        self.assertEqual(answer["size"], len(b"synthetic bytes"))
        self.assertEqual(answer["disposition"], {"delivery": "submitted", "retry": "unsafe"})

    def test_a_missing_or_empty_file_is_refused_before_the_client_is_asked(self) -> None:
        empty = self.folder / "empty.txt"
        empty.touch()
        for path in (str(self.folder / "absent.txt"), str(empty)):
            with self.subTest(path=path):
                code, answer = self.main(["sendfile", "--room", "Peer Renamed", path])
                self.assertEqual(code, 1)
                self.assertEqual(answer["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(
                    answer["error"]["disposition"], {"delivery": "not_delivered", "retry": "safe"}
                )
        self.made.assert_not_called()
        self.assertEqual(self.backend.actions, [])

    def test_an_uncertain_upload_is_reported_as_unknown(self) -> None:
        source = self.folder / "synthetic.txt"
        source.write_bytes(b"synthetic bytes")
        self.backend.popup_closes = False
        code, answer = self.main(["sendfile", "--room", "Peer Renamed", str(source)])
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], "SEND_FAILED")
        self.assertEqual(
            answer["error"]["disposition"], {"delivery": "unknown", "retry": "after_check"}
        )
        self.assertEqual(self.backend.actions.count("send"), 1)
