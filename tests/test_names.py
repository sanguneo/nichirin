"""Name history: what the client recorded, what we recorded, and what neither did.

The feature exists because KakaoTalk overwrites a nickname in place.  These
tests pin the two halves that replace it — the membership feeds the client
writes, and the ledger this tool appends — and they pin the boundary that
matters most: a reader built without a ledger must never write a name to disk,
because every reader in this suite is built that way and the real file holds
real people's names.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from kakaoread import cli, names
from kakaoread.names import NameLedger, NameRecord
from kakaoread.reader import Reader

from tests.fixtures import (
    DIRECT_ROOM,
    FRIEND_ID,
    OWN_ID,
    PEER_ID,
    STRANGER_ID,
    build_reader,
)

JOINED = 4
LEFT = 6


def feed(log_id: int, at: int, *members: tuple[int, str], kind: int = JOINED) -> tuple:
    """One membership feed row, shaped like the client's own."""
    body = json.dumps(
        {
            "feedType": kind,
            "members": [{"userId": user, "nickName": name} for user, name in members],
        },
        ensure_ascii=False,
    )
    return (log_id, 0, names.FEED_MESSAGE, body, None, at, 0)


def reader_with(rows: list[tuple], ledger: NameLedger | None = None) -> Reader:
    return build_reader(messages=rows, ledger=ledger)


class FeedTests(unittest.TestCase):
    def history(self, rows: list[tuple]) -> dict[int, list[NameRecord]]:
        reader = reader_with(rows)
        self.addCleanup(reader.close)
        return reader.name_history(DIRECT_ROOM)

    def test_reads_the_name_a_member_carried_when_they_joined(self) -> None:
        history = self.history([feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))])
        self.assertEqual([step.name for step in history[PEER_ID]], ["첫닉네임"])
        self.assertEqual(history[PEER_ID][0].at, 1_700_000_000)
        self.assertEqual(history[PEER_ID][0].source, names.FEED)

    def test_orders_the_steps_by_when_they_were_seen(self) -> None:
        history = self.history(
            [
                feed(12, 1_700_000_200, (PEER_ID, "셋째닉네임")),
                feed(10, 1_700_000_000, (PEER_ID, "첫닉네임")),
                feed(11, 1_700_000_100, (PEER_ID, "둘째닉네임"), kind=LEFT),
            ]
        )
        self.assertEqual(
            [step.name for step in history[PEER_ID]], ["첫닉네임", "둘째닉네임", "셋째닉네임"]
        )

    def test_collapses_a_name_that_was_recorded_twice(self) -> None:
        history = self.history(
            [
                feed(10, 1_700_000_000, (PEER_ID, "첫닉네임")),
                feed(11, 1_700_000_100, (PEER_ID, "첫닉네임"), kind=LEFT),
                feed(12, 1_700_000_200, (PEER_ID, "첫닉네임")),
            ]
        )
        self.assertEqual(len(history[PEER_ID]), 1)

    def test_a_body_that_is_not_json_is_skipped_rather_than_raising(self) -> None:
        history = self.history(
            [
                (10, 0, names.FEED_MESSAGE, "not json at all", None, 1_700_000_000, 0),
                (11, 0, names.FEED_MESSAGE, '{"feedType": 21, "hidden": true}', None, 1, 0),
                feed(12, 1_700_000_200, (PEER_ID, "첫닉네임")),
            ]
        )
        self.assertEqual(list(history), [PEER_ID])

    def test_ordinary_messages_contribute_no_names(self) -> None:
        history = self.history([(10, PEER_ID, 1, "안녕", None, 1_700_000_000, 0)])
        self.assertEqual(history, {})


