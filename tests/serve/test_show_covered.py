"""Request-scoped SafeBot disclosure through the real HTTP backends."""

import json
import unittest

from kamado.serve import backends
from tests.fixtures import DIRECT_ROOM, PEER_ID, build_reader
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server

SECRET = "synthetic covered disclosure"


class ShowCoveredTests(ServedCase):
    def setUp(self):
        self.readers = []

        def factory():
            reader = build_reader(messages=[
                (9100, DIRECT_ROOM, PEER_ID, 1, SECRET,
                 json.dumps({"safeBot": True}), 1_700_001_000, 0),
                (9101, DIRECT_ROOM, PEER_ID, 16385, "synthetic erased",
                 None, 1_700_001_010, 0),
            ])
            reader._owns_cache = True
            self.readers.append(reader)
            return reader

        self.serve(_start_server(
            StubSender(),
            context_provider=backends.make_live_context_provider(factory),
            digest_provider=backends.make_live_digest_provider(factory),
        ))

    def get(self, route, flag=""):
        return _request(
            self.base, "GET", f"{route}?roomId={DIRECT_ROOM}" + flag
        )

    def test_context_reveals_only_the_request_that_asked(self):
        for flag, visible in (("", False), ("&showCovered=true", True), ("", False)):
            with self.subTest(flag=flag):
                status, body, _ = self.get("/context", flag)
                self.assertEqual(status, 200)
                self.assertEqual(body["messages"][0]["text"], SECRET if visible else "")
                self.assertEqual(len(body["messages"]), 1)

    def test_digest_reveals_only_the_request_that_asked(self):
        for flag, visible in (("", False), ("&showCovered=true", True), ("", False)):
            with self.subTest(flag=flag):
                status, body, _ = self.get("/digest", flag)
                self.assertEqual(status, 200)
                self.assertEqual(SECRET in json.dumps(body), visible)
                self.assertNotIn("synthetic erased", json.dumps(body))

    def test_explicit_false_keeps_covered_messages_hidden(self):
        for route in ("/context", "/digest"):
            with self.subTest(route=route):
                status, body, _ = self.get(route, "&showCovered=false")
                self.assertEqual(status, 200)
                self.assertNotIn(SECRET, json.dumps(body))

    def test_a_malformed_flag_is_refused_before_opening_a_reader(self):
        for route in ("/context", "/digest"):
            for value in ("yes", "1", "0", "", "TRUE"):
                with self.subTest(route=route, value=value):
                    status, body, _ = self.get(route, "&showCovered=" + value)
                    self.assertEqual(status, 400)
                    self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.readers, [])

    def test_other_blank_parameters_keep_their_existing_defaults(self):
        for route, blank in (
            ("/context", "&limit="), ("/digest", "&limit="),
            ("/digest", "&span="), ("/digest", "&highlights="),
        ):
            with self.subTest(route=route, blank=blank):
                status, expected, _ = self.get(route)
                changed_status, actual, _ = self.get(route, blank)
                self.assertEqual((changed_status, actual), (status, expected))

    def test_the_digest_default_matches_the_machine_readable_contract(self):
        from tests.serve.test_openapi import load

        schema = next(
            parameter["schema"]
            for parameter in load()["paths"]["/digest"]["get"]["parameters"]
            if parameter.get("name") == "span"
        )
        status, body, _ = self.get("/digest")
        self.assertEqual(status, 200)
        self.assertEqual(body["span"], schema["default"])


if __name__ == "__main__":
    unittest.main()
