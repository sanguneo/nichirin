"""Page codec tests: real encrypt/decrypt round-trips, not mocked ones."""

from __future__ import annotations

import unittest

from kanao.cng import PreparedKey
from kanao.page import (
    SALT_SIZE,
    SQLITE_HEADER,
    read_salt,
    reconstruct_page,
    validate_geometry,
)

from tests.support import KEY, PAGE_SIZE, RESERVE, SALT, encrypted_page

_MARKER_OFFSET = 24
_MARKER = b"POSTSALT"


def _plaintext_page(filler: bytes = b"\xab") -> bytes:
    """A full page whose every region is distinguishable after a round-trip.

    Byte 20 is SQLite's "reserved bytes per page" field, so markers are placed
    clear of it rather than being silently overwritten by it.
    """
    body = bytearray(filler * PAGE_SIZE)
    body[:SALT_SIZE] = b"HEADERHEADER1234"
    body[20] = RESERVE
    body[_MARKER_OFFSET : _MARKER_OFFSET + len(_MARKER)] = _MARKER
    body[PAGE_SIZE - RESERVE - 8 : PAGE_SIZE - RESERVE] = b"LASTBYTE"
    body[PAGE_SIZE - RESERVE :] = bytes(RESERVE)
    return bytes(body)


class RoundTripTests(unittest.TestCase):
    def test_page_one_comes_back_with_the_sqlite_header_restored(self) -> None:
        plaintext = _plaintext_page()
        page = encrypted_page(plaintext, number=1)

        with PreparedKey(KEY) as key:
            restored = reconstruct_page(key, 1, page)

        self.assertEqual(len(restored), PAGE_SIZE)
        self.assertTrue(restored.startswith(SQLITE_HEADER))
        self.assertEqual(restored[_MARKER_OFFSET : _MARKER_OFFSET + len(_MARKER)], _MARKER)
        self.assertEqual(restored[20], RESERVE)
        self.assertEqual(restored[PAGE_SIZE - RESERVE - 8 : PAGE_SIZE - RESERVE], b"LASTBYTE")
        self.assertEqual(restored[PAGE_SIZE - RESERVE :], bytes(RESERVE))

    def test_page_one_keeps_every_byte_after_the_salt(self) -> None:
        plaintext = _plaintext_page()
        with PreparedKey(KEY) as key:
            restored = reconstruct_page(key, 1, encrypted_page(plaintext, number=1))
        self.assertEqual(
            restored[SALT_SIZE : PAGE_SIZE - RESERVE],
            plaintext[SALT_SIZE : PAGE_SIZE - RESERVE],
        )

    def test_later_pages_come_back_whole_including_their_first_bytes(self) -> None:
        plaintext = _plaintext_page(b"\x5c")
        page = encrypted_page(plaintext, number=2)

        with PreparedKey(KEY) as key:
            restored = reconstruct_page(key, 2, page)

        self.assertEqual(len(restored), PAGE_SIZE)
        self.assertEqual(restored[: PAGE_SIZE - RESERVE], plaintext[: PAGE_SIZE - RESERVE])
        self.assertEqual(restored[PAGE_SIZE - RESERVE :], bytes(RESERVE))

    def test_page_one_is_encrypted_from_a_different_offset_than_later_pages(self) -> None:
        plaintext = _plaintext_page()
        with PreparedKey(KEY) as key:
            first = reconstruct_page(key, 1, encrypted_page(plaintext, number=1))
            second = reconstruct_page(key, 2, encrypted_page(plaintext, number=2))
        self.assertEqual(first[:SALT_SIZE], SQLITE_HEADER)
        self.assertEqual(second[:SALT_SIZE], plaintext[:SALT_SIZE])
        self.assertNotEqual(first[:SALT_SIZE], second[:SALT_SIZE])

    def test_one_prepared_key_reconstructs_a_whole_run_of_pages(self) -> None:
        numbers = list(range(2, 14))
        plaintexts = [_plaintext_page(bytes([number])) for number in numbers]
        encrypted = [
            encrypted_page(body, number=number) for number, body in zip(numbers, plaintexts)
        ]

        with PreparedKey(KEY) as key:
            restored = [
                reconstruct_page(key, number, body)
                for number, body in zip(numbers, encrypted)
            ]

        for number, expected, actual in zip(numbers, plaintexts, restored):
            with self.subTest(page=number):
                self.assertEqual(
                    actual[: PAGE_SIZE - RESERVE], expected[: PAGE_SIZE - RESERVE]
                )


class SaltTests(unittest.TestCase):
    def test_reads_the_salt_that_identifies_the_key(self) -> None:
        page = encrypted_page(_plaintext_page(), number=1)
        self.assertEqual(read_salt(page), SALT)

    def test_rejects_a_buffer_too_short_to_hold_a_salt(self) -> None:
        with self.assertRaises(ValueError):
            read_salt(bytes(SALT_SIZE - 1))


class GeometryTests(unittest.TestCase):
    def test_accepts_the_geometry_this_client_uses(self) -> None:
        validate_geometry(PAGE_SIZE, RESERVE)

    def test_rejects_page_sizes_that_are_not_whole_aes_blocks(self) -> None:
        for page_size in (0, -4096, 4095, 100):
            with self.subTest(page_size=page_size), self.assertRaises(ValueError):
                validate_geometry(page_size, RESERVE)

    def test_rejects_reserves_that_cannot_hold_an_iv_or_leave_no_payload(self) -> None:
        for reserve in (0, 15, PAGE_SIZE, PAGE_SIZE + 16):
            with self.subTest(reserve=reserve), self.assertRaises(ValueError):
                validate_geometry(PAGE_SIZE, reserve)

    def test_rejects_a_page_number_below_one(self) -> None:
        page = encrypted_page(_plaintext_page(), number=1)
        with PreparedKey(KEY) as key:
            for number in (0, -1):
                with self.subTest(number=number), self.assertRaises(ValueError):
                    reconstruct_page(key, number, page)

    def test_rejects_a_page_buffer_shorter_than_the_page_size(self) -> None:
        page = encrypted_page(_plaintext_page(), number=1)
        with PreparedKey(KEY) as key, self.assertRaises(ValueError):
            reconstruct_page(key, 1, page[:-1])


if __name__ == "__main__":
    unittest.main()
