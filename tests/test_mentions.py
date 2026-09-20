"""Being addressed: an @name and a reply, which the message text cannot show.

Both facts live in the client's ``attachement`` column rather than in the
message. A mention records the id it points at, because the name in the text
is only what that account wore at the time and anybody can wear it; a reply
records its own copy of what it was answering, which is the only way the
answer means anything once the original scrolls out of the window.

The parsing half runs against dictionaries, so it can be pushed at shapes a
live client would rarely produce without a database anywhere in sight.
"""

from __future__ import annotations

import json
import unittest

from kamado.model import (
    Message,
    everyone_mentioned,
    mentions_in,
    parse_attachment,
    quoted_in,
)

from tests.fixtures import (
    DIRECT_ROOM,
    GROUP_ROOM,
    OWN_ID,
    PEER_ID,
    REPLY_TYPE,
    build_reader,
)

from kamado.reader import Reader

def NOBODY(user_id: int) -> None:
    """A resolver for a profile that has never seen the id being asked about."""
    return None


class AttachmentTests(unittest.TestCase):
    """The column is written by another program, so it is never trusted."""

    def test_an_absent_attachment_reads_as_nothing_extra(self) -> None:
        for raw in (None, "", 0, b"{}"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_attachment(raw), {})

    def test_text_that_is_not_json_reads_as_nothing_extra(self) -> None:
        self.assertEqual(parse_attachment("{not json at all"), {})

    def test_json_that_is_not_an_object_reads_as_nothing_extra(self) -> None:
        self.assertEqual(parse_attachment(json.dumps([1, 2, 3])), {})

    def test_an_object_is_handed_back(self) -> None:
        self.assertEqual(parse_attachment(json.dumps({"src_logId": 7})), {"src_logId": 7})


class MentionParsingTests(unittest.TestCase):
    def test_reads_the_id_the_offsets_and_the_length(self) -> None:
        found = mentions_in({"mentions": [{"at": [3, 40], "len": 5, "user_id": 99}]}, NOBODY)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].user_id, 99)
        self.assertEqual(found[0].offsets, (3, 40))
        self.assertEqual(found[0].length, 5)

    def test_resolves_the_id_to_the_name_that_account_carries_now(self) -> None:
        found = mentions_in({"mentions": [{"user_id": 99}]}, {99: "이름"}.get)
        self.assertEqual(found[0].name, "이름")

    def test_an_id_this_profile_has_never_seen_stays_a_bare_id(self) -> None:
        found = mentions_in({"mentions": [{"user_id": 99}]}, NOBODY)
        self.assertIsNone(found[0].name)
        self.assertEqual(found[0].user_id, 99)

    def test_an_entry_that_names_nobody_is_dropped_rather_than_pointed_at_user_zero(
        self,
    ) -> None:
        found = mentions_in(
            {"mentions": [{"len": 4}, {"user_id": None}, "nonsense", {"user_id": 7}]}, NOBODY
        )
        self.assertEqual([one.user_id for one in found], [7])

    def test_an_identifier_written_as_text_is_still_an_identifier(self) -> None:
        found = mentions_in({"mentions": [{"user_id": "12345678901234567"}]}, NOBODY)
        self.assertEqual(found[0].user_id, 12345678901234567)

    def test_a_mentions_field_of_the_wrong_shape_mentions_nobody(self) -> None:
        for attachment in ({}, {"mentions": None}, {"mentions": "nobody"}):
            with self.subTest(attachment=attachment):
                self.assertEqual(mentions_in(attachment, NOBODY), ())

    def test_an_everyone_mention_is_read_from_its_presence(self) -> None:
        self.assertTrue(everyone_mentioned({"all_mention": {"at": 1}}))
        self.assertFalse(everyone_mentioned({}))


