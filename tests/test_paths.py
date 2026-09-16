"""macOS container discovery and bounded permission failures, using fixtures only."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from kakaoread import paths
from kakaoread.errors import ErrorCode, KakaoReadError


class HomePathsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.env = {"HOME": str(self.home)}

    def test_home_comes_from_the_supplied_environment(self) -> None:
        self.assertEqual(paths.home(self.env), self.home)

    def test_container_uses_the_macos_bundle_directory(self) -> None:
        self.assertEqual(paths.container_directory(self.env), self.home /
            "Library/Containers/com.kakao.KakaoTalkMac/Data/Library/Application Support/com.kakao.KakaoTalkMac")

    def test_state_is_outside_the_container_and_is_not_created_by_lookup(self) -> None:
        state = paths.state_directory(self.env)
        self.assertEqual(state, self.home / "Library/Application Support/kakaoread")
        self.assertFalse(state.exists())

    def test_preferences_prefer_suffixed_container_copies_then_plain_then_global(self) -> None:
        directory = self.home / "Library/Containers/com.kakao.KakaoTalkMac/Data/Library/Preferences"
        directory.mkdir(parents=True)
        for name in ("com.kakao.KakaoTalkMac.bb.plist", "com.kakao.KakaoTalkMac.aa.plist",
                     "com.kakao.KakaoTalkMac.plist", "unrelated.plist", "com.kakao.KakaoTalkMac.txt"):
            (directory / name).touch()
        self.assertEqual(paths.preference_paths(self.env), [
            directory / "com.kakao.KakaoTalkMac.aa.plist",
            directory / "com.kakao.KakaoTalkMac.bb.plist",
            directory / "com.kakao.KakaoTalkMac.plist",
            self.home / "Library/Preferences/com.kakao.KakaoTalkMac.plist",
        ])

    def test_missing_preferences_still_return_the_two_standard_paths(self) -> None:
        found = paths.preference_paths(self.env)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(path.is_relative_to(self.home) for path in found))


class DatabaseDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_only_78_hex_names_with_an_optional_db_suffix_are_databases(self) -> None:
        valid = ["b" * 78 + ".db", "a" * 78, "0" * 78]
        # Workaround for the current discovery contract: candidate entries here
        # are files. database_paths does not exclude same-named directories.
        for name in valid + ["a" * 77, "a" * 79, "g" * 78, "A" * 78,
                             "a" * 78 + "-wal", "a" * 78 + ".db-wal", ".db", "notes.txt"]:
            (self.root / name).touch()
        self.assertEqual(paths.database_paths(self.root), sorted(self.root / name for name in valid))

    def test_discovery_is_container_local_not_a_recursive_windows_profile_walk(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ("a" * 78)).touch()
        self.assertEqual(paths.database_paths(self.root), [])

    def test_a_missing_container_has_no_databases(self) -> None:
        self.assertEqual(paths.database_paths(self.root / "absent"), [])

    def test_find_database_accepts_bare_and_suffixed_names(self) -> None:
        name = "a" * 78
        suffixed = self.root / (name + ".db")
        suffixed.touch()
        self.assertEqual(paths.find_database(self.root, name), suffixed)
        bare = self.root / name
        bare.touch()
        self.assertEqual(paths.find_database(self.root, name), bare)

    def test_find_database_does_not_accept_a_directory(self) -> None:
        name = "a" * 78
        (self.root / name).mkdir()
        self.assertIsNone(paths.find_database(self.root, name))

    def test_the_log_sidecar_sits_next_to_its_database(self) -> None:
        database = self.root / ("a" * 78 + ".db")
        self.assertEqual(paths.wal_path(database), self.root / (database.name + "-wal"))


class GuardedTests(unittest.TestCase):
    def test_returns_the_operations_value(self) -> None:
        sentinel = object()
        self.assertIs(paths.guarded(lambda: sentinel, "fixture"), sentinel)

    def test_permission_error_becomes_a_typed_permission_failure(self) -> None:
        def denied():
            raise PermissionError("synthetic denial")
        with self.assertRaises(KakaoReadError) as raised:
            paths.guarded(denied, "fixture")
        self.assertEqual(raised.exception.code, ErrorCode.PERMISSION_REQUIRED)
        self.assertEqual(raised.exception.details["permission"], "fullDiskAccess")

    def test_a_call_that_never_returns_times_out_on_a_daemon_worker(self) -> None:
        blocked = threading.Event()
        entered = threading.Event()
        workers = []

        def never_returns():
            workers.append(threading.current_thread())
            entered.set()
            blocked.wait()

        # Time is the behavior under test. No real I/O or scheduling race can
        # make this callable finish; the worker is intentionally abandoned.
        with patch.object(paths, "ACCESS_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(KakaoReadError) as raised:
                paths.guarded(never_returns, "fixture-timeout")
        self.assertEqual(raised.exception.code, ErrorCode.PERMISSION_REQUIRED)
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(workers[0].daemon)

    def test_other_failures_keep_their_identity(self) -> None:
        failure = ValueError("synthetic failure")
        def fail():
            raise failure
        with self.assertRaises(ValueError) as raised:
            paths.guarded(fail, "fixture")
        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
