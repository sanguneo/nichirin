"""Completed, undelivered reads must count toward the worker's queue bound."""

from __future__ import annotations

import queue
from dataclasses import replace
from unittest.mock import patch

from kamado.model import FEED_MESSAGE
from kamado.tui.console import Key
from kamado.tui.worker import Job, QUEUE_LIMIT, ROOMS
from tests.fixtures import DIRECT_ROOM
from tests.test_tui_worker import AppCase, HEIGHT, WAIT, WIDTH, WorkerCase


class OutstandingReadLimitTests(WorkerCase):
    def test_completed_answers_cannot_accumulate_without_a_bound(self) -> None:
        completed: queue.Queue[None] = queue.Queue()
        deliver = self.worker._deliver

        def notify(done) -> None:
            deliver(done)
            completed.put(None)

        # A slow consumer leaves answers undrained while the owner finishes
        # each read. Distinct requests must not bypass the total queue bound.
        with patch.object(self.worker, "_deliver", side_effect=notify):
            for token in range(QUEUE_LIMIT):
                self.assertTrue(self.worker.submit(Job(ROOMS, token=token)))
                completed.get(timeout=WAIT)

            self.assertFalse(
                self.worker.submit(Job(ROOMS, token=QUEUE_LIMIT)),
                "finished answers bypassed the worker's outstanding-read bound",
            )


class SaturatedInterfaceTests(AppCase):
    def fill_queue(self):
        gate = self.gate("rooms")
        for token in range(QUEUE_LIMIT):
            self.assertTrue(self.worker.submit(Job(ROOMS, token=100 + token)))
        self.assertTrue(gate.arrived())
        return gate

    def empty_queue(self, gate) -> None:
        gate.release()
        for _ in range(QUEUE_LIMIT):
            done = self.worker._done.get(timeout=WAIT)
            self.worker._hand_over(done)

    def test_room_open_rejected_by_full_queue_can_be_requested_again(self) -> None:
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        gate = self.fill_queue()
        self.app.open_room(room)
        self.empty_queue(gate)

        self.app.open_room(room)
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertEqual(self.pane.room.id, DIRECT_ROOM)

    def test_friend_list_retries_a_request_rejected_by_full_queue(self) -> None:
        gate = self.fill_queue()
        self.app.show_friends = True
        self.app.render(WIDTH, HEIGHT)
        self.empty_queue(gate)

        self.app.render(WIDTH, HEIGHT)
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertTrue(self.app.friends)

    def test_cached_transcript_retries_feeds_rejected_by_full_queue(self) -> None:
        self.open(DIRECT_ROOM)
        feed = replace(self.pane.messages[0], id=5511, type=FEED_MESSAGE)
        self.pane.messages.append(feed)
        gate = self.fill_queue()
        self.body()
        self.empty_queue(gate)

        self.body()
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertIn(feed.id, self.pane.feeds)

    def test_closing_last_pane_eventually_releases_a_saturated_worker(self) -> None:
        self.open(DIRECT_ROOM)
        gate = self.fill_queue()
        self.app.handle(Key.ESCAPE, "")
        self.empty_queue(gate)

        self.app._pump(WIDTH)
        # Unwatch and release must be delivered even if the queue was full at
        # the instant Esc was pressed. Await those two acknowledgements.
        for _ in range(2):
            done = self.worker._done.get(timeout=WAIT)
            self.worker._hand_over(done)

        self.assertIsNone(self.worker.watcher)
