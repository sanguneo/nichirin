"""Watcher tests: the stat gate is what makes following affordable.

Only :meth:`Watcher.poll` is exercised, never :meth:`Watcher.follow`, so the
suite never waits on a clock.  Sleeping is the loop's job, and a test that
waited for it would be measuring the sleep rather than the detection.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kamado.errors import ErrorCode, KamadoError
from kamado.snapshot import Fingerprint

from kamado.watch import Watcher

from tests.fixtures import (
    DATABASE_NAME,
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
        connection = self.cache.database
        connection.execute(
            "INSERT INTO NTChatMessage VALUES (?, ?, ?, 1, ?, NULL, ?, 0)",
            (log_id, room_id, author, text, 1_800_000_000 + log_id),
        )
        connection.commit()
        log = self.root / (DATABASE_NAME + "-wal")
        touch_log(self.root, room_id, log.read_bytes() + b"arrival")


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
        self.assertEqual(set(watcher.watched_rooms()), {room.id for room in self.reader.rooms()})

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
    gain thousands of rows between two polls.  Reporting only one page and
    moving the cursor past the rest loses the difference permanently.
    """

    def burst(self, room_id: int, first_id: int, count: int) -> list[int]:
        """Insert *count* messages and move the database once, as a backfill does."""
        connection = self.cache.database
        ids = list(range(first_id, first_id + count))
        connection.executemany(
            "INSERT INTO NTChatMessage VALUES (?, ?, ?, 1, ?, NULL, ?, 0)",
            [
                (log_id, room_id, PEER_ID, f"burst {log_id}", 1_800_000_000 + log_id)
                for log_id in ids
            ],
        )
        connection.commit()
        touch_log(self.root, room_id, b"backfilled")
        return ids

    def test_a_burst_larger_than_one_page_is_reported_in_full(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        expected = self.burst(DIRECT_ROOM, 5200, 520)

        found = [message.id for message in watcher.poll()]
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


class ResumeTests(WatcherTests):
    """Starting from what a reader already saw, rather than from now.

    Priming is the right default for a follower that has just started and the
    wrong one for a follower that is coming back: the messages it needs are
    exactly the ones between its cursor and this instant, and priming declares
    all of them old. One fingerprint covers the whole account here, so a
    resumed watcher leaves it unset and lets the first poll through.
    """

    def test_a_resumed_watcher_reports_what_arrived_while_it_was_away(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5300, text="before the disconnect")
        seen = [message.id for message in watcher.poll()]
        self.assertEqual(seen, [5300])

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
        connection = self.cache.database
        ids = list(range(5400, 5920))
        connection.executemany(
            "INSERT INTO NTChatMessage VALUES (?, ?, ?, 1, ?, NULL, ?, 0)",
            [(i, DIRECT_ROOM, PEER_ID, f"gap {i}", 1_800_000_000 + i) for i in ids],
        )
        connection.commit()
        log = self.root / (DATABASE_NAME + "-wal")
        touch_log(self.root, DIRECT_ROOM, log.read_bytes() + b"backfilled-during-the-gap")
        resumed = Watcher(self.reader)
        resumed.resume({DIRECT_ROOM: ids[0] - 1})
        self.assertEqual([message.id for message in resumed.poll()], ids)

    def test_a_room_the_cursor_does_not_name_starts_from_now(self) -> None:
        self.arrive(GROUP_ROOM, 6200, text="not covered by the cursor")
        resumed = Watcher(self.reader)
        resumed.resume({DIRECT_ROOM: 5000})
        reported = {message.room_id for message in resumed.poll()}
        self.assertNotIn(GROUP_ROOM, reported)
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

    def test_all_rooms_share_one_database_and_one_fingerprint_per_poll(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.cache.opened.clear()
        self.arrive(DIRECT_ROOM, 5105)
        real_fingerprint = Fingerprint.of
        with patch("kamado.watch.Fingerprint.of", wraps=real_fingerprint) as sampled:
            found = watcher.poll()
        sampled.assert_called_once_with(self.reader.database)
        self.assertEqual([message.id for message in found], [5105])
        self.assertEqual(set(self.cache.opened), {DATABASE_NAME})


class ResilienceTests(WatcherTests):
    def test_a_room_whose_read_failed_is_tried_again_on_the_next_poll(self) -> None:
        """A failed read must not consume the change that prompted it.

        One fingerprint covers the whole account here, so recording it after
        a failure lets that failure stand in for a success: the next poll
        finds the database unchanged, returns immediately, and what arrived
        during the failure waits for somebody else to write.
        """
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(GROUP_ROOM, 6200, text="arrived while the room was unreadable")
        real_messages = self.reader.messages

        def read(room_id, **kwargs):
            if room_id == GROUP_ROOM:
                raise KamadoError(ErrorCode.READ_FAILED, "synthetic query failure")
            return real_messages(room_id, **kwargs)

        with patch.object(self.reader, "messages", side_effect=read):
            self.assertEqual(watcher.poll(), [], "an unreadable room reports nothing")

        found = watcher.poll()
        self.assertEqual(
            [message.text for message in found],
            ["arrived while the room was unreadable"],
            "the arrival must survive the failed pass, with nothing else writing",
        )

    def test_a_room_read_failure_does_not_stop_the_other_rooms(self) -> None:
        watcher = Watcher(self.reader)
        watcher.prime()
        self.arrive(DIRECT_ROOM, 5106, text="still delivered")
        real_messages = self.reader.messages

        def read(room_id, **kwargs):
            if room_id == GROUP_ROOM:
                raise KamadoError(ErrorCode.READ_FAILED, "synthetic query failure")
            return real_messages(room_id, **kwargs)

        with patch.object(self.reader, "messages", side_effect=read):
            found = watcher.poll()
        self.assertEqual([message.text for message in found], ["still delivered"])


if __name__ == "__main__":
    unittest.main()
