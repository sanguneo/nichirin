"""One thread owns the database; the interface only ever owns the keyboard.

Everything here is gated on an explicit event rather than on a clock. A test
arms a gate, waits for the owner thread to *arrive* inside the read it is
blocking, and only then types — so "the screen kept working while a refresh
was in flight" is proven by the order the events happened in and not by a
sleep that happened to be long enough.

The synthetic database is a file rather than the usual in-memory fixture,
because the point of the split is that the two threads share no connection:
the owner opens its own handle on the owner thread, and the test opens its own
to write arrivals with.
"""

from __future__ import annotations

import queue
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable, Optional

from kamado.errors import ErrorCode, KamadoError
from kamado.reader import Reader
from kamado.snapshot import SnapshotCache
from kamado.tui.app import App, ChatState
from kamado.tui.console import Key
from kamado.tui.layout import strip_ansi
from kamado.tui.theme import Theme
from kamado.tui.worker import (
    FEEDS,
    NAMES,
    OLDER,
    POLL,
    QUEUE_LIMIT,
    ROOMS,
    DatabaseWorker,
    Job,
)

from tests.fixtures import (
    DATABASE_NAME,
    DIRECT_ROOM,
    GROUP_ROOM,
    PEER_ID,
    build_database,
    build_keyring,
    touch_log,
)
from tests.test_tui_send import FakeSender

WIDTH = 80
HEIGHT = 24
WAIT = 5.0
"""How long an awaited event may take before the test calls it a failure.

Bounded rather than absent: a wait that expires is a bug in the code under
test, and a wait that is never satisfied must fail rather than hang the suite.
"""


class Gate:
    """A door the owner thread walks into, and the test thread opens."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        """Set the moment the owner thread reaches the gated call."""
        self._open = threading.Event()
        self._open.set()

    def arm(self) -> None:
        self.entered.clear()
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    def pass_through(self) -> None:
        self.entered.set()
        if not self._open.wait(WAIT):
            raise AssertionError("the gate was never opened")

    def arrived(self) -> bool:
        return self.entered.wait(WAIT)


def materialise(root: Path) -> Path:
    """Write the synthetic database where two threads can each open their own.

    The placeholder file beside it is what the watcher stats: on macOS one
    fingerprint covers the whole account, so moving it is how a test says
    "something arrived" without the owner thread being told what.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / DATABASE_NAME).write_bytes(b"placeholder")
    (root / (DATABASE_NAME + "-wal")).write_bytes(b"log")
    plaintext = root / "plaintext.sqlite3"
    source = build_database()
    target = sqlite3.connect(str(plaintext))
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return plaintext


def instrumented(
    reader: Reader,
    calls: "queue.Queue[tuple[str, int]]",
    gates: dict,
    faults: Optional[Callable[[str, dict], None]] = None,
) -> Reader:
    """Record which thread each read ran on, and let a test hold one still."""

    def wrap(name: str) -> None:
        original = getattr(reader, name)

        def recorded(*arguments, **keywords):
            calls.put((name, threading.get_ident()))
            if faults is not None:
                faults(name, keywords)
            gate = gates.get(name)
            if gate is not None:
                gate.pass_through()
            return original(*arguments, **keywords)

        setattr(reader, name, recorded)

    for name in ("rooms", "messages", "stored_title", "newest_log_id", "friends", "close"):
        wrap(name)
    return reader


class OwnedCache(SnapshotCache):
    """Stands in for the decrypting cache, over a connection already open.

    A real subclass rather than a look-alike: the reader is handed the type it
    is built for, and a database it has no key for fails the way production
    fails rather than in a way only a test can produce.
    """

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        super().__init__()
        self._connection = connection
        self._path = path

    def connect(self, database: Path, geometry) -> sqlite3.Connection:
        if Path(database).name != self._path.name:
            raise KamadoError(ErrorCode.KEY_UNAVAILABLE, f"no key for {database}")
        return self._connection

    def close(self) -> None:
        super().close()
        self._connection.close()


