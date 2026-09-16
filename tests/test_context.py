"""Context tests: the answer shape an agent actually reasons over.

These assertions are mostly about refusing to overstate what is known — the
failure mode being designed against is an agent that drafts a reply the user
already sent, because it was handed a partial cache with no indication that it
was partial.
"""

from __future__ import annotations

import json
import unittest

from kakaoread.context import read_context
from kakaoread.errors import ErrorCode, KakaoReadError

from tests.fixtures import (
    DIRECT_ROOM,
    GROUP_ROOM,
    HUGE_ID,
    HUGE_ROOM,
    UNREADABLE_ROOM,
    build_reader,
)


class SuccessTests(unittest.TestCase):
    def test_returns_the_room_its_messages_and_its_coverage(self) -> None:
        with build_reader() as reader:
            answer = read_context(reader, DIRECT_ROOM)
        self.assertTrue(answer["ok"])
        self.assertEqual(
            set(answer),
            {"ok", "room", "messages", "lastMessageFromMe", "coverage", "attention"},
        )
        self.assertEqual(answer["room"]["id"], str(DIRECT_ROOM))

    def test_messages_are_oldest_first(self) -> None:
        with build_reader() as reader:
            messages = read_context(reader, DIRECT_ROOM)["messages"]
        ids = [int(message["id"]) for message in messages]
        self.assertEqual(ids, sorted(ids))

    def test_reports_reaching_the_latest_message_the_catalog_knows_about(self) -> None:
        with build_reader() as reader:
            coverage = read_context(reader, DIRECT_ROOM)["coverage"]
        self.assertTrue(coverage["reachesLatestKnown"])
        self.assertEqual(coverage["catalogLatestMessageId"], coverage["newestReadMessageId"])

    def test_never_claims_to_have_seen_the_server_history(self) -> None:
        with build_reader() as reader:
            self.assertTrue(read_context(reader, DIRECT_ROOM)["coverage"]["localOnly"])

    def test_reports_that_earlier_messages_exist_beyond_the_window(self) -> None:
        with build_reader() as reader:
            narrow = read_context(reader, DIRECT_ROOM, limit=1)
            wide = read_context(reader, DIRECT_ROOM, limit=50)
        self.assertTrue(narrow["coverage"]["hasEarlier"])
        self.assertFalse(wide["coverage"]["hasEarlier"])
        self.assertEqual(len(narrow["messages"]), 1)

    def test_a_narrow_window_still_returns_the_newest_messages(self) -> None:
        with build_reader() as reader:
            answer = read_context(reader, DIRECT_ROOM, limit=1)
        self.assertEqual(answer["messages"][0]["id"], "5003")

    def test_reports_who_sent_the_last_returned_message(self) -> None:
        with build_reader() as reader:
            answer = read_context(reader, DIRECT_ROOM)
        self.assertIs(answer["lastMessageFromMe"], False)

    def test_reports_how_much_is_stored_locally(self) -> None:
        with build_reader() as reader:
            self.assertEqual(read_context(reader, DIRECT_ROOM)["coverage"]["storedLocally"], 3)

    def test_resolves_a_room_by_title(self) -> None:
        with build_reader() as reader:
            answer = read_context(reader, "Peer Renamed")
        self.assertEqual(answer["room"]["id"], str(DIRECT_ROOM))

    def test_the_whole_answer_survives_ordinary_json_round_tripping(self) -> None:
        """Every identifier is a string, so a JavaScript consumer cannot lose one."""
        with build_reader() as reader:
            answer = read_context(reader, HUGE_ROOM)
        restored = json.loads(json.dumps(answer))
        self.assertEqual(restored["room"]["id"], str(HUGE_ROOM))
        self.assertEqual(restored["messages"][0]["id"], str(HUGE_ID))
        self.assertEqual(int(restored["messages"][0]["senderId"]), HUGE_ID)


