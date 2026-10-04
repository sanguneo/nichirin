"""`openapi.json` against the server it describes.

A specification nobody checks is a rumour. These do not read the document for
style — they ask the running handler and the constants whether what it claims
is still true, so the file fails here rather than in somebody's client.

The same file ships in the macOS sibling, byte for byte, which is the point of
it: one contract, two builds. What genuinely differs is marked `x-platform` in
the document, and the last test pins exactly which members are allowed to
carry that mark, so a third divergence cannot be added quietly.
"""

import json
import unittest
from pathlib import Path

from kanao.errors import ErrorCode
from kanao.model import ROOM_KINDS
from kanao.serve import config
from kanao.tui.digest import parse_duration

from tests.serve.helpers import TOKEN, StubSender, ServedCase, _raw_request, _request, _start_server

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def load() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load()

    def test_it_is_a_document_a_tool_can_open(self) -> None:
        """Swagger UI and Postman both start by parsing it and reading these."""
        self.assertEqual(self.spec["openapi"], "3.1.0")
        self.assertIn("title", self.spec["info"])
        self.assertIn("version", self.spec["info"])

    def test_the_numbers_are_the_server_s_numbers(self) -> None:
        """Every bound stated in the document, read back from the code."""
        paths = self.spec["paths"]

        def parameter(route: str, name: str) -> dict:
            for item in paths[route]["get"]["parameters"]:
                if item.get("name") == name:
                    return item["schema"]
            raise AssertionError(f"{route} does not document {name}")

        context_limit = parameter("/context", "limit")
        self.assertEqual(context_limit["maximum"], config.MAX_LIMIT)
        self.assertEqual(context_limit["minimum"], 1)
        self.assertEqual(context_limit["default"], 40)

        digest_limit = parameter("/digest", "limit")
        self.assertEqual(digest_limit["maximum"], config.MAX_LIMIT)
        self.assertEqual(digest_limit["default"], 400)

        highlights = parameter("/digest", "highlights")
        self.assertEqual(highlights["maximum"], config.MAX_HIGHLIGHTS)
        self.assertEqual(highlights["default"], 3)

        span = parameter("/digest", "span")
        self.assertEqual(span["default"], "30m")
        # The document once offered `1d`, which the slicer refuses with a 400.
        for example in [span["default"], *span["examples"]]:
            with self.subTest(span=example):
                parse_duration(example)

        port = self.spec["servers"][0]["variables"]["port"]["default"]
        self.assertEqual(port, str(config.DEFAULT_PORT))

        health = paths["/health"]["get"]["responses"]["200"]["content"]["application/json"]
        self.assertEqual(
            health["example"]["features"]["upload"]["maxBytes"], config.MAX_UPLOAD_BYTES
        )
        form = self.spec["components"]["schemas"]["UploadRequest"]
        self.assertEqual(set(form["properties"]), {"room", "file", "openRoom"})

    def test_every_error_code_this_build_can_raise_is_documented(self) -> None:
        documented = set(self.spec["components"]["schemas"]["ErrorCode"]["enum"])
        self.assertEqual(set(code.value for code in ErrorCode) - documented, set())

    def test_nothing_is_documented_that_no_build_can_raise(self) -> None:
        """Extra codes are allowed only where the document says which build.

        Stated as a subset rather than an equality so this file is the same
        on both sides: the sibling raises every code documented here, and
        this build raises all but the ones already marked as that platform's.
        """
        schema = self.spec["components"]["schemas"]["ErrorCode"]
        documented = set(schema["enum"])
        ours = set(code.value for code in ErrorCode)
        self.assertLessEqual(documented - ours, set(schema["x-platform"]))

    def test_the_platform_differences_are_the_ones_that_are_known(self) -> None:
        """Another divergence has to be declared here, not discovered later.

        Two are macOS-only (a room key and an error code), and the file send
        added in 0.22.0 is Windows-only with the health key that declares it
        until the macOS build answers it. The reads added
        in 0.15.0 are answered by both builds since contract 0.16.0, and the
        profile pictures added in 0.17.0 since 0.18.1, and a cut-off
        notice's whole text since 0.20.0, because both clients cut the
        notice row at the same 100 characters.
        """
        marked = set()
        def walk(node, trail=()):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "x-platform":
                        marked.add("/".join(trail))
                    else:
                        walk(value, trail + (str(key),))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, trail + (str(index),))

        walk(self.spec)
        self.assertEqual(
            marked,
            {
                "components/schemas/ErrorCode",
                "components/schemas/Room/properties/typeCode",
                "paths//upload/post",
                "paths//health/get/responses/200/content/application/json/schema/properties/features",
            },
        )

    def test_the_room_kinds_are_the_ones_this_build_sends(self) -> None:
        """Every name the map can yield is listed, and nothing else is.

        The list once named `open`, `plus` and `unknown`, which no build sent,
        and missed three the builds do send - a validating client refused
        every open chat and channel.
        """
        listed = self.spec["components"]["schemas"]["Room"]["properties"]["kind"]["enum"]
        self.assertEqual(set(listed), set(ROOM_KINDS.values()))

    def test_every_documented_code_says_what_it_means(self) -> None:
        schema = self.spec["components"]["schemas"]["ErrorCode"]
        self.assertEqual(set(schema["enum"]), set(schema["x-code-meanings"]))


