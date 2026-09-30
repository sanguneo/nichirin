"""A board notice cut at the client's 100 characters, and the message it came from.

The post records no message id, so the whole text is found only by its words:
exactly one live message beginning with the stored preview. Every value here is
synthetic.
"""

import dataclasses
import json
import unittest

from kanao import model, paths

from tests.extras_fixtures import SAFEBOT, build_extras_reader
from tests.fixtures import FRIEND_ID, GROUP_ROOM, PEER_ID

WHOLE = "가상 공지 전문입니다. " + "합성 문장이 길게 이어집니다. " * 20
PREVIEW = WHOLE[: model.BOARD_PREVIEW_CHARACTERS]


def _reader(*messages: tuple, notice: str = PREVIEW):
    reader = build_extras_reader()
    databases = reader._cache.databases  # type: ignore[attr-defined]
    catalog = databases[paths.CATALOG_DATABASE]
    catalog.execute(
        "UPDATE moimMetas SET content = ? WHERE chatId = ? AND type = 1",
        (
            json.dumps(
                {
                    "notice": True,
                    "type": "TEXT",
                    "content": notice,
                    "owner_id": FRIEND_ID,
                    "created_at": "2026-01-04T03:00:00.000Z",
                }
            ),
            GROUP_ROOM,
        ),
    )
    catalog.commit()
    log = databases[paths.chat_log_name(GROUP_ROOM)]
    log.executemany(
        "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted, "
        "threadId, is_encrypt_delete_msg, revision) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, 0, 0)",
        messages,
    )
    log.commit()
    return reader


class FullTextTests(unittest.TestCase):
    def test_the_one_message_that_begins_with_the_preview_is_the_whole_text(self) -> None:
        with _reader((9101, FRIEND_ID, 1, WHOLE, None, 1_700_002_000)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertTrue(notice.truncated)
        self.assertEqual(notice.full_text, WHOLE)
        self.assertEqual(notice.full_text_message_id, 9101)
        rendered = notice.as_json()
        self.assertEqual(rendered["fullText"], WHOLE)
        self.assertEqual(rendered["fullTextMessageId"], "9101")
        self.assertNotIn("fullTextCandidates", rendered)
        self.assertNotIn("fullTextTruncated", rendered)

    def test_a_long_message_says_it_is_itself_cut(self) -> None:
        long = (WHOLE * 20)[: model.LONG_MESSAGE_CHARACTERS]
        rest = json.dumps({"k": "synthetic", "path": "/talkm/synthetic/long.txt", "s": 9000, "sd": True})
        with _reader((9101, FRIEND_ID, 1, long, rest, 1_700_002_000)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.full_text, long)
        self.assertTrue(notice.full_text_truncated)
        self.assertTrue(notice.as_json()["fullTextTruncated"])

    def test_two_that_qualify_are_candidates_and_neither_is_picked(self) -> None:
        with _reader(
            (9101, FRIEND_ID, 1, WHOLE, None, 1_700_002_000),
            (9102, PEER_ID, 1, WHOLE + " 덧붙임", None, 1_700_002_100),
        ) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertTrue(notice.truncated)
        self.assertIsNone(notice.full_text)
        self.assertEqual(notice.full_text_candidates, (9101, 9102))
        self.assertEqual(notice.as_json()["fullTextCandidates"], ["9101", "9102"])
        self.assertNotIn("fullText", notice.as_json())

    def test_a_message_that_only_shares_the_start_is_not_it(self) -> None:
        with _reader((9101, FRIEND_ID, 1, PREVIEW[:50] + " 다른 이야기", None, 1_700_002_000)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertTrue(notice.truncated)
        self.assertIsNone(notice.full_text)
        self.assertEqual(notice.full_text_candidates, ())

    def test_a_covered_message_is_not_offered(self) -> None:
        with _reader((9101, FRIEND_ID, 1, WHOLE, SAFEBOT, 1_700_002_000)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertIsNone(notice.full_text)

    def test_a_short_notice_is_whole_and_nothing_is_looked_up(self) -> None:
        with _reader((9101, FRIEND_ID, 1, "짧은 공지와 그 뒤", None, 1_700_002_000), notice="짧은 공지") as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertFalse(notice.truncated)
        self.assertNotIn("truncated", notice.as_json())
        self.assertIsNone(notice.full_text)

    def test_a_room_whose_key_is_not_in_memory_is_never_opened(self) -> None:
        reader = _reader((9101, FRIEND_ID, 1, WHOLE, None, 1_700_002_000))
        name = paths.chat_log_name(GROUP_ROOM)
        reader._keyring = dataclasses.replace(  # type: ignore[attr-defined]
            reader._keyring,
            keys={
                key: value
                for key, value in reader._keyring.keys.items()  # type: ignore[attr-defined]
                if key not in (name, name.split("/")[-1])
            },
        )
        opened_before = list(reader._cache.opened)  # type: ignore[attr-defined]
        with reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertTrue(notice.truncated)
        self.assertIsNone(notice.full_text)
        self.assertNotIn(name, reader._cache.opened[len(opened_before):])  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
