"""``open`` is in the shared contract; here it opens nothing, and a typo is still a 400."""

import unittest

from tests.serve.helpers import ServedCase, StubSender, _request, _start_server


class OpenFlagTests(ServedCase):
    def serve_counting(self):
        calls = []

        def context_provider(room_id: int, limit: int, *, show_covered=False):
            calls.append(room_id)
            return {"room": {"id": str(room_id)}, "messages": []}

        self.serve(_start_server(StubSender(), context_provider=context_provider))
        return calls

    def test_the_flag_is_accepted_and_the_room_read(self) -> None:
        calls = self.serve_counting()
        status, _, _ = _request(self.base, "GET", "/context?roomId=7&open=true")
        self.assertEqual(status, 200)
        self.assertEqual(calls, [7])

    def test_a_flag_that_is_not_true_or_false_is_refused_before_reading(self) -> None:
        calls = self.serve_counting()
        status, body, _ = _request(self.base, "GET", "/context?roomId=7&open=yes")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
