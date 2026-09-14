"""Key ring tests: pairing a key to a file, and explaining a missing one.

The two ways a key can be absent need different answers — the client is
closed, or the client is open but has never touched this conversation — because
they call for different actions from whoever is asking.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kakaoread import paths
from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.keyring import Keyring
from kakaoread.process import CodecContext

USER_KEY = bytes(range(32))
CATALOG_KEY = bytes(range(32, 64))
USER_SALT = bytes(range(16))
CATALOG_SALT = bytes(range(16, 32))
LOG_SALT = bytes(range(32, 48))
LOG_KEY = bytes(range(64, 96))


class ProfileFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / paths.CHAT_DATA_DIRECTORY).mkdir()
        (self.root / paths.USER_DATABASE).write_bytes(USER_SALT + b"encrypted user data")
        (self.root / paths.CATALOG_DATABASE).write_bytes(CATALOG_SALT + b"encrypted catalog")
        (self.root / paths.chat_log_name(4242)).write_bytes(LOG_SALT + b"encrypted log")

    def discover(self, contexts: list[CodecContext], pid: int | None = 900) -> Keyring:
        with (
            patch("kakaoread.keyring.find_pid", return_value=pid),
            patch("kakaoread.keyring.scan_process", return_value=contexts) as scan,
        ):
            ring = Keyring.discover(self.root)
        self.scan = scan
        return ring


class DiscoveryTests(ProfileFixture):
    def test_a_key_is_paired_with_the_file_carrying_its_salt(self) -> None:
        ring = self.discover([CodecContext(0x1000, USER_SALT, USER_KEY)])
        self.assertEqual(ring.key_for(paths.USER_DATABASE), USER_KEY)

    def test_a_database_with_no_matching_context_stays_locked(self) -> None:
        ring = self.discover([CodecContext(0x1000, USER_SALT, USER_KEY)])
        self.assertFalse(ring.has(paths.CATALOG_DATABASE))

    def test_a_key_is_reachable_by_relative_path_and_by_bare_name(self) -> None:
        ring = self.discover([CodecContext(0x1000, CATALOG_SALT, CATALOG_KEY)])
        self.assertEqual(ring.key_for(paths.CATALOG_DATABASE), CATALOG_KEY)
        self.assertEqual(ring.key_for("chatListInfo.edb"), CATALOG_KEY)

    def test_backslash_paths_resolve_the_same_as_forward_slash_paths(self) -> None:
        ring = self.discover([CodecContext(0x1000, CATALOG_SALT, CATALOG_KEY)])
        self.assertEqual(ring.key_for("chat_data\\chatListInfo.edb"), CATALOG_KEY)

    def test_a_context_matching_nothing_on_disk_is_ignored(self) -> None:
        ring = self.discover([CodecContext(0x1000, b"unrelated salt!", USER_KEY)])
        self.assertEqual(ring.keys, {})

    def test_the_client_is_scanned_only_once_per_key_ring(self) -> None:
        self.discover([CodecContext(0x1000, USER_SALT, USER_KEY)])
        self.scan.assert_called_once_with(900)

    def test_a_closed_client_is_not_scanned_at_all(self) -> None:
        ring = self.discover([], pid=None)
        self.assertFalse(ring.running)
        self.scan.assert_not_called()

    def test_readable_room_ids_are_listed_from_the_log_databases(self) -> None:
        ring = self.discover([CodecContext(0x1000, LOG_SALT, LOG_KEY)])
        self.assertEqual(ring.chat_ids(), [4242])

    def test_two_databases_sharing_a_salt_both_receive_the_key(self) -> None:
        twin = self.root / paths.chat_log_name(4343)
        twin.write_bytes(LOG_SALT + b"another log")
        ring = self.discover([CodecContext(0x1000, LOG_SALT, LOG_KEY)])
        self.assertEqual(ring.chat_ids(), [4242, 4343])

    def test_room_ids_exclude_databases_that_are_not_chat_logs(self) -> None:
        ring = self.discover([CodecContext(0x1000, USER_SALT, USER_KEY)])
        self.assertEqual(ring.chat_ids(), [])


class ExplanationTests(ProfileFixture):
    def test_a_closed_client_is_reported_as_not_running(self) -> None:
        ring = self.discover([], pid=None)
        with self.assertRaises(KakaoReadError) as raised:
            ring.key_for(paths.USER_DATABASE)
        self.assertEqual(raised.exception.code, ErrorCode.KAKAO_NOT_RUNNING)

    def test_an_open_client_that_never_loaded_a_database_says_exactly_that(self) -> None:
        ring = self.discover([CodecContext(0x1000, USER_SALT, USER_KEY)])
        with self.assertRaises(KakaoReadError) as raised:
            ring.key_for(paths.chat_log_name(4242))  # salt never matched
        failure = raised.exception
        self.assertEqual(failure.code, ErrorCode.KEY_UNAVAILABLE)
        self.assertIn("open that conversation in KakaoTalk", failure.message)
        self.assertEqual(failure.details["database"], paths.chat_log_name(4242))

    def test_the_two_absences_are_never_reported_as_the_same_thing(self) -> None:
        closed = self.discover([], pid=None)
        open_client = self.discover([CodecContext(0x1000, USER_SALT, USER_KEY)])
        with self.assertRaises(KakaoReadError) as first:
            closed.key_for(paths.USER_DATABASE)
        with self.assertRaises(KakaoReadError) as second:
            open_client.key_for(paths.chat_log_name(4242))
        self.assertNotEqual(first.exception.code, second.exception.code)


class PathTests(ProfileFixture):
    def test_resolves_a_relative_name_against_the_profile(self) -> None:
        ring = self.discover([])
        self.assertEqual(
            ring.path_for(paths.USER_DATABASE), self.root / paths.USER_DATABASE
        )


if __name__ == "__main__":
    unittest.main()
