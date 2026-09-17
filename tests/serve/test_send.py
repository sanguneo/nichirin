"""``POST /send``: how a body is framed, and how two sends are spaced."""

import json
import threading
import unittest
from http.client import HTTPConnection

from kanao.errors import ErrorCode, KanaoError
from tests.serve.helpers import (
    TOKEN,
    ServedCase,
    StubSender,
    _raw_request,
    _request,
    _start_server,
)


class SendTests(ServedCase):
    # ---- body framing --------------------------------------------------

    def test_post_without_content_length_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        host, _, port = self.base.partition("://")[2].partition(":")
        connection = HTTPConnection(host, int(port), timeout=5)
        try:
            connection.putrequest("POST", "/send", skip_host=True,
                                  skip_accept_encoding=True)
            connection.putheader("Host", f"{host}:{port}")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.endheaders()
            connection.send(b'{"room": "r", "text": "t"}')
            response = connection.getresponse()
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_negative_content_length_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body = _raw_request(
            self.base, "POST", "/send", b"",
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "-5",
             "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_chunked_body_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body = _raw_request(
            self.base, "POST", "/send", b'{"room": "r", "text": "t"}',
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "24",
             "Transfer-Encoding": "chunked", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_invalid_utf8_body_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body = _raw_request(
            self.base, "POST", "/send", b'{"room": "\xff\xfe", "text": "t"}',
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "26",
             "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_truncated_body_is_rejected(self) -> None:
        self.serve(_start_server(StubSender()))
        import socket

        host, _, port = self.base.partition("://")[2].partition(":")
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            sock.sendall(
                f"POST /send HTTP/1.1\r\n"
                f"Host: x\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: 100\r\n"
                f"Connection: close\r\n\r\n".encode("ascii") + b'{"room":'
            )
            # Half-close: the client will send no more, so the server's
            # Content-Length wait must end in a 400, not a silent hang.
            sock.shutdown(socket.SHUT_WR)
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
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    # ---- send ----------------------------------------------------------

    def test_send_happy_path_returns_the_sender_envelope(self) -> None:
        sender = StubSender()
        self.serve(_start_server(sender))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "hello"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["submitted"])
        self.assertEqual(body["room"], "Synthetic")
        self.assertEqual(body["length"], 5)
        self.assertEqual(sender.calls[0]["room"], "Synthetic")

    def test_send_missing_text_is_a_bad_request(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(self.base, "POST", "/send", {"room": "Synthetic"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_send_string_flags_are_rejected_not_coerced(self) -> None:
        self.serve(_start_server(StubSender()))
        for flag in ("dryRun", "openRoom"):
            status, body, _ = _request(
                self.base, "POST", "/send",
                {"room": "Synthetic", "text": "hi", flag: "false"},
            )
            self.assertEqual(status, 400, flag)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_ambiguous_room_surfaces_candidates(self) -> None:
        self.serve(_start_server(StubSender()))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Ambiguous room", "text": "hello"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "AMBIGUOUS_CHAT")
        self.assertEqual(len(body["candidates"]), 2)

    def test_back_to_back_sends_are_rate_limited(self) -> None:
        self.serve(_start_server(StubSender(), min_interval=60.0))
        status, _, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "one"}
        )
        self.assertEqual(status, 200)
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "two"}
        )
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "RATE_LIMITED")

    def test_zero_min_interval_disables_the_limiter(self) -> None:
        sender = StubSender()
        self.serve(_start_server(sender, min_interval=0))
        for word in ("one", "two", "three"):
            status, _, _ = _request(
                self.base, "POST", "/send", {"room": "Synthetic", "text": word}
            )
            self.assertEqual(status, 200, word)
        self.assertEqual(len(sender.calls), 3)

    def test_dry_run_never_consumes_quota(self) -> None:
        self.serve(_start_server(StubSender(), min_interval=3600.0))
        status, _, _ = _request(
            self.base, "POST", "/send",
            {"room": "Synthetic", "text": "probe", "dryRun": True},
        )
        self.assertEqual(status, 200)
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "real"}
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["submitted"])

    def test_concurrent_sends_are_serialized_and_quota_holds(self) -> None:
        sender = StubSender()
        entered_holder: list = []
        barrier = threading.Barrier(2)
        results: list = []

        self.serve(_start_server(sender, min_interval=3600.0))

        def one(word: str) -> None:
            barrier.wait(timeout=5)
            results.append(
                _request(self.base, "POST", "/send", {"room": "Synthetic", "text": word})
            )
            entered_holder.append(word)

        first = threading.Thread(target=one, args=("aaa",), daemon=True)
        second = threading.Thread(target=one, args=("bbb",), daemon=True)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        statuses = sorted(status for status, _, _ in results)
        # One wins the lock (200), the other is quota-refused (429) — never two
        # real sends through the single input box, and never a 500.
        self.assertEqual(statuses, [200, 429])
        self.assertEqual(len(sender.calls), 1)

    def test_lock_is_released_after_a_sender_error(self) -> None:
        sender = StubSender(fail_with=KanaoError(ErrorCode.SEND_FAILED, "boom"))
        self.serve(_start_server(sender))
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "x"}
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "SEND_FAILED")
        sender.fail_with = None
        status, body, _ = _request(
            self.base, "POST", "/send", {"room": "Synthetic", "text": "y"}
        )
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main()
