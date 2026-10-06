"""Topic flow tests: where a conversation turns, what each stretch is called, which lines carry it.

Every conversation here is invented. The vocabularies are kept apart on
purpose — a release, a dinner, a trip — so a test that expects a turn is
testing the scoring, not luck in the wording.

The cut is `kakaocommon.topics`, which both builds run unchanged, so this file
is kept in both suites as `test_wal.py` is: each feeds it its own `Message`.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# The build first: importing it is what puts `kakaocommon` on the path from a
# checkout, so this file also runs on its own.
from kamado.model import FEED_MESSAGE, Message, Photo, Quote
from kamado.tui import digest
from kamado.tui.layout import ELLIPSIS, truncate_to_width

from kakaocommon import topics, words

NOW = int(datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc).timestamp())
ROOM = 7

RELEASE = [
    "배포 스크립트 리뷰 부탁해요",
    "배포 일정은 목요일 맞나요",
    "스크립트 수정했고 배포 전에 리뷰 필요",
    "목요일 배포 확정입니다",
    "리뷰 끝나면 스크립트 머지",
    "배포 목요일 오후 진행",
]
DINNER = [
    "저녁 메뉴 국밥 어때",
    "국밥 좋다 저녁 예약 가능",
    "메뉴 국밥으로 확정",
    "저녁 국밥집 예약 완료",
    "국밥 메뉴 찬성",
    "예약 시간 저녁 일곱시",
]
TRIP = [
    "주말 캠핑 장소 골라보자",
    "캠핑 장비 텐트 챙겨",
    "텐트 두개 캠핑장 예약",
    "주말 캠핑장 날씨 맑음",
    "장비 목록 텐트 버너",
    "캠핑 주말 출발 아침",
]


def message(
    log_id: int,
    minutes_ago: float,
    text: str,
    sender: str = "가",
    reply_to: int | None = None,
    **extra: object,
) -> Message:
    return Message(
        id=log_id,
        room_id=ROOM,
        sender_id=abs(hash(sender)) % 10_000,
        sender=sender,
        text=text,
        type=26 if reply_to is not None else 1,
        sent_at=NOW - int(minutes_ago * 60),
        from_me=sender == "나",
        reply=Quote(reply_to, 1, "가", "", 1) if reply_to is not None else None,
        **extra,  # type: ignore[arg-type]
    )


def run(lines: list[str], first_id: int, start_ago: float, step: float = 2) -> list[Message]:
    """A stretch of conversation, one line every *step* minutes, speakers alternating."""
    return [
        message(first_id + index, start_ago - index * step, text, "가나다"[index % 3])
        for index, text in enumerate(lines)
    ]


def ids(topic: topics.Topic) -> list[int]:
    return [one.id for one in topic.messages]


def holding(flow: topics.TopicFlow, log_id: int) -> topics.Topic:
    return next(topic for topic in flow.topics if log_id in ids(topic))


class SegmentationTests(unittest.TestCase):
    def test_a_long_silence_ends_a_topic_even_when_the_words_continue(self) -> None:
        before = run(RELEASE, 1, 100)
        after = run(RELEASE, 101, 60)
        flow = topics.topic_flow(before + after, NOW)
        self.assertEqual([ids(topic) for topic in flow.topics], [ids_of(before), ids_of(after)])

    def test_a_change_of_subject_with_no_pause_is_a_boundary(self) -> None:
        conversation = run(RELEASE, 1, 60) + run(DINNER, 101, 48)
        flow = topics.topic_flow(conversation, NOW)
        self.assertEqual(len(flow.topics), 2)
        self.assertEqual(ids(flow.topics[0]), list(range(1, 7)))
        self.assertEqual(ids(flow.topics[1]), list(range(101, 107)))

    def test_a_reply_across_the_turn_keeps_it_with_what_it_answered(self) -> None:
        plain = run(RELEASE, 1, 60) + run(DINNER, 101, 48)
        self.assertIsNot(holding(topics.topic_flow(plain, NOW), 6), holding(topics.topic_flow(plain, NOW), 101))
        answered = list(plain)
        answered[6] = replace(answered[6], type=26, reply=Quote(6, 1, "가", RELEASE[5], 1))
        flow = topics.topic_flow(answered, NOW)
        self.assertIn(6, ids(holding(flow, 101)))

    def test_a_reply_after_a_silence_continues_the_topic(self) -> None:
        conversation = run(RELEASE, 1, 90) + [message(50, 50, "그 스크립트 저도 봤어요", reply_to=6)]
        conversation += run(RELEASE[:4], 51, 48)
        flow = topics.topic_flow(conversation, NOW)
        self.assertEqual(len(flow.topics), 1)

    def test_a_scrap_too_small_to_be_a_topic_joins_its_neighbour(self) -> None:
        conversation = run(RELEASE, 1, 60) + run(DINNER[:2], 101, 48)
        flow = topics.topic_flow(conversation, NOW)
        self.assertEqual(len(flow.topics), 1)
        self.assertEqual(flow.topics[0].count, 8)

    def test_topics_are_capped_by_joining_the_most_alike(self) -> None:
        # Eleven four-line exchanges, each after a silence, none sharing a word.
        conversation: list[Message] = []
        for group in range(11):
            lines = [f"{word}{group}" for word in ("회의", "자료", "정리", "공유")]
            conversation += run(lines, group * 10 + 1, 230 - group * 20, step=1)
        flow = topics.topic_flow(conversation, NOW, window=timedelta(hours=4))
        self.assertEqual(len(flow.topics), topics.MAX_TOPICS)
        self.assertEqual(sum(topic.count for topic in flow.topics), len(conversation))

    def test_topics_come_back_oldest_first_and_cover_every_message(self) -> None:
        conversation = run(RELEASE, 1, 100) + run(DINNER, 101, 70) + run(TRIP, 201, 40)
        flow = topics.topic_flow(conversation, NOW)
        starts = [topic.start for topic in flow.topics]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(sorted(one for topic in flow.topics for one in ids(topic)), ids_of(conversation))

    def test_the_same_conversation_always_cuts_the_same_way(self) -> None:
        conversation = run(RELEASE, 1, 100) + run(DINNER, 101, 70) + run(TRIP, 201, 40)
        first = topics.topic_flow(conversation, NOW)
        second = topics.topic_flow(list(reversed(conversation)), NOW)
        self.assertEqual(
            [(ids(topic), topic.keywords, topic.label) for topic in first.topics],
            [(ids(topic), topic.keywords, topic.label) for topic in second.topics],
        )

    def test_erased_and_feed_rows_are_not_part_of_any_topic(self) -> None:
        conversation = run(RELEASE, 1, 60)
        conversation.append(message(90, 30, "", erased=True))
        conversation.append(replace(message(91, 29, '{"feedType": 4}'), type=0))
        flow = topics.topic_flow(conversation, NOW)
        held = [one for topic in flow.topics for one in ids(topic)]
        self.assertNotIn(90, held)
        self.assertNotIn(91, held)


class WordTests(unittest.TestCase):
    def test_particles_are_folded_so_one_noun_counts_once(self) -> None:
        lines = ["회의는 몇시", "회의를 미루자", "회의 자료", "회의가 길다", "자료 공유", "자료는 내일"]
        flow = topics.topic_flow(run(lines, 1, 30), NOW)
        self.assertEqual(flow.topics[0].keywords[0], "회의")
        self.assertIn("자료", flow.topics[0].keywords)
        self.assertNotIn("회의는", flow.topics[0].keywords)

    def test_a_noun_that_merely_ends_like_a_particle_is_left_alone(self) -> None:
        lines = ["고양이 사료 주문", "고양이 간식", "사료 도착", "고양이 사료 좋아함"]
        flow = topics.topic_flow(run(lines, 1, 30), NOW)
        self.assertIn("고양이", flow.topics[0].keywords)
        self.assertNotIn("고양", flow.topics[0].keywords)

    def test_attachment_placeholders_never_name_a_topic(self) -> None:
        photo = Photo(url="https://example.invalid/p.jpg", width=10, height=10)
        conversation = [message(index, 30 - index, "사진", photo=photo) for index in range(1, 6)]
        conversation += [message(10 + index, 20 - index, "사진 사진 보내요") for index in range(3)]
        flow = topics.topic_flow(conversation, NOW)
        self.assertEqual(len(flow.topics), 1)
        self.assertNotIn("사진", flow.topics[0].keywords)
        self.assertEqual(flow.topics[0].count, 8)

    def test_a_topic_without_keywords_is_named_by_its_first_line(self) -> None:
        lines = ["안녕하세요 반가워요", "네 안녕", "오랜만", "그러게"]
        flow = topics.topic_flow(run(lines, 1, 30), NOW)
        self.assertEqual(flow.topics[0].keywords, ())
        self.assertTrue(flow.topics[0].label.startswith("안녕하세요"))

    def test_a_label_is_its_top_keywords(self) -> None:
        flow = topics.topic_flow(run(RELEASE, 1, 30), NOW)
        self.assertEqual(flow.topics[0].label, " · ".join(flow.topics[0].keywords[:3]))
        self.assertIn("배포", flow.topics[0].keywords)


class WindowTests(unittest.TestCase):
    def test_a_quiet_room_falls_back_to_the_window_ending_at_its_last_message(self) -> None:
        conversation = run(RELEASE, 1, 300)
        flow = topics.topic_flow(conversation, NOW)
        self.assertTrue(flow.fallback)
        newest = datetime.fromtimestamp(conversation[-1].sent_at, timezone.utc)  # type: ignore[arg-type]
        self.assertEqual(flow.end, newest)
        self.assertEqual(flow.start, newest - topics.TOPIC_WINDOW)
        self.assertEqual(sum(topic.count for topic in flow.topics), 6)

    def test_an_empty_room_has_no_topics(self) -> None:
        flow = topics.topic_flow([], NOW)
        self.assertEqual(flow.topics, [])
        self.assertFalse(flow.fallback)

    def test_a_topic_that_comes_back_points_at_where_it_was(self) -> None:
        conversation = run(RELEASE, 1, 110) + run(DINNER, 101, 80) + run(RELEASE, 201, 40)
        flow = topics.topic_flow(conversation, NOW)
        self.assertEqual(len(flow.topics), 3)
        self.assertIsNone(flow.topics[0].resumes)
        self.assertIsNone(flow.topics[1].resumes)
        self.assertEqual(flow.topics[2].resumes, 0)

    def test_the_window_count_is_the_nominal_two_hours(self) -> None:
        conversation = run(RELEASE, 1, 130) + run(DINNER, 101, 40)
        flow = topics.topic_flow(conversation, NOW)
        inside = [one for one in conversation if one.sent_at >= NOW - 7200]  # type: ignore[operator]
        self.assertEqual(flow.count, len(inside))


class ExtensionTests(unittest.TestCase):
    """A topic already running at the window's start is followed back to where it began."""

    def test_a_topic_that_began_before_the_window_is_kept_from_its_real_start(self) -> None:
        running = run(RELEASE + RELEASE, 1, 150, step=4)  # 150 to 106 minutes ago
        flow = topics.topic_flow(running, NOW)
        self.assertEqual(len(flow.topics), 1)
        first = flow.topics[0]
        self.assertEqual(first.messages[0].id, 1)
        self.assertTrue(first.extends_before)
        self.assertFalse(first.truncated)
        # The window itself is still the two hours that were asked about.
        self.assertEqual(flow.start, datetime.fromtimestamp(NOW, timezone.utc) - topics.TOPIC_WINDOW)

    def test_a_topic_that_ended_before_the_window_is_left_out(self) -> None:
        finished = run(DINNER, 1, 175)  # 175 to 165 minutes ago
        straddling = run(RELEASE, 101, 135, step=5)  # 135 to 110: thirty silent minutes after the last
        flow = topics.topic_flow(finished + straddling, NOW)
        self.assertEqual([ids(topic) for topic in flow.topics], [ids_of(straddling)])
        self.assertTrue(flow.topics[0].extends_before)

    def test_a_reply_into_the_earlier_stretch_pulls_the_start_back(self) -> None:
        # A trip before the window, then the release talk crossing its start
        # with no pause: on words alone the topic starts where the release does.
        asked = run(TRIP, 1, 140)  # 140 to 130 minutes ago
        answer = run(RELEASE, 101, 128)  # 128 to 118: across the window's start
        plain = topics.topic_flow(asked + answer, NOW)
        self.assertEqual(plain.topics[0].messages[0].id, 101)
        # Answering the trip from inside the window, within reach, brings
        # what it answered into the topic.
        answer[5] = replace(answer[5], type=26, reply=Quote(4, 1, "가", TRIP[3], 1))
        first = topics.topic_flow(asked + answer, NOW).topics[0]
        self.assertLessEqual(first.messages[0].id, 4)
        self.assertTrue(first.extends_before)

    def test_a_reply_to_something_hours_old_does_not_swallow_what_came_between(self) -> None:
        # Release 08:30-08:40, trip 09:00-09:10, dinner 10:30-10:40, now 12:30,
        # and one dinner line answering the 08:30 message. Dinner alone is the
        # last two hours; the reply is a topic coming back, not one topic.
        release = run(RELEASE, 1, 240)
        trip = run(TRIP, 101, 210)
        dinner = run(DINNER, 201, 120)
        dinner[3] = replace(dinner[3], type=26, reply=Quote(1, 1, "가", RELEASE[0], 1))
        flow = topics.topic_flow(release + trip + dinner, NOW)
        self.assertEqual([ids(topic) for topic in flow.topics], [ids_of(dinner)])
        self.assertFalse(flow.topics[0].extends_before)

    def test_a_topic_still_running_at_the_cap_is_marked_truncated(self) -> None:
        endless = run((RELEASE * 10)[:60], 1, 300, step=5)  # no silence for five hours
        flow = topics.topic_flow(endless, NOW)
        first = flow.topics[0]
        horizon = datetime.fromtimestamp(NOW, timezone.utc) - topics.TOPIC_WINDOW - topics.TOPIC_EXTENSION_MAX
        self.assertTrue(first.extends_before)
        self.assertTrue(first.truncated)
        self.assertGreaterEqual(first.start, horizon)
        self.assertLess(first.start - horizon, timedelta(minutes=5))

    def test_history_the_caller_could_not_load_marks_it_truncated_too(self) -> None:
        running = run(RELEASE + RELEASE, 1, 150, step=4)
        self.assertFalse(topics.topic_flow(running, NOW, complete=True).topics[0].truncated)
        self.assertTrue(topics.topic_flow(running, NOW, complete=False).topics[0].truncated)

    def test_nothing_straddling_the_start_changes_nothing(self) -> None:
        earlier = run(TRIP, 1, 200)
        inside = run(RELEASE, 101, 100) + run(DINNER, 201, 60)
        with_history = topics.topic_flow(earlier + inside, NOW)
        without = topics.topic_flow(inside, NOW)
        self.assertEqual(
            [ids(topic) for topic in with_history.topics], [ids(topic) for topic in without.topics]
        )
        self.assertFalse(any(topic.extends_before for topic in with_history.topics))

    def test_the_window_edge_is_not_a_cut_in_itself(self) -> None:
        crossing = run(RELEASE + RELEASE, 1, 130, step=2)  # 130 to 108 minutes ago
        flow = topics.topic_flow(crossing, NOW)
        self.assertEqual([ids(topic) for topic in flow.topics], [ids_of(crossing)])

    def test_the_fallback_window_follows_the_same_rule(self) -> None:
        running = run(RELEASE + RELEASE, 1, 500, step=4)  # 500 to 456 minutes ago
        flow = topics.topic_flow(running, NOW)
        self.assertTrue(flow.fallback)
        self.assertEqual(ids(flow.topics[0]), ids_of(running))
        self.assertFalse(flow.topics[0].extends_before)

    def test_lookback_reaches_past_the_window_by_the_cap(self) -> None:
        start = topics.lookback_start(run(RELEASE, 1, 30), NOW)
        self.assertEqual(
            start,
            datetime.fromtimestamp(NOW, timezone.utc) - topics.TOPIC_WINDOW - topics.TOPIC_EXTENSION_MAX,
        )
        self.assertIsNone(topics.lookback_start([], NOW))


