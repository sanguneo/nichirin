"""One thread owns the database; the interface owns the keyboard.

Reading this client's database is not cheap and cannot be made cheap: a poll
that finds the file moved decrypts it, which measured 0.62 s and 0.94 s on two
live rooms, and the interface used to do that between two keystrokes. Nothing
about the read got faster here. It simply stopped happening on the thread that
has to answer the keyboard.

Three rules make that safe, and they are the whole design:

* **One owner.** The :class:`Reader`, the :class:`Watcher` and every SQLite
  connection behind them are touched by exactly one thread. The interface
  holds no handle it could query — it hands over a :class:`Job` and is given a
  :class:`Done` back. Cross-thread use of a connection is therefore not
  guarded against, it is unrepresentable.
* **Answers are applied where they are drawn.** A finished job waits in a
  queue until the drawing thread drains it, so every mutation of what is on
  screen still happens on one thread and in the order the loop chose.
* **The queue is bounded and coalesced.** A conversation that polls faster
  than the database answers must not build a backlog of stale polls, so a job
  already in flight refuses its own duplicate rather than queueing behind it.
  A job counts as in flight until its answer has been *applied*, not until it
  has been computed: that is what bounds the answers waiting to be drained by
  the same number the queue itself is bounded by, and what makes the release
  of a question happen before the answer to it is handed over — so a settler
  that reacts by asking again is never refused.

:class:`DatabaseOwner` runs the same work on whatever thread submits it. That
is not a second implementation of anything — it is the same jobs and the same
answers, delivered immediately — and it is what lets the interface be driven
synchronously by a test while production runs it over a thread.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..reader import Reader
from ..watch import Watcher

ROOMS = "rooms"
OPEN = "open"
POLL = "poll"
OLDER = "older"
FRIENDS = "friends"
FEEDS = "feeds"
NAMES = "names"
UNWATCH = "unwatch"
RELEASE = "release"

QUEUE_LIMIT = 16
"""How many jobs may wait at once, and so how many answers can be waiting.

Every job the interface asks for is either a duplicate of one in flight — and
refused as one — or a distinct question about a pane, of which there are as
many as the screen is wide enough to hold. A queue deeper than this means the
database has stopped answering, and stacking more work on it then is how a
backlog turns into a screen showing answers to questions nobody still has.
"""

CLOSE_TIMEOUT_SECONDS = 5.0
"""How long quitting waits for a read already in flight.