class WorkerCase(unittest.TestCase):
    """A worker over a synthetic database it opens on its own thread."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plaintext = materialise(self.root)
        self.calls: "queue.Queue[tuple[str, int]]" = queue.Queue()
        self.gates: dict = {}
        self.faults: Optional[Callable[[str, dict], None]] = None
        self.worker = DatabaseWorker(open_reader=self.open_reader)
        self.addCleanup(self.close_worker)
        self.worker.start()

    def open_reader(self) -> Reader:
        """Built on the owner thread: this connection belongs to that thread."""
        database = self.root / DATABASE_NAME
        cache = OwnedCache(sqlite3.connect(str(self.plaintext)), database)
        reader = Reader(cache=cache, keyring=build_keyring(database))
        return instrumented(reader, self.calls, self.gates, lambda n, k: self.fault(n, k))

    def fault(self, name: str, keywords: dict) -> None:
        if self.faults is not None:
            self.faults(name, keywords)

    def close_worker(self) -> None:
        for gate in self.gates.values():
            gate.release()
        self.worker.close(timeout=WAIT)

    def gate(self, name: str) -> Gate:
        gate = Gate()
        gate.arm()
        self.gates[name] = gate
        return gate

    def recorded(self) -> list:
        found = []
        while True:
            try:
                found.append(self.calls.get_nowait())
            except queue.Empty:
                return found

    def write(self, log_id: int, text: str, room_id: int = DIRECT_ROOM) -> None:
        """Insert an arrival through the test thread's own connection."""
        connection = sqlite3.connect(str(self.plaintext))
        try:
            connection.execute(
                "INSERT INTO NTChatMessage "
                "(logId, chatId, authorId, type, message, attachment, sentAt, deleted) "
                "VALUES (?, ?, ?, 1, ?, NULL, ?, 0)",
                (log_id, room_id, PEER_ID, text, 1_800_000_000 + log_id),
            )
            connection.commit()
        finally:
            connection.close()
        touch_log(self.root, room_id, b"moved" + bytes(log_id % 251))


