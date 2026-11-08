"""Send CLI integration through synthetic databases and a fake window backend."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace

from kamado import cli
from kamado.errors import ErrorCode, KamadoError
from kamado.macauto import ChatWindow, Sender
from tests.fixtures import DIRECT_ROOM, build_reader
from tests.test_send import FakeWindows


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

    def test_dry_run_by_id_has_standard_envelope_and_string_id(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["send", "--room-id", str(DIRECT_ROOM), "--dry-run", "synthetic"]
        )
        output = io.StringIO()
        with redirect_stdout(output):
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