class QuoteParsingTests(unittest.TestCase):
    def test_reads_the_answered_message_its_sender_and_its_stored_text(self) -> None:
        quote = quoted_in(
            {"src_logId": 42, "src_userId": 7, "src_message": "원문", "src_type": 1},
            {7: "쓴 사람"}.get,
        )
        assert quote is not None
        self.assertEqual(quote.message_id, 42)
        self.assertEqual(quote.sender_id, 7)
        self.assertEqual(quote.sender, "쓴 사람")
        self.assertEqual(quote.text, "원문")
        self.assertEqual(quote.as_json()["kind"], "text")

    def test_an_attachment_without_a_source_is_not_a_reply(self) -> None:
        self.assertIsNone(quoted_in({"mentions": []}, NOBODY))

    def test_a_quote_without_stored_text_is_empty_rather_than_invented(self) -> None:
        quote = quoted_in({"src_logId": 42, "src_type": 2}, NOBODY)
        assert quote is not None
        self.assertEqual(quote.text, "")
        self.assertEqual(quote.as_json()["kind"], "photo")

    def test_a_quote_renders_its_identifiers_as_strings(self) -> None:
        quote = quoted_in({"src_logId": 9007199254740993, "src_userId": 7}, NOBODY)
        assert quote is not None
        rendered = quote.as_json()
        self.assertEqual(rendered["messageId"], "9007199254740993")
        self.assertEqual(rendered["senderId"], "7")
        self.assertIsNone(rendered["kind"])


class ReadMessageTests(unittest.TestCase):
    """The same facts, read out of a room the way every command reads one."""

    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.messages = {
            message.id: message for message in self.reader.messages(GROUP_ROOM, limit=50)
        }

    def test_a_mention_is_attributed_to_an_id_not_to_the_name_in_the_text(self) -> None:
        mentioned = self.messages[6003]
        self.assertEqual([one.user_id for one in mentioned.mentions], [OWN_ID])
        self.assertEqual(mentioned.sender_id, PEER_ID)

    def test_the_mentioned_id_is_resolved_to_its_current_name(self) -> None:
        self.assertEqual(self.messages[6003].mentions[0].name, "My Own Name")

    def test_a_message_naming_the_user_is_flagged(self) -> None:
        self.assertTrue(self.messages[6003].mentions_me)
        self.assertFalse(self.messages[6001].mentions_me)

    def test_an_everyone_mention_names_nobody_in_particular(self) -> None:
        everyone = self.messages[6005]
        self.assertTrue(everyone.mentions_all)
        self.assertEqual(everyone.mentions, ())
        self.assertFalse(everyone.mentions_me)

    def test_a_reply_carries_the_message_it_answers(self) -> None:
        reply = self.messages[6004]
        assert reply.reply is not None
        self.assertEqual(reply.reply.message_id, 6002)
        self.assertEqual(reply.reply.text, "내가 남긴 줄")
        self.assertEqual(reply.reply.sender, "My Own Name")
        self.assertTrue(reply.replies_to_me)

    def test_a_reply_is_named_rather_than_left_as_a_type_number(self) -> None:
        self.assertEqual(self.messages[6004].as_json()["kind"], "reply")

    def test_an_ordinary_message_carries_none_of_these_keys(self) -> None:
        """Absence is the negative: forty messages must not each deny five things."""
        rendered = self.messages[6001].as_json()
        for key in ("mentions", "mentionsAll", "mentionsMe", "replyTo", "repliesToMe"):
            self.assertNotIn(key, rendered)

    def test_the_rendered_mention_keeps_the_identifier_as_a_string(self) -> None:
        rendered = self.messages[6003].as_json()
        self.assertEqual(rendered["mentions"][0]["userId"], str(OWN_ID))
        self.assertEqual(rendered["mentions"][0]["at"], [0])
        self.assertIs(rendered["mentionsMe"], True)

    def test_a_room_nobody_was_addressed_in_reads_as_before(self) -> None:
        for message in self.reader.messages(DIRECT_ROOM, limit=50):
            self.assertEqual(message.mentions, ())
            self.assertIsNone(message.reply)
            self.assertFalse(message.mentions_me)

    def test_a_record_states_the_fact_even_where_a_summary_would_drop_it(self) -> None:
        """Answering your own message is still a reply to you; context filters it."""
        mine = Message(
            id=1,
            room_id=GROUP_ROOM,
            sender_id=OWN_ID,
            sender="My Own Name",
            text="스스로 답장",
            type=REPLY_TYPE,
            sent_at=None,
            from_me=True,
            replies_to_me=True,
        )
        self.assertTrue(mine.as_json()["repliesToMe"])

    def test_the_search_results_carry_the_same_facts(self) -> None:
        found = [message for message in self.reader.search("맞아요") if message.id == 6004]
        self.assertEqual(len(found), 1)
        assert found[0].reply is not None
        self.assertEqual(found[0].reply.sender_id, OWN_ID)


