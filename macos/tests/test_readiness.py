"""Login diagnostics never open a window or make offline history unreadable."""

import unittest
import socket
from typing import Optional
from unittest.mock import patch

from tests.fixtures import build_reader
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server


class StatusReadinessTests(unittest.TestCase):
    def test_a_logged_out_client_is_not_ready_even_with_a_readable_database(self):
        with build_reader() as reader, patch.object(
            reader, "session_probe", return_value="logged-out"
        ):
            status = reader.status()
            self.assertFalse(status["ready"])
            self.assertTrue(status["stale"])
            self.assertEqual(len(reader.rooms()), 6)

    def test_a_cached_count_does_not_hide_a_later_logout(self):
        with build_reader() as reader, patch.object(
            reader, "session_probe", return_value="logged-out"
        ), patch.object(reader, "_cached_status_rooms", return_value=6):
            self.assertFalse(reader.status()["ready"])


class SessionTests(unittest.TestCase):
    class Node:
        def __init__(self, role="", identifier="", subrole="", children=(), title=""):
            self.role = role
            self.identifier = identifier
            self.subrole = subrole
            self.kids = list(children)
            self.title = title

        def attribute(self, name):
            if name == "AXChildren" and self.role == "AXTable":
                raise AssertionError("must not enumerate chat rows")
            return {
                "AXIdentifier": self.identifier, "AXSubrole": self.subrole,
                "AXRole": self.role, "AXTitle": self.title, "AXChildren": self.kids,
                "AXWindows": self.kids,
            }.get(name)

        def children(self):
            if self.role == "AXTable":
                raise AssertionError("must not enumerate chat rows")
            return self.kids

        def windows(self):
            return self.kids

        def same_as(self, other):
            return self is other

    def session(self, nodes=(), trusted=True, pid: Optional[int] = 123):
        from kamado.macauto import client_session

        app = self.Node(children=nodes)
        with patch("kamado.macauto.client_pid", return_value=pid), patch(
            "kamado.ax.is_trusted", return_value=trusted
        ), patch("kamado.ax.application", return_value=app):
            return client_session()

    def test_the_main_login_password_field_identifies_logout(self):
        window = self.Node(
            "AXWindow", "Main Window",
            children=[
                self.Node("AXTextField", subrole="AXSecureTextField"),
                self.Node("AXButton", title="로그인"),
            ],
        )
        self.assertEqual(self.session([window]), "logged-out")

    def test_a_chat_table_identifies_a_signed_in_client_without_reading_rows(self):
        window = self.Node(
            "AXWindow", "Main Window", children=[self.Node("AXTable")]
        )
        self.assertEqual(self.session([window]), "signed-in")

    def test_a_secure_field_in_an_unrelated_window_is_not_a_login(self):
        window = self.Node(
            "AXWindow", "Preferences",
            children=[self.Node("AXTextField", subrole="AXSecureTextField")],
        )
        self.assertEqual(self.session([window]), "unknown")

    def test_missing_windows_or_permission_are_unknown_not_signed_in(self):
        self.assertEqual(self.session(), "unknown")
        self.assertEqual(self.session(trusted=False), "unknown")

    def test_a_closed_client_keeps_offline_reading_available(self):
        self.assertEqual(self.session(pid=None), "closed")

    def test_a_lock_password_alone_is_not_proof_of_logout(self):
        window = self.Node(
            "AXWindow", "Main Window", children=[
                self.Node("AXTextField", subrole="AXSecureTextField"),
                self.Node("AXButton", title="Unlock"),
            ],
        )
        self.assertEqual(self.session([window]), "unknown")

    def test_native_element_creation_failure_is_unknown(self):
        from kamado.macauto import client_session

        with patch("kamado.macauto.client_pid", return_value=123), patch(
            "kamado.ax.is_trusted", return_value=True
        ), patch("kamado.ax.application", side_effect=RuntimeError("synthetic AX failure")):
            self.assertEqual(client_session(), "unknown")

    def test_the_deadline_prevents_the_next_native_read(self):
        from kamado.macauto import client_session

        app = self.Node()
        with patch("kamado.macauto.client_pid", return_value=123), patch(
            "kamado.ax.is_trusted", return_value=True
        ), patch("kamado.ax.application", return_value=app), patch(
            "kamado.macauto.time.monotonic", side_effect=[0.0, 0.0, 4.0]
        ), patch.object(
            app, "attribute",
            side_effect=[[], AssertionError("deadline started another native read")],
        ):
            self.assertEqual(client_session(), "unknown")


class HTTPReadinessTests(ServedCase):
    def test_health_and_rooms_preserve_payloads_but_signal_stale_data(self):
        self.serve(_start_server(
            StubSender(), rooms=[{"id": "1"}],
            session_provider=lambda: "logged-out",
        ))
        for path in ("/health", "/rooms"):
            with self.subTest(path=path):
                status, body, headers = _request(self.base, "GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
                self.assertEqual(headers["X-Kamado-Ready"], "false")
                self.assertEqual(headers["X-Kamado-Stale"], "true")

    def test_an_unobservable_session_is_not_certified_fresh(self):
        self.serve(_start_server(
            StubSender(), session_provider=lambda: "unknown",
        ))
        _, _, headers = _request(self.base, "GET", "/health")
        self.assertEqual(headers["X-Kamado-Ready"], "unknown")
        self.assertEqual(headers["X-Kamado-Stale"], "unknown")

    def test_public_health_does_not_disclose_session_or_run_the_probe(self):
        def forbidden():
            raise AssertionError("public request ran native inspection")

        self.serve(_start_server(StubSender(), session_provider=forbidden))
        status, body, headers = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn("X-Kamado-Ready", headers)

    def test_a_failed_session_probe_does_not_break_health(self):
        def failed():
            raise RuntimeError("synthetic native failure")

        self.serve(_start_server(StubSender(), session_provider=failed))
        status, body, headers = _request(self.base, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(headers["X-Kamado-Ready"], "unknown")

    def test_a_malformed_request_still_receives_its_protocol_error(self):
        def forbidden():
            raise AssertionError("malformed request ran native inspection")

        def receive():
            with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=5
            ) as connection, connection.makefile("rb") as response:
                connection.sendall(b"GET /health HTTP/9.9\r\n")
                return response.read(8192)

        # The stdlib answers this version with its error body, not a status
        # line. Compare against the unchanged route instead of pinning prose.
        self.serve(_start_server(StubSender()))
        expected = receive()
        self.assertTrue(expected)
        self.server.RequestHandlerClass = _start_server(
            StubSender(), session_provider=forbidden
        )
        self.assertEqual(receive(), expected)
