"""Where the parallel decryption is allowed to fork, and where it is not.

``fork`` hands the child one thread and every lock the others were holding,
so a process that forks while anything else is running can produce a child
that simply never returns. The terminal interface reads its database on a
thread of its own, which puts every decryption it asks for on the wrong side
of that line.

The rule is therefore about the process rather than about the child: fork only
from a process that is alone in itself, and decrypt serially everywhere else.
These tests hold the two halves of that rule to the same answer — the bytes —
and prove that the forbidden case never even asks for a context.
"""

from __future__ import annotations

import multiprocessing
import threading
import unittest

from kamado import page as pages, snapshot

from tests import support

WAIT = 30.0
"""A bounded wait: a child that deadlocks must fail this, never hang the suite."""

PAGES = 16
"""Enough pages to cross the lowered threshold and give every worker some."""


def image_of(count: int) -> bytes:
    """An encrypted image of *count* pages, page one carrying the salt.

    The pages after the first hold the same plaintext, so one encryption
    serves all of them: this is about which process decrypts them, not about
    what they say.
    """
    first = support.encrypted_page(bytes(support.PAGE_SIZE), number=1)
    body = bytes(range(256)) * (support.PAGE_SIZE // 256)
    return first + support.encrypted_page(body, number=2) * (count - 1)


def decrypted_serially(encrypted: bytes, count: int) -> bytes:
    """The reference answer, one page after another on this thread."""
    geometry = support.geometry()
    image = bytearray(len(encrypted))
    with geometry.prepared() as prepared:
        for number in range(1, count + 1):
            start = (number - 1) * support.PAGE_SIZE
            image[start : start + support.PAGE_SIZE] = pages.reconstruct_page(
                prepared,
                number,
                encrypted[start : start + support.PAGE_SIZE],
                support.PAGE_SIZE,
                support.RESERVE,
            )
    return bytes(image)


class DecryptionCase(unittest.TestCase):
    """A database big enough to be worth parallelising, and its right answer."""

    def setUp(self) -> None:
        threshold = snapshot._PARALLEL_PAGE_THRESHOLD
        self.addCleanup(setattr, snapshot, "_PARALLEL_PAGE_THRESHOLD", threshold)
        snapshot._PARALLEL_PAGE_THRESHOLD = 4

        # Bound before patching, or the wrapper would call itself.
        self.get_context = multiprocessing.get_context
        self.addCleanup(setattr, multiprocessing, "get_context", self.get_context)
        self.asked: list = []
        self.started: list = []

        self.encrypted = image_of(PAGES)
        self.expected = decrypted_serially(self.encrypted, PAGES)

    def refusing_context(self, method=None):
        """A context that records being asked for and then refuses.

        Refusing is not enough on its own: the parallel path falls back to
        serial decryption on any failure, so a wrong answer would still come
        out right. What is asserted is that it was never asked.
        """
        self.asked.append(method)
        raise AssertionError(f"this process asked to fork with {method!r}")

    def recording_context(self, method=None):
        """The real context, with every child it makes written down."""
        self.asked.append(method)
        context = self.get_context(method)
        started = self.started

        class Recorded:
            def Process(self, *arguments, **keywords):
                child = context.Process(*arguments, **keywords)
                started.append(child)
                return child

        return Recorded()

    def decrypt(self) -> bytes:
        return snapshot._decrypt_base_pages(
            self.encrypted, support.geometry(), PAGES, support.PAGE_SIZE, support.RESERVE
        )

    def on_a_thread(self) -> bytes:
        """Run one decryption on a thread that is not the main one."""
        answer: list = []
        failure: list = []

        def run() -> None:
            try:
                answer.append(self.decrypt())
            except BaseException as error:  # noqa: BLE001 - re-raised below
                failure.append(error)

        thread = threading.Thread(target=run, daemon=True, name="not-the-main-thread")
        thread.start()
        thread.join(WAIT)
        self.assertFalse(thread.is_alive(), "the decryption never came back")
        if failure:
            raise failure[0]
        return answer[0]


class ForkRefusedTests(DecryptionCase):
    """The cases that must decrypt without forking at all."""

    def setUp(self) -> None:
        super().setUp()
        multiprocessing.get_context = self.refusing_context

    def test_a_worker_thread_decrypts_without_forking(self) -> None:
        self.assertEqual(self.on_a_thread(), self.expected)
        self.assertEqual(self.asked, [], "a thread forked the whole interpreter")

    def test_the_main_thread_with_company_does_not_fork_either(self) -> None:
        """A fork is about what else is running, not about who calls it.

        The interface's own thread is alive for as long as the screen is up,
        so the main thread is not on its own either while that is true.
        """
        held = threading.Event()
        done = threading.Event()
        company = threading.Thread(
            target=lambda: (held.set(), done.wait(WAIT)), daemon=True, name="company"
        )
        company.start()
        self.addCleanup(company.join, WAIT)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(WAIT))

        self.assertEqual(self.decrypt(), self.expected)
        self.assertEqual(self.asked, [], "a multi-threaded process forked")

    def test_the_gate_is_shut_while_anything_else_runs(self) -> None:
        self.assertFalse(snapshot._forkable() and threading.active_count() > 1)
        results: list = []
        thread = threading.Thread(target=lambda: results.append(snapshot._forkable()))
        thread.start()
        thread.join(WAIT)
        self.assertEqual(results, [False])


class ForkAllowedTests(DecryptionCase):
    """A command-line run on its own thread: the one case that may fork.

    The gate is held open rather than waited for, because a test process is
    not the single-threaded process this describes \u2014 what is under test is
    that the parallel path agrees with the serial one, byte for byte.
    """

    def setUp(self) -> None:
        super().setUp()
        multiprocessing.get_context = self.recording_context
        forkable = snapshot._forkable
        self.addCleanup(setattr, snapshot, "_forkable", forkable)
        snapshot._forkable = lambda: True

    def test_the_parallel_path_returns_exactly_what_the_serial_one_does(self) -> None:
        self.assertEqual(self.decrypt(), self.expected)
        self.assertTrue(self.started, "the parallel path was never taken")
        self.assertEqual(
            [child.exitcode for child in self.started],
            [0] * len(self.started),
            "a child died and the serial fallback covered for it",
        )

    def test_it_is_forked_rather_than_spawned(self) -> None:
        """Spawning would pickle the encrypted image to every child."""
        self.decrypt()
        self.assertEqual(self.asked, ["fork"])


if __name__ == "__main__":
    unittest.main()
