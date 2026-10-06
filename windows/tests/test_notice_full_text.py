"""A notice cut at the client's 100 characters, and where the rest of it is.

Two places can hold more: the post's share, matched to a board post by its id
and to the older notice row by its words, and the message the notice was
pinned from, found only by its words (exactly one live message beginning with
the stored preview). Every value here is synthetic.
"""

import dataclasses
import json
import unittest

from kanao import model, paths

from tests.extras_fixtures import SAFEBOT, build_extras_reader
from tests.fixtures import FRIEND_ID, GROUP_ROOM, PEER_ID

WHOLE = "가상 공지 전문입니다. " + "합성 문장이 길게 이어집니다. " * 20
PREVIEW = WHOLE[: model.BOARD_PREVIEW_CHARACTERS]
POST_ID = "syntheticPost0000001"


def _share(log_id: int, text: str, post_id: str = POST_ID, room_id: int = GROUP_ROOM) -> tuple:
    """A board share row, in the attachment shape the client writes."""
    attachment = {
        "os": [
            {"st": 1, "u": {"id": FRIEND_ID}, "fc": False, "t": 3},
            {"ct": text, "jct": json.dumps([{"text": text, "type": "text"}]), "t": 1},
            {
                "st": 1,
                "url": f"kakaomoim://post?referer=b&chat_id={room_id}&post_id={post_id}",
                "t": 2,
            },
        ],
        "version": {"win": "0.0.0"},
    }
    return (
        log_id,
        FRIEND_ID,
        model.BOARD_SHARE_MESSAGE,
        "게시글을 공유했습니다",
        json.dumps(attachment),
        1_700_001_000,
    )


def _reader(
    *messages: tuple,
    notice: str = PREVIEW,
    post_id: str = POST_ID,
    legacy: bool = False,
):
    """A reader whose group room's board post carries *notice*, or its older row does."""
    reader = build_extras_reader()
    databases = reader._cache.databases  # type: ignore[attr-defined]
    catalog = databases[paths.CATALOG_DATABASE]
    room = GROUP_ROOM
    if legacy:
        catalog.execute("DELETE FROM moimMetas WHERE chatId = ? AND type = 1", (room,))
        catalog.execute(
            "UPDATE chatMetas SET content = ? WHERE chatId = ? AND type = 1", (notice, room)
        )
    else:
        catalog.execute(
            "UPDATE moimMetas SET content = ? WHERE chatId = ? AND type = 1",
            (
                json.dumps(
                    {
                        "id": post_id,
                        "notice": True,
                        "type": "TEXT",
                        "content": notice,
                        "owner_id": FRIEND_ID,
                        "created_at": "2026-01-04T03:00:00.000Z",
                    }
                ),
                room,
            ),
        )
    catalog.commit()
    log = databases[paths.chat_log_name(room)]
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


class SharedPostTests(unittest.TestCase):
    def test_the_share_of_the_same_post_holds_the_rest(self) -> None:
        with _reader(_share(9201, WHOLE)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.full_text, WHOLE)
        self.assertEqual(notice.full_text_source, "share")
        self.assertFalse(notice.full_text_truncated)
        rendered = notice.as_json()
        self.assertEqual(rendered["fullTextSource"], "share")
        self.assertEqual(rendered["fullTextMessageId"], "9201")
        self.assertNotIn("fullTextTruncated", rendered)

    def test_a_share_of_another_post_is_not_it(self) -> None:
        with _reader(_share(9201, WHOLE, post_id="syntheticPost0000002")) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertIsNone(notice.full_text)

    def test_a_share_from_before_the_post_was_edited_is_not_it(self) -> None:
        with _reader(_share(9201, "고치기 전 문장. " + WHOLE)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertIsNone(notice.full_text)

    def test_a_share_at_its_500_characters_says_it_is_cut(self) -> None:
        cut = (WHOLE * 3)[: model.BOARD_SHARE_CHARACTERS]
        with _reader(_share(9201, cut)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.full_text, cut)
        self.assertTrue(notice.full_text_truncated)

    def test_the_longer_of_the_share_and_the_message_wins(self) -> None:
        long = (WHOLE * 20)[: model.LONG_MESSAGE_CHARACTERS]
        cut = long[: model.BOARD_SHARE_CHARACTERS]
        rest = json.dumps({"k": "synthetic", "path": "/talkm/synthetic/long.txt", "s": 9000, "sd": True})
        with _reader(_share(9201, cut), (9202, FRIEND_ID, 1, long, rest, 1_700_002_000)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.full_text, long)
        self.assertEqual(notice.full_text_source, "message")
        self.assertEqual(notice.full_text_message_id, 9202)

    def test_an_equal_share_wins_over_words_alone(self) -> None:
        with _reader(_share(9201, WHOLE), (9202, FRIEND_ID, 1, WHOLE, None, 1_700_002_000)) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.full_text_source, "share")
        self.assertEqual(notice.full_text_message_id, 9201)


class OlderNoticeRowTests(unittest.TestCase):
    def test_the_older_row_is_cut_at_the_same_100_and_its_message_is_found(self) -> None:
        with _reader((9301, FRIEND_ID, 1, WHOLE, None, 1_700_002_000), legacy=True) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.source, "legacy")
        self.assertTrue(notice.truncated)
        self.assertEqual(notice.full_text, WHOLE)
        self.assertEqual(notice.full_text_source, "message")

    def test_with_no_post_id_a_share_is_matched_by_its_words(self) -> None:
        share = _share(9301, WHOLE)
        with _reader(share, legacy=True) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertEqual(notice.full_text, WHOLE)
        self.assertEqual(notice.full_text_source, "share")

    def test_two_shares_saying_different_things_are_no_answer(self) -> None:
        first = _share(9301, WHOLE)
        second = _share(9302, WHOLE + " 덧붙임", post_id="syntheticPost0000002")
        with _reader(first, second, legacy=True) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertIsNone(notice.full_text)

    def test_a_short_older_row_is_whole(self) -> None:
        with _reader(notice="짧은 예전 공지", legacy=True) as reader:
            notice = reader.notice(GROUP_ROOM)
        assert notice is not None
        self.assertFalse(notice.truncated)
        self.assertNotIn("truncated", notice.as_json())


if __name__ == "__main__":
    unittest.main()
