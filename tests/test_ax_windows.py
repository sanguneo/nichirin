"""Synthetic AX window states; enumeration never performs a window action."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from kamado import ax
from kamado.macauto import AXBackend, MAIN_WINDOW_IDENTIFIER, Sender
from kamado.errors import ErrorCode, KamadoError


class WindowStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {"AXMinimized": False, "AXHidden": False}
        self.window = Mock(spec=ax.Element)
        self.window.identifier = MAIN_WINDOW_IDENTIFIER
        self.window.role = "AXWindow"
        self.window.same_as.side_effect = lambda other: other is self.window
        self.window.attribute.side_effect = self.state.get
        self.window.set_attribute.side_effect = self.set_attribute
        self.app = Mock(spec=ax.Element)
        self.app.windows.return_value = [self.window]
        self.app.attribute.side_effect = self.state.get
        self.app.element_at_position.return_value = None
        points = patch("kamado.macauto.ax.window_centers", return_value=[])
        self.points = points.start()
        self.addCleanup(points.stop)
        with patch("kamado.macauto.ax.application", return_value=self.app):
            self.backend = AXBackend(99)
        self.handle = self.backend._register(self.window)
        self.elapsed = 0.0
        clock = patch("kamado.macauto.time.monotonic", side_effect=lambda: self.elapsed)
        sleep = patch("kamado.macauto.time.sleep", side_effect=self.advance)
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds

    def set_attribute(self, name: str, value: str | bool) -> bool:
        # The observed client acknowledges strings but changes nothing.
        if isinstance(value, bool):
            self.state[name] = value
        return True

    def test_minimization_changes_the_actual_boolean_state(self) -> None:
        self.backend.minimize_window(self.handle)
        self.assertIs(self.state["AXMinimized"], True)

    def test_showing_the_main_window_actually_unminimizes_it(self) -> None:
        self.state["AXMinimized"] = True
        self.assertTrue(self.backend.show_main_window())
        self.assertIs(self.state["AXMinimized"], False)

    def test_restoration_reinstates_the_minimized_state(self) -> None:
        self.backend.restore_main_window("minimized")
        self.assertIs(self.state["AXMinimized"], True)

    def test_a_refused_minimization_is_not_silently_accepted(self) -> None:
        self.window.set_attribute.return_value = False
        self.window.set_attribute.side_effect = None
        with self.assertRaises(OSError):
            self.backend.minimize_window(self.handle)

    def test_empty_discovery_never_asks_the_window_menu_to_show_a_window(self) -> None:
        self.app.windows.return_value = []
        with self.assertRaises(KamadoError):
            Sender(self.backend, running=lambda: True).send("synthetic", "hello", dry_run=True)
        self.app.attribute.assert_not_called()

    def test_main_state_inspection_does_not_activate_a_missing_window(self) -> None:
        self.app.windows.return_value = []
        with patch("kamado.macauto.activate") as activate:
            self.assertEqual(self.backend.main_state(), "missing")
        activate.assert_not_called()

    def test_minimized_chat_discovery_does_not_restore_it(self) -> None:
        self.window.identifier = ""
        self.window.title = "synthetic"
        self.state["AXMinimized"] = True
        self.assertEqual(self.backend.chat_windows()[0].title, "synthetic")
        self.window.set_attribute.assert_not_called()
        self.window.perform.assert_not_called()

    def test_an_app_scoped_hit_finds_a_window_missing_from_both_lists(self) -> None:
        target = Mock(spec=ax.Element)
        target.role = "AXWindow"
        target.identifier = ""
        target.title = "Synthetic hidden room"
        child = Mock(spec=ax.Element)
        child.role = "AXButton"
        child.attribute.return_value = target
        self.points.return_value = [(-800.0, 320.0)]
        self.app.element_at_position.return_value = child

        found = Sender(self.backend, running=lambda: True).resolve(target.title)

        self.assertEqual(found.title, target.title)
        self.app.element_at_position.assert_called_once_with(-800.0, 320.0)
        child.attribute.assert_called_once_with("AXWindow")
        self.app.set_attribute.assert_not_called()
        target.perform.assert_not_called()

    def test_a_hit_does_not_duplicate_an_already_enumerated_window(self) -> None:
        self.points.return_value = [(200.0, 200.0)]
        self.app.element_at_position.return_value = self.window
        self.assertEqual(self.backend._windows(), [self.window])

    def test_a_hit_on_another_window_never_substitutes_for_the_target(self) -> None:
        other = Mock(spec=ax.Element)
        other.role = "AXWindow"
        other.identifier = ""
        other.title = "Other synthetic room"
        self.points.return_value = [(200.0, 200.0)]
        self.app.element_at_position.return_value = other
        with self.assertRaises(KamadoError) as caught:
            Sender(self.backend, running=lambda: True).resolve("Target")
        self.assertEqual(caught.exception.code, ErrorCode.WINDOW_NOT_FOUND)
        other.perform.assert_not_called()
