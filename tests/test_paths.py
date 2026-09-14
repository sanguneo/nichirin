"""Profile discovery tests: find the signed-in account on *this* machine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kakaoread import paths
from kakaoread.errors import ErrorCode, KakaoReadError


class UserDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.local = Path(directory.name)
        self.profiles = self.local / "Kakao" / "KakaoTalk" / "users"

    def add_profile(self, name: str, mtime: int, complete: bool = True) -> Path:
        profile = self.profiles / name
        profile.mkdir(parents=True)
        if complete:
            (profile / "chat_data").mkdir()
            state = profile / "appstate.dat"
            state.touch()
            import os

            os.utime(state, ns=(mtime, mtime))
        return profile

    def test_finds_the_only_signed_in_profile(self) -> None:
        expected = self.add_profile("only", 1_000)
        self.assertEqual(
            paths.user_directory({"LOCALAPPDATA": str(self.local)}), expected
        )

    def test_prefers_the_most_recently_used_profile(self) -> None:
        self.add_profile("stale", 1_000)
        active = self.add_profile("active", 9_000)
        self.assertEqual(paths.user_directory({"LOCALAPPDATA": str(self.local)}), active)

    def test_ignores_a_directory_without_chat_data(self) -> None:
        partial = self.profiles / "partial"
        partial.mkdir(parents=True)
        (partial / "appstate.dat").touch()
        complete = self.add_profile("complete", 1_000)
        self.assertEqual(paths.user_directory({"LOCALAPPDATA": str(self.local)}), complete)

    def test_explains_an_unset_environment_rather_than_raising_a_key_error(self) -> None:
        with self.assertRaises(KakaoReadError) as raised:
            paths.user_directory({})
        self.assertEqual(raised.exception.code, ErrorCode.NO_USER_DATA)

    def test_explains_a_machine_with_no_kakaotalk_at_all(self) -> None:
        with self.assertRaises(KakaoReadError) as raised:
            paths.user_directory({"LOCALAPPDATA": str(self.local)})
        self.assertEqual(raised.exception.code, ErrorCode.NO_USER_DATA)

    def test_explains_a_profile_directory_that_holds_no_account(self) -> None:
        self.profiles.mkdir(parents=True)
        with self.assertRaises(KakaoReadError) as raised:
            paths.user_directory({"LOCALAPPDATA": str(self.local)})
        self.assertEqual(raised.exception.code, ErrorCode.NO_USER_DATA)
        self.assertIn("sign in", raised.exception.message)


class NameTests(unittest.TestCase):
    def test_a_room_log_is_named_after_its_identifier(self) -> None:
        self.assertEqual(paths.chat_log_name(1234), "chat_data/chatLogs_1234.edb")

    def test_large_room_identifiers_are_not_abbreviated(self) -> None:
        huge = 9007199254740993
        self.assertIn(str(huge), paths.chat_log_name(huge))

    def test_the_log_sidecar_sits_next_to_its_database(self) -> None:
        database = Path("C:/profile/chat_data/chatLogs_7.edb")
        self.assertEqual(paths.wal_path(database).name, "chatLogs_7.edb-wal")

    def test_database_discovery_only_yields_encrypted_databases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chat_data").mkdir()
            (root / "TalkUserDB.edb").touch()
            (root / "chat_data" / "chatListInfo.edb").touch()
            (root / "chat_data" / "chatListInfo.edb-wal").touch()
            (root / "notes.txt").touch()

            found = [path.name for path in paths.database_paths(root)]

        self.assertEqual(sorted(found), ["TalkUserDB.edb", "chatListInfo.edb"])

    def test_database_discovery_is_ordered_so_results_are_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("c.edb", "a.edb", "b.edb"):
                (root / name).touch()
            found = [path.name for path in paths.database_paths(root)]
        self.assertEqual(found, sorted(found))


class DatabaseDiscoveryTests(unittest.TestCase):
    """Pinned before the walk was rewritten: same files, same order."""

    def profile(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "chat_data" / "deep").mkdir(parents=True)
        (root / "media").mkdir()
        for relative in (
            "TalkUserDB.edb",
            "chat_data/chatListInfo.edb",
            "chat_data/chatLogs_1001.edb",
            "chat_data/deep/nested.EDB",
            "chat_data/chatLogs_1001.edb-wal",
            "media/photo.jpg",
            "appstate.dat",
            ".edb",
        ):
            (root / relative).write_bytes(b"x")
        return root

    def test_every_database_at_any_depth_is_found(self) -> None:
        root = self.profile()
        found = [path.relative_to(root).as_posix() for path in paths.database_paths(root)]
        self.assertEqual(
            found,
            [
                # Windows path comparison folds case, so the profile-root
                # database sorts after the chat_data directory.
                "chat_data/chatListInfo.edb",
                "chat_data/chatLogs_1001.edb",
                "chat_data/deep/nested.EDB",
                "TalkUserDB.edb",
            ],
        )

    def test_a_name_that_is_only_the_suffix_is_not_a_database(self) -> None:
        root = self.profile()
        self.assertNotIn(".edb", [path.name for path in paths.database_paths(root)])

    def test_the_write_ahead_log_is_not_a_database(self) -> None:
        root = self.profile()
        self.assertNotIn(
            "chatLogs_1001.edb-wal", [path.name for path in paths.database_paths(root)]
        )


if __name__ == "__main__":
    unittest.main()