class LedgerTests(unittest.TestCase):
    def ledger(self) -> NameLedger:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return NameLedger(Path(directory.name) / "names.jsonl")

    def test_records_what_it_is_shown(self) -> None:
        ledger = self.ledger()
        self.assertEqual(ledger.observe({PEER_ID: "첫닉네임"}, at=100), 1)
        self.assertEqual([(r.user_id, r.name) for r in ledger.records()], [(PEER_ID, "첫닉네임")])

    def test_writes_nothing_when_no_name_changed(self) -> None:
        ledger = self.ledger()
        ledger.observe({PEER_ID: "첫닉네임", FRIEND_ID: "둘"}, at=100)
        self.assertEqual(ledger.observe({PEER_ID: "첫닉네임", FRIEND_ID: "둘"}, at=200), 0)

    def test_appends_only_the_name_that_moved(self) -> None:
        ledger = self.ledger()
        ledger.observe({PEER_ID: "첫닉네임", FRIEND_ID: "둘"}, at=100)
        self.assertEqual(ledger.observe({PEER_ID: "둘째닉네임", FRIEND_ID: "둘"}, at=200), 1)
        self.assertEqual(
            [(r.user_id, r.name, r.at) for r in ledger.records()],
            [(PEER_ID, "첫닉네임", 100), (FRIEND_ID, "둘", 100), (PEER_ID, "둘째닉네임", 200)],
        )

    def test_a_damaged_line_costs_only_itself(self) -> None:
        ledger = self.ledger()
        ledger.observe({PEER_ID: "첫닉네임"}, at=100)
        with open(ledger.path, "a", encoding="utf-8") as handle:
            handle.write("{truncated\n")
        ledger.observe({FRIEND_ID: "둘"}, at=200)
        self.assertEqual(
            [(r.user_id, r.name) for r in ledger.records()], [(PEER_ID, "첫닉네임"), (FRIEND_ID, "둘")]
        )

    def test_a_missing_file_reads_as_an_empty_history(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertEqual(NameLedger(Path(directory.name) / "absent.jsonl").records(), [])

    def test_a_reader_given_a_ledger_records_the_live_names(self) -> None:
        ledger = self.ledger()
        reader = reader_with([], ledger=ledger)
        self.addCleanup(reader.close)
        reader.names()
        recorded = {record.name for record in ledger.records()}
        self.assertIn("Peer Renamed", recorded)
        self.assertIn("My Own Name", recorded)

    def test_a_reader_without_a_ledger_writes_nothing(self) -> None:
        ledger = self.ledger()
        reader = reader_with([])
        self.addCleanup(reader.close)
        reader.names()
        self.assertFalse(ledger.path.exists())

    def test_a_stranger_to_the_room_is_not_pulled_in_from_the_ledger(self) -> None:
        # STRANGER_ID belongs to no room in the fixture, so a name it carries
        # account-wide must not answer a question asked about this room.
        ledger = self.ledger()
        ledger.observe({STRANGER_ID: "겹치는이름", PEER_ID: "겹치는이름"}, at=1_700_000_500)
        reader = reader_with([feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))], ledger=ledger)
        self.addCleanup(reader.close)
        history = reader.name_history(DIRECT_ROOM)
        self.assertEqual(names.carriers(history, "겹치는이름"), [PEER_ID])

    def test_the_ledger_extends_a_history_the_feeds_never_reached(self) -> None:
        ledger = self.ledger()
        ledger.observe({PEER_ID: "나중닉네임"}, at=1_700_000_500)
        reader = reader_with([feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))], ledger=ledger)
        self.addCleanup(reader.close)
        history = reader.name_history(DIRECT_ROOM)
        self.assertEqual([step.name for step in history[PEER_ID]], ["첫닉네임", "나중닉네임"])
        self.assertEqual(history[PEER_ID][-1].source, names.OBSERVED)


class LookupTests(unittest.TestCase):
    def test_every_account_that_wore_a_name_is_returned(self) -> None:
        history = names.timeline(
            [
                NameRecord(PEER_ID, "겹치는이름", 100, names.FEED),
                NameRecord(FRIEND_ID, "겹치는이름", 110, names.FEED),
                NameRecord(STRANGER_ID, "다른사람", 120, names.FEED),
            ]
        )
        self.assertEqual(names.carriers(history, "겹치는이름"), sorted([PEER_ID, FRIEND_ID]))

    def test_a_name_nobody_wore_returns_nobody(self) -> None:
        history = names.timeline([NameRecord(PEER_ID, "첫닉네임", 100, names.FEED)])
        self.assertEqual(names.carriers(history, "없는이름"), [])

    def test_the_live_name_closes_a_history_that_stops_short(self) -> None:
        steps = names.steps_with_current(
            PEER_ID, [NameRecord(PEER_ID, "첫닉네임", 100, names.FEED)], "나중닉네임"
        )
        self.assertEqual([step.name for step in steps], ["첫닉네임", "나중닉네임"])
        self.assertEqual(steps[-1].source, names.CURRENT)
        self.assertIsNone(steps[-1].at)

    def test_the_live_name_is_not_repeated_when_the_history_reaches_it(self) -> None:
        steps = names.steps_with_current(
            PEER_ID, [NameRecord(PEER_ID, "첫닉네임", 100, names.FEED)], "첫닉네임"
        )
        self.assertEqual(len(steps), 1)