class ThreadedReplyTests(unittest.TestCase):
    """A reply the client recorded as a thread rather than as its own copy.

    The attachment used to carry a snapshot of what was answered. This client
    writes an ordinary text row and points a ``threadId`` at the message it
    answers, so the quote is no longer in the row: it has to be read back out
    of the conversation, and it says what that message says *now*.
    """

    def build(self, rows: list[tuple]) -> Reader:
        reader = build_reader(messages=rows)
        self.addCleanup(reader.close)
        return reader

    def answered(self, rows: list[tuple], log_id: int):
        reader = self.build(rows)
        found = {message.id: message for message in reader.messages(DIRECT_ROOM, limit=50)}
        return found[log_id]

    def test_a_threaded_reply_is_read_as_a_reply(self) -> None:
        answer = self.answered(
            [
                (5000, DIRECT_ROOM, PEER_ID, 1, "먼저 한 말", None, 1_700_000_000, 0),
                (5001, DIRECT_ROOM, OWN_ID, 1, "거기 답", None, 1_700_000_100, 0, 5000),
            ],
            5001,
        )
        assert answer.reply is not None
        self.assertEqual(answer.reply.message_id, 5000)
        self.assertEqual(answer.reply.sender_id, PEER_ID)
        self.assertEqual(answer.reply.text, "먼저 한 말")
        self.assertFalse(answer.replies_to_me)

    def test_a_threaded_reply_to_my_own_line_is_marked_as_one(self) -> None:
        answer = self.answered(
            [
                (5000, DIRECT_ROOM, OWN_ID, 1, "내가 남긴 줄", None, 1_700_000_000, 0),
                (5001, DIRECT_ROOM, PEER_ID, 1, "거기 답", None, 1_700_000_100, 0, 5000),
            ],
            5001,
        )
        self.assertTrue(answer.replies_to_me)

    def test_a_thread_naming_no_stored_message_quotes_nothing(self) -> None:
        # The answered message can be older than anything kept locally, and
        # inventing a blank quote for it would claim to know what it said.
        answer = self.answered(
            [(5001, DIRECT_ROOM, PEER_ID, 1, "거기 답", None, 1_700_000_100, 0, 4999)],
            5001,
        )
        self.assertIsNone(answer.reply)

    def test_a_row_naming_itself_is_not_a_reply_to_itself(self) -> None:
        answer = self.answered(
            [(5000, DIRECT_ROOM, PEER_ID, 1, "혼잣말", None, 1_700_000_000, 0, 5000)],
            5000,
        )
        self.assertIsNone(answer.reply)

    def test_the_copy_the_reply_carried_wins_over_the_thread(self) -> None:
        """What the sender was answering is what they saw, not what it says now."""
        answer = self.answered(
            [
                (5000, DIRECT_ROOM, PEER_ID, 1, "고쳐 쓴 줄", None, 1_700_000_000, 0),
                (
                    5001, DIRECT_ROOM, OWN_ID, REPLY_TYPE, "거기 답",
                    json.dumps(
                        {
                            "src_logId": 5000,
                            "src_userId": PEER_ID,
                            "src_message": "보낼 때 있던 줄",
                            "src_type": 1,
                        }
                    ),
                    1_700_000_100, 0, 5000,
                ),
            ],
            5001,
        )
        assert answer.reply is not None
        self.assertEqual(answer.reply.text, "보낼 때 있던 줄")

    def test_a_threaded_reply_is_rendered_for_a_caller(self) -> None:
        answer = self.answered(
            [
                (5000, DIRECT_ROOM, PEER_ID, 1, "먼저 한 말", None, 1_700_000_000, 0),
                (5001, DIRECT_ROOM, OWN_ID, 1, "거기 답", None, 1_700_000_100, 0, 5000),
            ],
            5001,
        )
        self.assertEqual(answer.as_json()["replyTo"]["messageId"], "5000")


if __name__ == "__main__":
    unittest.main()