class RefusalTests(unittest.TestCase):
    def test_an_ambiguous_title_returns_candidates_instead_of_a_guess(self) -> None:
        with build_reader() as reader:
            with self.assertRaises(KakaoReadError) as raised:
                read_context(reader, "e")
        failure = raised.exception
        self.assertEqual(failure.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertGreater(len(failure.details["candidates"]), 1)
        self.assertIn("id", failure.details["candidates"][0])

    def test_an_unknown_title_is_reported_as_not_found(self) -> None:
        with build_reader() as reader:
            with self.assertRaises(KakaoReadError) as raised:
                read_context(reader, "no such conversation anywhere")
        self.assertEqual(raised.exception.code, ErrorCode.CHAT_NOT_FOUND)

    def test_an_unknown_id_is_reported_as_not_found(self) -> None:
        with build_reader() as reader:
            with self.assertRaises(KakaoReadError) as raised:
                read_context(reader, 999999)
        self.assertEqual(raised.exception.code, ErrorCode.CHAT_NOT_FOUND)

    def test_a_room_without_a_loaded_key_says_so_and_names_the_room(self) -> None:
        with build_reader() as reader:
            with self.assertRaises(KakaoReadError) as raised:
                read_context(reader, UNREADABLE_ROOM)
        failure = raised.exception
        self.assertEqual(failure.code, ErrorCode.KEY_UNAVAILABLE)
        self.assertEqual(failure.details["room"]["id"], str(UNREADABLE_ROOM))
        self.assertIn("open it in KakaoTalk", failure.message)

    def test_a_non_positive_limit_is_rejected(self) -> None:
        with build_reader() as reader:
            for limit in (0, -5):
                with self.subTest(limit=limit), self.assertRaises(KakaoReadError) as raised:
                    read_context(reader, DIRECT_ROOM, limit=limit)
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_every_failure_renders_the_same_envelope(self) -> None:
        with build_reader() as reader:
            with self.assertRaises(KakaoReadError) as raised:
                read_context(reader, 999999)
        envelope = raised.exception.envelope()
        self.assertIs(envelope["ok"], False)
        self.assertEqual(set(envelope["error"]), {"code", "message"})


class EmptyRoomTests(unittest.TestCase):
    def test_a_room_with_messages_behind_the_catalog_is_reported_as_behind(self) -> None:
        with build_reader(messages=[]) as reader:
            coverage = read_context(reader, DIRECT_ROOM)["coverage"]
        self.assertFalse(coverage["reachesLatestKnown"])
        self.assertIsNone(coverage["newestReadMessageId"])

    def test_an_empty_room_reports_no_last_sender_rather_than_guessing(self) -> None:
        with build_reader(messages=[]) as reader:
            answer = read_context(reader, DIRECT_ROOM)
        self.assertEqual(answer["messages"], [])
        self.assertIsNone(answer["lastMessageFromMe"])


class GroupTests(unittest.TestCase):
    def test_a_group_room_reads_like_any_other(self) -> None:
        with build_reader() as reader:
            answer = read_context(reader, GROUP_ROOM)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["room"]["kind"], "group")
        self.assertEqual(len(answer["messages"]), 5)


class AttentionTests(unittest.TestCase):
    """Which messages were addressed at the user, which the text cannot say."""

    def attention(self, room_id: int = GROUP_ROOM) -> dict:
        with build_reader() as reader:
            return read_context(reader, room_id)["attention"]

    def test_names_the_message_that_mentioned_the_user(self) -> None:
        self.assertEqual(self.attention()["mentionsMe"], ["6003"])

    def test_names_the_message_that_replied_to_the_user(self) -> None:
        self.assertEqual(self.attention()["repliesToMe"], ["6004"])

    def test_an_everyone_mention_is_reported_apart_from_a_named_one(self) -> None:
        """@전체 reaches the user without being addressed at them."""
        found = self.attention()
        self.assertEqual(found["mentionsEveryone"], ["6005"])
        self.assertNotIn("6005", found["mentionsMe"])

    def test_a_conversation_nobody_was_addressed_in_reports_nothing(self) -> None:
        self.assertEqual(
            self.attention(DIRECT_ROOM),
            {"mentionsMe": [], "repliesToMe": [], "mentionsEveryone": []},
        )

    def test_the_quoted_message_travels_with_the_reply(self) -> None:
        with build_reader() as reader:
            messages = read_context(reader, GROUP_ROOM)["messages"]
        reply = next(message for message in messages if message["id"] == "6004")
        self.assertEqual(reply["kind"], "reply")
        self.assertEqual(reply["replyTo"]["messageId"], "6002")
        self.assertEqual(reply["replyTo"]["text"], "내가 남긴 줄")
        self.assertIs(reply["repliesToMe"], True)


if __name__ == "__main__":
    unittest.main()