class NamesCommandTests(unittest.TestCase):
    def invoke(self, rows: list[tuple], *argv: str) -> tuple[int, dict]:
        reader = reader_with(rows)
        self.addCleanup(reader.close)
        arguments = cli.build_parser().parse_args(argv)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.run(arguments, reader)
        return code, json.loads(output.getvalue())

    def test_without_an_argument_only_the_renamers_are_listed(self) -> None:
        code, answer = self.invoke(
            [
                feed(10, 1_700_000_000, (PEER_ID, "첫닉네임")),
                feed(11, 1_700_000_100, (PEER_ID, "둘째닉네임")),
                feed(12, 1_700_000_200, (FRIEND_ID, "그대로")),
            ],
            "names",
            "--room-id",
            str(DIRECT_ROOM),
        )
        self.assertEqual(code, 0)
        self.assertEqual([user["userId"] for user in answer["users"]], [str(PEER_ID)])

    def test_a_nickname_finds_the_account_that_answers_to_it_now(self) -> None:
        # "Peer Renamed" was never written into a feed; it is only the live
        # name, which is exactly the case a feed-only lookup would miss.
        _, answer = self.invoke(
            [feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))],
            "names",
            "--room-id",
            str(DIRECT_ROOM),
            "Peer Renamed",
        )
        self.assertEqual([user["userId"] for user in answer["users"]], [str(PEER_ID)])
        self.assertEqual(
            [step["name"] for step in answer["users"][0]["history"]], ["첫닉네임", "Peer Renamed"]
        )

    def test_a_live_name_from_another_chat_is_not_an_answer_here(self) -> None:
        # STRANGER_ID is in no room; its live name must not answer a question
        # asked about this one, however exactly it matches.
        _, answer = self.invoke(
            [feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))],
            "names",
            "--room-id",
            str(DIRECT_ROOM),
            "Decoy With Badge",
        )
        self.assertEqual(answer["count"], 0)

    def test_two_accounts_under_one_name_are_both_reported(self) -> None:
        _, answer = self.invoke(
            [
                feed(10, 1_700_000_000, (PEER_ID, "겹치는이름")),
                feed(11, 1_700_000_100, (FRIEND_ID, "겹치는이름")),
            ],
            "names",
            "--room-id",
            str(DIRECT_ROOM),
            "겹치는이름",
        )
        self.assertEqual(answer["count"], 2)

    def test_one_account_can_be_asked_for_by_identifier(self) -> None:
        _, answer = self.invoke(
            [feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))],
            "names",
            "--room-id",
            str(DIRECT_ROOM),
            "--user-id",
            str(PEER_ID),
        )
        self.assertEqual([user["userId"] for user in answer["users"]], [str(PEER_ID)])

    def test_identifiers_cross_the_boundary_as_strings(self) -> None:
        _, answer = self.invoke(
            [feed(10, 1_700_000_000, (OWN_ID, "하나"), (PEER_ID, "둘"))],
            "names",
            "--room-id",
            str(DIRECT_ROOM),
            "--user-id",
            str(PEER_ID),
        )
        user = answer["users"][0]
        self.assertIsInstance(user["userId"], str)
        self.assertIsInstance(user["history"][0]["userId"], str)


class RealCommandTests(unittest.TestCase):
    """`/real` in the browser, driven the way a reader drives it: by typing."""

    def app_with(self, rows: list[tuple], ledger: NameLedger | None = None):
        from kakaoread.tui.app import App
        from kakaoread.tui.theme import Theme

        reader = reader_with(rows, ledger=ledger)
        self.addCleanup(reader.close)
        app = App(reader, theme=Theme(truecolor=True, enabled=False))
        app.load_rooms()
        app.open_room(next(room for room in app.rooms if room.id == DIRECT_ROOM))
        return app

    def notice_after(self, rows: list[tuple], typed: str) -> str:
        app = self.app_with(rows)
        chat = app.chat
        assert chat is not None
        chat.draft = typed
        app._submit(chat)
        return chat.notice

    def test_shows_the_chain_and_ends_it_at_the_live_name(self) -> None:
        notice = self.notice_after(
            [
                feed(10, 1_700_000_000, (PEER_ID, "첫닉네임")),
                feed(11, 1_700_000_100, (PEER_ID, "둘째닉네임")),
            ],
            "/real Peer Renamed",
        )
        self.assertIn("첫닉네임", notice)
        self.assertIn("둘째닉네임", notice)
        self.assertIn("Peer Renamed(지금)", notice)

    def test_a_shared_name_reports_how_many_accounts_wore_it(self) -> None:
        notice = self.notice_after(
            [
                feed(10, 1_700_000_000, (PEER_ID, "겹치는이름")),
                feed(11, 1_700_000_100, (FRIEND_ID, "겹치는이름")),
            ],
            "/real 겹치는이름",
        )
        self.assertIn("2", notice)

    def test_a_nickname_holding_a_space_is_one_name(self) -> None:
        notice = self.notice_after(
            [feed(10, 1_700_000_000, (PEER_ID, "합성이름 (handle)"))],
            "/real 합성이름 (handle)",
        )
        self.assertIn("합성이름 (handle)", notice)
        self.assertNotIn("기록에 없습니다", notice)

    def test_an_unrecorded_name_is_refused_rather_than_guessed(self) -> None:
        notice = self.notice_after(
            [feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))], "/real 아무도아님"
        )
        self.assertIn("기록에 없습니다", notice)

    def test_the_bare_command_asks_for_a_name(self) -> None:
        notice = self.notice_after([feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))], "/real")
        self.assertIn("이름이 필요합니다", notice)

    def test_typing_real_sends_nothing(self) -> None:
        app = self.app_with([feed(10, 1_700_000_000, (PEER_ID, "첫닉네임"))])
        chat = app.chat
        assert chat is not None
        chat.draft = "/real Peer Renamed"
        app._submit(chat)
        self.assertIsNone(app.pending_send)


if __name__ == "__main__":
    unittest.main()
