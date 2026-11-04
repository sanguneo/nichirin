"""Heap scanning tests.

The equivalent code in the predecessor project had no coverage of the block
walk at all — only of the pure decode helpers — so a context landing across a
chunk boundary could have been dropped silently, making a conversation simply
invisible.  The split tests below are the point of this file.
"""

from __future__ import annotations

import struct
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from kanao.process import (
    CONTEXT_SIZE,
    KEY_SIZE,
    SALT_SIZE,
    CodecContext,
    decode_context,
    resolve_context,
    resolve_addresses,
    resolve_many,
    scan_process,
    scan_blocks,
)

from tests.support import codec_context, memory_reader

SALT_ADDRESS = 0x1000
READ_CONTEXT_ADDRESS = 0x2000
KEY_ADDRESS = 0x3000
BASE_ADDRESS = 0x10_0000
SALT = bytes(range(SALT_SIZE))
KEY = bytes(range(KEY_SIZE))


def _memory():
    return memory_reader(
        {
            (SALT_ADDRESS, SALT_SIZE): SALT,
            (READ_CONTEXT_ADDRESS + 0x08, 8): struct.pack("<Q", KEY_ADDRESS),
            (KEY_ADDRESS, KEY_SIZE): KEY,
        }
    )


class DecodeTests(unittest.TestCase):
    def test_accepts_the_layout_this_client_uses(self) -> None:
        self.assertEqual(
            decode_context(codec_context()), (SALT_ADDRESS, READ_CONTEXT_ADDRESS)
        )

    def test_rejects_a_struct_whose_every_marker_does_not_match(self) -> None:
        for field, wrong in (
            ("iterations", 1000),
            ("key_size", 16),
            ("page_size", 1024),
            ("reserve", 48),
        ):
            with self.subTest(field=field):
                self.assertIsNone(decode_context(codec_context(**{field: wrong})))

    def test_rejects_a_truncated_struct(self) -> None:
        self.assertIsNone(decode_context(codec_context()[: CONTEXT_SIZE - 1]))

    def test_rejects_null_pointers(self) -> None:
        self.assertIsNone(decode_context(codec_context(salt_pointer=0)))
        self.assertIsNone(decode_context(codec_context(read_context=0)))


class ResolveTests(unittest.TestCase):
    def test_follows_salt_and_key_pointers(self) -> None:
        found = resolve_context(BASE_ADDRESS, codec_context(), _memory())
        self.assertEqual(found, CodecContext(BASE_ADDRESS, SALT, KEY))

    def test_gives_up_when_the_salt_cannot_be_read(self) -> None:
        memory = memory_reader({(READ_CONTEXT_ADDRESS + 0x08, 8): struct.pack("<Q", KEY_ADDRESS)})
        self.assertIsNone(resolve_context(BASE_ADDRESS, codec_context(), memory))

    def test_gives_up_on_a_short_pointer_read(self) -> None:
        memory = memory_reader(
            {(SALT_ADDRESS, SALT_SIZE): SALT, (READ_CONTEXT_ADDRESS + 0x08, 8): b"\x00" * 4}
        )
        self.assertIsNone(resolve_context(BASE_ADDRESS, codec_context(), memory))

    def test_gives_up_when_the_key_itself_is_unreadable(self) -> None:
        memory = memory_reader(
            {
                (SALT_ADDRESS, SALT_SIZE): SALT,
                (READ_CONTEXT_ADDRESS + 0x08, 8): struct.pack("<Q", KEY_ADDRESS),
            }
        )
        self.assertIsNone(resolve_context(BASE_ADDRESS, codec_context(), memory))

    def test_gives_up_on_a_null_key_pointer(self) -> None:
        memory = memory_reader(
            {
                (SALT_ADDRESS, SALT_SIZE): SALT,
                (READ_CONTEXT_ADDRESS + 0x08, 8): struct.pack("<Q", 0),
            }
        )
        self.assertIsNone(resolve_context(BASE_ADDRESS, codec_context(), memory))


