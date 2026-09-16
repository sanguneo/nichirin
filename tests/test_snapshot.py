"""Snapshot tests: consistency under a live writer, and the cache that pays for it."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.snapshot import (
    ConsistencyError,
    Fingerprint,
    SnapshotCache,
    build_image,
    open_image,
)

from tests.support import PAGE_SIZE, build_database, encrypt_pages, make_wal, geometry

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
        with self.assertRaises(KakaoReadError) as raised:
            build_image(self.database, geometry())
        self.assertEqual(raised.exception.code, ErrorCode.READ_FAILED)

    def test_rejects_an_empty_database(self) -> None:
        self.database.write_bytes(b"")
        with self.assertRaises(KakaoReadError):
            build_image(self.database, geometry())

    def test_rejects_a_commit_that_leaves_pages_undefined(self) -> None:
        # Two frames raise the log's page ceiling to four, then the commit
        # claims four pages while page three is never supplied by either the
        # base file or the log.
        body = self.encrypted(self.pages_b, 2)
        self.log.write_bytes(make_wal([(2, 0, body), (4, 4, body)]))
        with self.assertRaises(KakaoReadError) as raised:
            build_image(self.database, geometry())
        self.assertEqual(raised.exception.code, ErrorCode.READ_FAILED)

    def test_detects_a_same_size_rewrite_even_when_stat_reports_no_change(self) -> None:
        """The case a timestamp check would wave through.

        The replacement is exactly as long as the original and the fingerprint
        is pinned to a constant, so only comparing the bytes themselves can
        notice that the database moved underneath the read.
        """
        frozen = Fingerprint.of(self.database)
        self.assertEqual(len(encrypt_pages(self.pages_b)), len(self.database.read_bytes()))

        with (
            patch("kakaoread.snapshot.Fingerprint.of", return_value=frozen),
            patch(
                "kakaoread.snapshot.pages.reconstruct_page", side_effect=self._rewriting()
            ),
        ):
            with self.assertRaises(ConsistencyError):
                build_image(self.database, geometry())

    def test_detects_the_log_growing_a_new_prefix_during_the_read(self) -> None:
        self.log.write_bytes(make_wal([(2, 2, self.encrypted(self.pages_b, 2))]))
        replacement = make_wal([(2, 2, self.encrypted(self.pages_a, 2))], salt=(99, 98))

        # Bind the real function before patching, or the wrapper would look up
        # its own replacement and recurse.
        from kakaoread.page import reconstruct_page as real_reconstruct

        def swap_log(prepared, number, page, page_size=PAGE_SIZE, reserve=80):
            self.log.write_bytes(replacement)
            return real_reconstruct(prepared, number, page, page_size, reserve)

        with patch("kakaoread.snapshot.pages.reconstruct_page", side_effect=swap_log):
            with self.assertRaises(ConsistencyError):
                build_image(self.database, geometry())

    def _rewriting(self):
        """Rewrite the database once, part-way through reconstructing it.

        The real function is captured here, before any patching, so the
        wrapper cannot end up calling itself.
        """
        from kakaoread.page import reconstruct_page as real_reconstruct

        state = {"done": False}

        def wrapper(prepared, number, page, page_size=PAGE_SIZE, reserve=80):
            if not state["done"]:
                state["done"] = True
                self.database.write_bytes(encrypt_pages(self.pages_b))
            return real_reconstruct(prepared, number, page, page_size, reserve)

        return wrapper


class OpenImageTests(_Fixture):
    def test_refuses_an_image_that_is_not_a_valid_database(self) -> None:
        with self.assertRaises(Exception):
            open_image(b"not a database at all" + bytes(4096), "fixture")

    def test_accepts_a_genuine_image(self) -> None:
        connection = open_image(build_image(self.database, geometry()), "fixture")
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])


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
        with patch("kakaoread.snapshot.build_image", side_effect=AssertionError("called full build_image")):
            second = self.cache.connect(self.database, geometry())
        self.assertIsNotNone(second)

    def test_an_unchanged_database_is_not_decrypted_twice(self) -> None:
        first = self.cache.connect(self.database, geometry())
        with patch("kakaoread.snapshot.build_image", side_effect=AssertionError("decrypted")):
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
                "kakaoread.snapshot.build_image",
                side_effect=ConsistencyError("still moving"),
            ),
            patch("kakaoread.snapshot.time.sleep", naps.append),
        ):
            with self.assertRaises(KakaoReadError) as raised:
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
            patch("kakaoread.snapshot.build_image", side_effect=flaky),
            patch("kakaoread.snapshot.time.sleep", lambda _seconds: None),
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