class OwnershipTests(WorkerCase):
    """No query may run anywhere but on the thread that owns the reader."""

    def setUp(self) -> None:
        super().setUp()
        self.replies: list = []
        self.worker.sink = self.replies.append

    def test_every_read_runs_on_the_one_owning_thread(self) -> None:
        self.assertTrue(self.worker.submit(Job(ROOMS)))
        self.assertEqual(self.worker.drain(timeout=WAIT), 1)

        threads = {ident for _name, ident in self.recorded()}
        self.assertEqual(len(threads), 1, "the reader was touched from two threads")
        self.assertNotIn(threading.get_ident(), threads, "a read ran on the caller")
        self.assertTrue(self.replies[0].value, "the catalog came back empty")

    def test_the_reader_is_closed_by_the_thread_that_opened_it(self) -> None:
        self.worker.submit(Job(ROOMS))
        self.worker.drain(timeout=WAIT)
        owner = {ident for _name, ident in self.recorded()}.pop()

        self.assertTrue(self.worker.close(timeout=WAIT))

        closed = [ident for name, ident in self.recorded() if name == "close"]
        self.assertEqual(closed, [owner], "the reader was closed off its own thread")

    def test_a_read_still_running_does_not_hold_the_interface_at_quit(self) -> None:
        gate = self.gate("rooms")
        self.worker.submit(Job(ROOMS))
        self.assertTrue(gate.arrived())

        self.assertFalse(self.worker.close(timeout=0.0), "quit waited on a blocked read")

        # Giving up waiting must not become closing the database underneath
        # the read that is still in it.
        self.assertEqual([name for name, _ in self.recorded()].count("close"), 0)

        gate.release()
        self.assertTrue(self.worker.close(timeout=WAIT))
        closed = [(name, ident) for name, ident in self.recorded() if name == "close"]
        self.assertEqual(len(closed), 1)
        self.assertNotEqual(closed[0][1], threading.get_ident())

    def test_asking_again_from_inside_the_answer_is_accepted(self) -> None:
        """A settler routinely reacts to an answer by asking for more."""
        accepted: list = []
        second = Gate()

        def ask_again(done) -> None:
            self.replies.append(done)
            if len(self.replies) == 1:
                second.arm()
                self.gates["rooms"] = second
                accepted.append(self.worker.submit(Job(ROOMS)))

        self.worker.sink = ask_again
        self.worker.submit(Job(ROOMS))
        self.worker.drain(timeout=WAIT)

        self.assertEqual(accepted, [True], "the retry was refused as a duplicate")
        self.assertTrue(second.arrived())
        second.release()
        self.worker.drain(timeout=WAIT)
        self.assertEqual(len(self.replies), 2)

    def test_an_answer_nobody_has_taken_yet_holds_its_own_place(self) -> None:
        """What bounds the answers waiting is the queue that produced them.

        A question is in flight until its answer has been applied, not until
        it has been computed, so one key can have at most one answer waiting.
        """
        first = self.gate("rooms")
        self.worker.submit(Job(ROOMS))
        self.assertTrue(first.arrived())

        second = self.gate("messages")
        self.assertTrue(self.worker.submit(Job(OLDER, room_id=DIRECT_ROOM, payload=(9_999, 10))))
        first.release()
        # The owner reached the second job, so the first has an answer waiting.
        self.assertTrue(second.arrived())

        self.assertFalse(self.worker.submit(Job(ROOMS)), "a second answer was allowed")
        self.worker.drain(timeout=WAIT)
        self.assertTrue(self.worker.submit(Job(ROOMS)))
        second.release()

    def test_a_different_pane_is_never_refused_the_predecessors_read(self) -> None:
        """A reopened room asks the same question and must still be heard."""
        gate = self.gate("messages")
        first = Job(OLDER, room_id=DIRECT_ROOM, token=1, payload=(9_999, 10))
        self.assertTrue(self.worker.submit(first))
        self.assertTrue(gate.arrived())

        self.assertFalse(self.worker.submit(first), "the same pane asked twice")
        self.assertTrue(
            self.worker.submit(Job(OLDER, room_id=DIRECT_ROOM, token=2, payload=(9_999, 10))),
            "the reopened pane was refused its own read",
        )
        gate.release()

    def test_two_names_asked_of_one_pane_are_two_questions(self) -> None:
        gate = self.gate("messages")
        self.worker.submit(Job(OLDER, room_id=DIRECT_ROOM, token=1, payload=(9_999, 10)))
        self.assertTrue(gate.arrived())

        self.assertTrue(self.worker.submit(Job(NAMES, room_id=DIRECT_ROOM, token=1, about="가")))
        self.assertTrue(self.worker.submit(Job(NAMES, room_id=DIRECT_ROOM, token=1, about="나")))
        self.assertFalse(self.worker.submit(Job(NAMES, room_id=DIRECT_ROOM, token=1, about="가")))
        gate.release()

    def test_a_repeated_request_is_coalesced_while_one_is_in_flight(self) -> None:
        gate = self.gate("rooms")
        self.assertTrue(self.worker.submit(Job(ROOMS)))
        self.assertTrue(gate.arrived())

        self.assertFalse(self.worker.submit(Job(ROOMS)))
        self.assertFalse(self.worker.submit(Job(ROOMS)))
        gate.release()
        self.worker.drain(timeout=WAIT)

        self.assertEqual(len(self.replies), 1)
        self.assertEqual([name for name, _ in self.recorded()].count("rooms"), 1)

    def test_the_queue_refuses_to_grow_without_bound(self) -> None:
        gate = self.gate("rooms")
        self.worker.submit(Job(ROOMS))
        self.assertTrue(gate.arrived())

        accepted = [
            self.worker.submit(Job(FEEDS, room_id=index, payload=((),)))
            for index in range(QUEUE_LIMIT * 2)
        ]

        self.assertEqual(accepted.count(True) + 1, QUEUE_LIMIT, "the active read counts too")
        self.assertIn(False, accepted, "the queue accepted more than it can hold")
        gate.release()

    def test_a_failed_read_comes_back_as_a_failure_and_the_worker_lives(self) -> None:
        def once(name: str, _keywords: dict) -> None:
            if name == "rooms":
                self.faults = None
                raise KamadoError(ErrorCode.READ_FAILED, "복호화 실패")

        self.faults = once
        self.worker.submit(Job(ROOMS))
        self.worker.drain(timeout=WAIT)
        self.assertIsInstance(self.replies[0].error, KamadoError)

        self.worker.submit(Job(ROOMS))
        self.worker.drain(timeout=WAIT)
        self.assertIsNone(self.replies[1].error, "the worker died with the failed read")
        self.assertTrue(self.replies[1].value)