class RouteTests(ServedCase):
    """The routes the document promises, asked of the handler itself."""

    def setUp(self) -> None:
        self.spec = load()
        self.serve(_start_server(StubSender(), rooms=[{"id": "1", "title": "Room"}]))

    def missing(self, body) -> bool:
        return (
            isinstance(body, dict)
            and body.get("error", {}).get("code") == "INVALID_REQUEST"
            and body["error"]["message"] == "not found"
        )

    def test_every_documented_route_is_routed(self) -> None:
        for route, operations in self.spec["paths"].items():
            if "get" not in operations:
                continue
            with self.subTest(route=route):
                # /stream would hold the connection; asking it for a bad
                # cursor proves the route exists and answers immediately.
                path = route + ("?since=nonsense" if route == "/stream" else "")
                _status, body, _headers = _request(self.base, "GET", path)
                self.assertFalse(self.missing(body), f"{route} is not routed")

    def test_send_is_routed_and_the_undocumented_neighbour_is_not(self) -> None:
        status, body, _headers = _request(
            self.base, "POST", "/send", {"room": "Room", "text": "hi"}
        )
        self.assertEqual(status, 200, body)
        status, body, _headers = _request(
            self.base, "POST", "/sendmail", {"room": "Room", "text": "hi"}
        )
        self.assertEqual(status, 404)
        self.assertTrue(self.missing(body))

    def test_upload_is_routed_where_health_declares_it(self) -> None:
        calls = []

        def upload(room, name, data, open_if_needed=False):
            calls.append(name)
            return {"submitted": True, "room": room, "name": name, "size": len(data), "roomId": "1"}

        self.tearDown()
        self.serve(_start_server(StubSender(), upload=upload))
        _status, health, _headers = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(health["features"]["upload"]["maxBytes"], config.MAX_UPLOAD_BYTES)
        body = (
            b"--b\r\nContent-Disposition: form-data; name=\"room\"\r\n\r\nRoom\r\n"
            b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n\r\nx\r\n"
            b"--b--\r\n"
        )
        status, answer = _raw_request(
            self.base,
            "POST",
            "/upload",
            body,
            {
                "Content-Type": "multipart/form-data; boundary=b",
                "Content-Length": str(len(body)),
                "Authorization": f"Bearer {TOKEN}",
            },
        )
        self.assertEqual(status, 200, answer)
        self.assertEqual(set(answer), set(self.spec["components"]["schemas"]["UploadResult"]["required"]))
        self.assertEqual(calls, ["a.png"])

    def test_health_needs_no_token_exactly_as_documented(self) -> None:
        self.assertEqual(self.spec["paths"]["/health"]["get"]["security"], [])
        status, body, _headers = _request(self.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_the_methods_the_document_refuses_are_refused(self) -> None:
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.assertIn(method.lower(), self.spec["paths"]["/send"])
                status, _body, _headers = _request(self.base, method, "/send")
                self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
