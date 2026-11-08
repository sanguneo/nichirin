"""Send CLI integration through synthetic databases and a fake window backend."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from kamado import cli
from kamado.errors import ErrorCode, KamadoError
from kamado.macauto import ChatWindow, Sender
from kamado.macupload import Uploader
from kamado.sendgate import SendGate
from tests.fixtures import DIRECT_ROOM, build_reader
from tests.platform import require_posix_console
from tests.test_send import FakeWindows
from tests.test_upload import ScriptedBackend


def isolate_gate(case: unittest.TestCase) -> list[float]:
    """Use synthetic pacing and private state, never the account's real gate."""
    require_posix_console()
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    waits: list[float] = []
    gate = SendGate(Path(directory.name), clock=lambda: 1000.0, wait=waits.append)
    patcher = patch("kamado.macauto.send_gate", return_value=gate)
    patcher.start()
    case.addCleanup(patcher.stop)
    return waits


class SendCommandTests(unittest.TestCase):
    def test_default_send_preserves_the_existing_json_bytes_without_log_reads(self):
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "synthetic"]
        )
        output = io.StringIO()
        with patch.object(self.reader, "newest_log_id", side_effect=AssertionError("unexpected read")), \
             redirect_stdout(output):
            cli.run(arguments, self.reader)
        expected = {
            "ok": True, "submitted": True, "dryRun": False, "room": "Peer Renamed",
            "length": 9, "clearedDraft": False, "roomId": str(DIRECT_ROOM),
            "disposition": {"delivery": "submitted", "retry": "unsafe"},
        }
        self.assertEqual(output.getvalue(), json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def test_confirmation_captures_after_window_discovery_and_never_reuses_old_text(self):
        rows = [SimpleNamespace(id=1, text="synthetic", from_me=True)]
        original_input = self.backend.input_handle
        original_submit = self.backend.submit

        def discover(window):
            rows.append(SimpleNamespace(id=2, text="synthetic", from_me=True))
            return original_input(window)

        def submit(control):
            original_submit(control)
            rows.append(SimpleNamespace(id=3, text="synthetic", from_me=True))

        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "synthetic", "--confirm"]
        )
        output = io.StringIO()
        with patch.object(self.backend, "input_handle", side_effect=discover), \
             patch.object(self.backend, "submit", side_effect=submit), \
             patch.object(self.reader, "newest_log_id", side_effect=lambda _: max(row.id for row in rows)), \
             patch.object(self.reader, "messages", side_effect=lambda _, **kw: [
                 row for row in rows if row.id > kw["after_id"]
             ]), redirect_stdout(output):
            self.assertEqual(cli.run(arguments, self.reader), 0)
        body = json.loads(output.getvalue())
        self.assertEqual((body["logged"], body["messageId"]), (True, "3"))
        self.assertEqual(body["disposition"], {"delivery": "submitted", "retry": "unsafe"})
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_confirmation_read_failure_preserves_submission_without_resending(self):
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "synthetic", "--confirm=1"]
        )
        output = io.StringIO()
        with patch.object(self.reader, "newest_log_id", side_effect=OSError("synthetic")), \
             redirect_stdout(output):
            self.assertEqual(cli.run(arguments, self.reader), 0)
        body = json.loads(output.getvalue())
        self.assertIs(body["logged"], False)
        self.assertNotIn("messageId", body)
        self.assertTrue(body["submitted"])
        self.assertEqual(body["disposition"], {"delivery": "submitted", "retry": "unsafe"})
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_default_and_dry_confirmation_leave_json_bytes_unchanged(self):
        outputs = []
        for flags in ([], ["--confirm"]):
            arguments = cli.build_parser().parse_args(
                ["send", "--room-id", str(DIRECT_ROOM), "--dry-run", "synthetic", *flags]
            )
            output = io.StringIO()
            with patch.object(self.reader, "newest_log_id", side_effect=AssertionError("unexpected read")), \
                 redirect_stdout(output):
                cli.run(arguments, self.reader)
            outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertNotIn("logged", json.loads(outputs[0]))

    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.backend = FakeWindows()
        self.backend.windows = [ChatWindow(42, "Peer Renamed")]
        self.sender = Sender(self.backend, running=lambda: True)
        self.factory = patch("kamado.macauto.make_sender", return_value=self.sender)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.waits = isolate_gate(self)

    def main(self, argv: list[str], stdin: bytes = b"") -> tuple[int, dict]:
        output = io.StringIO()
        with io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8") as piped, \
                patch("sys.stdin", piped), \
                patch("kamado.cli.Reader", side_effect=lambda **_: build_reader()), \
                patch("kamado.daemon.is_daemon_running", return_value=False), \
                redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_stdin_preserves_utf8_quotes_and_line_endings_without_bom(self) -> None:
        text = 'say "hi" to $USER\n둘째 줄\r\nlast'
        for ending, kept in (
            ("", ""), ("\n", ""), ("\r\n", ""), ("\n\n", "\n"),
            ("\r\n\r\n", "\r\n"), ("\r", "\r"), (" \t", " \t"),
            (" \t\n", " \t"), ("\n \t", "\n \t"), ("\r\r\n", "\r"),
        ):
            with self.subTest(ending=ending):
                code, answer = self.main(
                    ["send", "--room", "Peer Renamed", "--text-file", "-"],
                    b"\xef\xbb\xbf" + (text + ending).encode("utf-8"),
                )
                self.assertEqual(code, 0, answer)
                self.assertEqual(self.backend.draft, text + kept)
                self.assertEqual(answer["length"], len(text + kept))

    def test_empty_stdin_after_removing_one_line_ending_is_refused(self) -> None:
        for data in (b"", b"\n", b"\r\n", b"\xef\xbb\xbf\n"):
            with self.subTest(data=data):
                code, answer = self.main(
                    ["send", "--room", "Peer Renamed", "--text-file", "-"], data
                )
                self.assertEqual(code, 1, answer)
                self.assertEqual(answer["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(answer["error"]["disposition"],
                                 {"delivery": "not_delivered", "retry": "safe"})
        self.assertEqual(self.backend.actions, [])

    def test_positional_text_preserves_trailing_whitespace(self) -> None:
        text = "synthetic \t\r\n\n"
        code, answer = self.main(["send", "--room", "Peer Renamed", text])
        self.assertEqual(code, 0, answer)
        self.assertEqual(self.backend.draft, text)
        self.assertEqual(answer["length"], len(text))

    def test_file_text_is_read_as_utf8_without_newline_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.txt"
            path.write_bytes("합성\r\n본문\n".encode("utf-8-sig"))
            code, answer = self.main(
                ["send", "--room-id", str(DIRECT_ROOM), "--text-file", str(path)]
            )
        self.assertEqual(code, 0, answer)
        self.assertEqual(self.backend.draft, "합성\r\n본문\n")

    def test_exactly_one_text_source_is_accepted(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(
                ["send", "--room", "Peer Renamed", "--text-file", "-", "extra"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_unreadable_or_invalid_utf8_text_is_safe_and_private(self) -> None:
        for path, data in (("-", b"PRIVATE\xff"), ("no-such-synthetic.txt", b"")):
            with self.subTest(path=path):
                code, answer = self.main(
                    ["send", "--room", "Peer Renamed", "--text-file", path], data
                )
                self.assertEqual(code, 1)
                self.assertEqual(answer["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(
                    answer["error"]["disposition"],
                    {"delivery": "not_delivered", "retry": "safe"},
                )
                self.assertNotIn("PRIVATE", json.dumps(answer))
        self.assertEqual(self.backend.actions, [])

    def test_live_commands_book_the_same_pacing_record(self) -> None:
        for _ in range(2):
            self.assertEqual(self.main(["send", "--room", "Peer Renamed", "synthetic"])[0], 0)
        self.assertEqual(self.waits, [2.0])
        self.assertEqual(self.backend.actions, ["fill", "submit", "fill", "submit"])

    def test_stdin_confirmation_receipt_replays_and_rejects_changed_body(self) -> None:
        argv = ["send", "--room", "Peer Renamed", "--text-file", "-",
                "--operation-id", "stdin-confirmed", "--confirm"]
        rows = []
        original_submit = self.backend.submit

        def submit(control):
            original_submit(control)
            rows.append(SimpleNamespace(id=9, text="synthetic", from_me=True))

        with tempfile.TemporaryDirectory() as root, \
                patch("kakaocommon.operations.directory", return_value=Path(root)), \
                patch.object(self.backend, "submit", side_effect=submit), \
                patch("kamado.reader.Reader.newest_log_id", return_value=8), \
                patch("kamado.reader.Reader.messages", side_effect=lambda _, **kw: [
                    row for row in rows if row.id > kw["after_id"]
                ]):
            first = self.main(argv, b"synthetic\n")
            replay = self.main(argv, b"synthetic\r\n")
            without_ending = self.main(argv, b"synthetic")
            changed = self.main(argv, b"different\n")
        self.assertEqual(first, replay)
        self.assertEqual(first, without_ending)
        self.assertEqual(first[0], 0, first[1])
        self.assertEqual((first[1]["logged"], first[1]["messageId"]), (True, "9"))
        self.assertEqual(changed[1]["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_stdin_is_consumed_once_before_daemon_dispatch_with_trace(self) -> None:
        from kamado.daemon import DaemonServer

        server = DaemonServer()
        server._reader = self.reader
        text = 'PRIVATE "$USER"\r\n둘째 줄'
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, \
                io.TextIOWrapper(io.BytesIO((text + "\n\n").encode("utf-8-sig")), encoding="utf-8") as piped, \
                patch("sys.stdin", piped), \
                patch("kamado.daemon.is_daemon_running", return_value=True), \
                patch("kamado.daemon.send_to_daemon", side_effect=lambda command, args, **_: (
                    server._dispatch(command, args)
                )), redirect_stdout(output):
            code = cli.main([
                "send", "--room", "Peer Renamed", "--text-file", "-", "--trace", root,
            ])
            raw = "".join(path.read_text() for path in Path(root).glob("*.jsonl"))
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(self.backend.draft, text + "\n")
        self.assertEqual(self.backend.actions, ["fill", "submit"])
        self.assertNotIn("PRIVATE", raw)

    def test_stdin_dry_run_does_not_book_a_send(self) -> None:
        code, answer = self.main(
            ["send", "--room", "Peer Renamed", "--text-file", "-", "--dry-run", "--confirm"],
            b"synthetic\n",
        )
        self.assertEqual(code, 0, answer)
        self.assertFalse(answer["submitted"])
        self.assertNotIn("logged", answer)
        self.assertEqual(self.backend.actions, [])
        self.assertEqual(self.waits, [])

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
            with self.assertRaises(KamadoError) as caught:
                cli.run(by_title, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(self.backend.actions, ["fill", "submit"])

    def test_operation_id_with_confirmation_replays_output_without_second_input(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "synthetic",
             "--operation-id", "confirmed-cli", "--confirm"]
        )
        rows = []
        original_submit = self.backend.submit

        def submit(control):
            original_submit(control)
            rows.append(SimpleNamespace(id=3, text="synthetic", from_me=True))

        with tempfile.TemporaryDirectory() as root, \
                patch("kakaocommon.operations.directory", return_value=Path(root)), \
                patch.object(self.backend, "submit", side_effect=submit), \
                patch.object(self.reader, "newest_log_id", return_value=2), \
                patch.object(self.reader, "messages", side_effect=lambda _, **kw: [
                    row for row in rows if row.id > kw["after_id"]
                ]):
            outputs = []
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.run(arguments, self.reader), 0)
                outputs.append(output.getvalue())
            for confirm in (None, 1.0):
                with self.subTest(confirm=confirm), self.assertRaises(KamadoError) as caught:
                    cli.run(Namespace(
                        **{**vars(arguments), "confirm": confirm}
                    ), self.reader)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(outputs[0], outputs[1])
        body = json.loads(outputs[0])
        self.assertEqual((body["logged"], body["messageId"]), (True, "3"))
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
            with self.assertRaises(KamadoError) as caught:
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
            with self.assertRaises(KamadoError) as caught:
                cli.run(arguments, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertGreater(len(caught.exception.details["candidates"]), 1)
        self.assertEqual(self.backend.actions, [])

    def test_native_failure_uses_send_error_not_read_error(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["send", "--room", "Peer Renamed", "synthetic"]
        )
        with patch.object(self.backend, "get_text", side_effect=OSError("timeout")):
            with self.assertRaises(KamadoError) as caught:
                cli.run(arguments, self.reader)
        self.assertEqual(caught.exception.code, ErrorCode.SEND_FAILED)

    def test_main_emits_handled_error_json(self) -> None:
        output = io.StringIO()
        # A window whose message box cannot be found: a handled failure that
        # the envelope has to carry, now that a draft is simply overwritten.
        self.backend.control = 0
        with patch("kamado.cli.Reader", return_value=self.reader), redirect_stdout(output):
            result = cli.main(["send", "--room", "Peer Renamed", "synthetic"])
        body = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INPUT_NOT_FOUND")

    def test_a_draft_in_the_way_is_cleared_and_reported(self) -> None:
        output = io.StringIO()
        self.backend.draft = "unfinished synthetic draft"
        with patch("kamado.cli.Reader", return_value=self.reader), redirect_stdout(output):
            result = cli.main(["send", "--room", "Peer Renamed", "synthetic"])
        body = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(body["ok"])
        self.assertIs(body["clearedDraft"], True)
        self.assertEqual(self.backend.draft, "synthetic")


class SendFileCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.backend = ScriptedBackend()
        windows = FakeWindows()
        windows.windows = [ChatWindow(42, "Peer Renamed")]
        self.sender = Sender(windows, running=lambda: True)
        self.uploader = Uploader(self.backend, self.sender)
        patcher = patch("kamado.macupload.make_uploader", return_value=self.uploader)
        self.made = patcher.start()
        self.addCleanup(patcher.stop)
        self.waits = isolate_gate(self)

    def main(self, argv: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with patch("kamado.cli.Reader", side_effect=lambda **_: build_reader()), \
                redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_file_upload_uses_mac_paste_confirmation_and_string_room_id(self) -> None:
        path = self.folder / "synthetic.txt"
        path.write_bytes(b"synthetic bytes")
        code, answer = self.main([
            "sendfile", "--room-id", str(DIRECT_ROOM), "--open", str(path),
            "--trace", str(self.folder / "trace"),
        ])
        self.assertEqual(code, 0, answer)
        self.assertEqual((answer["path"], answer["name"], answer["size"]),
                         (str(path), path.name, 15))
        self.assertEqual(answer["roomId"], str(DIRECT_ROOM))
        self.assertEqual(answer["disposition"], {"delivery": "submitted", "retry": "unsafe"})
        self.assertEqual(self.backend.steps.count("confirm"), 1)
        self.assertEqual(self.backend.pasteboard, ["person's clipboard"])

    def test_missing_or_empty_file_is_refused_before_upload(self) -> None:
        empty = self.folder / "empty.txt"
        empty.touch()
        for path in (self.folder / "absent.txt", empty, self.folder):
            with self.subTest(path=path):
                code, answer = self.main(["sendfile", "--room", "Peer Renamed", str(path)])
                self.assertEqual(code, 1)
                self.assertEqual(answer["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(answer["error"]["disposition"],
                                 {"delivery": "not_delivered", "retry": "safe"})
        self.made.assert_not_called()
        self.assertEqual(self.backend.steps, [])

    def test_uncertain_upload_is_reported_without_retry(self) -> None:
        path = self.folder / "synthetic.txt"
        path.write_bytes(b"synthetic bytes")
        self.backend.fail["stays_open"] = True
        code, answer = self.main(["sendfile", "--room", "Peer Renamed", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], "SEND_FAILED")
        self.assertEqual(answer["error"]["disposition"],
                         {"delivery": "unknown", "retry": "after_check"})
        self.assertEqual(self.backend.steps.count("confirm"), 1)

    def test_sendfile_rejects_unsupported_text_send_flags(self) -> None:
        for flags in (["--dry-run"], ["--confirm"], ["--operation-id", "synthetic"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args(
                    ["sendfile", "--room", "Peer Renamed", "synthetic.txt", *flags]
                )
            self.assertEqual(caught.exception.code, 2)
