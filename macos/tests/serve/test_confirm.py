"""HTTP confirmation through the real live wiring and fake client/readers."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kamado.macauto import ChatWindow, Sender
from kamado.serve import backends
from kamado.reader import Reader
from tests.fixtures import DIRECT_ROOM, build_reader
from tests.test_send import FakeWindows
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server


class ConfirmationHTTPTests(ServedCase):
    def test_http_observes_the_submitted_row_through_live_send_wiring(self):
        backend = FakeWindows()
        backend.windows = [ChatWindow(42, "Peer Renamed")]
        sender = Sender(backend, running=lambda: True)
        rows = []
        original_submit = backend.submit

        def submit(control):
            original_submit(control)
            rows.append(SimpleNamespace(id=9, text="synthetic", from_me=True))

        send = backends.make_live_send(build_reader)
        self.serve(_start_server(SimpleNamespace(send=send), min_interval=0))
        request = {
            "room": str(DIRECT_ROOM), "text": "synthetic", "confirm": 1,
            "operationId": "confirmed-http",
        }
        with tempfile.TemporaryDirectory() as root, \
             patch("kakaocommon.operations.directory", return_value=Path(root)), \
             patch("kamado.macauto.make_sender", return_value=sender), \
             patch.object(backend, "submit", side_effect=submit), \
             patch.object(Reader, "newest_log_id", return_value=8), \
             patch.object(Reader, "messages", side_effect=lambda _, **kw: [
                 row for row in rows if row.id > kw["after_id"]
             ]):
            status, body, _ = _request(self.base, "POST", "/send", request)
            replay = _request(self.base, "POST", "/send", request)
            for confirm in ({}, {"confirm": 2}):
                changed = {key: value for key, value in request.items() if key != "confirm"}
                status_changed, refusal, _ = _request(
                    self.base, "POST", "/send", {**changed, **confirm}
                )
                self.assertEqual(status_changed, 400, refusal)
                self.assertEqual(refusal["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(status, 200, body)
        self.assertEqual((status, body), replay[:2])
        self.assertEqual((body["logged"], body["messageId"]), (True, "9"))
        self.assertEqual(body["disposition"], {"delivery": "submitted", "retry": "unsafe"})
        self.assertEqual(backend.actions, ["fill", "submit"])

    def test_invalid_budgets_are_rejected_before_queue_or_sender(self):
        sender = StubSender()
        self.serve(_start_server(sender))
        for value in (True, False, None, "1", 0, -1, 31, 10**400, float("nan"), float("inf")):
            with self.subTest(value=value):
                status, body, _ = _request(self.base, "POST", "/send", {
                    "room": "Synthetic", "text": "synthetic", "confirm": value,
                })
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["disposition"], {"delivery": "not_delivered", "retry": "safe"})
        self.assertEqual(sender.calls, [])
