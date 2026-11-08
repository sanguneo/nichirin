"""#112 HTTP wiring using only synthetic backends on any host."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.serve.helpers import ServedCase, StubSender, _request, _start_server
from tests.serve.test_upload import StubUploader, form, post


class OperationRouteTests(ServedCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        receipt_path = patch("kakaocommon.operations.directory", return_value=root / "receipts")
        receipt_path.start()
        self.addCleanup(receipt_path.stop)

    def test_http_send_replays_result_and_refuses_changed_payload(self):
        sender = StubSender()
        handler = _start_server(sender, min_interval=2, max_queued_sends=1)
        self.serve(handler)
        request = {"room": "Synthetic", "text": "private synthetic", "operationId": "http-id"}
        first = _request(self.base, "POST", "/send", request)
        queue = getattr(handler, "_send_queue")
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 1)
        try:
            replay = _request(self.base, "POST", "/send", request)
            status, refusal, _ = _request(self.base, "POST", "/send", {**request, "text": "different"})
        finally:
            queue.release()
        self.assertEqual(first[:2], replay[:2])
        self.assertEqual(first[0], 200)
        self.assertEqual(status, 400)
        self.assertEqual(refusal["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(len(sender.calls), 1)

    def test_upload_operation_id_deduplicates_input(self):
        uploader = StubUploader()
        handler = _start_server(StubSender(), upload=uploader.upload,
                                min_interval=2, max_queued_sends=1)
        self.serve(handler)
        body = form(("room", None, b"Synthetic"),
                    ("file", "synthetic.txt", b"private file bytes"),
                    ("operationId", None, b"upload-id"))
        first = post(self.base, body)
        queue = getattr(handler, "_send_queue")
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 1)
        try:
            replay = post(self.base, body)
        finally:
            queue.release()
        self.assertEqual(first, replay)
        self.assertEqual(first[0], 200)
        self.assertEqual(len(uploader.calls), 1)
