"""Registration with saya (kakaocommon/register.py), against a local stand-in for saya."""

from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

from kakaocommon.register import (
    Registrar,
    Registration,
    registration_from_env,
)

SAYA_TOKEN = "synthetic-saya-register-token-0123456789"
CORE_TOKEN = "synthetic-core-token-0123456789abcdef"


class FakeSaya:
    """A register listener that answers like saya, including its refusals."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.seen: List[Dict[str, Any]] = []
        self.arrived = threading.Semaphore(0)
        answer_status = status
        answer_body = {"interval": 30, "ttl": 90} if body is None else body
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": json.loads(self.rfile.read(length) or b"{}"),
                    }
                )
                payload = json.dumps(answer_body).encode("utf-8")
                self.send_response(answer_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                outer.arrived.release()

            do_POST = _answer
            do_DELETE = _answer

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def registration(url: str) -> Registration:
    return Registration(
        register_url=url,
        register_token=SAYA_TOKEN,
        kind="kakao",
        id="kanao",
        url="https://pc.example.ts.net:8765",
        token=CORE_TOKEN,
    )


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RegistrationFromEnvTest(unittest.TestCase):
    def test_off_unless_both_address_and_token_are_given(self) -> None:
        kwargs: Dict[str, Any] = {"kind": "kakao", "id": "kanao", "port": 8765, "token": CORE_TOKEN}
        self.assertIsNone(registration_from_env({}, **kwargs))
        self.assertIsNone(registration_from_env({"SAYA_REGISTER_URL": "http://127.0.0.1:8798"}, **kwargs))
        found = registration_from_env(
            {"SAYA_REGISTER_URL": "http://127.0.0.1:8798/", "SAYA_REGISTER_TOKEN": SAYA_TOKEN}, **kwargs
        )
        self.assertEqual(
            found,
            Registration("http://127.0.0.1:8798", SAYA_TOKEN, "kakao", "kanao", "http://127.0.0.1:8765", CORE_TOKEN),
        )
        advertised = registration_from_env(
            {
                "SAYA_REGISTER_URL": "https://saya.example.ts.net:8798",
                "SAYA_REGISTER_TOKEN": SAYA_TOKEN,
                "SAYA_ADVERTISE_URL": "https://pc.example.ts.net:8765",
            },
            **kwargs,
        )
        assert advertised is not None
        self.assertEqual(advertised.url, "https://pc.example.ts.net:8765")


class RegistrarTest(unittest.TestCase):
    def test_announces_with_saya_token_and_says_goodbye(self) -> None:
        saya = FakeSaya()
        self.addCleanup(saya.close)
        logs: List[str] = []
        registrar = Registrar(registration(saya.url), log=logs.append)
        self.assertEqual(registrar.beat(), 30.0)
        registrar.stop()
        self.assertEqual(
            saya.seen,
            [
                {
                    "method": "POST",
                    "path": "/register",
                    "auth": f"Bearer {SAYA_TOKEN}",
                    "body": {
                        "kind": "kakao",
                        "id": "kanao",
                        "url": "https://pc.example.ts.net:8765",
                        "token": CORE_TOKEN,
                    },
                },
                {
                    "method": "DELETE",
                    "path": "/register",
                    "auth": f"Bearer {SAYA_TOKEN}",
                    "body": {"kind": "kakao", "id": "kanao"},
                },
            ],
        )
        self.assertEqual(logs, ["registered kanao with saya as https://pc.example.ts.net:8765"])

    def test_heartbeat_repeats_until_stopped(self) -> None:
        saya = FakeSaya(body={"interval": 0.01, "ttl": 90})
        self.addCleanup(saya.close)
        registrar = Registrar(registration(saya.url), log=lambda _: None).start()
        for _ in range(3):
            self.assertTrue(saya.arrived.acquire(timeout=5), "no heartbeat within 5s")
        registrar.stop()
        posts = [entry for entry in saya.seen if entry["method"] == "POST"]
        self.assertGreaterEqual(len(posts), 3)
        self.assertEqual(saya.seen[-1]["method"], "DELETE")

    def test_refusal_and_unreachable_are_logged_once_without_tokens(self) -> None:
        saya = FakeSaya(401, {"error": {"code": "UNAUTHORIZED", "message": "A valid bearer token is required"}})
        self.addCleanup(saya.close)
        logs: List[str] = []
        refused = Registrar(registration(saya.url), log=logs.append)
        refused.beat()
        refused.beat()
        self.assertEqual(logs, ["saya registration of kanao refused with 401 UNAUTHORIZED; retrying every 30s"])

        dead_logs: List[str] = []
        dead = Registrar(registration(f"http://127.0.0.1:{closed_port()}"), log=dead_logs.append)
        self.assertEqual(dead.beat(), 30.0)
        dead.stop()
        self.assertEqual(len(dead_logs), 1)
        self.assertTrue(dead_logs[0].startswith("saya registration of kanao unreachable"))
        for line in logs + dead_logs:
            self.assertNotIn(SAYA_TOKEN, line)
            self.assertNotIn(CORE_TOKEN, line)

    def test_recovery_is_reported(self) -> None:
        answers: List[Tuple[int, Any]] = [(503, {}), (200, {"interval": 30})]
        logs: List[str] = []
        registrar = Registrar(
            registration("http://127.0.0.1:1"),
            send=lambda *_: answers.pop(0),
            log=logs.append,
        )
        registrar.beat()
        registrar.beat()
        self.assertEqual(
            logs,
            [
                "saya registration of kanao refused with 503; retrying every 30s",
                "registered kanao with saya as https://pc.example.ts.net:8765",
            ],
        )


if __name__ == "__main__":
    unittest.main()
