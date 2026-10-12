"""Reads share one snapshot and take turns on it (ported from kamado #93).

A reader per request with a cache per request decrypted the catalog and the
room's log again on every request, all at the same time, so overlapping reads
each held their own decrypted copy until the read slots ran out.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

from kanao.errors import ErrorCode, KanaoError
from kanao.serve import backends


class SharedReadsTests(unittest.TestCase):
    def setUp(self) -> None:
        import kanao.keyring as keyring_module
        import kanao.reader as reader_module
        import kanao.snapshot as snapshot_module

        test = self
        self.decrypts = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.ledgers: list = []
        self.discovered: list = []
        self.keyrings_given: list = []
        self.fail_with: list = []

        class _CountingCache:
            """Decrypts on its first connect, as a real cache does for one database."""

            def __init__(self, budget_bytes=None) -> None:
                self._ready = False

            def connect(self, database, key):
                if not self._ready:
                    test.decrypts += 1
                    test.entered.set()
                    test.assertTrue(test.release.wait(10), "the first read was never let go")
                    self._ready = True
                return object()

            def close(self) -> None:
                return None

        class _Reader:
            def __init__(self, user_directory=None, cache=None, keyring=None, ledger=None):
                self._cache = cache if cache is not None else _CountingCache()
                self._keyring = keyring
                test.ledgers.append(ledger)
                test.keyrings_given.append(keyring)

            def rooms(self):
                if self._keyring is None:
                    self._keyring = keyring_module.Keyring.discover(Path())
                self._cache.connect(None, None)
                if test.fail_with:
                    raise test.fail_with.pop(0)
                return []

            def __enter__(self):
                return self

            def __exit__(self, *_exception):
                self.close()
                return None

            def close(self) -> None:
                return None

        def _discover(user_directory=None, rescan=False):
            self.discovered.append(object())
            return self.discovered[-1]

        for owner, name, value in (
            (snapshot_module, "SnapshotCache", _CountingCache),
            (reader_module, "Reader", _Reader),
            (keyring_module.Keyring, "discover", staticmethod(_discover)),
        ):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reads_at_once_decrypt_once_and_take_turns(self) -> None:
        self.release.clear()
        reads = backends.SharedReads()
        rooms = backends.make_live_rooms_provider(reads.reading)
        threads = [threading.Thread(target=rooms) for _ in range(6)]
        threads[0].start()
        self.assertTrue(self.entered.wait(10), "the first read never reached the cache")
        for thread in threads[1:]:
            thread.start()
        self.assertFalse(
            reads.lock.acquire(blocking=False), "the cache was in use without the lock"
        )
        self.release.set()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.decrypts, 1, "each request decrypted the database again")
        self.assertEqual(len(self.discovered), 1, "the account was rediscovered")
        self.assertEqual(self.ledgers, [None] * 6, "a read recorded names")

    def test_a_missing_key_drops_the_shared_account(self) -> None:
        """A room opened since the account was found must become readable.

        The handler answers KEY_UNAVAILABLE by having the client open the room
        and reading again; that read has to look for the account afresh, or
        the room stays unreadable until the server restarts.
        """
        reads = backends.SharedReads()
        rooms = backends.make_live_rooms_provider(reads.reading)
        rooms()
        first = self.discovered[-1]
        self.fail_with.append(KanaoError(ErrorCode.KEY_UNAVAILABLE, "not opened yet"))
        with self.assertRaises(KanaoError):
            rooms()
        rooms()
        self.assertEqual(self.keyrings_given, [None, first, None])
        self.assertEqual(len(self.discovered), 2)

    def test_any_other_failure_keeps_the_account(self) -> None:
        reads = backends.SharedReads()
        rooms = backends.make_live_rooms_provider(reads.reading)
        rooms()
        self.fail_with.append(KanaoError(ErrorCode.CHAT_NOT_FOUND, "no such room"))
        with self.assertRaises(KanaoError):
            rooms()
        rooms()
        self.assertEqual(len(self.discovered), 1)

    def test_a_stream_polls_under_the_same_lock(self) -> None:
        import kanao.watch as watch_module

        reads = backends.SharedReads()
        free_while_polling: list = []

        class _Watcher:
            def __init__(self, reader, interval) -> None:
                return None

            def poll(self):
                probe = threading.Thread(
                    target=lambda: free_while_polling.append(reads.lock.acquire(blocking=False))
                )
                probe.start()
                probe.join(10)
                return []

        with mock.patch.object(watch_module, "Watcher", _Watcher):
            factory = backends.make_live_watcher_factory(
                reads.open_reader, interval=1.0, lock=lambda: reads.lock
            )
            factory().poll()
        self.assertEqual(free_while_polling, [False])


if __name__ == "__main__":
    unittest.main()
