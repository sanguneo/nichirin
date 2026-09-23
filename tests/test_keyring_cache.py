"""Remembering where the keys were, so the next call skips the heap scan.

The scan measured 2.8 s on a live client and every command paid it again. What
is remembered is the address of each context — never a key, which is why one
of these tests reads the cache file back and looks for the key bytes in it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kanao import keyring as keyring_module
from kanao.keyring import Keyring
from kanao.process import CodecContext, resolve_many

SALT = bytes(range(16))
KEY = bytes(range(100, 132))
ADDRESS = 0x7FF6_0000_1000


class ResolveManyTests(unittest.TestCase):
    """Re-reading known addresses must not trust them blindly."""

    def context_bytes(self, salt_pointer: int, read_context: int) -> bytes:
        blob = bytearray(0x68)
        blob[0x04:0x08] = (256000).to_bytes(4, "little")
        blob[0x10:0x14] = (32).to_bytes(4, "little")
        blob[0x1C:0x20] = (4096).to_bytes(4, "little")
        blob[0x24:0x28] = (80).to_bytes(4, "little")
        blob[0x40:0x48] = salt_pointer.to_bytes(8, "little")
        blob[0x60:0x68] = read_context.to_bytes(8, "little")
        return bytes(blob)

    def reader(self):
        memory = {
            ADDRESS: self.context_bytes(0x1000, 0x2000),
            0x1000: SALT,
            0x2008: (0x3000).to_bytes(8, "little"),
            0x3000: KEY,
        }

        def read_memory(address: int, size: int):
            value = memory.get(address)
            return value[:size] if value is not None and len(value) >= size else None

        return read_memory

    def test_a_known_address_is_resolved_without_searching(self) -> None:
        found = resolve_many([ADDRESS], self.reader())
        self.assertEqual([context.key for context in found], [KEY])

    def test_an_address_that_no_longer_holds_a_context_is_dropped(self) -> None:
        found = resolve_many([ADDRESS + 0x40], self.reader())
        self.assertEqual(found, [])

    def test_addresses_are_resolved_independently(self) -> None:
        found = resolve_many([ADDRESS, ADDRESS + 0x40], self.reader())
        self.assertEqual(len(found), 1)


class DiscoveryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / "profile"
        (self.profile / "chat_data").mkdir(parents=True)
        self.database = self.profile / "chat_data" / "chatLogs_1001.edb"
        self.database.write_bytes(SALT + b"\x00" * 64)
        self.cache = self.root / "contexts.json"
        self.scans = 0

        # Bind the originals before patching, or the wrapper calls itself.
        self.patched = {
            "find_pid": keyring_module.find_pid,
            "scan_process": keyring_module.scan_process,
            "resolve_addresses": keyring_module.resolve_addresses,
            "_cache_path": keyring_module._cache_path,
        }

        def scan_process(pid: int) -> list[CodecContext]:
            self.scans += 1
            return [CodecContext(address=ADDRESS, salt=SALT, key=KEY)]

        keyring_module.find_pid = lambda: 4242
        keyring_module.scan_process = scan_process
        keyring_module.resolve_addresses = lambda pid, addresses: [
            CodecContext(address=address, salt=SALT, key=KEY) for address in addresses
        ]
        keyring_module._cache_path = lambda: self.cache
        for name, original in self.patched.items():
            self.addCleanup(setattr, keyring_module, name, original)

    def test_the_first_discovery_scans_and_remembers(self) -> None:
        ring = Keyring.discover(self.profile)
        self.assertEqual(self.scans, 1)
        self.assertTrue(ring.has("chatLogs_1001.edb"))
        self.assertTrue(self.cache.exists())

    def test_the_second_discovery_does_not_scan_again(self) -> None:
        Keyring.discover(self.profile)
        ring = Keyring.discover(self.profile)
        self.assertEqual(self.scans, 1, "the heap was searched again with a usable cache")
        self.assertTrue(ring.has("chatLogs_1001.edb"))

    def test_a_cache_from_another_process_is_ignored(self) -> None:
        self.cache.write_text(json.dumps({"pid": 9999, "addresses": [ADDRESS], "keys": 1}))
        Keyring.discover(self.profile)
        self.assertEqual(self.scans, 1)

    def test_addresses_that_stop_resolving_fall_back_to_a_scan(self) -> None:
        Keyring.discover(self.profile)
        keyring_module.resolve_addresses = lambda pid, addresses: []
        ring = Keyring.discover(self.profile)
        self.assertEqual(self.scans, 2, "a stale cache was used instead of rescanning")
        self.assertTrue(ring.has("chatLogs_1001.edb"))

    def test_a_damaged_cache_is_survivable(self) -> None:
        self.cache.write_text("{not json")
        ring = Keyring.discover(self.profile)
        self.assertTrue(ring.has("chatLogs_1001.edb"))

    def test_the_cache_never_holds_key_material(self) -> None:
        Keyring.discover(self.profile)
        raw = self.cache.read_bytes()
        self.assertNotIn(KEY, raw)
        self.assertNotIn(KEY.hex().encode(), raw)
        self.assertNotIn(SALT, raw)


class NewerRoomTests(DiscoveryCacheTests):
    """A room the client opens after the scan is not in the remembered set.

    The remembered addresses are trusted when they pair as many databases as
    before, which is exactly what they still do when one more room has been
    opened since — so that room stayed unreadable until the client restarted.
    """

    def test_a_remembered_ring_says_that_it_is_one(self) -> None:
        self.assertFalse(Keyring.discover(self.profile).remembered)
        self.assertTrue(Keyring.discover(self.profile).remembered)

    def test_a_rescan_ignores_the_remembered_addresses(self) -> None:
        Keyring.discover(self.profile)
        ring = Keyring.discover(self.profile, rescan=True)
        self.assertEqual(self.scans, 2)
        self.assertFalse(ring.remembered)

    def test_a_reader_scans_again_for_a_room_the_remembered_set_lacks(self) -> None:
        from kanao.reader import Reader

        newer = self.profile / "chat_data" / "chatLogs_2002.edb"
        newer_salt, newer_key = bytes(range(16, 32)), bytes(range(200, 232))
        newer.write_bytes(newer_salt + b"\x00" * 64)
        Keyring.discover(self.profile)

        def scan_process(pid: int) -> list[CodecContext]:
            self.scans += 1
            return [
                CodecContext(address=ADDRESS, salt=SALT, key=KEY),
                CodecContext(address=ADDRESS + 0x100, salt=newer_salt, key=newer_key),
            ]

        keyring_module.scan_process = scan_process
        reader = Reader(user_directory=self.profile, cache=object())  # type: ignore[arg-type]
        self.assertTrue(reader.keyring.remembered)
        self.assertEqual(reader._key_for("chat_data/chatLogs_2002.edb"), newer_key)
        self.assertEqual(self.scans, 2)

    def test_a_reader_does_not_rescan_for_a_room_it_already_has(self) -> None:
        from kanao.reader import Reader

        Keyring.discover(self.profile)
        reader = Reader(user_directory=self.profile, cache=object())  # type: ignore[arg-type]
        reader._key_for("chat_data/chatLogs_1001.edb")
        self.assertEqual(self.scans, 1)


if __name__ == "__main__":
    unittest.main()