class BlockWalkTests(unittest.TestCase):
    """A context must be found no matter where the chunk boundaries land."""

    PREFIX = 300
    SUFFIX = 300

    def _region(self) -> bytes:
        return b"\x00" * self.PREFIX + codec_context() + b"\x00" * self.SUFFIX

    def test_finds_a_context_inside_a_single_block(self) -> None:
        found = list(scan_blocks([(BASE_ADDRESS, self._region())], _memory()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].address, BASE_ADDRESS + self.PREFIX)
        self.assertEqual(found[0].key, KEY)

    def test_finds_a_context_split_across_every_possible_boundary(self) -> None:
        region = self._region()
        for split in range(1, len(region)):
            with self.subTest(split=split):
                blocks = [
                    (BASE_ADDRESS, region[:split]),
                    (BASE_ADDRESS + split, region[split:]),
                ]
                found = list(scan_blocks(blocks, _memory()))
                self.assertEqual(len(found), 1, f"lost the context at split {split}")
                self.assertEqual(found[0].address, BASE_ADDRESS + self.PREFIX)

    def test_finds_a_context_split_across_three_blocks(self) -> None:
        region = self._region()
        first, second = self.PREFIX + 8, self.PREFIX + CONTEXT_SIZE - 8
        blocks = [
            (BASE_ADDRESS, region[:first]),
            (BASE_ADDRESS + first, region[first:second]),
            (BASE_ADDRESS + second, region[second:]),
        ]
        found = list(scan_blocks(blocks, _memory()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].address, BASE_ADDRESS + self.PREFIX)

    def test_does_not_stitch_together_unrelated_regions(self) -> None:
        region = self._region()
        split = self.PREFIX + 8
        blocks = [
            (BASE_ADDRESS, region[:split]),
            (BASE_ADDRESS + 0x9000_0000, region[split:]),  # A different allocation.
        ]
        self.assertEqual(list(scan_blocks(blocks, _memory())), [])

    def test_reports_each_address_once_even_if_a_block_repeats(self) -> None:
        region = self._region()
        blocks = [(BASE_ADDRESS, region), (BASE_ADDRESS, region)]
        self.assertEqual(len(list(scan_blocks(blocks, _memory()))), 1)

    def test_finds_several_distinct_contexts_in_one_block(self) -> None:
        region = self._region() + self._region()
        found = list(scan_blocks([(BASE_ADDRESS, region)], _memory()))
        self.assertEqual(len(found), 2)
        self.assertNotEqual(found[0].address, found[1].address)

    def test_ignores_a_signature_whose_struct_does_not_validate(self) -> None:
        decoy = b"\x00" * self.PREFIX + codec_context(page_size=1024) + b"\x00" * self.SUFFIX
        self.assertEqual(list(scan_blocks([(BASE_ADDRESS, decoy)], _memory())), [])

    def test_ignores_a_valid_struct_whose_key_cannot_be_read(self) -> None:
        self.assertEqual(
            list(scan_blocks([(BASE_ADDRESS, self._region())], memory_reader({}))), []
        )

    def test_handles_an_empty_stream_of_blocks(self) -> None:
        self.assertEqual(list(scan_blocks([], _memory())), [])