A read cannot be cancelled: it is inside SQLite or inside a decryption. Quit
therefore stops feeding the owner, waits this long for it to notice, and then
leaves it to the process exit rather than holding the terminal hostage.
"""

WATCH_INTERVAL_SECONDS = 0.15
"""The interval the watcher is built with; this loop drives its polls itself."""


@dataclass(frozen=True)
class Job:
    """One question for the database, and who is waiting for the answer."""

    kind: str
    room_id: Optional[int] = None
    token: int = 0
    """Which pane asked, so an answer to a closed pane can be dropped."""
    about: str = ""
    """What else tells two jobs apart, when the room and the pane do not.

    ``/real 가`` and ``/real 나`` are two questions about one room from one
    pane, and the second must not be mistaken for a repeat of the first.
    """
    payload: tuple = ()

    @property
    def key(self) -> tuple:
        """What makes two jobs the same question, for coalescing.

        The pane is part of the question. A pane that was closed and reopened
        is a different one asking, and its answer would be dropped for having
        the wrong token — so without this, a reopened room could be refused a
        read on the strength of the read its predecessor asked for.
        """
        return (self.kind, self.room_id, self.token, self.about)


@dataclass
class Done:
    """What one job produced, or why it produced nothing."""

    job: Job
    value: Any = None
    error: Optional[BaseException] = None


def _ignore(_done: Done) -> None:
    """Where answers go before anyone has asked to be told about them."""


class DatabaseOwner:
    """Owns the reader and the watcher, and runs every job against them.

    This base class runs a job on the thread that submits it, which is what a
    test drives and what keeps a synchronous caller synchronous.
    :class:`DatabaseWorker` runs the same jobs on a thread of its own.
    """

    def __init__(
        self,
        reader: Optional[Reader] = None,
        *,
        open_reader: Optional[Callable[[], Reader]] = None,
        interval: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        if reader is None and open_reader is None:
            raise ValueError("a reader, or a way of opening one, is required")
        self._reader = reader
        self._open_reader = open_reader
        self._interval = interval
        self.watcher: Optional[Watcher] = None
        """Built on the first pane, on the owning thread, and never shared."""
        self.sink: Callable[[Done], None] = _ignore
        """Where a finished job is delivered, on the thread that drains it."""
        self.closed = False
        self.close_error: Optional[BaseException] = None
        """What closing the reader raised, for a caller that wants to know."""

    # -- the interface's side ------------------------------------------------

    def submit(self, job: Job) -> bool:
        """Ask for one piece of work. Returns whether it was taken on."""
        self._deliver(self._run(job))
        return True

    def drain(self, timeout: Optional[float] = None) -> int:
        """Deliver every finished job. Nothing waits here: none are pending."""
        if timeout is not None:
            raise TimeoutError("this owner finishes its work before it returns")
        return 0

    def close(self, timeout: float = CLOSE_TIMEOUT_SECONDS) -> bool:
        """Close the database down. Returns whether the owner finished."""
        self._shutdown()
        return True

    def __enter__(self) -> "DatabaseOwner":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    # -- the owner's side ----------------------------------------------------

    def _deliver(self, done: Done) -> None:
        self.sink(done)

    def _run(self, job: Job) -> Done:
        """Do one job, turning any failure into an answer rather than a crash.

        A room can lose its key, be closed in the client, or have its database
        rewritten underneath a read; none of that is a reason for the owner to
        die, and none of it is a reason for the interface not to hear about
        it. The failure is carried back to the thread that can say so.
        """
        try:
            return Done(job, self._perform(job))
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            return Done(job, None, error)

    def _reader_here(self) -> Reader:
        """Resolve the reader on the owning thread, once.

        Opening it here rather than handing one in is what keeps a SQLite
        connection on the thread that made it.
        """
        if self._reader is None:
            assert self._open_reader is not None
            self._reader = self._open_reader()
        return self._reader

    def _perform(self, job: Job) -> Any:
        reader = self._reader_here()
        if job.kind == ROOMS:
            return reader.rooms()
        if job.kind == OPEN:
            return self._open(reader, job)
        if job.kind == POLL:
            return [] if self.watcher is None else self.watcher.poll()
        if job.kind == OLDER:
            before_id, limit = job.payload
            assert job.room_id is not None
            return reader.messages(job.room_id, limit=limit, before_id=before_id)
        if job.kind == FRIENDS:
            return reader.friends()
        if job.kind == FEEDS:
            (wanted,) = job.payload
            assert job.room_id is not None
            return reader.feed_details(job.room_id, list(wanted))
        if job.kind == NAMES:
            return self._names(reader, job)
        if job.kind == UNWATCH:
            if self.watcher is not None and job.room_id is not None:
                self.watcher.unwatch(job.room_id)
            return None
        if job.kind == RELEASE:
            self.watcher = None
            return None
        raise ValueError(f"unknown database job {job.kind!r}")

    def _open(self, reader: Reader, job: Job) -> tuple:
        """Start following a room, then read the window — in that order.

        Two reads cannot be made one, so the only choice is which way they
        overlap. Following first puts the watcher's cursor at a message the
        window is then certain to contain or to be newer than, so a message
        written between the two is reported as an arrival *and* already on
        screen — and the pane drops what it already has. Reading the window
        first inverts that into a gap: the cursor lands past a message the
        window never held, and nothing ever asks for it again.
        """
        room, limit = job.payload
        self._follow(reader, room.id)
        try:
            messages = reader.messages(room.id, limit=limit)
            stored = reader.stored_title(room.id)
        except BaseException:
            # No pane will exist to read it, so it is not left followed.
            if self.watcher is not None:
                self.watcher.unwatch(room.id)
            raise
        return messages, stored

    def _follow(self, reader: Reader, room_id: int) -> None:
        """Add a room to the one watcher, building it on the first pane."""
        if self.watcher is None:
            self.watcher = Watcher(reader, room_ids=[room_id], interval=self._interval)
            self.watcher.prime()
        else:
            self.watcher.watch(room_id)

    def _names(self, reader: Reader, job: Job) -> tuple:
        """Answer who has answered to a name, with each account's chain."""
        from ..names import steps_with_current

        who = job.about
        assert job.room_id is not None
        history = reader.name_history(job.room_id)
        found = reader.users_named(job.room_id, who, history)
        current = reader.names()
        return found, {
            user_id: list(
                steps_with_current(user_id, history.get(user_id, ()), current.get(user_id))
            )
            for user_id in found
        }

    def _shutdown(self) -> None:
        """Give up the watcher and close the reader, on the owning thread."""
        if self.closed:
            return
        self.watcher = None
        try:
            if self._reader is not None:
                self._reader.close()
        except Exception as error:  # noqa: BLE001 - kept, not swallowed
            self.close_error = error
        finally:
            self.closed = True


