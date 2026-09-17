"""Deterministic derivation replaces the Windows heap-address cache.

No addresses or keys need persisting. Candidate preferences and machine identity
are inputs, not proof; test_keyring proves them against an encrypted database.
"""

from __future__ import annotations

import base64
import hashlib
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kamado import derive, keyring, paths
from kamado.errors import ErrorCode, KamadoError
from kamado.reader import Reader
from tests.fixtures import OWN_ID, PEER_ID, build_keyring

UUID = "00000000-1111-2222-3333-444444444444"


class DerivationTests(unittest.TestCase):
    def test_name_and_passphrase_match_the_published_derivation(self) -> None:
        # Independent standard-library calculation pins the machine-consumed
        # strings, including ordering, reversals, output length and slicing.
        hashed = base64.b64encode(
            hashlib.sha1(UUID.encode()).digest() + hashlib.sha256(UUID.encode()).digest()
        ).decode("ascii")
        name_password = f"..F.{OWN_ID}.A.F.{UUID[::-1]}...|"
        expected_name = hashlib.pbkdf2_hmac(
            "sha256", name_password.encode(), hashed[::-1].encode(), 100_000, 128
        ).hex()[28:106]
        password = f"AF{hashed}F|FFF{UUID[:5]}FHF{OWN_ID}F|F{UUID[7:]}"[::-1]
        expected_passphrase = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), UUID[10:].encode(), 100_000, 128
        ).hex()
        self.assertEqual(derive.database_name(OWN_ID, UUID), expected_name)
        self.assertEqual(derive.passphrase(OWN_ID, UUID), expected_passphrase)
        self.assertEqual(derive.database_name(OWN_ID, UUID), expected_name)
        self.assertEqual(derive.passphrase(OWN_ID, UUID), expected_passphrase)
        self.assertRegex(expected_name, r"^[0-9a-f]{78}$")
        self.assertEqual(len(expected_passphrase), 256)

    def test_different_accounts_and_machines_derive_different_values(self) -> None:
        other_uuid = "99999999-1111-2222-3333-444444444444"
        for function in (derive.database_name, derive.passphrase):
            with self.subTest(function=function.__name__):
                self.assertNotEqual(function(OWN_ID, UUID), function(PEER_ID, UUID))
                self.assertNotEqual(function(OWN_ID, UUID), function(OWN_ID, other_uuid))


class CandidateTests(unittest.TestCase):
    def test_transparency_keys_share_the_account_suffix(self) -> None:
        self.assertEqual(derive.candidate_user_ids({
            f"FSChatWindowTransparency111_{OWN_ID}": 0.8,
            f"FSChatWindowTransparency222_{OWN_ID}": 0.9,
        }), [OWN_ID])

    def test_frame_keys_share_the_account_suffix(self) -> None:
        self.assertEqual(derive.candidate_user_ids({
            f"NSWindow Frame FSChatWindowFrame_111_{OWN_ID}": "frame",
            f"NSWindow Frame FSChatWindowFrame_222_{OWN_ID}": "frame",
        }), [OWN_ID])

    def test_one_window_is_not_enough_to_infer_an_account(self) -> None:
        self.assertEqual(derive.candidate_user_ids({f"FSChatWindowTransparency111_{OWN_ID}": 1}), [])

    def test_alert_account_list_and_direct_keys_are_deduplicated(self) -> None:
        self.assertEqual(derive.candidate_user_ids({
            "userId": OWN_ID, "user_id": str(OWN_ID),
            "AlertKakaoIDsList": [str(PEER_ID), OWN_ID, "invalid", 0, -1, None],
        }), [OWN_ID, PEER_ID])

    def test_candidates_from_all_preferences_preserve_order_without_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.plist", root / "second.plist"
            first.write_bytes(plistlib.dumps({"userId": OWN_ID}))
            second.write_bytes(plistlib.dumps({"AlertKakaoIDsList": [OWN_ID, PEER_ID]}))
            with patch.object(paths, "preference_paths", return_value=[first, second]):
                self.assertEqual(keyring._candidate_ids({"HOME": directory}), [OWN_ID, PEER_ID])

    def test_missing_damaged_and_non_dictionary_preferences_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.plist"
            self.assertEqual(derive.read_preferences(path), {})
            path.write_bytes(b"not a plist")
            self.assertEqual(derive.read_preferences(path), {})
            path.write_bytes(plistlib.dumps([OWN_ID]))
            self.assertEqual(derive.read_preferences(path), {})


class MachineAndMemoizationTests(unittest.TestCase):
    def test_hardware_uuid_is_parsed_from_the_injected_runner(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, f'"IOPlatformUUID" = "{UUID}"'))
        self.assertEqual(derive.platform_uuid(runner), UUID)
        self.assertEqual(runner.call_args.args[0], ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        self.assertGreater(runner.call_args.kwargs["timeout"], 0)

    def test_absent_uuid_and_failed_command_are_typed_failures(self) -> None:
        for runner in (Mock(return_value=subprocess.CompletedProcess([], 0, "")),
                       Mock(side_effect=OSError("missing")),
                       Mock(side_effect=subprocess.TimeoutExpired("ioreg", 10))):
            with self.subTest(runner=runner), self.assertRaises(KamadoError) as raised:
                derive.platform_uuid(runner)
            self.assertEqual(raised.exception.code, ErrorCode.NO_USER_DATA)

    def test_a_reader_resolves_its_account_only_once_and_only_on_demand(self) -> None:
        ring = build_keyring()
        with patch.object(keyring.Keyring, "discover", return_value=ring) as discover:
            with Reader() as reader:
                discover.assert_not_called()
                self.assertIs(reader.account, ring.account)
                self.assertIs(reader.account, ring.account)
            discover.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
