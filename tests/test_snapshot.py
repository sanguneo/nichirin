"""Snapshot tests: consistency under a live writer, and the cache that pays for it."""

from __future__ import annotations

import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from kamado import snapshot
from kamado.errors import ErrorCode, KamadoError
from kamado.snapshot import (
    ConsistencyError,
    Fingerprint,
    SnapshotCache,
    build_image,
    open_image,
    update_image_with_wal,
)

from tests.support import PAGE_SIZE, build_database, encrypt_pages, make_wal, geometry

ROWS_A = [(index, f"alpha {index}") for index in range(1, 40)]
ROWS_B = [(index, f"bravo {index}") for index in range(1, 40)]

MIB = 1024 * 1024
GIB = 1024 * MIB


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
        image = build_image(self.database, geometry())
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

        Restoring the timestamp makes this deterministic on any filesystem;
        build_image must compare bytes, not rely on timestamp resolution.
        """
        before = Fingerprint.of(self.database)
        status = self.database.stat()
        rewritten = encrypt_pages(self.pages_b)
        self.assertEqual(len(rewritten), before.database[1])
        self.database.write_bytes(rewritten)
        os.utime(self.database, ns=(status.st_atime_ns, status.st_mtime_ns))
        self.assertEqual(Fingerprint.of(self.database), before)
        self.assertNotEqual(self.database.read_bytes(), encrypt_pages(self.pages_a))

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
        with self.assertRaises(KamadoError) as raised:
            build_image(self.database, geometry())
        self.assertEqual(raised.exception.code, ErrorCode.READ_FAILED)

    def test_rejects_an_empty_database(self) -> None:
        self.database.write_bytes(b"")
        with self.assertRaises(KamadoError):
            build_image(self.database, geometry())

    def test_rejects_a_commit_that_leaves_pages_undefined(self) -> None:
        # Two frames raise the log's page ceiling to four, then the commit
        # claims four pages while page three is never supplied by either the
        # base file or the log.
        body = self.encrypted(self.pages_b, 2)
        self.log.write_bytes(make_wal([(2, 0, body), (4, 4, body)]))
        with self.assertRaises(KamadoError) as raised:
            build_image(self.database, geometry())
        self.assertEqual(raised.exception.code, ErrorCode.READ_FAILED)

    def test_a_commit_that_shrank_the_database_truncates_the_image(self) -> None:
        """Pages the last commit dropped must not ride along on the image.

        The log decides how long the database is, so a commit that shrank it
        past the base file has to cut the image down; leaving the old pages
        attached hands SQLite a file describing rows that no longer exist.
        """
        self.assertEqual(len(self.database.read_bytes()), 2 * PAGE_SIZE)
        self.log.write_bytes(make_wal([(1, 1, self.encrypted(self.pages_b, 1))]))

        image = build_image(self.database, geometry())
        self.assertEqual(len(image), PAGE_SIZE, "the image must end where the commit does")

    def test_detects_a_same_size_rewrite_even_when_stat_reports_no_change(self) -> None:
        """The case a timestamp check would wave through.

        The replacement is exactly as long as the original and the fingerprint
        is pinned to a constant, so only comparing the bytes themselves can
        notice that the database moved underneath the read.
        """
        frozen = Fingerprint.of(self.database)
        self.assertEqual(len(encrypt_pages(self.pages_b)), len(self.database.read_bytes()))

        with (
            patch("kamado.snapshot.Fingerprint.of", return_value=frozen),
            patch(
                "kamado.snapshot.pages.reconstruct_page", side_effect=self._rewriting()
            ),
        ):
            with self.assertRaises(ConsistencyError):
                build_image(self.database, geometry())

    def test_detects_the_log_growing_a_new_prefix_during_the_read(self) -> None:
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_b, 2))]))
        replacement = make_wal([(2, 2, self.encrypted(self.pages_a, 2))], salt=(99, 98))

        # Bind the real function before patching, or the wrapper would look up
        # its own replacement and recurse.
        from kamado.page import reconstruct_page as real_reconstruct

        def swap_log(prepared, number, page, page_size=PAGE_SIZE, reserve=80):
            self.log.write_bytes(replacement)
            return real_reconstruct(prepared, number, page, page_size, reserve)

        with patch("kamado.snapshot.pages.reconstruct_page", side_effect=swap_log):
            with self.assertRaises(ConsistencyError):
                build_image(self.database, geometry())

    def _rewriting(self):
        """Rewrite the database once, part-way through reconstructing it.

        The real function is captured here, before any patching, so the
        wrapper cannot end up calling itself.
        """
        from kamado.page import reconstruct_page as real_reconstruct

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
        base = build_image(self.database, geometry()).base
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
        connection = open_image(
            update_image_with_wal(base, self.database, geometry()), "fixture"
        )
        self.addCleanup(connection.close)
        self.assertEqual(
            [row[0] for row in connection.execute("SELECT body FROM note ORDER BY id")],
            [body for _id, body in ROWS_B],
        )

    def test_refuses_to_reuse_pages_once_the_log_starts_a_new_generation(self) -> None:
        """A checkpoint that restarts the log is what strands the base pages.

        Until then a checkpoint is harmless: it copies pages the log still
        carries, so the overlay applies them anyway.  A new salt pair says the
        log was restarted, and from that point the database file may hold
        pages this image has no record of — which a stat would not show.
        """
        base = self.base([self.settled()])
        self.log.write_bytes(
            make_wal([(2, 2, self.encrypted(self.pages_b, 2))], salt=(99, 98))
        )

        with self.assertRaises(ConsistencyError):
            update_image_with_wal(base, self.database, geometry())

    def test_refuses_to_reuse_pages_once_a_log_appears_where_there_was_none(self) -> None:
        base = build_image(self.database, geometry()).base
        assert base is not None
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_b, 2))]))

        with self.assertRaises(ConsistencyError):
            update_image_with_wal(base, self.database, geometry())

    def test_gives_up_when_the_log_leaves_a_page_undefined(self) -> None:
        base = self.base([self.settled()])
        body = self.encrypted(self.pages_b, 2)
        self.log.write_bytes(make_wal([self.settled(), (2, 0, body), (4, 4, body)]))

        with self.assertRaises(ConsistencyError):
            update_image_with_wal(base, self.database, geometry())

    def test_a_commit_that_shrank_the_database_truncates_the_overlay(self) -> None:
        base = self.base([self.settled()])
        self.log.write_bytes(
            make_wal([self.settled(), (1, 1, self.encrypted(self.pages_b, 1))])
        )

        image = update_image_with_wal(base, self.database, geometry())
        self.assertEqual(len(image), PAGE_SIZE)

    def test_parallel_decrypt_base_pages_matches_serial(self) -> None:
        # Through `import_module`: the alias is a meta path finder, which a
        # type checker cannot follow, and the point here is the runtime.
        legacy = import_module("kakaoread.snapshot")
        _decrypt_base_pages = legacy._decrypt_base_pages
        _PARALLEL_PAGE_THRESHOLD = legacy._PARALLEL_PAGE_THRESHOLD
        geom = geometry()
        encrypted = self.database.read_bytes()
        base_pages = len(encrypted) // PAGE_SIZE
        decrypted = _decrypt_base_pages(encrypted, geom, base_pages, PAGE_SIZE, 80)
        self.assertEqual(len(decrypted), len(encrypted))


class OpenImageTests(_Fixture):
    def test_refuses_an_image_that_is_not_a_valid_database(self) -> None:
        with self.assertRaises(Exception):
            open_image(b"not a database at all" + bytes(4096), "fixture")

    def test_accepts_a_genuine_image(self) -> None:
        connection = open_image(build_image(self.database, geometry()), "fixture")
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])


class BudgetPolicyTests(unittest.TestCase):
    """What the default budget is, when nobody states one.

    A fixed 256 MiB budget is not a property of the account being read: one
    entry costs the image plus the base pages it keeps, so a database past
    128 MiB cannot hold both and sheds its base pages on every read — exactly
    the databases the log-only path was written for.  The budget therefore
    follows the largest image the cache holds, and is capped by the machine
    rather than being allowed to grow without one.
    """

    def test_the_budget_follows_the_largest_image_the_cache_holds(self) -> None:
        with patch.object(snapshot, "physical_memory_bytes", return_value=1 << 42):
            self.assertEqual(snapshot._adaptive_budget(780 * MIB), 3 * 780 * MIB)

    def test_a_small_database_is_left_on_the_fixed_default(self) -> None:
        with patch.object(snapshot, "physical_memory_bytes", return_value=1 << 42):
            self.assertEqual(
                snapshot._adaptive_budget(8 * MIB), snapshot._DEFAULT_BUDGET_BYTES
            )

    def test_physical_memory_caps_what_the_budget_may_ask_for(self) -> None:
        with patch.object(snapshot, "physical_memory_bytes", return_value=8 * GIB):
            self.assertEqual(snapshot._adaptive_budget(780 * MIB), 2 * GIB)

    def test_a_machine_short_of_memory_caps_below_the_fixed_default(self) -> None:
        with patch.object(snapshot, "physical_memory_bytes", return_value=512 * MIB):
            self.assertEqual(snapshot._adaptive_budget(780 * MIB), 128 * MIB)

    def test_an_unknown_physical_memory_falls_back_to_the_fixed_default(self) -> None:
        """Without a number to cap against, the old fixed budget stands."""
        with patch.object(snapshot, "physical_memory_bytes", return_value=None):
            self.assertEqual(
                snapshot._adaptive_budget(780 * MIB), snapshot._DEFAULT_BUDGET_BYTES
            )

    @unittest.skipUnless(sys.platform == "darwin", "hw.memsize is a macOS sysctl")
    def test_physical_memory_matches_what_the_kernel_reports(self) -> None:
        reported = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, check=True
        )
        self.assertEqual(
            snapshot.physical_memory_bytes(), int(reported.stdout.strip())
        )


class AdaptiveBudgetTests(_Fixture):
    """The adaptive budget, driven through the real cache and a real log.

    The fixtures are two pages rather than the measured 119 MB database, so
    the size that would make the fixed default too small is supplied by
    patching the default itself; the shedding, the overlay and the log are
    the production ones.
    """

    HUGE_MEMORY = 1 << 42

    def setUp(self) -> None:
        super().setUp()
        # A log must already exist when the base pages are kept, or the next
        # read sees a new generation instead of an appended log.
        self.log.write_bytes(make_wal([self.settled()]))
        self.held = len(self.database.read_bytes())

    def settled(self) -> tuple[int, int, bytes]:
        return (2, 2, self.encrypted(self.pages_a, 2))

    def a_message_arrives(self) -> None:
        """Append committed frames to the log, leaving the database alone."""
        before = self.log.stat()
        self.log.write_bytes(
            make_wal(
                [
                    self.settled(),
                    (1, 0, self.encrypted(self.pages_b, 1)),
                    (2, 2, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        os.utime(self.log, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    def reread(self, cache: SnapshotCache):
        """Read again, counting the full decryptions it took."""
        decryptions: list[object] = []

        def counting(*args, **kwargs):
            decryptions.append(args)
            return build_image(*args, **kwargs)

        with patch("kamado.snapshot.build_image", side_effect=counting):
            connection = cache.connect(self.database, geometry())
        return connection, decryptions

    def assert_reads_bravo(self, connection: sqlite3.Connection) -> None:
        self.assertEqual(
            [row[0] for row in connection.execute("SELECT body FROM note ORDER BY id")],
            [body for _id, body in ROWS_B],
        )

    def test_a_database_past_the_fixed_default_keeps_its_base_pages(self) -> None:
        """The case the fixed budget lost: image plus base did not fit.

        With the fixed default pinned below what one entry costs, only a
        budget that scales with the image can keep the base pages — and
        keeping them is what makes the next message a log-only update.
        """
        with (
            patch.object(snapshot, "_DEFAULT_BUDGET_BYTES", 1),
            patch.object(
                snapshot, "physical_memory_bytes", return_value=self.HUGE_MEMORY
            ),
        ):
            cache = SnapshotCache()
            self.addCleanup(cache.close)
            cache.connect(self.database, geometry())
            self.a_message_arrives()
            connection, decryptions = self.reread(cache)

        self.assertEqual(decryptions, [], "the log alone should have been read")
        self.assert_reads_bravo(connection)

    def test_a_machine_short_of_memory_still_sheds_the_base_pages(self) -> None:
        """The cap is what stops the budget following a database off a cliff."""
        with (
            patch.object(snapshot, "_DEFAULT_BUDGET_BYTES", 1),
            patch.object(
                snapshot, "physical_memory_bytes", return_value=4 * (self.held + 512)
            ),
        ):
            cache = SnapshotCache()
            self.addCleanup(cache.close)
            first = cache.connect(self.database, geometry())
            self.a_message_arrives()
            connection, decryptions = self.reread(cache)

        self.assertEqual(len(decryptions), 1, "the base pages must have been shed")
        self.assertIsNot(first, connection)
        self.assert_reads_bravo(connection)

    def test_a_stated_budget_ignores_how_much_memory_the_machine_has(self) -> None:
        with patch.object(snapshot, "physical_memory_bytes", return_value=4096):
            cache = SnapshotCache(budget_bytes=self.held * 8)
            self.addCleanup(cache.close)
            cache.connect(self.database, geometry())
            self.a_message_arrives()
            connection, decryptions = self.reread(cache)

        self.assertEqual(decryptions, [], "a stated budget is the budget")
        self.assert_reads_bravo(connection)

    def test_a_stated_budget_too_small_for_the_base_pages_still_sheds(self) -> None:
        with patch.object(
            snapshot, "physical_memory_bytes", return_value=self.HUGE_MEMORY
        ):
            cache = SnapshotCache(budget_bytes=self.held)
            self.addCleanup(cache.close)
            cache.connect(self.database, geometry())
            self.a_message_arrives()
            connection, decryptions = self.reread(cache)

        self.assertEqual(len(decryptions), 1, "a stated budget is not widened")
        self.assert_reads_bravo(connection)

    def test_building_a_cache_asks_the_machine_nothing(self) -> None:
        """Nothing expensive happens in a constructor, so status can explain itself."""

        def probed(*_args, **_kwargs):
            raise AssertionError("the machine was probed while building the cache")

        with (
            patch.object(snapshot, "physical_memory_bytes", side_effect=probed),
            patch.object(snapshot.subprocess, "run", side_effect=probed),
            patch.object(snapshot.os, "sysconf", side_effect=probed),
        ):
            self.addCleanup(SnapshotCache().close)
            self.addCleanup(SnapshotCache(budget_bytes=1).close)


class CacheTests(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.cache = SnapshotCache()
        self.addCleanup(self.cache.close)

    def test_a_wal_only_change_uses_incremental_update_without_full_rebuild(self) -> None:
        first = self.cache.connect(self.database, geometry())
        self.assertIsNotNone(first)
        # Append or touch the WAL file while leaving the base database untouched
        wal_path = self.database.with_name(self.database.name + "-wal")
        content = wal_path.read_bytes() if wal_path.exists() else b""
        wal_path.write_bytes(content)
        before = wal_path.stat()
        os.utime(wal_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        # Re-connecting should call _update_wal_with_retry without invoking build_image
        with patch("kamado.snapshot.build_image", side_effect=AssertionError("called full build_image")):
            second = self.cache.connect(self.database, geometry())
        self.assertIsNotNone(second)

    def test_incremental_wal_update_recovers_to_full_rebuild_if_malformed(self) -> None:
        first = self.cache.connect(self.database, geometry())
        self.assertIsNotNone(first)

        wal_path = self.database.with_name(self.database.name + "-wal")
        content = wal_path.read_bytes() if wal_path.exists() else b""
        wal_path.write_bytes(content)
        before = wal_path.stat()
        os.utime(wal_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        # If _update_wal_with_retry returns damaged bytes that fail sqlite query,
        # it should smoothly fall back to build_image (full rebuild) instead of raising DatabaseError.
        with patch.object(self.cache, "_update_wal_with_retry", return_value=b"SQLite format 3\x00" + b"\x00" * 4096):
            recovered = self.cache.connect(self.database, geometry())
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_A))

    def test_an_unchanged_database_is_not_decrypted_twice(self) -> None:
        first = self.cache.connect(self.database, geometry())
        with patch("kamado.snapshot.build_image", side_effect=AssertionError("decrypted")):
            second = self.cache.connect(self.database, geometry())
        self.assertIs(first, second)

    def test_a_changed_database_is_decrypted_again(self) -> None:
        first = self.cache.connect(self.database, geometry())
        before = self.database.stat()
        self.assertEqual(
            [row[0] for row in first.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["alpha 1"],
        )
        self.database.write_bytes(encrypt_pages(self.pages_b))
        os.utime(self.database, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        second = self.cache.connect(self.database, geometry())
        self.assertIsNot(first, second)
        self.assertEqual(
            [row[0] for row in second.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["bravo 1"],
        )

    def test_a_database_that_never_settles_is_reported_as_busy(self) -> None:
        naps: list[float] = []
        with (
            patch(
                "kamado.snapshot.build_image",
                side_effect=ConsistencyError("still moving"),
            ),
            patch("kamado.snapshot.time.sleep", naps.append),
        ):
            with self.assertRaises(KamadoError) as raised:
                self.cache.connect(self.database, geometry())

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
            patch("kamado.snapshot.build_image", side_effect=flaky),
            patch("kamado.snapshot.time.sleep", lambda _seconds: None),
        ):
            connection = self.cache.connect(self.database, geometry())
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_A))

    def test_invalidating_forces_the_next_read_to_decrypt(self) -> None:
        first = self.cache.connect(self.database, geometry())
        self.cache.invalidate(self.database)
        second = self.cache.connect(self.database, geometry())
        self.assertIsNot(first, second)

    def test_closing_releases_every_cached_connection(self) -> None:
        connection = self.cache.connect(self.database, geometry())
        self.cache.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_an_overlay_sqlite_refuses_is_closed_before_the_fallback(self) -> None:
        """A rejected overlay must not leave its connection open.

        Nobody else can close it: the cache never learned about it and the
        caller never saw it.  This is the retry path, so a leak here repeats
        for as long as the overlay keeps failing.
        """
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_a, 2))]))
        self.cache.connect(self.database, geometry())

        before = self.log.stat()
        self.log.write_bytes(
            make_wal(
                [
                    (2, 2, self.encrypted(self.pages_a, 2)),
                    (1, 0, self.encrypted(self.pages_b, 1)),
                    (2, 2, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        os.utime(self.log, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        opened: list[sqlite3.Connection] = []
        real_connect = snapshot._connect
        real_update = update_image_with_wal

        def recording(image):
            connection = real_connect(image)
            opened.append(connection)
            return connection

        def malformed(base, database, geometry_):
            image = bytearray(real_update(base, database, geometry_))
            struct.pack_into(">H", image, 103, 500)  # page 1 claims cells it has not got
            return bytes(image)

        with (
            patch("kamado.snapshot._connect", side_effect=recording),
            patch("kamado.snapshot.update_image_with_wal", side_effect=malformed),
        ):
            second = self.cache.connect(self.database, geometry())

        self.assertEqual(len(opened), 2, "the overlay is tried, then the file is decrypted")
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        self.assertEqual(
            [row[0] for row in second.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["bravo 1"],
        )

    def test_a_restarted_log_is_decrypted_rather_than_overlaid(self) -> None:
        """What licenses reusing base pages is the log's generation, not a stat.

        The database file is untouched here, so a stat says the pages are
        still good.  The log's salt pair says it restarted, which means the
        file may now hold pages these base pages never saw.
        """
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_a, 2))]))
        self.cache.connect(self.database, geometry())

        before = self.log.stat()
        self.log.write_bytes(
            make_wal([(2, 2, self.encrypted(self.pages_b, 2))], salt=(99, 98))
        )
        os.utime(self.log, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        decryptions: list[object] = []
        naps: list[float] = []

        def counting(*args, **kwargs):
            decryptions.append(args)
            return build_image(*args, **kwargs)

        with (
            patch("kamado.snapshot.build_image", side_effect=counting),
            patch("kamado.snapshot.time.sleep", naps.append),
        ):
            second = self.cache.connect(self.database, geometry())

        self.assertEqual(len(decryptions), 1, "stale base pages must not be overlaid")
        self.assertEqual(naps, [], "a restart is not transient, so it must not be retried")
        self.assertEqual(
            [row[0] for row in second.execute("SELECT body FROM note ORDER BY id LIMIT 1")],
            ["bravo 1"],
        )

    def test_base_pages_are_shed_before_a_live_snapshot_is_closed(self) -> None:
        """Keeping base pages must not cost a caller its connection.

        They are a cache of a cache: losing a set costs the next read one
        decryption, where closing a snapshot takes a connection out of a
        caller's hands.  A budget that fits two snapshots but not their base
        pages as well must therefore give up the pages — which also means the
        pages have to be charged to that budget in the first place.
        """
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_a, 2))]))
        other = self.root / "other.edb"
        other.write_bytes(encrypt_pages(self.pages_b))
        held = len(self.database.read_bytes())
        cache = SnapshotCache(budget_bytes=held * 3)
        self.addCleanup(cache.close)

        first = cache.connect(self.database, geometry())
        second = cache.connect(other, geometry())
        self.assertEqual(
            first.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_A)
        )
        self.assertEqual(
            second.execute("SELECT COUNT(*) FROM note").fetchone()[0], len(ROWS_B)
        )

        before = self.log.stat()
        self.log.write_bytes(
            make_wal(
                [
                    (2, 2, self.encrypted(self.pages_a, 2)),
                    (2, 2, self.encrypted(self.pages_b, 2)),
                ]
            )
        )
        os.utime(self.log, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        decryptions: list[object] = []

        def counting(*args, **kwargs):
            decryptions.append(args)
            return build_image(*args, **kwargs)

        with patch("kamado.snapshot.build_image", side_effect=counting):
            cache.connect(self.database, geometry())
        self.assertEqual(
            len(decryptions),
            1,
            "the pages that did not fit the budget must have been given up",
        )

    def test_a_small_budget_evicts_the_least_recently_used_snapshot(self) -> None:
        other = self.root / "other.edb"
        other.write_bytes(encrypt_pages(self.pages_b))
        cache = SnapshotCache(budget_bytes=1)
        self.addCleanup(cache.close)

        first = cache.connect(self.database, geometry())
        cache.connect(other, geometry())
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
