"""Write-ahead log tests.

Getting this wrong is silent: a mistake here does not crash, it reports an old
or torn conversation as if it were current.  So every rule about what counts as
committed is pinned down, including the ones that only matter while KakaoTalk
is actively writing.
"""

from __future__ import annotations

import struct
import unittest

from kakaoread.wal import WAL_HEADER_SIZE, WalFormatError, parse

from tests.support import PAGE_SIZE, make_wal

BASE_PAGES = 4
FRAME_SIZE = PAGE_SIZE + 24


def body(marker: bytes) -> bytes:
    return (marker * PAGE_SIZE)[:PAGE_SIZE]


FIRST = body(b"A")
SECOND = body(b"B")
THIRD = body(b"C")


class EmptyLogTests(unittest.TestCase):
    def test_absent_log_contributes_nothing(self) -> None:
        snapshot = parse(b"", PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.pages, {})
        self.assertEqual(snapshot.database_pages, BASE_PAGES)

    def test_header_only_log_contributes_nothing(self) -> None:
        snapshot = parse(make_wal([]), PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.pages, {})
        self.assertEqual(snapshot.database_pages, BASE_PAGES)


class CommitVisibilityTests(unittest.TestCase):
    def test_frames_are_invisible_until_a_commit_frame_arrives(self) -> None:
        snapshot = parse(make_wal([(2, 0, FIRST), (3, 0, SECOND)]), PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.pages, {})
        self.assertEqual(snapshot.database_pages, BASE_PAGES)

    def test_a_commit_reveals_every_frame_of_its_transaction_at_once(self) -> None:
        snapshot = parse(
            make_wal([(2, 0, FIRST), (3, BASE_PAGES, SECOND)]), PAGE_SIZE, BASE_PAGES
        )
        self.assertEqual(snapshot.pages, {2: FIRST, 3: SECOND})
        self.assertEqual(snapshot.database_pages, BASE_PAGES)

    def test_the_last_commit_wins_for_a_repeated_page(self) -> None:
        snapshot = parse(
            make_wal([(2, BASE_PAGES, FIRST), (2, BASE_PAGES, SECOND)]), PAGE_SIZE, BASE_PAGES
        )
        self.assertEqual(snapshot.pages[2], SECOND)

    def test_an_uncommitted_overwrite_does_not_replace_a_committed_page(self) -> None:
        snapshot = parse(
            make_wal([(2, BASE_PAGES, FIRST), (2, 0, THIRD)]), PAGE_SIZE, BASE_PAGES
        )
        self.assertEqual(snapshot.pages[2], FIRST)

    def test_both_checksum_byte_orders_are_accepted(self) -> None:
        for order in ("<", ">"):
            with self.subTest(order=order):
                snapshot = parse(
                    make_wal([(2, BASE_PAGES, FIRST)], order=order), PAGE_SIZE, BASE_PAGES
                )
                self.assertEqual(snapshot.pages, {2: FIRST})

    def test_a_commit_that_truncates_the_database_is_reported(self) -> None:
        snapshot = parse(make_wal([(2, 2, FIRST)]), PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.database_pages, 2)

    def test_a_commit_that_grows_the_database_is_reported(self) -> None:
        snapshot = parse(make_wal([(5, 5, FIRST)]), PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.database_pages, 5)
        self.assertEqual(snapshot.pages, {5: FIRST})


class WriterInFlightTests(unittest.TestCase):
    """A live writer leaves partial and stale bytes behind; neither is corruption."""

    def test_a_partial_trailing_frame_leaves_the_previous_commit_intact(self) -> None:
        log = make_wal([(2, BASE_PAGES, FIRST), (3, BASE_PAGES, SECOND)])
        snapshot = parse(log[:-1], PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.pages, {2: FIRST})

    def test_a_frame_from_an_older_generation_ends_the_current_one(self) -> None:
        log = bytearray(make_wal([(2, BASE_PAGES, FIRST), (3, BASE_PAGES, SECOND)]))
        # Corrupt only the second frame's salt, as log reuse would.
        salt_offset = WAL_HEADER_SIZE + FRAME_SIZE + 8
        log[salt_offset] ^= 0xFF
        snapshot = parse(bytes(log), PAGE_SIZE, BASE_PAGES)
        self.assertEqual(snapshot.pages, {2: FIRST})


class CorruptionTests(unittest.TestCase):
    def test_a_truncated_header_is_refused(self) -> None:
        with self.assertRaises(WalFormatError):
            parse(make_wal([])[: WAL_HEADER_SIZE - 1], PAGE_SIZE, BASE_PAGES)

    def test_an_unknown_magic_is_refused(self) -> None:
        log = bytearray(make_wal([]))
        struct.pack_into(">I", log, 0, 0x12345678)
        with self.assertRaises(WalFormatError):
            parse(bytes(log), PAGE_SIZE, BASE_PAGES)

    def test_an_unsupported_version_is_refused(self) -> None:
        with self.assertRaises(WalFormatError):
            parse(make_wal([], version=3006000), PAGE_SIZE, BASE_PAGES)

    def test_a_page_size_mismatch_is_refused(self) -> None:
        with self.assertRaises(WalFormatError):
            parse(make_wal([], page_size=PAGE_SIZE), PAGE_SIZE * 2, BASE_PAGES)

    def test_a_bad_header_checksum_is_refused(self) -> None:
        log = bytearray(make_wal([]))
        log[24] ^= 1
        with self.assertRaises(WalFormatError):
            parse(bytes(log), PAGE_SIZE, BASE_PAGES)

    def test_a_bad_frame_checksum_is_refused_even_after_a_good_commit(self) -> None:
        log = bytearray(make_wal([(2, BASE_PAGES, FIRST), (3, BASE_PAGES, SECOND)]))
        log[-40] ^= 1  # Flip a byte inside the second frame's page image.
        with self.assertRaises(WalFormatError):
            parse(bytes(log), PAGE_SIZE, BASE_PAGES)

    def test_impossible_page_numbers_are_refused(self) -> None:
        for number in (0, 0xFFFFFFFF):
            with self.subTest(number=number), self.assertRaises(WalFormatError):
                parse(make_wal([(number, BASE_PAGES, FIRST)]), PAGE_SIZE, BASE_PAGES)

    def test_an_impossible_commit_size_is_refused(self) -> None:
        with self.assertRaises(WalFormatError):
            parse(make_wal([(2, 0xFFFFFFFF, FIRST)]), PAGE_SIZE, BASE_PAGES)


if __name__ == "__main__":
    unittest.main()