class KeyMessageTests(unittest.TestCase):
    def test_key_messages_come_back_in_the_order_they_were_sent(self) -> None:
        flow = topics.topic_flow(run(RELEASE, 1, 30), NOW)
        chosen = flow.topics[0].key_messages(limit=3)
        self.assertEqual([one.id for one in chosen], sorted(one.id for one in chosen))
        self.assertEqual(len(chosen), 3)

    def test_an_answered_line_beats_a_long_one(self) -> None:
        conversation = [
            message(1, 30, "배포 언제"),
            message(2, 29, "아 그리고 오늘 날씨가 정말 좋아서 산책을 다녀왔는데 공원에 사람이 아주 많았고 벚꽃이 예뻤어요"),
            message(3, 28, "목요일 오후", reply_to=1),
            message(4, 27, "목요일 좋아요", reply_to=1),
            message(5, 26, "배포 목요일 확정"),
        ]
        chosen = topics.topic_flow(conversation, NOW).topics[0].key_messages(limit=2)
        self.assertIn(1, [one.id for one in chosen])
        self.assertNotIn(2, [one.id for one in chosen])

    def test_a_wall_of_text_does_not_win_on_length_alone(self) -> None:
        wall = "아무 상관 없는 이야기 " * 40
        conversation = run(RELEASE, 1, 30) + [message(50, 10, wall)]
        chosen = topics.topic_flow(conversation, NOW).topics[0].key_messages(limit=2)
        self.assertNotIn(50, [one.id for one in chosen])

    def test_key_messages_skip_covered_and_textless_rows(self) -> None:
        photo = Photo(url="https://example.invalid/p.jpg", width=10, height=10)
        conversation = run(RELEASE, 1, 30) + [
            message(60, 15, "배포 배포 배포 스크립트 리뷰", covered="safebot"),
            message(61, 14, "", photo=photo),
        ]
        chosen = topics.topic_flow(conversation, NOW).topics[0].key_messages(limit=10)
        self.assertNotIn(60, [one.id for one in chosen])
        self.assertNotIn(61, [one.id for one in chosen])

    def test_speakers_name_the_reader_as_on_screen(self) -> None:
        conversation = run(RELEASE, 1, 30) + [message(70, 5, "배포 확인", sender="나")]
        speakers = dict(topics.topic_flow(conversation, NOW).topics[0].speakers)
        self.assertIn("나", speakers)


