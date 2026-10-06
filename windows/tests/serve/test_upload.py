"""``POST /upload``: the form a file arrives in, and the line it waits in."""

import itertools
import select
import socket
import threading
import os
import tempfile
import time
import unittest
from pathlib import Path

from kanao.errors import ErrorCode, KanaoError
from kanao.serve import MAX_UPLOAD_BYTES, ServeHTTPServer
from kanao.serve.backends import stage_upload, sweep_uploads
from kanao.serve.config import UPLOAD_KEEP_SECONDS, upload_name
from kanao.serve.multipart import MAX_PARTS, boundary_of, parse
from tests.serve.helpers import TOKEN, ServedCase, StubSender, _raw_request, _request, _start_server

BOUNDARY = "synthetic-boundary-0123"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def form(*parts: "tuple[str, str | None, bytes]", boundary: str = BOUNDARY) -> bytes:
    body = b""
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        head = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
        if filename is not None:
            head += "Content-Type: application/octet-stream\r\n"
        body += (head + "\r\n").encode("utf-8") + data + b"\r\n"
    return body + f"--{boundary}--\r\n".encode("ascii")


def good_form(**overrides: bytes) -> bytes:
    room = overrides.get("room", b"Room")
    data = overrides.get("data", b"synthetic file bytes")
    return form(("room", None, room), ("file", "synthetic.png", data))


class StubUploader:
    """Mimics the live upload callable without touching a desktop."""

    def __init__(self, fail_with=None) -> None:
        self.calls: list[dict] = []
        self.fail_with = fail_with

    def upload(self, room: str, name: str, data: bytes, open_if_needed: bool = False):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"room": room, "name": name, "data": data, "open": open_if_needed})
        return {"submitted": True, "room": room, "name": name, "size": len(data), "roomId": "1"}


def post(base: str, body: bytes, content_type: str = CONTENT_TYPE, length=None):
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body) if length is None else length),
        "Authorization": f"Bearer {TOKEN}",
    }
    return _raw_request(base, "POST", "/upload", body, headers)