class X86ContextTests(unittest.TestCase):
    """32-bit pointers must survive scanning and cached-address resolution."""

    def setUp(self) -> None:
        self.blob = bytearray(0x50)
        for offset, value in (
            (0x04, 256000), (0x10, 32), (0x1C, 4096), (0x24, 80),
            (0x3C, SALT_ADDRESS), (0x4C, READ_CONTEXT_ADDRESS),
        ):
            struct.pack_into("<I", self.blob, offset, value)
        self.blob = bytes(self.blob)
        self.mapping = {
            (BASE_ADDRESS, len(self.blob)): self.blob,
            (SALT_ADDRESS, SALT_SIZE): SALT,
            (READ_CONTEXT_ADDRESS + 0x08, 4): struct.pack("<I", KEY_ADDRESS),
            (KEY_ADDRESS, KEY_SIZE): KEY,
        }
        self.expected = CodecContext(BASE_ADDRESS, SALT, KEY)
        self.wow64 = True
        self.architecture_query_ok = True

    @contextmanager
    def opened(self, pid):
        blob = self.blob
        wow64, query_ok = self.wow64, self.architecture_query_ok

        class Library:
            def IsWow64Process(self, handle, result):
                result._obj.value = wow64
                return query_ok

            def VirtualQueryEx(self, handle, address, result, size):
                if address.value:
                    return 0
                region = result._obj
                region.base_address = BASE_ADDRESS
                region.region_size = len(blob)
                region.state = 0x1000
                region.type = 0x20000
                region.protect = 0x04
                return size

        yield Library(), 123, memory_reader(self.mapping)

    def test_live_scan_uses_the_target_width_not_the_python_width(self) -> None:
        with patch("kanao.process._opened", self.opened):
            self.assertEqual(scan_process(123), [self.expected])

    def test_cached_addresses_use_the_same_target_width(self) -> None:
        with patch("kanao.process._opened", self.opened):
            self.assertEqual(resolve_addresses(123, [BASE_ADDRESS]), [self.expected])

    def test_does_not_guess_a_layout_when_the_architecture_query_fails(self) -> None:
        self.architecture_query_ok = False
        with patch("kanao.process._opened", self.opened):
            self.assertEqual(scan_process(123), [])
            self.assertEqual(resolve_addresses(123, [BASE_ADDRESS]), [])

    @unittest.skipUnless(struct.calcsize("P") == 8, "native x64 requires 64-bit Python")
    def test_native_x64_scanning_and_cached_resolution_still_work(self) -> None:
        self.wow64 = False
        self.blob = codec_context()
        self.mapping = {
            (BASE_ADDRESS, len(self.blob)): self.blob,
            (SALT_ADDRESS, SALT_SIZE): SALT,
            (READ_CONTEXT_ADDRESS + 0x08, 8): struct.pack("<Q", KEY_ADDRESS),
            (KEY_ADDRESS, KEY_SIZE): KEY,
        }
        with patch("kanao.process._opened", self.opened):
            self.assertEqual(scan_process(123), [self.expected])
            self.assertEqual(resolve_addresses(123, [BASE_ADDRESS]), [self.expected])

    def test_follows_four_byte_pointers(self) -> None:
        self.assertEqual(
            resolve_context(BASE_ADDRESS, self.blob, memory_reader(self.mapping), pointer_size=4),
            self.expected,
        )

    def test_finds_a_context_at_the_region_end_across_every_split(self) -> None:
        for split in range(1, len(self.blob)):
            with self.subTest(split=split):
                blocks = [
                    (BASE_ADDRESS, self.blob[:split]),
                    (BASE_ADDRESS + split, self.blob[split:]),
                ]
                self.assertEqual(
                    list(scan_blocks(blocks, memory_reader(self.mapping), pointer_size=4)),
                    [self.expected],
                )

    def test_cached_context_does_not_require_bytes_past_its_last_pointer(self) -> None:
        self.assertEqual(
            resolve_many([BASE_ADDRESS], memory_reader(self.mapping), pointer_size=4),
            [self.expected],
        )

    def test_rejects_a_truncated_context_or_key_pointer(self) -> None:
        self.assertIsNone(decode_context(self.blob[:-1], pointer_size=4))
        self.mapping[(READ_CONTEXT_ADDRESS + 0x08, 4)] = b"\x01\x02\x03"
        self.assertIsNone(
            resolve_context(BASE_ADDRESS, self.blob, memory_reader(self.mapping), pointer_size=4)
        )

    def test_rejects_invalid_markers_and_null_context_pointers(self) -> None:
        for offset, wrong in (
            (0x04, 1000), (0x10, 16), (0x1C, 1024), (0x24, 48),
            (0x3C, 0), (0x4C, 0),
        ):
            with self.subTest(offset=offset):
                blob = bytearray(self.blob)
                struct.pack_into("<I", blob, offset, wrong)
                self.assertIsNone(decode_context(blob, pointer_size=4))

    def test_rejects_a_null_key_pointer(self) -> None:
        self.mapping[(READ_CONTEXT_ADDRESS + 0x08, 4)] = bytes(4)
        self.assertIsNone(
            resolve_context(BASE_ADDRESS, self.blob, memory_reader(self.mapping), pointer_size=4)
        )

    def test_does_not_decode_x86_bytes_as_a_native_context(self) -> None:
        self.assertIsNone(decode_context(self.blob))

    def test_x64_pointers_above_four_gib_are_not_truncated(self) -> None:
        salt, read, key = (0x1_0000_1000, 0x1_0000_2000, 0x1_0000_3000)
        blob = codec_context(salt_pointer=salt, read_context=read)
        memory = memory_reader({
            (salt, SALT_SIZE): SALT,
            (read + 0x08, 8): struct.pack("<Q", key),
            (key, KEY_SIZE): KEY,
        })
        self.assertEqual(
            resolve_context(BASE_ADDRESS, blob, memory), self.expected,
        )


if __name__ == "__main__":
    unittest.main()
