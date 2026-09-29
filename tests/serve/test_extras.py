"""`/notice`, `/attachments`, `/revisions` and `/file` over HTTP."""

import io
import unittest
import urllib.parse
import urllib.request

from kamado.errors import ErrorCode, KamadoError
from kamado.files import Download

from tests.serve.helpers import TOKEN, ServedCase, StubSender, _request, _start_server

NAME = "보고서 \"최종\".pdf"
BODY = b"x" * 200_000


class ExtraRouteTests(ServedCase):
    def setUp(self) -> None:
        self.calls: list[tuple] = []
        self.closed: list[bool] = []

        def notice(room_id):
            self.calls.append(("notice", room_id))
            return {"room": {"id": str(room_id)}, "notice": None}

        def attachments(room_id, kind, limit, before_id):
            self.calls.append(("attachments", room_id, kind, limit, before_id))
            return {"room": {"id": str(room_id)}, "attachments": [], "count": 0}

        def revisions(room_id, message_id):
            if message_id == 404:
                raise KamadoError(ErrorCode.MESSAGE_NOT_FOUND, "no such message")
            return {
                "room": {"id": str(room_id)},
                "messageId": str(message_id),
                "revisions": [],
                "count": 0,
            }

        def file(room_id, message_id):
            if message_id == 410:
                raise KamadoError(ErrorCode.FILE_EXPIRED, "expired")
            stream = io.BytesIO(BODY)
            original = stream.close

            def close() -> None:
                self.closed.append(True)
                original()

            stream.close = close  # type: ignore[method-assign]
            return Download(name=NAME, size=len(BODY), stream=stream, source="local")

        self.serve(
            _start_server(
                StubSender(),
                notice_provider=notice,
                attachments_provider=attachments,
                revisions_provider=revisions,
                file_provider=file,
            )
        )

    def test_the_read_routes_hand_their_parameters_over(self) -> None:
        status, body, _headers = _request(self.base, "GET", "/notice?roomId=7")
        self.assertEqual((status, body["ok"]), (200, True))
        status, _body, _headers = _request(
            self.base, "GET", "/attachments?roomId=7&kind=file&limit=5&beforeId=99&open=true"
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [("notice", 7), ("attachments", 7, "file", 5, 99)])

    def test_bad_parameters_are_refused_before_any_read(self) -> None:
        for path in (
            "/attachments?roomId=7&kind=sticker",
            "/attachments?roomId=7&limit=0",
            "/attachments?roomId=7&limit=1001",
            "/attachments?roomId=7&open=maybe",
            "/revisions?roomId=7",
            "/file?roomId=7&messageId=-1",
            "/notice?roomId=99999999999999999999",
        ):
            with self.subTest(path=path):
                status, body, _headers = _request(self.base, "GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.calls, [])

    def test_a_missing_message_is_404_and_an_expired_file_410(self) -> None:
        status, body, _headers = _request(self.base, "GET", "/revisions?roomId=7&messageId=404")
        self.assertEqual((status, body["error"]["code"]), (404, "MESSAGE_NOT_FOUND"))
        status, body, _headers = _request(self.base, "GET", "/file?roomId=7&messageId=410")
        self.assertEqual((status, body["error"]["code"]), (410, "FILE_EXPIRED"))

    def test_a_file_arrives_whole_with_its_name_and_the_stream_is_closed(self) -> None:
        request = urllib.request.Request(
            self.base + "/file?roomId=7&messageId=1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
            headers = response.headers
        self.assertEqual(data, BODY)
        self.assertEqual(headers["Content-Length"], str(len(BODY)))
        self.assertEqual(headers["X-Kanao-Source"], "local")
        disposition = headers["Content-Disposition"]
        self.assertIn("filename*=UTF-8''" + urllib.parse.quote(NAME, safe=""), disposition)
        fallback = disposition.split('filename="')[1].split('"')[0]
        self.assertTrue(all(32 <= ord(c) < 127 and c not in '"\\' for c in fallback))
        self.assertEqual(self.closed, [True])

    def test_a_file_needs_the_token(self) -> None:
        status, _body, _headers = _request(
            self.base, "GET", "/file?roomId=7&messageId=1", token=None
        )
        self.assertEqual(status, 401)


class UnconfiguredTests(ServedCase):
    def test_a_route_with_no_source_says_so(self) -> None:
        self.serve(_start_server(StubSender()))
        for path in ("/notice?roomId=7", "/file?roomId=7&messageId=1"):
            with self.subTest(path=path):
                status, body, _headers = _request(self.base, "GET", path)
                self.assertEqual((status, body["error"]["code"]), (500, "READ_FAILED"))


class LiveProviderTests(unittest.TestCase):
    """The live wiring, against the synthetic database rather than a client."""

    def test_the_providers_answer_from_a_reader(self) -> None:
        from kamado.serve import backends

        from tests.extras_fixtures import EDITED, FILE, NOTICE_TEXT, build_extras_reader
        from tests.fixtures import GROUP_ROOM

        readers = []

        def factory():
            reader = build_extras_reader()
            readers.append(reader)
            return reader

        notice = backends.make_live_notice_provider(factory)(GROUP_ROOM)
        self.assertEqual(notice["notice"]["text"], NOTICE_TEXT)
        listed = backends.make_live_attachments_provider(factory)(GROUP_ROOM, "file", 50, None)
        self.assertEqual([one["messageId"] for one in listed["attachments"]], [str(FILE)])
        versions = backends.make_live_revisions_provider(factory)(GROUP_ROOM, EDITED)
        self.assertEqual(versions["count"], 0)
        with self.assertRaises(KamadoError) as caught:
            backends.make_live_notice_provider(factory)(424242)
        self.assertIs(caught.exception.code, ErrorCode.CHAT_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
