"""Heap scanning tests.

The equivalent code in the predecessor project had no coverage of the block
walk at all — only of the pure decode helpers — so a context landing across a
chunk boundary could have been dropped silently, making a conversation simply
invisible.  The split tests below are the point of this file.
"""

from __future__ import annotations

import struct
import unittest

from kakaoread.process import (
    CONTEXT_SIZE,
    KEY_SIZE,
    SALT_SIZE,
    CodecContext,
    decode_context,
    resolve_context,
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


if __name__ == "__main__":
    unittest.main()