_STOP = Job("stop")
"""What the owner thread is given instead of a job when it is time to stop."""


class DatabaseWorker(DatabaseOwner):
    """The same owner, on one thread of its own."""

    def __init__(
        self,
        reader: Optional[Reader] = None,
        *,
        open_reader: Optional[Callable[[], Reader]] = None,
        interval: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(reader, open_reader=open_reader, interval=interval)
        self._pending: "queue.Queue[Job]" = queue.Queue(maxsize=QUEUE_LIMIT)
        self._done: "queue.Queue[Done]" = queue.Queue()
        self._busy: set = set()
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "DatabaseWorker":
        """Begin owning the database. Idempotent, so entering twice is safe."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._own, name="kamado-database", daemon=True
            )
            self._thread.start()
        return self

    def __enter__(self) -> "DatabaseWorker":
        return self.start()

    def submit(self, job: Job) -> bool:
        """Queue one job, refusing a duplicate of one already in flight."""
        if self._stopping.is_set():
            return False
        with self._guard:
            if job.key in self._busy or len(self._busy) >= QUEUE_LIMIT:
                return False
            self._busy.add(job.key)
        try:
            self._pending.put_nowait(job)
        except queue.Full:
            with self._guard:
                self._busy.discard(job.key)
            return False
        return True

    def drain(self, timeout: Optional[float] = None) -> int:
        """Deliver every finished job to the sink, on the caller's thread.

        With a *timeout* this waits for the first answer and fails if none
        arrives, which is how a caller says "there is work in flight and I
        cannot continue without it" without ever guessing at a duration.
        """
        delivered = 0
        if timeout is not None:
            try:
                done = self._done.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(
                    f"the database owner returned nothing within {timeout}s"
                ) from None
            self._hand_over(done)
            delivered += 1
        while True:
            try:
                done = self._done.get_nowait()
            except queue.Empty:
                return delivered
            self._hand_over(done)
            delivered += 1

    def _hand_over(self, done: Done) -> None:
        """Free the question, then answer it.

        In that order: a settler routinely reacts by asking the same thing
        again — a poll that failed is retried, a pane that took its history
        asks for more — and a question still marked as in flight while its own
        answer is being applied would refuse exactly that.
        """
        with self._guard:
            self._busy.discard(done.job.key)
        self.sink(done)

    def close(self, timeout: float = CLOSE_TIMEOUT_SECONDS) -> bool:
        """Stop feeding the owner and wait *timeout* for it to finish.

        Returns whether it did. A read already inside SQLite cannot be taken
        back, so a False here means the thread is still in one — it is a
        daemon and the process exit collects it, and the caller is told rather
        than blocked.
        """
        thread = self._thread
        if thread is None:
            self._shutdown()
            return True
        if not self._stopping.is_set():
            self._stopping.set()
            # Emptied first so the stop always fits, however full the queue is.
            while True:
                try:
                    self._pending.get_nowait()
                except queue.Empty:
                    break
            self._pending.put(_STOP)
        thread.join(timeout)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    def _deliver(self, done: Done) -> None:
        self._done.put(done)

    def _own(self) -> None:
        """Take jobs one at a time, for as long as there are any to take."""
        try:
            while True:
                job = self._pending.get()
                if job is _STOP:
                    return
                # The key is held until the answer is drained, not until it is
                # computed: that is what stops a backlog of undrained answers
                # from growing past what the queue itself allows.
                self._deliver(self._run(job))
        finally:
            self._shutdown()
