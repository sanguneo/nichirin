"""Native Windows safety checks on owned, isolated synthetic files and children."""

from __future__ import annotations

import ctypes
import os
import sys
import unittest
from unittest.mock import patch

from tests.platform import require_windows_libraries

require_windows_libraries()

from kanao._snapshot_temp import _WindowsRecovery
from tests.test_snapshot_temp import _Fixture


@unittest.skipUnless(sys.platform == "win32", "Windows native orphan recovery")
class OrphanSafetyTests(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.recovery = _WindowsRecovery()
        forbid_kill = patch("os.kill", side_effect=AssertionError("must not signal a PID"))
        forbid_kill.start()
        self.addCleanup(forbid_kill.stop)

    def candidate(self):
        path = self.temps / (self.recovery.prefix() + "abcdefgh.sqlite3")
        path.write_bytes(self.image)
        return path

    def test_living_pid_keeps_a_closed_file(self) -> None:
        candidate = self.candidate()
        self.recovery.cleanup(self.temps)
        self.assertEqual(candidate.read_bytes(), self.image)

    def test_living_child_survives_another_fallback(self) -> None:
        with self.child() as child:
            candidate, = self.temps.iterdir()
            self.open_fallback().close()
            self.assertIsNone(child.poll())
            self.assertEqual(candidate.read_bytes(), self.image)

    def test_a_pid_now_held_by_a_later_process_allows_recovery(self) -> None:
        # This process is alive, but not the one born at the tagged time: the
        # number was handed out again, so the tagged process has exited.
        parts = self.recovery.prefix().split("-")
        parts[4] = f"{int(parts[4], 16) - 1:016x}"
        reused = self.temps / ("-".join(parts) + "abcdefgh.sqlite3")
        reused.write_bytes(self.image)
        self.recovery.cleanup(self.temps)
        self.assertFalse(reused.exists())

    def test_inaccessible_and_unknown_pid_are_not_evidence_of_exit(self) -> None:
        candidate = self.candidate()
        for error in (5, 6, 0):  # ACCESS_DENIED, INVALID_HANDLE, no known error
            with self.subTest(error=error):
                def unavailable(*_args):
                    ctypes.set_last_error(error)
                    return None
                with patch.object(self.recovery.kernel, "OpenProcess", side_effect=unavailable):
                    self.recovery.cleanup(self.temps)
                self.assertEqual(candidate.read_bytes(), self.image)

    def test_absent_pid_allows_recovery(self) -> None:
        candidate = self.candidate()
        def absent(*_args):
            ctypes.set_last_error(87)  # ERROR_INVALID_PARAMETER for a positive PID
            return None
        with patch.object(self.recovery.kernel, "OpenProcess", side_effect=absent):
            self.recovery.cleanup(self.temps)
        self.assertFalse(candidate.exists())

    def test_unsignalled_or_unknown_wait_result_keeps_the_file(self) -> None:
        candidate = self.candidate()
        for result in (258, 0xFFFFFFFF, 128):
            with self.subTest(result=result):
                with patch.object(self.recovery.kernel, "WaitForSingleObject", return_value=result):
                    self.recovery.cleanup(self.temps)
                self.assertEqual(candidate.read_bytes(), self.image)

    def test_unknown_process_creation_time_keeps_the_file(self) -> None:
        candidate = self.candidate()
        def unknown(*_args):
            ctypes.set_last_error(5)
            return False
        with (
            patch.object(self.recovery.kernel, "WaitForSingleObject", return_value=0),
            patch.object(self.recovery.kernel, "GetProcessTimes", side_effect=unknown),
        ):
            self.recovery.cleanup(self.temps)
        self.assertEqual(candidate.read_bytes(), self.image)

    def test_unrelated_and_malformed_names_are_not_probed(self) -> None:
        names = [
            "tmpabcdefgh.sqlite3", "notes.sqlite3", "kanao-snapshot-abcdefgh.sqlite3",
            "kanao-snapshot-v2-1-0000000000000001-abcdefgh.sqlite3",
            "kanao-snapshot-v1-0-0000000000000001-abcdefgh.sqlite3",
            "kanao-snapshot-v1--1-0000000000000001-abcdefgh.sqlite3",
            "kanao-snapshot-v1-1-short-abcdefgh.sqlite3",
            "kanao-snapshot-v1-1-0000000000000001-short.sqlite3",
            "kanao-snapshot-v1-1-0000000000000001-abcdefgh.sqlite3-wal",
        ]
        for name in names:
            (self.temps / name).write_bytes(self.image)
        with patch.object(self.recovery, "_exited", side_effect=AssertionError("not our file")):
            self.recovery.cleanup(self.temps)
        self.assertEqual(sorted(p.name for p in self.temps.iterdir()), sorted(names))

    def test_out_of_range_pid_and_zero_birth_are_not_probed(self) -> None:
        for pid, birth in ((4294967296, 1), (1, 0)):
            path = self.temps / f"kanao-snapshot-v1-{pid}-{birth:016x}-abcdefgh.sqlite3"
            path.write_bytes(self.image)
        with patch.object(self.recovery.kernel, "OpenProcess", side_effect=AssertionError("invalid PID")):
            self.recovery.cleanup(self.temps)
        self.assertEqual(len(list(self.temps.iterdir())), 2)

    def test_a_different_user_cannot_reclaim_a_file(self) -> None:
        candidate = self.candidate()
        # LocalSystem's valid SID differs from this account's user and default
        # owner. Keep the native GetSecurityInfo/EqualSid calls; change only the
        # expected identities.
        self.recovery.owners = (bytes.fromhex("010100000000000512000000"),)
        self.assertFalse(self.recovery._remove(candidate))
        self.assertEqual(candidate.read_bytes(), self.image)

    def test_unknown_owner_keeps_the_file(self) -> None:
        candidate = self.candidate()
        with patch.object(self.recovery.security, "GetSecurityInfo", return_value=5):
            self.assertFalse(self.recovery._remove(candidate))
        self.assertEqual(candidate.read_bytes(), self.image)

    def test_unknown_file_metadata_keeps_the_file(self) -> None:
        candidate = self.candidate()
        with patch.object(self.recovery.kernel, "GetFileInformationByHandle", return_value=False):
            self.assertFalse(self.recovery._remove(candidate))
        self.assertEqual(candidate.read_bytes(), self.image)

    def test_open_file_is_left_alone(self) -> None:
        candidate = self.candidate()
        with candidate.open("rb"):
            self.assertFalse(self.recovery._remove(candidate))
        self.assertEqual(candidate.read_bytes(), self.image)

    def test_hard_links_are_left_alone(self) -> None:
        candidate = self.temps / (self.recovery.prefix() + "abcdefgh.sqlite3")
        os.link(self.seed, candidate)
        self.assertFalse(self.recovery._remove(candidate))
        self.assertEqual(candidate.read_bytes(), self.image)
        self.assertEqual(self.seed.read_bytes(), self.image)

    def test_symbolic_link_and_target_are_left_alone(self) -> None:
        candidate = self.temps / (self.recovery.prefix() + "abcdefgh.sqlite3")
        os.symlink(self.seed, candidate)
        self.assertFalse(self.recovery._remove(candidate))
        self.assertTrue(candidate.is_symlink())
        self.assertEqual(self.seed.read_bytes(), self.image)

    def test_directory_is_left_alone(self) -> None:
        candidate = self.temps / (self.recovery.prefix() + "abcdefgh.sqlite3")
        candidate.mkdir()
        self.assertFalse(self.recovery._remove(candidate))
        self.assertTrue(candidate.is_dir())

    def test_inspected_file_cannot_be_replaced_before_deletion(self) -> None:
        candidate = self.candidate()
        owned = self.recovery._owned
        def check_while_pinned(handle):
            with self.assertRaises(PermissionError):
                candidate.rename(self.temps / "replacement.sqlite3")
            return owned(handle)
        with patch.object(self.recovery, "_owned", side_effect=check_while_pinned):
            self.assertTrue(self.recovery._remove(candidate))
        self.assertFalse(candidate.exists())

    def test_failed_deletion_warns_and_preserves_the_file(self) -> None:
        candidate = self.candidate()
        with (
            patch.object(self.recovery.kernel, "SetFileInformationByHandle", return_value=False),
            self.assertWarns(RuntimeWarning),
        ):
            self.assertFalse(self.recovery._remove(candidate))
        self.assertEqual(candidate.read_bytes(), self.image)


if __name__ == "__main__":
    unittest.main()
