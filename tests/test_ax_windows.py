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


class ClosedMainWindowTests(unittest.TestCase):
    """A client running with every window closed, which only a reopen changes.

    Closing them all leaves KakaoTalk running and addressable but with nothing
    in any window list: there is no window to unminimize, unhide or hit-test
    for, because none exists yet. Asking the client to reopen is the only
    thing that produces one, and it draws that window asynchronously.
    """

    def setUp(self) -> None:
        self.elapsed = 0.0
        self.delay = 0.0
        """How long the client takes to draw a window after being asked."""
        self.drawn_at = float("inf")
        self.state = {"AXMinimized": False}
        self.window = Mock(spec=ax.Element)
        self.window.identifier = MAIN_WINDOW_IDENTIFIER
        self.window.role = "AXWindow"
        self.window.attribute.side_effect = self.state.get
        self.window.set_attribute.side_effect = self.set_attribute
        self.app = Mock(spec=ax.Element)
        self.app.element_at_position.return_value = None
        self.app.windows.side_effect = (
            lambda: [self.window] if self.elapsed >= self.drawn_at else []
        )
        points = patch("kamado.macauto.ax.window_centers", return_value=[])
        points.start()
        self.addCleanup(points.stop)
        with patch("kamado.macauto.ax.application", return_value=self.app):
            self.backend = AXBackend(99)
        clock = patch("kamado.macauto.time.monotonic", side_effect=lambda: self.elapsed)
        sleep = patch("kamado.macauto.time.sleep", side_effect=self.advance)
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)
        reopen = patch("kamado.macauto.activate", side_effect=self.reopen)
        self.activate = reopen.start()
        self.addCleanup(reopen.stop)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds

    def reopen(self) -> None:
        """The client answers by drawing a window, some time after the request."""
        self.drawn_at = self.elapsed + self.delay

    def set_attribute(self, name: str, value: str | bool) -> bool:
        if isinstance(value, bool):
            self.state[name] = value
        return True

    def test_a_window_the_client_draws_late_is_waited_for(self) -> None:
        # Measured against the live client: 0.828 s from the request to the
        # window existing, which outlasts activation's own settle. Reading
        # once after that settle finds nothing and calls the client empty.
        self.delay = 0.828
        self.assertTrue(self.backend.show_main_window())
        self.assertEqual(self.activate.call_count, 1)
        self.assertGreaterEqual(self.elapsed, self.delay)

    def test_a_client_that_draws_nothing_is_reported_rather_than_assumed(self) -> None:
        self.delay = float("inf")
        self.assertFalse(self.backend.show_main_window())
        self.assertEqual(self.activate.call_count, 1)

    def test_a_window_drawn_for_the_work_is_parked_minimized(self) -> None:
        self.assertTrue(self.backend.show_main_window())
        self.backend.restore_main_window("missing")
        self.assertIs(self.state["AXMinimized"], True)

    def test_a_drawn_window_is_never_closed_again(self) -> None:
        # The client quits a while after its main window goes away, chat
        # windows open or not, so closing it back is not a way back at all:
        # the tidying would take the messenger down with it.
        self.assertTrue(self.backend.show_main_window())
        self.backend.restore_main_window("missing")
        self.window.descendants.assert_not_called()
        self.window.perform.assert_not_called()

    def test_a_window_that_will_not_park_is_reported(self) -> None:
        self.assertTrue(self.backend.show_main_window())
        self.window.set_attribute.side_effect = None
        self.window.set_attribute.return_value = False
        with self.assertRaises(OSError):
            self.backend.restore_main_window("missing")

    def test_a_window_that_never_appeared_is_left_alone(self) -> None:
        self.delay = float("inf")
        self.backend.show_main_window()
        self.backend.restore_main_window("missing")
        self.window.set_attribute.assert_not_called()