class SharedModuleTests(unittest.TestCase):
    """`kakaocommon.topics` restates what it cannot import from this build."""

    def test_its_feed_code_and_ellipsis_are_this_build_s(self) -> None:
        self.assertEqual(topics.FEED_MESSAGE, FEED_MESSAGE)
        self.assertEqual(topics.ELLIPSIS, ELLIPSIS)

    def test_the_digest_counts_the_same_words(self) -> None:
        self.assertIs(digest._WORD, words.WORD)
        self.assertIs(digest._STOPWORDS, words.STOPWORDS)
        self.assertIs(digest._PLACEHOLDERS, words.PLACEHOLDERS)

    def test_a_label_cut_from_a_line_is_cut_as_the_layout_cuts(self) -> None:
        for said in ("안녕하세요 오늘 회의는 몇 시에 하나요 궁금합니다", "짧은 말", "a" * 40, "가족 👨‍👩‍👧 사진 ❤️ " * 3):
            with self.subTest(said=said):
                self.assertEqual(
                    topics._shorten(said, topics.LABEL_COLUMNS),
                    truncate_to_width(said, topics.LABEL_COLUMNS, ELLIPSIS),
                )


def ids_of(messages: list[Message]) -> list[int]:
    return sorted(one.id for one in messages)


if __name__ == "__main__":
    unittest.main()
