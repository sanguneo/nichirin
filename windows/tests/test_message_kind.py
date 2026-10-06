"""The kinds a message is named by, and the codes left as numbers."""

import unittest

from kanao.model import FILE_MESSAGE, message_kind


class MessageKindTests(unittest.TestCase):
    def test_a_file_is_named_file(self) -> None:
        self.assertEqual(message_kind(FILE_MESSAGE), "file")

    def test_an_unconfirmed_code_keeps_its_number(self) -> None:
        self.assertEqual(message_kind(9999), "other:9999")


if __name__ == "__main__":
    unittest.main()
