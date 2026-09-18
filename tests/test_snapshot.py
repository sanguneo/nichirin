"""Snapshot tests: consistency under a live writer, and the cache that pays for it."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from kanao.errors import ErrorCode, KanaoError
from kanao.snapshot import (
    _BUDGET_HEADROOM,
    _DEFAULT_BUDGET_BYTES,
    _MEMORY_SHARE,
    _CachedSnapshot,
    _adaptive_budget,
    _physical_memory,
    ConsistencyError,
    Fingerprint,
    SnapshotCache,
    build_image,
    open_image,
    physical_memory_bytes,
    update_image_with_wal,
)

from tests.support import KEY, PAGE_SIZE, build_database, encrypt_pages, make_wal

ROWS_A = [(index, f"alpha {index}") for index in range(1, 40)]
ROWS_B = [(index, f"bravo {index}") for index in range(1, 40)]


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "fixture.edb"
        self.log = self.root / "fixture.edb-wal"
        self.pages_a = build_database(self.root / "a.db", ROWS_A)
        self.pages_b = build_database(self.root / "b.db", ROWS_B)
        self.database.write_bytes(encrypt_pages(self.pages_a))

    def bodies(self) -> list[str]:
        image = build_image(self.database, KEY)
        connection = open_image(image, "fixture")
        try:
            return [row[0] for row in connection.execute("SELECT body FROM note ORDER BY id")]
        finally:
            connection.close()

    def encrypted(self, pages: list[bytes], number: int) -> bytes:
        from tests.support import encrypted_page

        return encrypted_page(pages[number - 1], number=number)


class FingerprintTests(_Fixture):
    def test_an_untouched_database_keeps_its_fingerprint(self) -> None:
        self.assertEqual(Fingerprint.of(self.database), Fingerprint.of(self.database))

    def test_a_database_of_a_different_size_changes_its_fingerprint(self) -> None:
        before = Fingerprint.of(self.database)
        self.database.write_bytes(self.database.read_bytes() + bytes(PAGE_SIZE))
        self.assertNotEqual(Fingerprint.of(self.database), before)

    def test_a_same_size_rewrite_can_leave_the_fingerprint_untouched(self) -> None:
        """Document the limit that makes the exact consistency check necessary.

        Windows file times advance in ticks far coarser than a write, so a
        rewrite of equal length can be invisible to stat.  This is why
        build_image compares content instead of timestamps.
        """
        before = Fingerprint.of(self.database)
        rewritten = encrypt_pages(self.pages_b)
        self.assertEqual(len(rewritten), before.database[1])
        self.database.write_bytes(rewritten)
        after = Fingerprint.of(self.database)
        if after == before:
            self.assertNotEqual(
                self.database.read_bytes(),
                encrypt_pages(self.pages_a),
                "content differs even though the fingerprint did not",
            )

    def test_writing_the_log_changes_the_fingerprint(self) -> None:
        before = Fingerprint.of(self.database)
        self.log.write_bytes(make_wal([]))
        self.assertNotEqual(Fingerprint.of(self.database), before)

    def test_a_missing_file_fingerprints_as_absent_rather_than_raising(self) -> None:
        self.assertEqual(Fingerprint.of(self.root / "absent.edb").database, (0, 0))


class BuildImageTests(_Fixture):
    def test_decrypts_a_database_that_has_no_log(self) -> None:
        self.assertEqual(self.bodies(), [body for _id, body in ROWS_A])

    def test_applies_a_committed_log_over_the_base_pages(self) -> None:
        self.log.write_bytes(
            make_wal(
                [
                    (1, 0, self.encrypted(self.pages_b, 1)),
                    (2, 2, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        self.assertEqual(self.bodies(), [body for _id, body in ROWS_B])

    def test_ignores_log_frames_that_were_never_committed(self) -> None:
        self.log.write_bytes(
            make_wal(
                [
                    (1, 0, self.encrypted(self.pages_b, 1)),
                    (2, 0, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        self.assertEqual(self.bodies(), [body for _id, body in ROWS_A])

    def test_rejects_a_database_ending_in_a_partial_page(self) -> None:
        self.database.write_bytes(self.database.read_bytes()[:-1])
        with self.assertRaises(KanaoError) as raised:
            build_image(self.database, KEY)
        self.assertEqual(raised.exception.code, ErrorCode.READ_FAILED)

    def test_rejects_an_empty_database(self) -> None:
        self.database.write_bytes(b"")
        with self.assertRaises(KanaoError):
            build_image(self.database, KEY)

    def test_rejects_a_commit_that_leaves_pages_undefined(self) -> None:
        # Two frames raise the log's page ceiling to four, then the commit
        # claims four pages while page three is never supplied by either the
        # base file or the log.
        body = self.encrypted(self.pages_b, 2)
        self.log.write_bytes(make_wal([(2, 0, body), (4, 4, body)]))
        with self.assertRaises(KanaoError) as raised:
            build_image(self.database, KEY)
        self.assertIn("undefined", raised.exception.message)

    def test_detects_a_same_size_rewrite_even_when_stat_reports_no_change(self) -> None:
        """The case a timestamp check would wave through.

        The replacement is exactly as long as the original and the fingerprint
        is pinned to a constant, so only comparing the bytes themselves can
        notice that the database moved underneath the read.
        """
        frozen = Fingerprint.of(self.database)
        self.assertEqual(len(encrypt_pages(self.pages_b)), len(self.database.read_bytes()))

        with (
            patch("kanao.snapshot.Fingerprint.of", return_value=frozen),
            patch(
                "kanao.snapshot.pages.reconstruct_page", side_effect=self._rewriting()
            ),
        ):
            with self.assertRaises(ConsistencyError):
                build_image(self.database, KEY)

    def test_detects_the_log_growing_a_new_prefix_during_the_read(self) -> None:
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_b, 2))]))
        replacement = make_wal([(2, 2, self.encrypted(self.pages_a, 2))], salt=(99, 98))

        # Bind the real function before patching, or the wrapper would look up
        # its own replacement and recurse.
        from kanao.page import reconstruct_page as real_reconstruct

        def swap_log(prepared, number, page, page_size=PAGE_SIZE, reserve=80):
            self.log.write_bytes(replacement)
            return real_reconstruct(prepared, number, page, page_size, reserve)

        with patch("kanao.snapshot.pages.reconstruct_page", side_effect=swap_log):
            with self.assertRaises(ConsistencyError):
                build_image(self.database, KEY)

    def _rewriting(self):
        """Rewrite the database once, part-way through reconstructing it.

        The real function is captured here, before any patching, so the
        wrapper cannot end up calling itself.
        """
        from kanao.page import reconstruct_page as real_reconstruct

        state = {"done": False}

        def wrapper(prepared, number, page, page_size=PAGE_SIZE, reserve=80):
            if not state["done"]:
                state["done"] = True
                self.database.write_bytes(encrypt_pages(self.pages_b))
            return real_reconstruct(prepared, number, page, page_size, reserve)

        return wrapper


class OverlayTests(_Fixture):
    """The log-only path, which reuses base pages instead of decrypting them."""

    def settled(self) -> tuple[int, int, bytes]:
        """One committed frame that changes nothing, so a log already exists."""
        return (2, 2, self.encrypted(self.pages_a, 2))

    def base(self, frames=()):
        """Decrypt the file against whatever log is beside it, and keep its pages."""
        self.log.write_bytes(make_wal(list(frames)))
        base = build_image(self.database, KEY).base
        assert base is not None
        return base

    def test_lays_a_committed_log_over_pages_decrypted_earlier(self) -> None:
        base = self.base([self.settled()])
        self.log.write_bytes(
            make_wal(
                [
                    self.settled(),
                    (1, 0, self.encrypted(self.pages_b, 1)),
                    (2, 2, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        connection = open_image(update_image_with_wal(base, self.database, KEY), "fixture")
        self.addCleanup(connection.close)
        self.assertEqual(
            [row[0] for row in connection.execute("SELECT body FROM note ORDER BY id")],
            [body for _id, body in ROWS_B],
        )

    def test_refuses_to_reuse_pages_once_the_log_starts_a_new_generation(self) -> None:
        """A checkpoint that restarts the log is what strands the base pages.

        Until then a checkpoint is harmless: it copies pages the log still
        carries, so they are overlaid anyway.  A new salt pair says the log
        was restarted, and from that point the database file may hold pages
        this image no longer has any record of.
        """
        base = self.base([self.settled()])
        self.log.write_bytes(
            make_wal([(2, 2, self.encrypted(self.pages_b, 2))], salt=(99, 98))
        )

        with self.assertRaises(ConsistencyError):
            update_image_with_wal(base, self.database, KEY)

    def test_refuses_to_reuse_pages_once_a_log_appears_where_there_was_none(self) -> None:
        base = build_image(self.database, KEY).base
        assert base is not None
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_b, 2))]))
        with self.assertRaises(ConsistencyError):
            update_image_with_wal(base, self.database, KEY)

    def test_gives_up_when_the_log_leaves_a_page_undefined(self) -> None:
        base = self.base([self.settled()])
        body = self.encrypted(self.pages_b, 2)
        self.log.write_bytes(make_wal([self.settled(), (2, 0, body), (4, 4, body)]))
        with self.assertRaises(ConsistencyError):
            update_image_with_wal(base, self.database, KEY)


class OpenImageTests(_Fixture):
    def test_refuses_an_image_that_is_not_a_valid_database(self) -> None:
        with self.assertRaises(Exception):
            open_image(b"not a database at all" + bytes(4096), "fixture")

    def test_accepts_a_genuine_image(self) -> None:
        connection = open_image(build_image(self.database, KEY), "fixture")
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])


class CacheTests(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.cache = SnapshotCache()
        self.addCleanup(self.cache.close)

    def test_a_cached_snapshot_is_readable_from_another_thread(self) -> None:
        """A cache outlives the thread that filled it, so its snapshots must too.

        The server hands one cache to the send path and every request arrives
        on a thread of its own, so the second send reads a connection the
        first one opened. sqlite3 refuses that by default — and the refusal
        is a request that dies with no answer at all, which is how it was
        found: by sending twice against a live client.
        """
        connection = self.cache.connect(self.database, KEY)
        answer: list = []

        def read() -> None:
            try:
                answer.append(connection.execute("SELECT count(*) FROM note").fetchone())
            except Exception as error:  # noqa: BLE001 - the failure is the point
                answer.append(error)

        thread = threading.Thread(target=read)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(len(answer), 1, "the reading thread never finished")
        self.assertNotIsInstance(answer[0], Exception)
        self.assertEqual(answer[0], (len(ROWS_A),))

    def test_an_unchanged_database_is_not_decrypted_twice(self) -> None:
        first = self.cache.connect(self.database, KEY)
        with patch("kanao.snapshot.build_image", side_effect=AssertionError("decrypted")):
            second = self.cache.connect(self.database, KEY)
        self.assertIs(first, second)

    def settled(self) -> tuple[int, int, bytes]:
        return (2, 2, self.encrypted(self.pages_a, 2))

    def test_a_new_log_is_laid_over_the_cached_pages_rather_than_decrypted(self) -> None:
        """The arrival path: the database file stood still, so only the log is read."""
        self.log.write_bytes(make_wal([self.settled()]))
        self.cache.connect(self.database, KEY)
        self.log.write_bytes(
            make_wal(
                [
                    self.settled(),
                    (1, 0, self.encrypted(self.pages_b, 1)),
                    (2, 2, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        with patch("kanao.snapshot.build_image", side_effect=AssertionError("decrypted")):
            second = self.cache.connect(self.database, KEY)
        self.assertEqual(
            [row[0] for row in second.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["bravo 1"],
        )

    def test_an_overlay_sqlite_refuses_falls_back_to_a_full_decryption(self) -> None:
        """A torn overlay costs a decryption, never an error the caller sees."""
        self.log.write_bytes(make_wal([self.settled()]))
        self.cache.connect(self.database, KEY)
        self.log.write_bytes(
            make_wal([self.settled(), (2, 2, self.encrypted(self.pages_b, 2))])
        )

        torn = b"SQLite format 3\x00" + bytes(PAGE_SIZE)
        with patch.object(self.cache, "_update_log_with_retry", return_value=torn):
            recovered = self.cache.connect(self.database, KEY)
        self.assertEqual(
            [row[0] for row in recovered.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["bravo 1"],
        )

    def test_a_changed_database_is_decrypted_again(self) -> None:
        first = self.cache.connect(self.database, KEY)
        self.assertEqual(
            [row[0] for row in first.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["alpha 1"],
        )
        self.database.write_bytes(encrypt_pages(self.pages_b))
        second = self.cache.connect(self.database, KEY)
        self.assertIsNot(first, second)
        self.assertEqual(
            [row[0] for row in second.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["bravo 1"],
        )

    def test_a_database_that_never_settles_is_reported_as_busy(self) -> None:
        naps: list[float] = []
        with (
            patch(
                "kanao.snapshot.build_image",
                side_effect=ConsistencyError("still moving"),
            ),
            patch("kanao.snapshot.time.sleep", naps.append),
        ):
            with self.assertRaises(KanaoError) as raised:
                self.cache.connect(self.database, KEY)

        self.assertEqual(raised.exception.code, ErrorCode.BUSY)
        self.assertEqual(naps, [0.05, 0.15, 0.4], "should back off between attempts")

    def test_a_transient_change_is_retried_rather_than_failed(self) -> None:
        real = build_image
        attempts = {"count": 0}

        def flaky(*args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ConsistencyError("moved once")
            return real(*args, **kwargs)

        with (
            patch("kanao.snapshot.build_image", side_effect=flaky),
            patch("kanao.snapshot.time.sleep", lambda _seconds: None),
        ):
            connection = self.cache.connect(self.database, KEY)
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_A))

    def test_invalidating_forces_the_next_read_to_decrypt(self) -> None:
        first = self.cache.connect(self.database, KEY)
        self.cache.invalidate(self.database)
        second = self.cache.connect(self.database, KEY)
        self.assertIsNot(first, second)

    def test_closing_releases_every_cached_connection(self) -> None:
        connection = self.cache.connect(self.database, KEY)
        self.cache.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_base_pages_are_shed_before_a_live_snapshot_is_closed(self) -> None:
        """Keeping base pages must not cost a caller its connection.

        Base pages are a cache of a cache: losing them costs the next read one
        decryption, where closing a snapshot can cost a caller the connection
        it is holding. A budget that fits two snapshots but not their base
        pages as well must therefore give up the pages, not the snapshots.
        """
        other = self.root / "other.edb"
        other.write_bytes(encrypt_pages(self.pages_b))
        held = len(self.database.read_bytes())
        cache = SnapshotCache(budget_bytes=int(held * 2.5))
        self.addCleanup(cache.close)

        first = cache.connect(self.database, KEY)
        second = cache.connect(other, KEY)

        self.assertEqual(
            first.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_A)
        )
        self.assertEqual(
            second.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_B)
        )

    def test_a_small_budget_evicts_the_least_recently_used_snapshot(self) -> None:
        other = self.root / "other.edb"
        other.write_bytes(encrypt_pages(self.pages_b))
        cache = SnapshotCache(budget_bytes=1)
        self.addCleanup(cache.close)

        first = cache.connect(self.database, KEY)
        cache.connect(other, KEY)
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")


class BudgetTests(unittest.TestCase):
    """What the cache may hold, and why a fixed number was the wrong answer.

    A fixed 256 MiB is a bet on the account being small. One entry costs the
    snapshot *plus* the base pages kept beside it, so a database past roughly
    128 MiB sheds those pages every time -- and a shed entry means the next
    arriving message pays a full decryption where it should have paid an
    overlay. This account already holds a 73.0 MB room beside a 56.6 MB one.
    """

    def setUp(self) -> None:
        remembered = list(_physical_memory)
        _physical_memory.clear()
        self.addCleanup(_physical_memory.extend, remembered)
        self.addCleanup(_physical_memory.clear)

    def say_memory(self, total) -> None:
        """Answer the memory probe without asking the machine underneath."""
        _physical_memory.clear()
        _physical_memory.append(total)

    def test_this_machine_answers_how_much_memory_it_has(self) -> None:
        """The probe is the whole mechanism; a silent None disables it."""
        self.assertIsNotNone(physical_memory_bytes())

    def test_a_machine_that_will_not_say_keeps_the_budget_it_always_had(self) -> None:
        self.say_memory(None)
        self.assertEqual(_adaptive_budget(900 * 1024 * 1024), _DEFAULT_BUDGET_BYTES)

    def test_the_budget_follows_the_database_once_it_outgrows_the_default(self) -> None:
        self.say_memory(64 * 1024 * 1024 * 1024)
        image = 200 * 1024 * 1024
        self.assertEqual(_adaptive_budget(image), image * _BUDGET_HEADROOM)

    def test_a_small_database_does_not_shrink_the_budget(self) -> None:
        self.say_memory(64 * 1024 * 1024 * 1024)
        self.assertEqual(_adaptive_budget(4 * 1024 * 1024), _DEFAULT_BUDGET_BYTES)

    def test_the_machine_caps_what_the_database_may_ask_for(self) -> None:
        memory = 8 * 1024 * 1024 * 1024
        self.say_memory(memory)
        huge = 4 * 1024 * 1024 * 1024
        self.assertEqual(_adaptive_budget(huge), int(memory * _MEMORY_SHARE))

    def test_a_large_database_can_now_keep_its_base_pages(self) -> None:
        """The bug and the fix, stated as the two numbers that differ.

        An entry is charged its image and the base pages it keeps -- about
        twice the image. The fixed budget cannot hold that for a 200 MiB
        database, so the pages went; the derived budget can, so they stay.
        """
        self.say_memory(64 * 1024 * 1024 * 1024)
        image = 200 * 1024 * 1024
        self.assertLess(_DEFAULT_BUDGET_BYTES, image * 2)
        self.assertGreaterEqual(_adaptive_budget(image), image * 2)

    def test_a_stated_budget_is_not_second_guessed(self) -> None:
        cache = SnapshotCache(budget_bytes=123456)
        self.say_memory(64 * 1024 * 1024 * 1024)
        self.assertEqual(cache._budget, 123456)

    def test_an_unstated_budget_is_derived_from_the_largest_image_held(self) -> None:
        self.say_memory(64 * 1024 * 1024 * 1024)
        cache = SnapshotCache()
        self.assertEqual(cache._budget, _DEFAULT_BUDGET_BYTES)
        cache._entries[Path("synthetic.edb")] = _CachedSnapshot(
            fingerprint=Fingerprint(database=(0, 0), log=(0, 0)),
            connection=sqlite3.connect(":memory:"),
            size=0,
            base=None,
            image_bytes=300 * 1024 * 1024,
        )
        self.addCleanup(cache.close)
        self.assertEqual(cache._budget, 300 * 1024 * 1024 * _BUDGET_HEADROOM)


if __name__ == "__main__":
    unittest.main()
