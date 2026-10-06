"""Account discovery proves both the derived filename and the encrypted header.

These replace Windows heap/salt pairing: no running client or address cache is
needed, and a missing account key applies to the whole database.
"""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kamado import derive, paths
from kamado.errors import ErrorCode, KamadoError
from kamado.keyring import Keyring
from tests import support
from tests.fixtures import OWN_ID, PEER_ID

UUID = "00000000-1111-2222-3333-444444444444"


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.name = derive.database_name(OWN_ID, UUID)
        cls.passphrase = derive.passphrase(OWN_ID, UUID)
        cls.key = support.PROFILE.derive(cls.passphrase, support.SALT)
        plain = bytearray(support.PAGE_SIZE)
        plain[16:18] = support.PAGE_SIZE.to_bytes(2, "big")
        plain[20] = support.RESERVE
        cls.encrypted = support.encrypted_page(bytes(plain), 1, key=cls.key)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.env = {"HOME": str(self.home)}
        self.container = paths.container_directory(self.env)
        self.container.mkdir(parents=True)
        self.database = self.container / self.name
        self.database.write_bytes(self.encrypted)
        preferences = paths.preference_paths(self.env)[-1]
        preferences.parent.mkdir(parents=True)
        preferences.write_bytes(plistlib.dumps({"AlertKakaoIDsList": [PEER_ID, OWN_ID]}))
        uuid = patch("kamado.keyring.derive.platform_uuid", return_value=UUID)
        self.uuid = uuid.start()
        self.addCleanup(uuid.stop)

    def test_the_candidate_whose_derived_name_matches_is_chosen(self) -> None:
        ring = Keyring.discover(self.env)
        account = ring.require()
        self.assertTrue(ring.readable)
        self.assertEqual(account.user_id, OWN_ID)
        self.assertEqual(account.user, str(OWN_ID))
        self.assertEqual(account.database, self.database)
        self.assertEqual(account.geometry.key, self.key)
        self.assertEqual(account.geometry.page_size, support.PAGE_SIZE)

    def test_only_the_filename_matched_candidate_is_decrypted(self) -> None:
        from kamado.cipher import detect_file
        with patch("kamado.keyring.cipher.detect_file", wraps=detect_file) as detect:
            ring = Keyring.discover(self.env)
        self.assertTrue(ring.readable)
        detect.assert_called_once_with(self.database, self.passphrase)
        self.uuid.assert_called_once_with()

    def test_the_optional_db_suffix_does_not_change_the_account(self) -> None:
        suffixed = self.database.with_name(self.name + ".db")
        self.database.rename(suffixed)
        self.assertEqual(Keyring.discover(self.env).require().database, suffixed)

    def test_a_filename_match_does_not_make_a_wrong_passphrase_valid(self) -> None:
        with patch("kamado.keyring.derive.passphrase", return_value="wrong synthetic passphrase"):
            ring = Keyring.discover(self.env)
        self.assertFalse(ring.readable)
        self.assertIsNone(ring.account)
        with self.assertRaises(KamadoError) as raised:
            ring.require()
        self.assertEqual(raised.exception.code, ErrorCode.KEY_UNAVAILABLE)

    def test_when_no_filename_matches_the_header_can_still_prove_an_account(self) -> None:
        renamed = self.container / ("f" * 78)
        self.database.rename(renamed)
        ring = Keyring.discover(self.env)
        self.assertEqual(ring.require().user_id, OWN_ID)
        self.assertEqual(ring.require().database, renamed)

    def test_a_missing_database_needs_no_hardware_lookup(self) -> None:
        self.database.unlink()
        ring = Keyring.discover(self.env)
        self.assertEqual(ring.databases, [])
        self.assertFalse(ring.readable)
        self.uuid.assert_not_called()
        with self.assertRaises(KamadoError) as raised:
            ring.require()
        self.assertEqual(raised.exception.code, ErrorCode.NO_USER_DATA)
        self.assertEqual(raised.exception.details["container"], str(self.container))

    def test_databases_without_candidate_preferences_stay_locked(self) -> None:
        for preference in paths.preference_paths(self.env):
            if preference.exists():
                preference.unlink()
        ring = Keyring.discover(self.env)
        self.assertEqual(ring.databases, [self.database])
        with self.assertRaises(KamadoError) as raised:
            ring.require()
        self.assertEqual(raised.exception.code, ErrorCode.KEY_UNAVAILABLE)
        self.assertEqual(raised.exception.details["databases"], 1)

    def test_discovery_persists_only_a_non_secret_account_id(self) -> None:
        # Discovery may remember the resolved account id so the recovery search
        # runs at most once per machine, but it must never write key material:
        # not the passphrase, not the derived key, not the salt.
        first = Keyring.discover(self.env)
        second = Keyring.discover(self.env)
        self.assertEqual(first, second)

        cache = paths.state_directory(self.env) / "account.json"
        self.assertTrue(cache.exists())
        written = cache.read_bytes()
        for secret in (self.passphrase.encode(), self.key, support.SALT):
            self.assertNotIn(secret, written)
        # What it does hold is the account id and the hardware id, both of
        # which are already recoverable from the machine itself.
        import json

        stored = json.loads(cache.read_text("utf-8"))
        self.assertEqual(stored["userId"], OWN_ID)
        self.assertEqual(set(stored), {"userId", "uuid"})


if __name__ == "__main__":
    unittest.main()
