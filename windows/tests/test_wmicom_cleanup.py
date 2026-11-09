"""A WMI call that fails part way is reported as itself (review of #118).

``_Com.create`` registers each out-pointer before the call that fills it, so a
failure part way leaves the later ones NULL. Releasing those on the way out
raised ``ValueError: NULL pointer access`` over the ``OSError`` that
``WmiBoundary.create`` turns into ``LaunchFailed``. The object is built without
``__init__`` so no Windows library is loaded and the test runs anywhere.
"""

from __future__ import annotations

import unittest
from ctypes import c_void_p

from kanao import wmicom


def _com_with_unfilled_references(count: int) -> wmicom._Com:
    com = wmicom._Com.__new__(wmicom._Com)
    com.references = [c_void_p() for _ in range(count)]
    com.strings = []
    com.initialized = False
    return com


class ComCleanupTests(unittest.TestCase):
    def test_unfilled_references_are_skipped_on_exit(self) -> None:
        _com_with_unfilled_references(3).__exit__(None, None, None)

    def test_the_failure_that_left_them_unfilled_is_what_propagates(self) -> None:
        with self.assertRaises(OSError) as raised:
            with _com_with_unfilled_references(2):
                raise OSError(-2147217405, "ConnectServer failed")
        self.assertEqual(raised.exception.args[1], "ConnectServer failed")


if __name__ == "__main__":
    unittest.main()
