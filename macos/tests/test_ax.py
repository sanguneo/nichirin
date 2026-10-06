"""CoreFoundation payload types, without controlling a live application."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import Mock, patch

from kamado import ax


@unittest.skipUnless(sys.platform == "darwin", "needs Apple's CoreFoundation and AX")
class AttributeTypeTests(unittest.TestCase):
    def test_boolean_attributes_cross_the_boundary_as_cfbooleans(self) -> None:
        core, services = ax._load()
        element = ax.application(os.getpid())
        received: list[tuple[int, bool]] = []

        def accept(_element, _key, value) -> int:
            kind = core.CFGetTypeID(value)
            flag = bool(core.CFBooleanGetValue(value)) if kind == core.CFBooleanGetTypeID() else False
            received.append((kind, flag))
            return 0

        with patch.object(services, "AXUIElementSetAttributeValue", side_effect=accept):
            for value in (True, False):
                self.assertTrue(element.set_attribute("AXMinimized", value))
        self.assertEqual(
            received,
            [(core.CFBooleanGetTypeID(), True), (core.CFBooleanGetTypeID(), False)],
        )

    def test_text_attributes_still_cross_the_boundary_as_cfstrings(self) -> None:
        core, services = ax._load()
        element = ax.application(os.getpid())
        received: list[int] = []

        def accept(_element, _key, value) -> int:
            received.append(core.CFGetTypeID(value))
            return 0

        with patch.object(services, "AXUIElementSetAttributeValue", side_effect=accept):
            self.assertTrue(element.set_attribute("AXValue", "synthetic"))
        self.assertEqual(received, [core.CFStringGetTypeID()])

    def test_element_identity_is_not_python_wrapper_identity(self) -> None:
        first = ax.application(os.getpid())
        second = ax.application(os.getpid())
        different = ax.application(os.getppid())
        self.assertTrue(first.same_as(second))
        self.assertFalse(first.same_as(different))


class WindowEnumerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Mock(spec=ax.Element)
        self.visible = Mock(spec=ax.Element)
        self.visible.role = "AXWindow"
        self.hidden = Mock(spec=ax.Element)
        self.hidden.role = "AXWindow"
        # Two genuinely different windows may have identical titles.
        self.visible.title = self.hidden.title = "Synthetic room"
        self.duplicate = Mock(spec=ax.Element)
        self.duplicate.role = "AXWindow"
        self.visible.same_as = Mock(side_effect=lambda other: other is self.duplicate)
        self.hidden.same_as = Mock(return_value=False)
        self.menu = Mock(spec=ax.Element)
        self.menu.role = "AXMenuBar"

    def test_navigation_children_find_a_window_missing_from_axwindows(self) -> None:
        self.app.attribute.side_effect = {
            "AXWindows": [self.visible],
            "AXChildrenInNavigationOrder": [self.duplicate, self.hidden, self.menu],
        }.get
        found = ax.Element.windows(self.app)
        self.assertEqual(found, [self.visible, self.hidden])
        self.app.set_attribute.assert_not_called()
        self.hidden.perform.assert_not_called()

    def test_empty_axwindows_does_not_hide_navigation_windows(self) -> None:
        self.app.attribute.side_effect = {
            "AXWindows": [],
            "AXChildrenInNavigationOrder": [self.hidden],
        }.get
        self.assertEqual(ax.Element.windows(self.app), [self.hidden])

    def test_clients_without_navigation_children_keep_their_windows(self) -> None:
        self.app.attribute.side_effect = {
            "AXWindows": [self.visible],
            "AXChildrenInNavigationOrder": None,
        }.get
        self.assertEqual(ax.Element.windows(self.app), [self.visible])


class WindowGeometryTests(unittest.TestCase):
    def test_only_the_requested_processes_normal_window_centers_are_returned(self) -> None:
        entries = [
            {"kCGWindowOwnerPID": 99, "kCGWindowLayer": 0,
             "kCGWindowBounds": (-1000, 40, 400, 600)},
            {"kCGWindowOwnerPID": 100, "kCGWindowLayer": 0,
             "kCGWindowBounds": (0, 0, 500, 500)},
            {"kCGWindowOwnerPID": 99, "kCGWindowLayer": 25,
             "kCGWindowBounds": (0, 0, 500, 500)},
            {"kCGWindowOwnerPID": 99, "kCGWindowLayer": 0,
             "kCGWindowBounds": (0, 0, 0, 500)},
        ]
        core, services = Mock(), Mock()
        services.CGWindowListCopyWindowInfo.return_value = entries
        core.CFArrayGetCount.side_effect = len
        core.CFArrayGetValueAtIndex.side_effect = lambda array, index: array[index]
        core.CFDictionaryGetValue.side_effect = lambda entry, key: entry.get(key)

        def number(value, _kind, out) -> bool:
            out.value = value
            return True

        def rectangle(bounds, out) -> bool:
            out.origin.x, out.origin.y, out.size.width, out.size.height = bounds
            return True

        core.CFNumberGetValue.side_effect = number
        services.CGRectMakeWithDictionaryRepresentation.side_effect = rectangle
        with patch("kamado.ax._load", return_value=(core, services)), \
             patch("kamado.ax._cfstr", side_effect=lambda text: text), \
             patch("kamado.ax.ctypes.byref", side_effect=lambda value: value):
            self.assertEqual(ax.window_centers(99), [(-800.0, 340.0)])

    def test_unavailable_windowserver_metadata_leaves_ax_discovery_available(self) -> None:
        core, services = Mock(), Mock()
        services.CGWindowListCopyWindowInfo.return_value = None
        with patch("kamado.ax._load", return_value=(core, services)):
            self.assertEqual(ax.window_centers(99), [])
