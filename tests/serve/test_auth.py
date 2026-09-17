"""Who may reach which route, and which servers refuse to be built at all."""

import json
import unittest

from kanao.errors import KanaoError
from tests.serve.helpers import (
    TOKEN,
    ServedCase,
    StubSender,
    StubWatcher,
    _request,
    _start_server,
)


class AuthTests(ServedCase):
    # ---- health + auth -------------------------------------------------

    def test_health_needs_no_token(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_send_without_token_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "r", "text": "t"}, token=None
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_send_with_wrong_token_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "r", "text": "t"}, token="wrong"
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_non_ascii_authorization_never_crashes(self) -> None:
        self.serve(_start_server(StubSender()))
        import socket

        host, _, port = self.base.partition("://")[2].partition(":")
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            sock.sendall(
                b"POST /send HTTP/1.1\r\n"
                b"Host: x\r\n"
                b"Authorization: Bearer caf\xe9-token-value-123456\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 26\r\n"
                b"Connection: close\r\n\r\n"
                b'{"room": "r", "text": "t"}'
            )
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        finally:
            sock.close()
        head, _, response_body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        body = json.loads(response_body.decode("utf-8"))
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_bearer_scheme_is_case_insensitive_and_spacing_tolerant(self) -> None:
        self.serve(_start_server(StubSender()))
        for header in (f"bearer {TOKEN}", f"BEARER   {TOKEN}  "):
            status, body, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": "hi"},
                raw_headers={"Authorization": header},
            )
            self.assertEqual(status, 200, header)
            self.assertTrue(body["ok"])

    def test_every_protected_route_requires_auth(self) -> None:
        self.serve(_start_server(StubSender(), rooms=[{"id": "1"}],
                                 watcher=StubWatcher([[]])))
        for method, path in [
            ("GET", "/rooms"), ("GET", "/context?roomId=1"),
            ("GET", "/stream"), ("POST", "/send"), ("PUT", "/send"),
            ("GET", "/nope"),
        ]:
            status, body, _ = _request(
                self.base, method, path,
                {"room": "r", "text": "t"} if method == "POST" else None,
                token=None,
            )
            self.assertEqual(status, 401, f"{method} {path}")
            if body is not None:
                self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_bad_tokens_are_rejected_at_build_time(self) -> None:
        from kanao import serve

        for bad in ("short", "has space in it 123456", "한글토큰열여섯글자이상입니다", "", None):
            with self.assertRaises(KanaoError, msg=repr(bad)):
                serve.build_handler(StubSender().send, token=bad)

    def test_zero_poll_interval_is_rejected_at_build_time(self) -> None:
        from kanao import serve

        for bad in (0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(KanaoError, msg=repr(bad)):
                serve.build_handler(StubSender().send, token=TOKEN, poll_interval=bad)

    def test_negative_min_interval_is_rejected_at_build_time(self) -> None:
        from kanao import serve

        for bad in (-1.0, float("inf"), float("nan"), True):
            with self.assertRaises(KanaoError, msg=repr(bad)):
                serve.build_handler(StubSender().send, token=TOKEN, min_interval=bad)
    # ---- misc ----------------------------------------------------------

    def test_unknown_path_is_not_found(self) -> None:
        self.serve(_start_server(StubSender()))
        status, _, _ = _request(self.base, "GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
