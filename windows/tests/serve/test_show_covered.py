"""``showCovered`` on ``/context`` and ``/digest``: the HTTP twin of ``--show-covered``.

The command could reveal what an open chat's SafeBot covered and the server
could not, so a ``serve`` client received ``covered: "safebot"`` with no text
and no way to ask for it (#17). These drive the live backends over the
synthetic extras database, so the switch is followed all the way into the
reader rather than stopping at a stub.
"""

import json
import unittest

from kanao.serve.backends import make_live_context_provider, make_live_digest_provider

from tests.extras_fixtures import build_extras_reader
from tests.fixtures import GROUP_ROOM
from tests.serve.helpers import ServedCase, StubSender, _request, _start_server
from tests.serve.test_openapi import load

COVERED = "가려진 말"
FLAG = load()["components"]["parameters"]["ShowCovered"]["name"]


class ShowCoveredTests(ServedCase):
    def setUp(self) -> None:
        self.serve(
            _start_server(
                StubSender(),
                context_provider=make_live_context_provider(build_extras_reader),
                digest_provider=make_live_digest_provider(build_extras_reader),
            )
        )

    def covered(self, query: str = "") -> list[dict]:
        status, body, _ = _request(
            self.base, "GET", f"/context?roomId={GROUP_ROOM}&limit=200{query}"
        )
        self.assertEqual(status, 200, body)
        return [message for message in body["messages"] if message.get("covered")]

    def test_the_text_is_withheld_unless_asked_for(self) -> None:
        hidden = self.covered()
        self.assertTrue(hidden, "the fixture holds a covered message")
        self.assertEqual({message["text"] for message in hidden}, {""})

        shown = self.covered(f"&{FLAG}=true")
        self.assertIn(COVERED, [message["text"] for message in shown])
        self.assertEqual({message["covered"] for message in shown}, {"safebot"})

    def test_one_caller_asking_reveals_nothing_to_the_next(self) -> None:
        self.covered(f"&{FLAG}=true")
        self.assertEqual({message["text"] for message in self.covered()}, {""})

    def test_the_digest_takes_the_same_switch(self) -> None:
        def digest(query: str) -> str:
            status, body, _ = _request(
                self.base,
                "GET",
                f"/digest?roomId={GROUP_ROOM}&span=12h&highlights=20{query}",
            )
            self.assertEqual(status, 200, body)
            return json.dumps(body, ensure_ascii=False)

        self.assertNotIn(COVERED, digest(""))
        self.assertIn(COVERED, digest(f"&{FLAG}=true"))

    def test_a_value_that_is_not_true_or_false_is_refused(self) -> None:
        for route in ("/context", "/digest"):
            with self.subTest(route=route):
                status, body, _ = _request(
                    self.base, "GET", f"{route}?roomId={GROUP_ROOM}&{FLAG}=yes"
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
