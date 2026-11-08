"""#112 HTTP wiring; all writes use synthetic backends and temporary receipts."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from kamado.serve.upload import UploadStage
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server
from tests.serve.test_upload import BOUNDARY, StubUploader, multipart
from tests.serve.helpers import TOKEN, _raw_request


class OperationRouteTests(ServedCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        receipt_path = patch("kakaocommon.operations.directory", return_value=self.root / "receipts")
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

    def test_upload_operation_id_deduplicates_before_staging_or_input(self):
        uploader = StubUploader()
        stage = UploadStage(self.root / "uploads")
        handler = _start_server(StubSender(), upload=uploader, upload_stage=stage,
                                min_interval=2, max_queued_sends=1)
        self.serve(handler)
        body = multipart([
            ("room", b"Synthetic", None), ("file", b"private file bytes", "synthetic.txt"),
            ("operationId", b"upload-id", None),
        ])
        headers = {"Authorization": f"Bearer {TOKEN}",
                   "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                   "Content-Length": str(len(body))}
        first = _raw_request(self.base, "POST", "/upload", body, headers)
        queue = getattr(handler, "_send_queue")
        self.assertIsNone(queue.reserve())
        self.assertEqual(queue.reserve(), 1)
        try:
            replay = _raw_request(self.base, "POST", "/upload", body, headers)
        finally:
            queue.release()
        self.assertEqual(first, replay)
        self.assertEqual(first[0], 200)
        self.assertEqual(len(uploader.calls), 1)
        self.assertEqual(len(list((self.root / "uploads").glob("*/*"))), 1)
