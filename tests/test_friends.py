"""Friend-list tests: the list is a local read, and phone numbers are guarded."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from kakaoread import cli
from kakaoread.model import Friend

from tests.fixtures import FRIEND_ID, PEER_ID, STRANGER_ID, build_reader


class ReaderFriendsTests(unittest.TestCase):
    def test_only_type_one_users_are_friends(self) -> None:
        with build_reader() as reader:
            ids = {friend.id for friend in reader.friends()}
        self.assertEqual(ids, {PEER_ID, FRIEND_ID})
        self.assertNotIn(STRANGER_ID, ids)

    def test_favorites_are_listed_first(self) -> None:
        with build_reader() as reader:
            friends = reader.friends()
        self.assertTrue(friends[0].favorite)
        self.assertEqual(friends[0].id, PEER_ID)

    def test_favorites_only_filters_the_list(self) -> None:
        with build_reader() as reader:
            favourites = reader.friends(favorites_only=True)
        self.assertEqual([friend.id for friend in favourites], [PEER_ID])

    def test_the_name_the_user_assigned_wins(self) -> None:
        with build_reader() as reader:
            peer = next(friend for friend in reader.friends() if friend.id == PEER_ID)
        self.assertEqual(peer.name, "Peer Renamed")

    def test_a_friend_without_a_chat_reports_no_direct_room(self) -> None:
        with build_reader() as reader:
            friend = next(f for f in reader.friends() if f.id == FRIEND_ID)
        self.assertIsNone(friend.direct_chat_id)

    def test_a_friend_with_a_chat_carries_its_room_id(self) -> None:
        with build_reader() as reader:
            peer = next(f for f in reader.friends() if f.id == PEER_ID)
        self.assertIsNotNone(peer.direct_chat_id)


class FriendJsonTests(unittest.TestCase):
    def friend(self) -> Friend:
        return Friend(
            id=7, name="가나다", nickname="nick", phone="01000000000",
            status_message="hi", direct_chat_id=99, favorite=True,
        )

    def test_the_phone_number_is_withheld_by_default(self) -> None:
        self.assertNotIn("phone", self.friend().as_json())

    def test_the_phone_number_is_included_only_on_request(self) -> None:
        self.assertEqual(self.friend().as_json(include_phone=True)["phone"], "01000000000")

    def test_identifiers_are_strings(self) -> None:
        payload = self.friend().as_json()
        self.assertEqual(payload["id"], "7")
        self.assertEqual(payload["directChatId"], "99")

    def test_a_friend_without_a_chat_serialises_a_null_room(self) -> None:
        friend = Friend(id=7, name="a", nickname=None, phone=None, status_message=None, direct_chat_id=None, favorite=False)
        self.assertIsNone(friend.as_json()["directChatId"])


class FriendsCommandTests(unittest.TestCase):
    def invoke(self, *argv: str) -> dict:
        reader = build_reader()
        self.addCleanup(reader.close)
        arguments = cli.build_parser().parse_args(argv)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.run(arguments, reader)
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_friends_lists_with_the_standard_envelope(self) -> None:
        answer = self.invoke("friends")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["count"], 2)
        self.assertNotIn("phone", answer["friends"][0])

    def test_with_phone_flag_reveals_numbers(self) -> None:
        answer = self.invoke("friends", "--with-phone")
        self.assertIn("phone", answer["friends"][0])

    def test_favorites_flag_narrows_the_list(self) -> None:
        answer = self.invoke("friends", "--favorites")
        self.assertEqual(answer["count"], 1)
        self.assertTrue(answer["friends"][0]["favorite"])


class FriendsTuiTests(unittest.TestCase):
    def render(self):
        from kakaoread.tui.app import App
        from kakaoread.tui.layout import display_width, strip_ansi
        from kakaoread.tui.theme import Theme

        reader = build_reader()
        self.addCleanup(reader.close)
        app = App(reader, theme=Theme(truecolor=True, enabled=True))
        app.load_rooms()
        app.handle("char", "f")
        frame = app.render(80, 16)
        return app, [strip_ansi(line) for line in frame], display_width

    def test_the_friends_key_switches_into_the_friends_view(self) -> None:
        app, plain, _ = self.render()
        self.assertTrue(app.show_friends)
        self.assertIn("Friends", plain[0])

    def test_the_friends_view_never_overflows_the_width(self) -> None:
        _app, plain, width = self.render()
        for line in plain:
            self.assertLessEqual(width(line), 80)

    def test_the_key_toggles_back_to_the_room_list(self) -> None:
        app, _plain, _ = self.render()
        app.handle("char", "f")
        self.assertFalse(app.show_friends)

    def test_entering_a_friend_with_a_room_opens_the_conversation(self) -> None:
        app, _plain, _ = self.render()
        # PEER_ID is the favourite, sorted first, and has a direct chat.
        app.friend_selected = 0
        app.handle("enter", "")
        self.assertIsNotNone(app.chat)
        self.assertFalse(app.show_friends)


if __name__ == "__main__":
    unittest.main()
