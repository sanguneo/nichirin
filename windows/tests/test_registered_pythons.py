"""Each registered interpreter is listed once (#121 review).

HKCU\\Software is not split between the 64- and 32-bit registry views, so a
per-user install opened through both views came back twice on Windows 11
(2026-11-09). A fake ``winreg`` stands in for the registry so this runs anywhere.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from kanao import wmicom

HKCU, HKLM, VIEW64, VIEW32, READ = 1, 2, 0x100, 0x200, 0x20019
USER_PYTHON = r"C:\Users\me\AppData\Local\Programs\Python\Python313\python.exe"
MACHINE_PYTHON = r"C:\Program Files\Python311\python.exe"


class _Key:
    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self) -> "_Key":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def fake_winreg(machine: bool) -> types.ModuleType:
    tree = {
        (HKCU, VIEW64): {"3.13": ("3.13", USER_PYTHON)},
        (HKCU, VIEW32): {"3.13": ("3.13", USER_PYTHON)},
    }
    if machine:
        tree[(HKLM, VIEW64)] = {"3.11": ("3.11", MACHINE_PYTHON)}

    def open_key(parent: int | _Key, path: str, _reserved: int = 0, access: int = READ) -> _Key:
        if isinstance(parent, _Key):
            return _Key(parent.path + "\\" + path)
        entries = tree.get((parent, access & ~READ))
        if entries is None:
            raise OSError("no such key")
        return _Key(f"{parent}:{access & ~READ}")

    def enum_key(key: _Key, index: int) -> str:
        hive, view = (int(part) for part in key.path.split(":"))
        tags = list(tree[(hive, view)])
        if index >= len(tags):
            raise OSError("no more")
        return tags[index]

    def query_value(key: _Key, name: str) -> tuple[str, int]:
        root, *rest = key.path.split("\\")
        hive, view = (int(part) for part in root.split(":"))
        version, executable = tree[(hive, view)][rest[0]]
        if name == "SysVersion":
            return version, 1
        if name == "ExecutablePath":
            return executable, 1
        raise OSError("no such value")

    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = HKCU  # type: ignore[attr-defined]
    module.HKEY_LOCAL_MACHINE = HKLM  # type: ignore[attr-defined]
    module.KEY_WOW64_64KEY = VIEW64  # type: ignore[attr-defined]
    module.KEY_WOW64_32KEY = VIEW32  # type: ignore[attr-defined]
    module.KEY_READ = READ  # type: ignore[attr-defined]
    module.OpenKey = open_key  # type: ignore[attr-defined]
    module.EnumKey = enum_key  # type: ignore[attr-defined]
    module.QueryValueEx = query_value  # type: ignore[attr-defined]
    return module


class RegisteredPythonsTests(unittest.TestCase):
    def test_a_per_user_install_seen_through_both_views_is_listed_once(self) -> None:
        with patch.dict(sys.modules, {"winreg": fake_winreg(machine=False)}):
            self.assertEqual(wmicom.registered_pythons(), [((3, 13), USER_PYTHON)])

    def test_different_installs_are_all_kept_in_order(self) -> None:
        with patch.dict(sys.modules, {"winreg": fake_winreg(machine=True)}):
            self.assertEqual(
                wmicom.registered_pythons(),
                [((3, 13), USER_PYTHON), ((3, 11), MACHINE_PYTHON)],
            )


if __name__ == "__main__":
    unittest.main()