class UploadRouteTests(ServedCase):
    def start(self, uploader=None, **options) -> None:
        self.uploader = uploader or StubUploader()
        self.serve(_start_server(StubSender(), upload=self.uploader.upload, **options))

    def refused(self, body: bytes, content_type: str = CONTENT_TYPE) -> dict:
        status, payload = post(self.base, body, content_type)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.uploader.calls, [])
        return payload

    def test_health_declares_upload_with_its_limit(self) -> None:
        self.start()
        status, body, _headers = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["features"], {"upload": {"maxBytes": MAX_UPLOAD_BYTES}})

    def test_a_build_without_files_declares_and_routes_none(self) -> None:
        self.serve(_start_server(StubSender()))
        _status, body, _headers = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(body, {"ok": True})
        status, payload = post(self.base, good_form())
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["message"], "not found")

    def test_a_file_reaches_the_uploader_with_its_room_name_and_bytes(self) -> None:
        self.start()
        status, payload = post(self.base, good_form())
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            payload,
            {"ok": True, "submitted": True, "room": "Room", "name": "synthetic.png", "size": 20, "roomId": "1"},
        )
        self.assertEqual(
            self.uploader.calls,
            [{"room": "Room", "name": "synthetic.png", "data": b"synthetic file bytes", "open": False}],
        )

    def test_open_room_is_true_or_false_and_nothing_else(self) -> None:
        self.start()
        body = form(("room", None, b"Room"), ("file", "a.png", b"x"), ("openRoom", None, b"true"))
        status, _payload = post(self.base, body)
        self.assertEqual(status, 200)
        self.assertTrue(self.uploader.calls[-1]["open"])
        self.uploader.calls.clear()
        self.refused(form(("room", None, b"Room"), ("file", "a.png", b"x"), ("openRoom", None, b"yes")))

    def test_every_wrong_form_is_refused_and_sends_nothing(self) -> None:
        self.start()
        cases = {
            "no room": form(("file", "a.png", b"x")),
            "blank room": form(("room", None, b"  "), ("file", "a.png", b"x")),
            "room as a file": form(("room", "r.txt", b"Room"), ("file", "a.png", b"x")),
            "no file": form(("room", None, b"Room")),
            "file without a name": form(("room", None, b"Room"), ("file", None, b"x")),
            "empty file": form(("room", None, b"Room"), ("file", "a.png", b"")),
            "unknown field": form(("room", None, b"Room"), ("file", "a.png", b"x"), ("text", None, b"hi")),
            "room twice": form(("room", None, b"Room"), ("room", None, b"Other"), ("file", "a.png", b"x")),
            "no closing boundary": good_form()[: -len(f"--{BOUNDARY}--\r\n")],
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.refused(body)

    def test_a_body_that_is_not_a_form_is_refused(self) -> None:
        self.start()
        self.refused(b'{"room": "Room"}', "application/json")
        self.refused(good_form(), "multipart/form-data")

    def test_a_file_over_the_limit_is_refused_before_it_is_read(self) -> None:
        self.start()
        status, payload = post(self.base, b"", length=MAX_UPLOAD_BYTES * 2)
        self.assertEqual(status, 400)
        self.assertIn("larger than", payload["error"]["message"])
        self.assertEqual(self.uploader.calls, [])

    def test_a_file_over_a_smaller_limit_is_refused_inside_the_form(self) -> None:
        self.start(max_upload_bytes=10)
        payload = self.refused(good_form(data=b"eleven bytes"))
        self.assertIn("larger than 10 bytes", payload["error"]["message"])
        status, _health, _headers = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(_health["features"]["upload"]["maxBytes"], 10)

    def test_the_name_the_room_sees_has_no_folders(self) -> None:
        self.start()
        post(self.base, form(("room", None, b"Room"), ("file", "..\\..\\synthetic.png", b"x")))
        self.assertEqual(self.uploader.calls[-1]["name"], "synthetic.png")

    def test_a_refusal_from_the_client_keeps_its_status(self) -> None:
        cases = [
            (KanaoError(ErrorCode.WINDOW_NOT_FOUND, "no open window"), 404, "WINDOW_NOT_FOUND"),
            (KanaoError(ErrorCode.SEND_FAILED, "unknown outcome"), 500, "SEND_FAILED"),
            (OSError("synthetic desktop failure"), 500, "SEND_FAILED"),
        ]
        for error, expected, code in cases:
            with self.subTest(code=code, expected=expected):
                self.start(StubUploader(fail_with=error))
                status, payload = post(self.base, good_form())
                self.assertEqual(status, expected)
                self.assertEqual(payload["error"]["code"], code)
                self.tearDown()

    def test_files_wait_in_the_same_line_as_texts(self) -> None:
        self.start(
            min_interval=1.0,
            clock=itertools.count(0.0, 10.0).__next__,
            max_queued_sends=0,
        )
        status, payload = post(self.base, good_form())
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], "RATE_LIMITED")
        self.assertEqual(self.uploader.calls, [])


class MultipartTests(unittest.TestCase):
    def test_a_quoted_boundary_and_an_encoded_file_name_are_read(self) -> None:
        self.assertEqual(boundary_of('multipart/form-data; boundary="a b"'), "a b")
        body = (
            f"--{BOUNDARY}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename*=UTF-8''%ED%95%9C.png\r\n\r\n"
        ).encode() + b"x\r\n" + f"--{BOUNDARY}--".encode()
        [part] = parse(body, BOUNDARY)
        self.assertEqual((part.name, part.filename, part.data), ("file", "\ud55c.png", b"x"))

    def test_a_file_s_bytes_are_kept_exactly(self) -> None:
        data = b"\r\n--not-the-boundary\r\n\x00\xff"
        [_room, part] = parse(good_form(data=data), BOUNDARY)
        self.assertEqual(part.data, data)

    def test_too_many_parts_are_refused(self) -> None:
        body = form(*[(f"f{index}", None, b"x") for index in range(MAX_PARTS + 1)])
        with self.assertRaises(KanaoError):
            parse(body, BOUNDARY)


