"""Profile pictures on rooms and messages, read beside the names they belong to.

Synthetic addresses only: the fixture writes made-up paths on the CDN hosts a
live client was measured to use, and two addresses that must not be passed on.
"""

import unittest

from tests.fixtures import (
    DIRECT_ROOM,
    FRIEND_ID,
    FRIEND_PICTURE,
    GROUP_ROOM,
    HUGE_ROOM,
    LINK_PICTURE,
    NAMED_ROOM,
    OPEN_ROOM,
    OWN_ID,
    OWN_PICTURE,
    PEER_ID,
    PEER_PICTURE,
    ROOM_PICTURE,
    SELF_ROOM,
    build_reader,
)


class RoomPictureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader(pictures=True)
        self.rooms = {room.id: room for room in self.reader.rooms()}

    def tearDown(self) -> None:
        self.reader.close()

    def test_a_one_to_one_room_is_its_counterpart(self) -> None:
        self.assertEqual(self.rooms[DIRECT_ROOM].images, (PEER_PICTURE,))

    def test_a_group_is_its_other_members_in_the_room_s_order(self) -> None:
        # The group also carries a type-8 chat background, which is not its icon.
        self.assertEqual(self.rooms[GROUP_ROOM].images, (PEER_PICTURE, FRIEND_PICTURE))

    def test_the_chat_with_oneself_is_the_account_s_own_profile(self) -> None:
        # Not the open-chat profile the account also keeps a row for.
        self.assertEqual(self.rooms[SELF_ROOM].images, (OWN_PICTURE,))

    def test_an_address_off_the_cdn_or_over_plain_http_is_no_picture(self) -> None:
        # The huge room's counterpart carries a plain-HTTP address.
        self.assertEqual(self.rooms[HUGE_ROOM].images, ())
        self.assertNotIn("images", self.rooms[HUGE_ROOM].as_json())

    def test_a_room_with_its_own_photo_is_that_photo_alone(self) -> None:
        # Its only other member's address is off the CDN; the photo still wins.
        self.assertEqual(self.rooms[NAMED_ROOM].images, (ROOM_PICTURE,))

    def test_an_open_group_is_its_link_picture_not_its_members(self) -> None:
        self.assertEqual(self.rooms[OPEN_ROOM].images, (LINK_PICTURE,))

    def test_the_answer_carries_the_list_only_when_there_is_one(self) -> None:
        self.assertEqual(
            self.rooms[GROUP_ROOM].as_json()["images"], [PEER_PICTURE, FRIEND_PICTURE]
        )


class MessagePictureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader(pictures=True)

    def tearDown(self) -> None:
        self.reader.close()

    def test_each_sender_carries_their_picture(self) -> None:
        by_sender = {
            message.sender_id: message.sender_image
            for message in self.reader.messages(GROUP_ROOM)
        }
        self.assertEqual(by_sender[PEER_ID], PEER_PICTURE)
        self.assertEqual(by_sender[FRIEND_ID], FRIEND_PICTURE)
        self.assertEqual(by_sender[OWN_ID], OWN_PICTURE)

    def test_the_answer_names_it_senderImage(self) -> None:
        rendered = [message.as_json() for message in self.reader.messages(GROUP_ROOM)]
        self.assertTrue(all(item["senderImage"].startswith("https://") for item in rendered))


class NoPictureColumnTests(unittest.TestCase):
    """A client whose tables carry no picture answers exactly as before."""

    def test_rooms_and_messages_leave_the_keys_out(self) -> None:
        with build_reader() as reader:
            for room in reader.rooms():
                self.assertEqual(room.images, ())
                self.assertNotIn("images", room.as_json())
            for message in reader.messages(GROUP_ROOM):
                self.assertIsNone(message.sender_image)
                self.assertNotIn("senderImage", message.as_json())


if __name__ == "__main__":
    unittest.main()
