"""Digest tests: slicing by time, and describing a slice without inventing it."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from kamado.model import Message
from kamado.tui import digest

BASE = int(datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc).timestamp())
ROOM = 7


def message(offset_minutes: float, sender: str, text: str, log_id: int) -> Message:
    return Message(
        id=log_id,
        room_id=ROOM,
        sender_id=abs(hash(sender)) % 10_000,
        sender=sender,
        text=text,
        type=1,
        sent_at=BASE + int(offset_minutes * 60),
        from_me=sender == "나",
    )


class DurationTests(unittest.TestCase):
    def test_minutes_and_hours_are_both_accepted(self) -> None:
        self.assertEqual(digest.parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(digest.parse_duration("2h"), timedelta(hours=2))

    def test_spacing_and_case_do_not_matter(self) -> None:
        self.assertEqual(digest.parse_duration("  90M "), timedelta(minutes=90))

    def test_twelve_hours_is_the_documented_ceiling(self) -> None:
        self.assertEqual(digest.parse_duration("12h"), timedelta(hours=12))

    def test_anything_longer_than_the_ceiling_is_refused(self) -> None:
        with self.assertRaises(digest.DurationError):
            digest.parse_duration("13h")

    def test_zero_and_negative_spans_are_refused(self) -> None:
        for text in ("0m", "0h"):
            with self.subTest(text=text), self.assertRaises(digest.DurationError):
                digest.parse_duration(text)

    def test_unparseable_text_is_refused_with_an_example(self) -> None:
        for text in ("", "soon", "30", "30s", "2d"):
            with self.subTest(text=text), self.assertRaises(digest.DurationError) as raised:
                digest.parse_duration(text)
            self.assertIn("30m", str(raised.exception))


class SlicingTests(unittest.TestCase):
    def test_an_empty_conversation_yields_no_slices(self) -> None:
        self.assertEqual(digest.slice_messages([], timedelta(minutes=30)), [])

    def test_messages_inside_one_window_form_a_single_slice(self) -> None:
        messages = [message(index * 2, "a", "hi", index) for index in range(5)]
        slices = digest.slice_messages(messages, timedelta(minutes=30))
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].count, 5)

    def test_messages_spread_over_hours_split_into_separate_slices(self) -> None:
        messages = [message(index * 45, "a", "hi", index) for index in range(5)]
        slices = digest.slice_messages(messages, timedelta(minutes=30))
        self.assertGreater(len(slices), 1)
        self.assertEqual(sum(piece.count for piece in slices), 5)

    def test_slices_are_returned_oldest_first(self) -> None:
        messages = [message(index * 45, "a", "hi", index) for index in range(5)]
        slices = digest.slice_messages(messages, timedelta(minutes=30))
        starts = [piece.start for piece in slices]
        self.assertEqual(starts, sorted(starts))

    def test_nothing_older_than_the_horizon_is_included(self) -> None:
        messages = [message(-20 * 60, "a", "ancient", 1), message(0, "a", "recent", 2)]
        slices = digest.slice_messages(messages, timedelta(hours=1))
        kept = [m.text for piece in slices for m in piece.messages]
        self.assertEqual(kept, ["recent"])

    def test_messages_without_a_timestamp_are_skipped_rather_than_guessed(self) -> None:
        timed = message(0, "a", "timed", 1)
        untimed = Message(2, ROOM, 1, "a", "untimed", 1, None, False)
        slices = digest.slice_messages([timed, untimed], timedelta(minutes=30))
        kept = [m.text for piece in slices for m in piece.messages]
        self.assertEqual(kept, ["timed"])

    def test_every_message_lands_in_exactly_one_slice(self) -> None:
        messages = [message(index * 7, "a", f"m{index}", index) for index in range(40)]
        slices = digest.slice_messages(messages, timedelta(minutes=30))
        ids = [m.id for piece in slices for m in piece.messages]
        self.assertEqual(sorted(ids), sorted(m.id for m in messages))
        self.assertEqual(len(ids), len(set(ids)))


class DescriptionTests(unittest.TestCase):
    def slice_of(self, messages) -> digest.Slice:
        slices = digest.slice_messages(messages, timedelta(hours=1))
        self.assertEqual(len(slices), 1)
        return slices[0]

    def test_speakers_are_ranked_by_how_much_they_said(self) -> None:
        messages = [message(index, "많이", "x", index) for index in range(5)]
        messages.append(message(6, "조금", "y", 99))
        piece = self.slice_of(messages)
        self.assertEqual(piece.speakers[0][0], "많이")
        self.assertEqual(piece.speakers[0][1], 5)

    def test_filler_words_are_not_reported_as_topics(self) -> None:
        messages = [
            message(index, "a", "그리고 그래서 배포 배포 그냥", index) for index in range(4)
        ]
        words = [word for word, _count in self.slice_of(messages).keywords]
        self.assertIn("배포", words)
        self.assertNotIn("그리고", words)
        self.assertNotIn("그냥", words)

    def test_highlights_prefer_substantive_lines(self) -> None:
        messages = [
            message(0, "a", "ㅇㅇ", 1),
            message(1, "b", "배포는 목요일 오후에 진행하기로 했습니다", 2),
            message(2, "c", "ㅋㅋ", 3),
        ]
        highlights = self.slice_of(messages).highlights(limit=1)
        self.assertEqual(len(highlights), 1)
        self.assertIn("목요일", highlights[0].text)

    def test_highlights_come_back_in_conversation_order(self) -> None:
        messages = [
            message(0, "a", "첫 번째로 길게 적은 문장입니다 아주 깁니다", 1),
            message(1, "b", "짧다", 2),
            message(2, "c", "두 번째로 길게 적은 문장입니다 역시 깁니다", 3),
        ]
        highlights = self.slice_of(messages).highlights(limit=2)
        self.assertEqual([m.id for m in highlights], sorted(m.id for m in highlights))

    def test_a_slice_of_attachments_reports_no_highlights_rather_than_blanks(self) -> None:
        messages = [
            Message(index, ROOM, 1, "a", "", 2, BASE + index * 60, False) for index in range(3)
        ]
        self.assertEqual(self.slice_of(messages).highlights(), [])


class ExportTests(unittest.TestCase):
    def test_exported_text_carries_times_speakers_and_bodies(self) -> None:
        messages = [message(0, "가", "안녕", 1), message(5, "나", "반가워", 2)]
        slices = digest.slice_messages(messages, timedelta(hours=1))
        text = digest.as_text(slices, timezone(timedelta(hours=9)))
        self.assertIn("가: 안녕", text)
        self.assertIn("나: 반가워", text)
        self.assertIn("2 messages", text)

    def test_newlines_inside_a_message_do_not_break_the_line_format(self) -> None:
        messages = [message(0, "가", "첫줄\n둘째줄", 1)]
        slices = digest.slice_messages(messages, timedelta(hours=1))
        body = [
            line
            for line in digest.as_text(slices, timezone.utc).splitlines()
            if line.startswith("0") or line.startswith("1") or line.startswith("2")
        ]
        self.assertTrue(all("\n" not in line for line in body))
        self.assertIn("첫줄 둘째줄", "\n".join(body))


if __name__ == "__main__":
    unittest.main()
