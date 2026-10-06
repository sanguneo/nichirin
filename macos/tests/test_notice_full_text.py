"""A notice row cut at 100 characters, and the rest of it found in the room.

The client keeps only the first 100 characters of a notice in ``NTChatMeta``.
The rest may still be in the room: in the message the notice was pinned from,
or in the board post's share (type 98). Every row here is synthetic.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from typing import Optional

from kamado import cli
from kamado.reader import Reader

from tests.extras_fixtures import NOTICE_TEXT, build_notice_reader
from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, PEER_ID

CUT = ("이번 달 정기 모임은 셋째 주 토요일 오후 세 시입니다. " * 5)[:100]
assert len(CUT) == 100

SHARE_TYPE = 98
REPLY_TYPE = 26
POST_LINK = "kakaomoim://post?chat_id=1002&post_id=8800123"


def share(body: str, url: Optional[str] = POST_LINK, **extra: object) -> str:
    segments: list[dict[str, object]] = [{"t": 1, "ct": body}]
    if url is not None:
        segments.append({"t": 2, "st": 1, "url": url})
    return json.dumps({"os": segments, **extra})


def longer(length: int) -> str:
    return (CUT + "가" * length)[:length]


class NoticeFullTextTests(unittest.TestCase):
    def build(self, notice: str = CUT, rows: tuple[tuple, ...] = ()) -> Reader:
        """The extras reader with the group room's notice set to *notice* and *rows* added.

        A row is ``(logId, chatId, type, message, attachment, deleted)``.
        """
        reader = build_notice_reader(notice, rows)
        self.addCleanup(reader.close)
        return reader

    def answer(self, notice: str = CUT, rows: tuple[tuple, ...] = ()) -> dict:
        pinned = self.build(notice, rows).notice(GROUP_ROOM)
        assert pinned is not None
        return pinned.as_json()

    def full_text(self, answer: dict) -> dict:
        return {key: value for key, value in answer.items() if key.startswith("fullText")}

    def test_a_short_notice_is_answered_as_before(self) -> None:
        answer = self.answer(NOTICE_TEXT, ((9101, GROUP_ROOM, 1, NOTICE_TEXT + " 더", None, 0),))
        self.assertEqual(
            set(answer), {"text", "kind", "authorId", "author", "postedAt", "source"}
        )
        self.assertEqual(answer["text"], NOTICE_TEXT)

    def test_a_cut_notice_with_nothing_more_in_the_room_is_only_marked(self) -> None:
        answer = self.answer()
        self.assertIs(answer["truncated"], True)
        self.assertEqual(answer["text"], CUT)
        self.assertEqual(self.full_text(answer), {})

    def test_a_message_that_is_only_the_notice_holds_no_more_of_it(self) -> None:
        answer = self.answer(rows=((9101, GROUP_ROOM, 1, CUT, None, 0),))
        self.assertEqual(self.full_text(answer), {})

    def test_the_one_message_beginning_with_the_notice_gives_the_rest(self) -> None:
        whole = CUT + " 장소는 지난번과 같습니다."
        answer = self.answer(rows=((9101, GROUP_ROOM, 1, whole, None, 0),))
        self.assertEqual(
            self.full_text(answer),
            {"fullText": whole, "fullTextMessageId": "9101", "fullTextSource": "message"},
        )

    def test_a_reply_beginning_with_the_notice_counts_as_a_message(self) -> None:
        whole = CUT + " 답장으로 남긴 전문"
        answer = self.answer(rows=((9101, GROUP_ROOM, REPLY_TYPE, whole, None, 0),))
        self.assertEqual(answer["fullTextMessageId"], "9101")

    def test_a_message_that_only_resembles_the_notice_is_not_it(self) -> None:
        answer = self.answer(
            rows=(
                (9101, GROUP_ROOM, 1, CUT[:99] + "!" + " 이어지는 말", None, 0),
                (9102, GROUP_ROOM, 1, "앞에 붙은 말 " + CUT + " 이어지는 말", None, 0),
            )
        )
        self.assertEqual(self.full_text(answer), {})

    def test_a_message_in_another_room_is_not_this_room_s_notice(self) -> None:
        answer = self.answer(rows=((9101, DIRECT_ROOM, 1, CUT + " 다른 방", None, 0),))
        self.assertEqual(self.full_text(answer), {})
        self.assertNotIn("fullTextCandidates", answer)

    def test_two_messages_are_refused_with_their_ids(self) -> None:
        answer = self.answer(
            rows=(
                (9101, GROUP_ROOM, 1, CUT + " 첫 번째", None, 0),
                (9102, GROUP_ROOM, 1, CUT + " 두 번째", None, 0),
            )
        )
        self.assertEqual(answer["fullTextCandidates"], ["9101", "9102"])
        self.assertEqual(self.full_text(answer), {"fullTextCandidates": ["9101", "9102"]})

    def test_at_most_six_candidates_are_named(self) -> None:
        rows = tuple((9101 + n, GROUP_ROOM, 1, CUT + f" {n}번째", None, 0) for n in range(8))
        answer = self.answer(rows=rows)
        self.assertEqual(answer["fullTextCandidates"], [str(9101 + n) for n in range(6)])

    def test_a_message_at_the_client_s_limit_is_marked_cut(self) -> None:
        for length, cut in ((3999, True), (3998, False)):
            with self.subTest(length=length):
                answer = self.answer(rows=((9101, GROUP_ROOM, 1, longer(length), None, 0),))
                self.assertEqual(answer["fullText"], longer(length))
                self.assertEqual(answer.get("fullTextTruncated"), True if cut else None)

    def test_deleted_erased_and_covered_messages_are_not_sources(self) -> None:
        covered = json.dumps({"safeBot": True})
        answer = self.answer(
            rows=(
                (9101, GROUP_ROOM, 1, CUT + " 지운 줄", None, 1),
                (9102, GROUP_ROOM, 1 | 0x4000, CUT + " 모두에게서 지운 줄", None, 0),
                (9103, GROUP_ROOM, 1, CUT + " 가려진 줄", covered, 0),
                (9104, GROUP_ROOM, SHARE_TYPE, "", share(CUT + " 가려진 공유", safeBot=True), 0),
                (9105, GROUP_ROOM, SHARE_TYPE, "", share(CUT + " 지운 공유"), 1),
            )
        )
        self.assertEqual(self.full_text(answer), {})

    def test_a_hidden_message_does_not_make_a_visible_one_ambiguous(self) -> None:
        answer = self.answer(
            rows=(
                (9101, GROUP_ROOM, 1 | 0x4000, CUT + " 지운 줄", None, 0),
                (9102, GROUP_ROOM, 1, CUT + " 남은 줄", None, 0),
            )
        )
        self.assertEqual(
            self.full_text(answer),
            {"fullText": CUT + " 남은 줄", "fullTextMessageId": "9102", "fullTextSource": "message"},
        )

    def test_a_share_naming_a_post_and_beginning_with_the_notice_gives_the_rest(self) -> None:
        whole = CUT + " 게시판 본문의 나머지"
        answer = self.answer(rows=((9101, GROUP_ROOM, SHARE_TYPE, "", share(whole), 0),))
        self.assertEqual(
            self.full_text(answer),
            {"fullText": whole, "fullTextMessageId": "9101", "fullTextSource": "share"},
        )

    def test_a_share_at_the_500_it_carries_is_marked_cut(self) -> None:
        for length, cut in ((500, True), (499, False)):
            with self.subTest(length=length):
                answer = self.answer(
                    rows=((9101, GROUP_ROOM, SHARE_TYPE, "", share(longer(length)), 0),)
                )
                self.assertEqual(answer["fullTextSource"], "share")
                self.assertEqual(answer.get("fullTextTruncated"), True if cut else None)

    def test_a_share_of_another_text_is_not_this_notice(self) -> None:
        for body in (CUT[:99] + "?" + " 고친 글", "다른 게시글 " + CUT):
            with self.subTest(body=body[:5]):
                answer = self.answer(rows=((9101, GROUP_ROOM, SHARE_TYPE, "", share(body), 0),))
                self.assertEqual(self.full_text(answer), {})

    def test_a_share_in_another_room_is_not_this_room_s_notice(self) -> None:
        answer = self.answer(
            rows=((9101, DIRECT_ROOM, SHARE_TYPE, "", share(CUT + " 다른 방"), 0),)
        )
        self.assertEqual(self.full_text(answer), {})

    def test_a_share_that_names_no_post_or_cannot_be_read_is_ignored(self) -> None:
        body = CUT + " 본문"
        malformed = {
            "no link": share(body, url=None),
            "no post id": share(body, url="kakaomoim://post?chat_id=1002"),
            "empty post id": share(body, url="kakaomoim://post?chat_id=1002&post_id="),
            "another scheme": share(body, url="https://example.com/post?post_id=1"),
            "another kind of link": share(body, url="kakaomoim://vote?post_id=1"),
            "invalid URI": share(body, url="kakaomoim://[broken?post_id=1"),
            "not json": "{not json",
            "segments not a list": json.dumps({"os": {"t": 1, "ct": body}}),
            "no text segment": json.dumps({"os": [{"t": 2, "st": 1, "url": POST_LINK}]}),
        }
        for label, attachment in malformed.items():
            with self.subTest(label=label):
                answer = self.answer(rows=((9101, GROUP_ROOM, SHARE_TYPE, "", attachment, 0),))
                self.assertEqual(self.full_text(answer), {})

    def test_a_post_link_on_an_ordinary_message_is_not_a_share(self) -> None:
        answer = self.answer(rows=((9101, GROUP_ROOM, 1, "", share(CUT + " 본문"), 0),))
        self.assertEqual(self.full_text(answer), {})

    def test_the_longer_source_wins_and_a_tie_goes_to_the_share(self) -> None:
        cases = {
            "message longer": (longer(300), longer(200), "message", "9101"),
            "share longer": (longer(200), longer(300), "share", "9102"),
            "equal": (longer(250), longer(250), "share", "9102"),
        }
        for label, (said, shared, source, log_id) in cases.items():
            with self.subTest(label=label):
                answer = self.answer(
                    rows=(
                        (9101, GROUP_ROOM, 1, said, None, 0),
                        (9102, GROUP_ROOM, SHARE_TYPE, "", share(shared), 0),
                    )
                )
                self.assertEqual(
                    (answer["fullTextSource"], answer["fullTextMessageId"]), (source, log_id)
                )

    def test_ambiguous_messages_leave_the_share_to_answer(self) -> None:
        answer = self.answer(
            rows=(
                (9101, GROUP_ROOM, 1, longer(400), None, 0),
                (9102, GROUP_ROOM, 1, longer(401), None, 0),
                (9103, GROUP_ROOM, SHARE_TYPE, "", share(longer(200)), 0),
            )
        )
        self.assertEqual(
            (answer["fullTextSource"], answer["fullTextMessageId"], answer["fullText"]),
            ("share", "9103", longer(200)),
        )
        self.assertEqual(answer["fullTextCandidates"], ["9101", "9102"])
        self.assertNotIn(answer["fullTextMessageId"], answer["fullTextCandidates"])

    def test_two_shares_are_refused_even_when_one_is_longer(self) -> None:
        answer = self.answer(rows=(
            (9101, GROUP_ROOM, SHARE_TYPE, "", share(longer(200)), 0),
            (9102, GROUP_ROOM, SHARE_TYPE, "", share(longer(300)), 0),
        ))
        self.assertEqual(self.full_text(answer), {"fullTextCandidates": ["9101", "9102"]})

    def test_repeated_identical_shares_are_still_ambiguous_rows(self) -> None:
        body = share(longer(200))
        answer = self.answer(rows=(
            (9101, GROUP_ROOM, SHARE_TYPE, "", body, 0),
            (9102, GROUP_ROOM, SHARE_TYPE, "", body, 0),
        ))
        self.assertEqual(self.full_text(answer), {"fullTextCandidates": ["9101", "9102"]})

    def test_ambiguous_shares_leave_the_unique_message_to_answer(self) -> None:
        answer = self.answer(rows=(
            (9101, GROUP_ROOM, SHARE_TYPE, "", share(longer(400)), 0),
            (9102, GROUP_ROOM, SHARE_TYPE, "", share(longer(401)), 0),
            (9103, GROUP_ROOM, 1, longer(200), None, 0),
        ))
        self.assertEqual(
            (answer["fullTextSource"], answer["fullTextMessageId"]), ("message", "9103")
        )
        self.assertEqual(answer["fullTextCandidates"], ["9101", "9102"])
        self.assertNotIn(answer["fullTextMessageId"], answer["fullTextCandidates"])

    def test_both_ambiguous_sources_name_the_oldest_six_ids(self) -> None:
        rows = tuple(
            (9101 + n, GROUP_ROOM, SHARE_TYPE if n % 2 else 1,
             "" if n % 2 else longer(200),
             share(longer(300)) if n % 2 else None, 0)
            for n in range(8)
        )
        answer = self.answer(rows=rows)
        self.assertEqual(
            self.full_text(answer),
            {"fullTextCandidates": [str(9101 + n) for n in range(6)]},
        )

    def test_the_notice_command_carries_the_rest(self) -> None:
        reader = self.build(rows=((9101, GROUP_ROOM, 1, CUT + " 명령에서도", None, 0),))
        output = io.StringIO()
        with redirect_stdout(output):
            cli.run(cli.build_parser().parse_args(["notice", "--room-id", str(GROUP_ROOM)]), reader)
        notice = json.loads(output.getvalue())["notice"]
        self.assertEqual(
            (notice["truncated"], notice["fullText"], notice["fullTextMessageId"]),
            (True, CUT + " 명령에서도", "9101"),
        )


if __name__ == "__main__":
    unittest.main()
