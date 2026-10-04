"""``POST /upload`` and the ``features`` key of ``GET /health`` that declares it.

The uploader is a stub, so these prove the HTTP half on any platform: how a
multipart body is refused, the size limit that is checked before reading, the
name the room sees, the line texts and files share, and the staged copy that
outlives the request for exactly as long as the client may still read it.
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from kamado.errors import ErrorCode, KamadoError
from kamado.serve import config
from kamado.serve.upload import UploadStage, sanitize_name
from tests.serve.helpers import (
    TOKEN,
    ServedCase,
    StubSender,
    _raw_request,
    _request,
    _start_server,
)

BOUNDARY = "synthetic-boundary-7f3a"


def multipart(parts) -> bytes:
    """Frame ``(name, value, filename)`` parts the way a browser would."""
    chunks = []
    for name, value, filename in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        head = f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
        if filename is not None:
            head += "Content-Type: application/octet-stream\r\n"
        chunks.append(head.encode("utf-8") + b"\r\n" + value + b"\r\n")
    chunks.append(f"--{BOUNDARY}--\r\n".encode("ascii"))
    return b"".join(chunks)


class StubUploader:
    """Mimics the live uploader: reads the staged file, never a desktop."""

    def __init__(self, fail_with=None) -> None:
        self.calls: list[dict] = []
        self.fail_with = fail_with

    def __call__(self, room, path, name, open_if_needed=False):
        if self.fail_with is not None:
            raise self.fail_with
        data = Path(path).read_bytes()
        self.calls.append(
            {"room": room, "path": path, "name": name, "data": data,
             "open": open_if_needed}
        )
        return {"submitted": True, "room": room, "roomId": "42", "name": name,
                "size": len(data)}


class UploadRouteTests(ServedCase):
    def setUp(self) -> None:
        self.stage_dir = Path(tempfile.mkdtemp(prefix="kamado-upload-test-"))
        self.addCleanup(shutil.rmtree, self.stage_dir, True)

    def start(self, uploader=None, **kwargs):
        self.uploader = uploader if uploader is not None else StubUploader()
        handler = _start_server(
            StubSender(), upload=self.uploader,
            upload_stage=UploadStage(self.stage_dir), **kwargs
        )
        self.serve(handler)
        return handler

    def post(self, body: bytes, content_type=None, length=None):
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": content_type
            or f"multipart/form-data; boundary={BOUNDARY}",
            "Content-Length": str(len(body) if length is None else length),
        }
        return _raw_request(self.base, "POST", "/upload", body, headers)

    def test_health_declares_the_upload_and_its_limit(self) -> None:
        self.start()
        status, body, _ = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(
            body["features"]["upload"]["maxBytes"], config.MAX_UPLOAD_BYTES
        )

    def test_without_an_uploader_neither_health_nor_the_route_offer_it(self) -> None:
        self.serve(_start_server(StubSender()))
        _status, body, _ = _request(self.base, "GET", "/health", token=None)
        self.assertNotIn("features", body)
        status, body = _raw_request(
            self.base, "POST", "/upload", b"",
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"},
        )
        self.assertEqual(status, 404)

    def test_a_file_reaches_the_uploader_whole_under_its_cleaned_name(self) -> None:
        self.start()
        payload = bytes(range(256)) * 4
        status, body = self.post(multipart([
            ("room", b"Synthetic", None),
            ("file", payload, "../나의 사진:1.png"),
            ("openRoom", b"true", None),
        ]))
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body,
            {"ok": True, "submitted": True, "room": "Synthetic", "roomId": "42",
             "name": "나의 사진1.png", "size": len(payload)},
        )
        call = self.uploader.calls[0]
        self.assertEqual(call["data"], payload)
        self.assertTrue(call["open"])
        self.assertEqual(Path(call["path"]).name, "나의 사진1.png")
        self.assertTrue(Path(call["path"]).is_relative_to(self.stage_dir))

    def test_malformed_forms_are_refused_before_anything_is_sent(self) -> None:
        self.start()
        cases = {
            "unknown part": [("room", b"R", None), ("file", b"x", "a.txt"),
                             ("caption", b"hi", None)],
            "repeated part": [("room", b"R", None), ("room", b"S", None),
                              ("file", b"x", "a.txt")],
            "two files": [("room", b"R", None), ("file", b"x", "a.txt"),
                          ("file", b"y", "b.txt")],
            "no file": [("room", b"R", None)],
            "no room": [("file", b"x", "a.txt")],
            "blank room": [("room", b"  ", None), ("file", b"x", "a.txt")],
            "file without a name": [("room", b"R", None), ("file", b"x", None)],
            "empty file": [("room", b"R", None), ("file", b"", "a.txt")],
            "openRoom not a flag": [("room", b"R", None), ("file", b"x", "a.txt"),
                                    ("openRoom", b"yes", None)],
        }
        for label, parts in cases.items():
            with self.subTest(label):
                status, body = self.post(multipart(parts))
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.uploader.calls, [])

    def test_a_body_that_is_not_multipart_is_refused(self) -> None:
        self.start()
        status, body = self.post(b'{"room": "R"}', content_type="application/json")
        self.assertEqual(status, 400, body)
        status, body = self.post(b"x", content_type="multipart/form-data")
        self.assertEqual(status, 400, body)

    def test_a_declared_length_past_the_limit_is_refused_unread(self) -> None:
        self.start()
        too_long = config.MAX_UPLOAD_BYTES + config.UPLOAD_FRAMING_BYTES + 1
        status, body = self.post(b"", length=too_long)
        self.assertEqual(status, 400, body)
        self.assertEqual(self.uploader.calls, [])

    def test_a_file_past_the_limit_is_refused_even_inside_the_framing_slack(self) -> None:
        self.start(max_upload_bytes=16)
        status, body = self.post(multipart([
            ("room", b"R", None), ("file", b"x" * 17, "a.bin"),
        ]))
        self.assertEqual(status, 400, body)
        self.assertEqual(self.uploader.calls, [])

    def test_files_wait_in_the_same_line_as_texts(self) -> None:
        handler = self.start(min_interval=60.0, max_queued_sends=1)
        handler._send_queue.reserve()  # type: ignore[attr-defined]
        status, body = self.post(multipart([
            ("room", b"R", None), ("file", b"x", "a.txt"),
        ]))
        self.assertEqual(status, 429, body)
        self.assertEqual(body["error"]["code"], "RATE_LIMITED")
        self.assertEqual(self.uploader.calls, [])

    def test_uploader_failures_keep_their_status(self) -> None:
        expected = {
            ErrorCode.WINDOW_NOT_FOUND: 404,
            ErrorCode.AMBIGUOUS_CHAT: 409,
            ErrorCode.SEND_FAILED: 500,
            ErrorCode.PERMISSION_REQUIRED: 503,
        }
        for code, status in expected.items():
            with self.subTest(code=code.value):
                self.start(StubUploader(fail_with=KamadoError(code, "synthetic")))
                got, body = self.post(multipart([
                    ("room", b"R", None), ("file", b"x", "a.txt"),
                ]))
                self.assertEqual(got, status, body)
                self.assertEqual(body["error"]["code"], code.value)
                self.tearDown()

    def test_the_route_needs_the_token(self) -> None:
        self.start()
        body = multipart([("room", b"R", None), ("file", b"x", "a.txt")])
        status, _ = _raw_request(
            self.base, "POST", "/upload", body,
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
             "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.uploader.calls, [])


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kamado-stage-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_staged_copy_is_kept_until_it_is_old_then_swept(self) -> None:
        """The client reads the path after the confirmation closes, so the
        copy outlives the request; the next upload sweeps old ones."""
        stage = UploadStage(self.root)
        old = stage.stage("old.txt", b"old")
        fresh = stage.stage("fresh.txt", b"fresh")
        aged = time.time() - config.UPLOAD_KEEP_SECONDS - 60
        os.utime(old.parent, (aged, aged))
        stage.sweep()
        self.assertFalse(old.parent.exists())
        self.assertEqual(fresh.read_bytes(), b"fresh")

    def test_two_uploads_of_one_name_do_not_share_a_path(self) -> None:
        stage = UploadStage(self.root)
        first = stage.stage("same.txt", b"1")
        second = stage.stage("same.txt", b"2")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"1")


class NameTests(unittest.TestCase):
    def test_a_name_is_reduced_to_one_safe_file_name(self) -> None:
        cases = {
            "../../etc/passwd": "passwd",
            "C:\\Users\\me\\photo.jpg": "photo.jpg",
            'a<b>c:d"e|f?g*h.txt': "abcdefgh.txt",
            "tab\there\x00.txt": "tabhere.txt",
            "CON.txt": "_CON.txt",
            "lpt1": "_lpt1",
            "  trailing. ": "trailing",
        }
        for raw, cleaned in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_name(raw), cleaned)

    def test_a_long_name_keeps_its_extension(self) -> None:
        cleaned = sanitize_name("가" * 300 + ".mp4")
        self.assertLessEqual(len(cleaned), 120)
        self.assertTrue(cleaned.endswith(".mp4"))

    def test_a_name_with_nothing_left_is_refused(self) -> None:
        for raw in ("", "///", "...", "<>"):
            with self.subTest(raw=raw):
                with self.assertRaises(KamadoError) as caught:
                    sanitize_name(raw)
                self.assertIs(caught.exception.code, ErrorCode.INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
