"""CLI tests: one envelope, one exit-status convention, no room guessing."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from kamado import cli
from kamado.errors import ErrorCode, KamadoError

from tests.fixtures import (
    DIRECT_ROOM,
    GROUP_ROOM,
    HUGE_ID,
    HUGE_ROOM,
    NAMED_ROOM,
    build_reader,
)


class CommandTests(unittest.TestCase):
    def invoke(self, *argv: str) -> tuple[int, dict]:
        reader = build_reader()
        self.addCleanup(reader.close)
        arguments = cli.build_parser().parse_args(argv)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.run(arguments, reader)
        return code, json.loads(output.getvalue())

    def test_status_succeeds_and_uses_the_standard_envelope(self) -> None:
        code, answer = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["ready"])
        self.assertEqual(answer["rooms"], 6)

    def test_verify_reports_the_catalog_opened(self) -> None:
        code, answer = self.invoke("verify")
        self.assertEqual(code, 0)
        self.assertTrue(answer["verified"])

    def test_rooms_lists_every_room_with_a_readable_flag(self) -> None:
        code, answer = self.invoke("rooms")
        self.assertEqual(code, 0)
        self.assertEqual(answer["count"], len(answer["rooms"]))
        self.assertIn("readable", answer["rooms"][0])

    def test_rooms_can_be_narrowed_to_readable_ones(self) -> None:
        _code, answer = self.invoke("rooms", "--readable-only")
        self.assertTrue(all(room["readable"] for room in answer["rooms"]))
        self.assertIn(str(NAMED_ROOM), [room["id"] for room in answer["rooms"]])

    def test_rooms_honours_a_limit(self) -> None:
        _code, answer = self.invoke("rooms", "--limit", "2")
        self.assertEqual(answer["count"], 2)

    def test_context_returns_messages_and_coverage(self) -> None:
        code, answer = self.invoke("context", "--room-id", str(DIRECT_ROOM))
        self.assertEqual(code, 0)
        self.assertIn("coverage", answer)
        self.assertTrue(answer["coverage"]["localOnly"])

    def test_messages_pages_backwards(self) -> None:
        _code, answer = self.invoke(
            "messages", "--room-id", str(DIRECT_ROOM), "--before-id", "5001"
        )
        self.assertEqual([message["id"] for message in answer["messages"]], ["5000"])

    def test_search_declares_how_many_rooms_it_could_not_search(self) -> None:
        code, answer = self.invoke("search", "group hello")
        self.assertEqual(code, 0)
        self.assertEqual(answer["count"], 1)
        self.assertEqual(answer["unsearchableRooms"], 0)
        self.assertIn("not proof", answer["note"])

    def test_schema_describes_tables_and_columns(self) -> None:
        _code, answer = self.invoke("schema")
        self.assertIn("NTChatRoom", answer["tables"])
        self.assertIn(
            "chatId", [column["name"] for column in answer["tables"]["NTChatRoom"]]
        )

    def test_query_runs_read_only_sql(self) -> None:
        _code, answer = self.invoke("query", "SELECT COUNT(*) AS total FROM NTChatRoom")
        self.assertEqual(answer["rows"][0]["total"], 6)

    def test_query_reports_bad_sql_as_an_invalid_request(self) -> None:
        reader = build_reader()
        self.addCleanup(reader.close)
        arguments = cli.build_parser().parse_args(["query", "SELECT nope FROM nowhere"])
        with self.assertRaises(KamadoError) as raised:
            cli.run(arguments, reader)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)


class IdentifierTests(unittest.TestCase):
    def test_identifiers_stay_strings_through_a_json_round_trip(self) -> None:
        reader = build_reader()
        self.addCleanup(reader.close)
        arguments = cli.build_parser().parse_args(
            ["context", "--room-id", str(HUGE_ROOM)]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            cli.run(arguments, reader)
        answer = json.loads(output.getvalue())
        self.assertEqual(answer["messages"][0]["senderId"], str(HUGE_ID))


class FailureTests(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple[int, str]:
        reader = build_reader()
        output = io.StringIO()
        with patch("kamado.cli.Reader", return_value=reader), redirect_stdout(output):
            code = cli.main(list(argv))
        return code, output.getvalue()

    def test_a_missing_room_exits_one_with_a_typed_code(self) -> None:
        code, output = self.run_main("context", "--room-id", "999999")
        answer = json.loads(output)
        self.assertEqual(code, 1)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["code"], ErrorCode.CHAT_NOT_FOUND.value)

    def test_an_ambiguous_title_returns_candidates_and_exits_one(self) -> None:
        code, output = self.run_main("context", "--room", "e")
        answer = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], ErrorCode.AMBIGUOUS_CHAT.value)
        self.assertGreater(len(answer["candidates"]), 1)

    def test_an_unreadable_account_explains_the_missing_key(self) -> None:
        reader = build_reader()
        reader._keyring = type(reader.keyring)(
            container=reader.keyring.container, databases=reader.keyring.databases, account=None
        )
        output = io.StringIO()
        with patch("kamado.cli.Reader", return_value=reader), redirect_stdout(output):
            code = cli.main(["context", "--room-id", str(NAMED_ROOM)])
        answer = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], ErrorCode.KEY_UNAVAILABLE.value)

    def test_an_unexpected_failure_still_uses_the_envelope(self) -> None:
        reader = build_reader()
        self.addCleanup(reader.close)
        output = io.StringIO()
        with (
            patch("kamado.cli.Reader", return_value=reader),
            patch.object(type(reader), "status", side_effect=RuntimeError("boom")),
            redirect_stdout(output),
        ):
            code = cli.main(["status"])
        answer = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], ErrorCode.READ_FAILED.value)
        self.assertIn("boom", answer["error"]["message"])


class ParserTests(unittest.TestCase):
    def test_a_room_may_be_named_or_numbered_but_not_both(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()):
                cli.build_parser().parse_args(
                    ["context", "--room", "Team", "--room-id", "7"]
                )
        self.assertEqual(raised.exception.code, 2)

    def test_a_room_must_be_selected_explicitly(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()):
                cli.build_parser().parse_args(["context"])
        self.assertEqual(raised.exception.code, 2)

    def test_limits_must_be_positive(self) -> None:
        for limit in ("0", "-3"):
            with self.subTest(limit=limit), self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    cli.build_parser().parse_args(
                        ["context", "--room-id", "7", "--limit", limit]
                    )

    def test_follow_intervals_must_be_positive(self) -> None:
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                cli.build_parser().parse_args(["follow", "--interval", "0"])

    def test_follow_accepts_several_rooms(self) -> None:
        arguments = cli.build_parser().parse_args(
            ["follow", "--room-id", "1", "--room-id", "2"]
        )
        self.assertEqual(arguments.room_ids, [1, 2])

    def test_every_command_is_reachable(self) -> None:
        for command in (
            ["status"], ["verify"], ["rooms"], ["context", "--room-id", "1"],
            ["messages", "--room-id", "1"], ["search", "x"], ["follow"],
            ["query", "SELECT 1"], ["schema"],
        ):
            with self.subTest(command=command[0]):
                self.assertEqual(cli.build_parser().parse_args(command).command, command[0])


if __name__ == "__main__":
    unittest.main()