class UploadNameTests(unittest.TestCase):
    def test_a_name_is_one_windows_file_name(self) -> None:
        cases = {
            "photo.jpg": "photo.jpg",
            "C:\\Users\\x\\photo.jpg": "photo.jpg",
            "../../photo.jpg": "photo.jpg",
            'a<b>c:d"e|f?g*.png': "a_b_c_d_e_f_g_.png",
            "con.txt": "_con.txt",
            "LPT1": "_LPT1",
            " trailing. ": "trailing",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(upload_name(given), expected)

    def test_a_long_name_keeps_its_extension(self) -> None:
        name = upload_name("x" * 300 + ".mp4")
        self.assertEqual(len(name), 120)
        self.assertTrue(name.endswith(".mp4"))

    def test_nothing_left_is_refused(self) -> None:
        for given in ("", "...", "dir/", None):
            with self.subTest(given=given):
                with self.assertRaises(KanaoError):
                    upload_name(given)


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "uploads"

    def test_a_file_is_written_under_its_own_name_in_its_own_folder(self) -> None:
        first = stage_upload(self.folder, "same.png", b"one", now=time.time())
        second = stage_upload(self.folder, "same.png", b"two", now=time.time())
        self.assertEqual((first.name, first.read_bytes()), ("same.png", b"one"))
        self.assertEqual(second.read_bytes(), b"two")
        self.assertNotEqual(first.parent, second.parent)

    def test_only_copies_older_than_the_keep_are_swept(self) -> None:
        old = stage_upload(self.folder, "old.png", b"old", now=time.time())
        fresh = stage_upload(self.folder, "fresh.png", b"fresh", now=time.time())
        past = time.time() - UPLOAD_KEEP_SECONDS - 60
        os.utime(old.parent, (past, past))
        sweep_uploads(self.folder, now=time.time())
        self.assertFalse(old.parent.exists())
        self.assertTrue(fresh.exists())


class UploadAdmissionTests(ServedCase):
    def test_an_upload_beyond_the_slots_is_refused_before_its_body_is_read(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def slow_upload(room, name, data, open_if_needed=False):
            calls.append(name)
            entered.set()
            self.assertTrue(release.wait(10))
            return {"submitted": True, "room": room, "name": name, "size": len(data), "roomId": "1"}

        self.serve(
            _start_server(StubSender(), upload=slow_upload, max_concurrent_uploads=1),
            server_class=ServeHTTPServer,
        )
        first: dict = {}
        worker = threading.Thread(target=lambda: first.update(answer=post(self.base, good_form())))
        worker.start()
        self.assertTrue(entered.wait(10), "the first upload never reached the client")
        try:
            status, payload = post(self.base, good_form())
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "BUSY")
            self.assertEqual(calls, ["synthetic.png"])
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(first["answer"][0], 200)
        self.assertEqual(post(self.base, good_form())[0], 200, "the slot was not given back")


class BodyDeadlineTests(ServedCase):
    def test_a_body_dripped_inside_each_timeout_is_still_cut_off(self) -> None:
        """Time is the behaviour under test: each byte comes well inside the
        per-receive timeout, so only a deadline on the whole body ends it."""
        self.serve(_start_server(StubSender(), request_timeout=0.3))
        host, _, port = self.base.partition("://")[2].partition(":")
        with socket.create_connection((host, int(port)), timeout=5) as raw:
            raw.sendall(
                b"POST /send HTTP/1.1\r\nHost: localhost\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode()
                + b"Content-Type: application/json\r\nContent-Length: 400\r\n\r\n"
            )
            answered = False
            for _ in range(200):
                try:
                    raw.sendall(b" ")
                except OSError:
                    answered = True
                    break
                readable, _w, _x = select.select([raw], [], [], 0.05)
                if readable:
                    answered = True
                    break
            self.assertTrue(answered, "a dripping body held the request past its deadline")
            head = raw.recv(65536)
        self.assertIn(b" 408 ", head.split(b"\r\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