class AppCase(WorkerCase):
    """The interface, driven over a worker of its own."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = FakeSender()
        self.app = App(
            None, theme=Theme(truecolor=True, enabled=False), sender=self.sender, db=self.worker
        )
        self.app.load_rooms()
        self.assertTrue(self.app._pump(WIDTH, timeout=WAIT))

    def open(self, room_id: int) -> None:
        self.app.open_room(next(r for r in self.app.rooms if r.id == room_id))
        self.assertTrue(self.app._pump(WIDTH, timeout=WAIT))

    @property
    def pane(self) -> ChatState:
        """The conversation holding the keyboard, which there must be one of."""
        chat = self.app.chat
        assert chat is not None
        return chat

    def body(self) -> str:
        return "\n".join(strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT))

    def texts(self, chat: ChatState) -> list:
        return [message.text for message in chat.messages]


class BlockedRefreshTests(AppCase):
    """The whole point: a read in flight costs the keyboard nothing."""

    def test_typing_lands_while_a_refresh_is_blocked_in_the_database(self) -> None:
        self.open(DIRECT_ROOM)
        gate = self.gate("messages")
        self.write(5200, "BLOCKED_ARRIVAL")

        self.app.refresh(WIDTH)
        self.assertTrue(gate.arrived(), "the poll never reached the database")

        for character in "안녕하세요":
            self.app.handle("char", character)
        drawn = self.body()

        self.assertEqual(self.pane.draft, "안녕하세요")
        self.assertIn("안녕하세요", drawn)
        self.assertNotIn("BLOCKED_ARRIVAL", drawn, "the blocked read returned early")

        gate.release()
        self.assertTrue(self.app._pump(WIDTH, timeout=WAIT))
        self.assertIn("BLOCKED_ARRIVAL", self.texts(self.pane))
        self.assertEqual(self.pane.draft, "안녕하세요", "the arrival ate the draft")

    def test_nothing_is_sent_by_a_refresh_landing_under_a_draft(self) -> None:
        self.open(DIRECT_ROOM)
        for character in "보낼 말":
            self.app.handle("char", character)
        self.write(5210, "SOMETHING_ARRIVED")
        self.app.refresh(WIDTH)
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertEqual(self.sender.sent, [], "an arrival sent what was being typed")
        self.assertIsNone(self.app.pending_send)
        self.assertEqual(self.pane.draft, "보낼 말")

    def test_a_second_poll_is_not_queued_behind_the_one_in_flight(self) -> None:
        self.open(DIRECT_ROOM)
        gate = self.gate("messages")
        self.write(5220, "ONE")
        self.app.refresh(WIDTH)
        self.assertTrue(gate.arrived())

        self.assertFalse(self.worker.submit(Job(POLL)), "polls stacked up behind a slow one")
        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)


class StaleResultTests(AppCase):
    """A pane that is gone must not be written to by the read it asked for."""

    def older_rows(self) -> None:
        """History older than the window, the way a backfill writes it.

        Written after the room is open: rows that were already there would be
        inside the loaded window, and asking for what is above it would
        correctly find nothing.
        """
        for index in range(4):
            self.write(4000 + index, f"OLD{index}")

    def test_a_result_for_a_closed_pane_is_dropped(self) -> None:
        self.open(DIRECT_ROOM)
        self.older_rows()
        gate = self.gate("messages")
        self.app.load_older()
        self.assertTrue(gate.arrived())

        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.chats, [])

        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)
        self.assertEqual(self.app.chats, [], "a closed pane came back")

    def test_a_reopened_room_does_not_inherit_the_old_panes_answer(self) -> None:
        self.open(DIRECT_ROOM)
        self.older_rows()
        gate = self.gate("messages")
        self.app.load_older()
        self.assertTrue(gate.arrived())
        self.app.handle(Key.ESCAPE, "")
        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)

        self.open(DIRECT_ROOM)
        loaded = len(self.pane.messages)
        self.app._pump(WIDTH)

        self.assertEqual(len(self.pane.messages), loaded)
        self.assertEqual(self.pane.notice, "", "a stale answer spoke for a new pane")


class OpeningTests(AppCase):
    """A room is read before its pane exists, and minds change in that time."""

    def select(self, room_id: int) -> None:
        self.app.selected = next(
            index for index, room in enumerate(self.app.visible_rooms())
            if room.id == room_id
        )

    def test_a_room_left_behind_while_it_was_opening_does_not_open(self) -> None:
        gate = self.gate("messages")
        self.select(DIRECT_ROOM)
        self.app.handle(Key.ENTER, "")
        self.assertTrue(gate.arrived())

        self.app.handle("char", "f")  # off to the friend list instead
        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertEqual(self.app.chats, [], "a pane opened over the list they went to")
        self.assertTrue(self.app.show_friends)

    def test_a_room_asked_for_twice_before_it_opens_is_read_once(self) -> None:
        gate = self.gate("messages")
        self.select(DIRECT_ROOM)
        self.app.handle(Key.ENTER, "")
        self.assertTrue(gate.arrived())
        self.app.handle(Key.ENTER, "")
        self.recorded()
        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertEqual([chat.room.id for chat in self.app.chats], [DIRECT_ROOM])
        self.assertEqual(
            [name for name, _ident in self.recorded()].count("messages"),
            0,
            "the room was read a second time",
        )

    def test_a_message_written_while_the_room_opens_is_not_lost(self) -> None:
        """Two reads cannot be one, so they must overlap rather than gap.

        The watcher's cursor is taken first and the window read after it.
        Taking them the other way round leaves a message written in between
        below the window and behind the cursor, which nothing asks for again.
        """
        gate = self.gate("newest_log_id")
        self.app.open_room(next(r for r in self.app.rooms if r.id == DIRECT_ROOM))
        self.assertTrue(gate.arrived(), "the cursor was not taken first")

        self.write(5500, "WHILE_OPENING")
        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertIn("WHILE_OPENING", self.texts(self.pane))

        self.app.refresh(WIDTH)
        self.app._pump(WIDTH, timeout=WAIT)
        self.assertEqual(self.texts(self.pane).count("WHILE_OPENING"), 1)


class RoomSwitchTests(AppCase):
    """One owner answers for every pane, and each arrival knows its room."""

    def test_each_arrival_lands_in_the_room_it_was_written_in(self) -> None:
        self.open(DIRECT_ROOM)
        self.open(GROUP_ROOM)
        direct, group = self.app.chats

        self.write(5300, "FOR_THE_DIRECT_ROOM", room_id=DIRECT_ROOM)
        self.write(6300, "FOR_THE_GROUP", room_id=GROUP_ROOM)
        self.app.refresh(WIDTH)
        self.assertTrue(self.app._pump(WIDTH, timeout=WAIT))

        self.assertIn("FOR_THE_DIRECT_ROOM", self.texts(direct))
        self.assertNotIn("FOR_THE_GROUP", self.texts(direct))
        self.assertIn("FOR_THE_GROUP", self.texts(group))

    def test_switching_panes_while_a_read_is_in_flight_keeps_both(self) -> None:
        self.open(DIRECT_ROOM)
        self.open(GROUP_ROOM)
        gate = self.gate("messages")
        self.write(5301, "ARRIVED_WHILE_SWITCHING")
        self.app.refresh(WIDTH)
        self.assertTrue(gate.arrived())

        self.app.handle(Key.TAB, "")
        self.assertEqual(self.pane.room.id, DIRECT_ROOM)

        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)
        self.assertIn("ARRIVED_WHILE_SWITCHING", self.texts(self.app.chats[0]))


class FailureTests(AppCase):
    """A failed read is said out loud, and the next one tries again."""

    def test_a_failed_poll_is_reported_and_the_next_one_retries(self) -> None:
        self.open(DIRECT_ROOM)

        def once(name: str, keywords: dict) -> None:
            if name == "messages" and keywords.get("after_id") is not None:
                self.faults = None
                raise KamadoError(ErrorCode.READ_FAILED, "잠깐 읽지 못했습니다")

        self.faults = once
        self.write(5400, "AFTER_THE_FAILURE")
        self.app.refresh(WIDTH)
        self.app._pump(WIDTH, timeout=WAIT)
        self.assertNotIn("AFTER_THE_FAILURE", self.texts(self.pane))

        self.app.refresh(WIDTH)
        self.app._pump(WIDTH, timeout=WAIT)
        self.assertIn("AFTER_THE_FAILURE", self.texts(self.pane))

    def test_a_room_that_cannot_be_opened_says_so_and_keeps_the_list(self) -> None:
        def refuse(name: str, keywords: dict) -> None:
            if name == "messages":
                raise KamadoError(ErrorCode.READ_FAILED, "이 방을 읽지 못했습니다")

        self.faults = refuse
        self.app.selected = next(
            index for index, room in enumerate(self.app.visible_rooms())
            if room.id == DIRECT_ROOM
        )
        self.app.handle(Key.ENTER, "")
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertEqual(self.app.chats, [])
        self.assertIn("이 방을 읽지 못했습니다", self.app.status)
        self.assertIn("이 방을 읽지 못했습니다", self.body())


class OlderTests(AppCase):
    def test_older_history_is_prepended_once_it_arrives(self) -> None:
        self.open(DIRECT_ROOM)
        for index in range(4):
            self.write(4000 + index, f"OLD{index}")
        loaded = len(self.pane.messages)

        self.app.load_older()
        self.app._pump(WIDTH, timeout=WAIT)

        self.assertGreater(len(self.pane.messages), loaded)
        self.assertIn("OLD0", self.texts(self.pane))

    def test_a_reopened_pane_is_still_answered_after_its_predecessor(self) -> None:
        """The closed pane must not have spent the new pane's read."""
        self.open(DIRECT_ROOM)
        for index in range(4):
            self.write(4000 + index, f"OLD{index}")
        gate = self.gate("messages")
        self.app.load_older()
        self.assertTrue(gate.arrived())
        self.app.handle(Key.ESCAPE, "")
        gate.release()
        self.app._pump(WIDTH, timeout=WAIT)

        self.open(DIRECT_ROOM)
        self.app.load_older()
        # Nothing arriving here is the failure this is about: it would mean
        # the read was refused as a repeat of the closed pane's.
        self.app._pump(WIDTH, timeout=WAIT)
        self.assertNotEqual(self.pane.notice, "")


if __name__ == "__main__":
    unittest.main()
