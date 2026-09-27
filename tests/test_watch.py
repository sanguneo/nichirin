"""Watcher tests: the stat gate is what makes following affordable.

Only :meth:`Watcher.poll` is exercised, never :meth:`Watcher.follow`, so the
suite never waits on a clock.  Sleeping is the loop's job, and a test that
waited for it would be measuring the sleep rather than the detection.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kanao.watch import Watcher

from tests.fixtures import (
    DIRECT_ROOM,
    GROUP_ROOM,
    OWN_ID,
    PEER_ID,
    build_reader_on_disk,
    touch_log,
)


class WatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reader, self.cache = build_reader_on_disk(self.root)
        self.addCleanup(self.reader.close)

    def arrive(self, room_id: int, log_id: int, author: int = PEER_ID, text: str = "new") -> None:
        """Append a message the way the client would: row plus a changed log."""
        from kanao import paths

        connection = self.cache.databases[paths.chat_log_name(room_id)]
        connection.execute(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted) "
            "VALUES (?, ?, 1, ?, NULL, ?, 0)",
            (log_id, author, text, 1_800_000_000 + log_id),
        )
        connection.commit()
        touch_log(self.root, room_id, b"log" + bytes(log_id % 251))


class PrimingTests(WatcherTests):
    def test_the_first_poll_establishes_a_baseline_without_replaying_history(self) -> None:
        watcher = Watcher(self.reader)
        self.assertEqual(watcher.poll(), [])

    def test_priming_explicitly_has_the_same_effect(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.assertEqual(watcher.poll(), [])

    def test_every_readable_room_is_watched_by_default(self) -> None:
        watcher = Watcher(self.reader)
        self.assertEqual(set(watcher.watched_rooms()), set(self.reader.keyring.chat_ids()))

    def test_an_explicit_room_list_is_respected(self) -> None:
        watcher = Watcher(self.reader, room_ids=[DIRECT_ROOM])
        self.assertEqual(watcher.watched_rooms(), [DIRECT_ROOM])

    def test_a_non_positive_interval_is_rejected(self) -> None:
        for interval in (0, -1.5):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                Watcher(self.reader, interval=interval)


class DetectionTests(WatcherTests):
    def test_an_arriving_message_is_reported_once(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5100, text="hello there")

        first = watcher.poll()
        self.assertEqual([message.id for message in first], [5100])
        self.assertEqual(first[0].text, "hello there")
        self.assertEqual(watcher.poll(), [], "a reported message must not repeat")

    def test_messages_already_present_before_priming_are_not_reported(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        touch_log(self.root, DIRECT_ROOM, b"log-changed-but-no-new-rows")
        self.assertEqual(watcher.poll(), [])

    def test_several_arrivals_are_reported_together_in_time_order(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5101, text="one")
        self.arrive(DIRECT_ROOM, 5102, text="two")

        found = watcher.poll()
        self.assertEqual([message.id for message in found], [5101, 5102])

    def test_arrivals_in_different_rooms_are_all_reported(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5103)
        self.arrive(GROUP_ROOM, 6100)

        found = watcher.poll()
        self.assertEqual({message.room_id for message in found}, {DIRECT_ROOM, GROUP_ROOM})

    def test_the_users_own_messages_are_reported_and_marked(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5104, author=OWN_ID, text="sent from my phone")

        found = watcher.poll()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].from_me)


class CatchUpTests(WatcherTests):
    """A burst larger than one catch-up page must still arrive whole.

    The client backfills from the server after a reconnection, so a room can
    gain thousands of rows between two polls.  Reporting only the newest page
    and moving the cursor past the rest loses the difference permanently.
    """

    def burst(self, room_id: int, first_id: int, count: int) -> list[int]:
        """Insert *count* messages and touch the log once, as a backfill does."""
        from kanao import paths

        connection = self.cache.databases[paths.chat_log_name(room_id)]
        ids = list(range(first_id, first_id + count))
        connection.executemany(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted) "
            "VALUES (?, ?, 1, ?, NULL, ?, 0)",
            [(log_id, PEER_ID, f"burst {log_id}", 1_800_000_000 + log_id) for log_id in ids],
        )
        connection.commit()
        touch_log(self.root, room_id, b"backfilled")
        return ids

    def test_a_burst_larger_than_one_page_is_reported_in_full(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        expected = self.burst(DIRECT_ROOM, 5200, 520)

        found: list[int] = [message.id for message in watcher.poll()]
        self.assertEqual(
            found,
            expected,
            "every backfilled message must arrive, oldest first, with none skipped",
        )

    def test_a_burst_is_not_reported_twice(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.burst(DIRECT_ROOM, 5200, 520)
        watcher.poll()
        self.assertEqual(watcher.poll(), [])


class OrderTests(WatcherTests):
    """A room's messages leave in log-id order, whatever their clocks say.

    That order is what a cursor resumes from. Sorting a poll by time put a
    room's 11 before its 10 whenever 11 carried the earlier timestamp, and a
    follower that stored the last id it saw stored 10 and got 11 again.
    """

    def write(self, room_id: int, log_id: int, sent_at: int) -> None:
        from kanao import paths

        connection = self.cache.databases[paths.chat_log_name(room_id)]
        connection.execute(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted) "
            "VALUES (?, ?, 1, ?, NULL, ?, 0)",
            (log_id, PEER_ID, f"at {sent_at}", sent_at),
        )
        connection.commit()
        touch_log(self.root, room_id, b"log" + bytes(log_id % 251))

    def test_a_room_whose_clock_runs_backwards_keeps_its_id_order(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.write(DIRECT_ROOM, 5110, sent_at=1_800_000_061)
        self.write(DIRECT_ROOM, 5111, sent_at=1_800_000_060)
        self.assertEqual([message.id for message in watcher.poll()], [5110, 5111])

    def test_equal_and_missing_times_keep_their_id_order(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.write(DIRECT_ROOM, 5120, sent_at=1_800_000_100)
        self.write(DIRECT_ROOM, 5121, sent_at=1_800_000_100)
        self.write(GROUP_ROOM, 6120, sent_at=None)
        self.write(GROUP_ROOM, 6121, sent_at=None)
        found = [message.id for message in watcher.poll()]
        self.assertEqual([one for one in found if one < 6000], [5120, 5121])
        self.assertEqual([one for one in found if one > 6000], [6120, 6121])

    def test_rooms_still_interleave_by_time(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.write(DIRECT_ROOM, 5130, sent_at=1_800_000_300)
        self.write(DIRECT_ROOM, 5131, sent_at=1_800_000_100)
        self.write(GROUP_ROOM, 6130, sent_at=1_800_000_200)
        self.assertEqual(
            [message.id for message in watcher.poll()], [6130, 5130, 5131]
        )


class CatchUpCeilingTests(WatcherTests):
    """A room written to faster than it is paged through still lets go."""

    def test_the_catch_up_stops_at_the_newest_id_it_started_with(self) -> None:
        from unittest.mock import patch

        from kanao import paths, watch

        connection = self.cache.databases[paths.chat_log_name(DIRECT_ROOM)]
        watcher = Watcher(self.reader)
        watcher.prime()
        for log_id in range(5500, 5504):
            self.arrive(DIRECT_ROOM, log_id)

        real = self.reader.messages
        pages = {"count": 0}
        next_id = [5600]

        def busy(*args, **kwargs):
            pages["count"] += 1
            if pages["count"] > 20:
                raise AssertionError("the catch-up never stopped paging")
            # Every page, the client writes a full page more.
            for _ in range(2):
                connection.execute(
                    "INSERT INTO chatLogs (logId, authorId, type, message, attachement, "
                    "sendAt, deleted) VALUES (?, ?, 1, 'more', NULL, ?, 0)",
                    (next_id[0], PEER_ID, 1_800_000_000 + next_id[0]),
                )
                next_id[0] += 1
            connection.commit()
            return real(*args, **kwargs)

        with (
            patch.object(watch, "_CATCH_UP_LIMIT", 2),
            patch.object(self.reader, "messages", side_effect=busy),
        ):
            found = [message.id for message in watcher.poll()]
        self.assertEqual(found[:4], [5500, 5501, 5502, 5503])
        self.assertEqual(found, sorted(found))
        self.assertLessEqual(pages["count"], 3)

        # What was left past the ceiling arrives on the next poll, not never.
        touch_log(self.root, DIRECT_ROOM, b"moved again")
        rest = [message.id for message in watcher.poll()]
        self.assertEqual(sorted(set(found) | set(rest)), found + rest)
        self.assertEqual((found + rest)[-1], next_id[0] - 1)


class ResumeTests(WatcherTests):
    """Starting from what a reader already saw, rather than from now.

    Priming is the right default for a follower that has just started and the
    wrong one for a follower that is coming back: the messages it needs are
    exactly the ones between its cursor and this instant, and priming declares
    all of them old.
    """

    def test_a_resumed_watcher_reports_what_arrived_while_it_was_away(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5300, text="before the disconnect")
        seen = [message.id for message in watcher.poll()]
        self.assertEqual(seen, [5300])

        # The reader goes away, messages arrive, and a new watcher is built for
        # the reconnection — which is exactly what a new SSE connection does.
        self.arrive(DIRECT_ROOM, 5301, text="during the gap")
        self.arrive(DIRECT_ROOM, 5302, text="also during the gap")

        resumed = Watcher(self.reader)
        resumed.resume({str(DIRECT_ROOM): str(seen[-1])})
        found = resumed.poll()
        self.assertEqual([message.id for message in found], [5301, 5302])
        self.assertEqual(found[0].text, "during the gap")

    def test_a_primed_watcher_would_have_lost_them(self) -> None:
        """The negative that makes the test above mean something."""
        self.arrive(DIRECT_ROOM, 5310, text="during the gap")
        fresh = Watcher(self.reader)
        fresh.prime()
        self.assertEqual(fresh.poll(), [])

    def test_a_resumed_burst_larger_than_one_page_still_arrives_whole(self) -> None:
        from kanao import paths

        connection = self.cache.databases[paths.chat_log_name(DIRECT_ROOM)]
        ids = list(range(5400, 5400 + 520))
        connection.executemany(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted) "
            "VALUES (?, ?, 1, ?, NULL, ?, 0)",
            [(i, PEER_ID, f"gap {i}", 1_800_000_000 + i) for i in ids],
        )
        connection.commit()
        touch_log(self.root, DIRECT_ROOM, b"backfilled-during-the-gap")

        resumed = Watcher(self.reader)
        resumed.resume({DIRECT_ROOM: 5399})
        self.assertEqual([message.id for message in resumed.poll()], ids)

    def test_a_room_the_cursor_does_not_name_starts_from_now(self) -> None:
        self.arrive(GROUP_ROOM, 6200, text="not covered by the cursor")
        resumed = Watcher(self.reader)
        resumed.resume({DIRECT_ROOM: 5000})
        reported = {message.room_id for message in resumed.poll()}
        self.assertNotIn(GROUP_ROOM, reported)
        # And from then on it is followed like any other room.
        self.arrive(GROUP_ROOM, 6201, text="after the resume")
        self.assertEqual([m.id for m in resumed.poll()], [6201])

    def test_a_resumed_message_is_not_reported_twice(self) -> None:
        self.arrive(DIRECT_ROOM, 5500)
        resumed = Watcher(self.reader)
        resumed.resume({DIRECT_ROOM: 5499})
        self.assertEqual([m.id for m in resumed.poll()], [5500])
        self.assertEqual(resumed.poll(), [])


class CostTests(WatcherTests):
    """An idle conversation must not be decrypted, only stat'ed."""

    def test_an_unchanged_room_is_never_opened(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        before = len(self.cache.opened)

        self.assertEqual(watcher.poll(), [])
        self.assertEqual(watcher.poll(), [])

        self.assertEqual(
            len(self.cache.opened), before, "an idle poll must not open any database"
        )

    def test_only_the_room_that_changed_is_opened(self) -> None:
        from kanao import paths

        watcher = Watcher(self.reader)
        watcher.prime()
        self.cache.opened.clear()
        self.arrive(DIRECT_ROOM, 5105)
        watcher.poll()

        self.assertNotIn(paths.chat_log_name(GROUP_ROOM), self.cache.opened)
        self.assertIn(paths.chat_log_name(DIRECT_ROOM), self.cache.opened)


class ResilienceTests(WatcherTests):
    def test_a_room_that_failed_to_be_read_is_tried_again(self) -> None:
        """A failed read must not be recorded as if it had succeeded.

        The log is stat'ed to decide whether a room is worth opening, so a
        room whose read raised after its fingerprint was already stored would
        look unchanged forever after — and the arrival that caused the change
        would never be reported.
        """
        from kanao import paths

        watcher = Watcher(self.reader)
        watcher.prime()
        name = paths.chat_log_name(GROUP_ROOM)
        self.arrive(GROUP_ROOM, 6200, text="arrived while the key was gone")

        withheld = self.cache.databases.pop(name)
        self.assertEqual(watcher.poll(), [], "an unreadable room reports nothing")

        self.cache.databases[name] = withheld
        found = watcher.poll()
        self.assertEqual(
            [message.text for message in found], ["arrived while the key was gone"]
        )

    def test_a_room_that_becomes_unreadable_does_not_stop_the_others(self) -> None:
        from kanao import paths

        watcher = Watcher(self.reader)
        watcher.prime()

        # Drop one room's key, as closing that conversation in the client would.
        del self.cache.databases[paths.chat_log_name(GROUP_ROOM)]
        touch_log(self.root, GROUP_ROOM, b"changed")
        self.arrive(DIRECT_ROOM, 5106, text="still delivered")

        found = watcher.poll()
        self.assertEqual([message.text for message in found], ["still delivered"])


if __name__ == "__main__":
    unittest.main()
