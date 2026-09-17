"""Reader tests: who the user is, what a room is called, and what it contains."""

from __future__ import annotations

import unittest

from kanao.errors import ErrorCode, KanaoError
from kanao.keyring import Keyring

from tests.fixtures import (
    DIRECT_ROOM,
    FRIEND_ID,
    GROUP_ROOM,
    HUGE_ID,
    HUGE_ROOM,
    OWN_ID,
    PEER_ID,
    PROFILE,
    SELF_ROOM,
    STRANGER_ID,
    UNREADABLE_ROOM,
    build_reader,
)


class IdentityTests(unittest.TestCase):
    def test_the_signed_in_account_is_the_sole_member_of_the_self_chat(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.own_user_id(), OWN_ID)

    def test_the_profile_badge_is_not_mistaken_for_the_signed_in_account(self) -> None:
        """hasMeBadge marks a different account on a real client."""
        with build_reader() as reader:
            self.assertNotEqual(reader.own_user_id(), STRANGER_ID)

    def test_falls_back_to_a_direct_room_when_no_self_chat_exists(self) -> None:
        with build_reader(with_self_room=False) as reader:
            self.assertEqual(reader.own_user_id(), OWN_ID)

    def test_resolving_the_account_is_only_done_once(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.own_user_id(), reader.own_user_id())


class NameTests(unittest.TestCase):
    def test_a_name_the_user_assigned_beats_the_contacts_own_nickname(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.names()[PEER_ID], "Peer Renamed")

    def test_the_signed_in_account_is_named_from_its_profile(self) -> None:
        """talkUser never contains the user, so without Profile this is a bare id."""
        with build_reader() as reader:
            self.assertEqual(reader.names()[OWN_ID], "My Own Name")

    def test_a_contact_with_no_name_at_all_is_left_out_rather_than_invented(self) -> None:
        with build_reader() as reader:
            self.assertNotIn(STRANGER_ID, {key: value for key, value in reader.names().items() if key == STRANGER_ID and value == ""})


class RoomTests(unittest.TestCase):
    def test_rooms_are_listed_most_recently_active_first(self) -> None:
        with build_reader() as reader:
            order = [room.id for room in reader.rooms()]
        self.assertEqual(order[0], DIRECT_ROOM)
        self.assertEqual(order.index(DIRECT_ROOM) < order.index(GROUP_ROOM), True)

    def test_a_room_without_a_loaded_key_is_listed_but_marked_unreadable(self) -> None:
        with build_reader() as reader:
            rooms = {room.id: room for room in reader.rooms()}
        self.assertTrue(rooms[DIRECT_ROOM].readable)
        self.assertFalse(rooms[UNREADABLE_ROOM].readable)

    def test_a_direct_room_is_titled_with_the_other_person(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.room(DIRECT_ROOM).title, "Peer Renamed")

    def test_a_group_room_without_a_title_lists_its_other_members(self) -> None:
        with build_reader() as reader:
            title = reader.room(GROUP_ROOM).title
        self.assertIn("Peer Renamed", title)
        self.assertIn("Friend Nick", title)
        self.assertNotIn("My Own Name", title)

    def test_a_custom_group_title_is_used_as_stored(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.room(UNREADABLE_ROOM).title, "Named Group")

    def test_the_self_chat_is_named_as_such(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.room(SELF_ROOM).title, "나와의 채팅")

    def test_room_kinds_are_stable_names_an_agent_can_branch_on(self) -> None:
        with build_reader() as reader:
            kinds = {room.id: room.kind for room in reader.rooms()}
        self.assertEqual(kinds[DIRECT_ROOM], "direct")
        self.assertEqual(kinds[GROUP_ROOM], "group")
        self.assertEqual(kinds[SELF_ROOM], "self")

    def test_an_unknown_room_id_is_absent_rather_than_fabricated(self) -> None:
        with build_reader() as reader:
            self.assertIsNone(reader.room(424242))


class ResolutionTests(unittest.TestCase):
    def test_an_exact_title_wins_over_a_longer_one_containing_it(self) -> None:
        with build_reader() as reader:
            matches = reader.resolve("Named Group")
        self.assertEqual([room.id for room in matches], [UNREADABLE_ROOM])

    def test_matching_ignores_case(self) -> None:
        with build_reader() as reader:
            self.assertEqual([room.id for room in reader.resolve("nAmEd gRoUp")], [UNREADABLE_ROOM])

    def test_every_partial_match_is_returned_so_the_caller_can_refuse(self) -> None:
        with build_reader() as reader:
            matches = reader.resolve("e")
        self.assertGreater(len(matches), 1)

    def test_no_match_returns_nothing_rather_than_a_nearest_guess(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.resolve("no such conversation"), [])


class MessageTests(unittest.TestCase):
    def test_messages_are_returned_oldest_first(self) -> None:
        with build_reader() as reader:
            found = reader.messages(DIRECT_ROOM)
        self.assertEqual([message.id for message in found], [5000, 5001, 5003])

    def test_deleted_messages_are_not_returned(self) -> None:
        with build_reader() as reader:
            self.assertNotIn(5002, [message.id for message in reader.messages(DIRECT_ROOM)])

    def test_the_limit_keeps_the_newest_messages(self) -> None:
        with build_reader() as reader:
            found = reader.messages(DIRECT_ROOM, limit=2)
        self.assertEqual([message.id for message in found], [5001, 5003])

    def test_the_users_own_messages_are_marked(self) -> None:
        with build_reader() as reader:
            by_id = {message.id: message for message in reader.messages(DIRECT_ROOM)}
        self.assertTrue(by_id[5001].from_me)
        self.assertFalse(by_id[5000].from_me)

    def test_senders_are_named_including_the_user(self) -> None:
        with build_reader() as reader:
            by_id = {message.id: message for message in reader.messages(DIRECT_ROOM)}
        self.assertEqual(by_id[5001].sender, "My Own Name")
        self.assertEqual(by_id[5000].sender, "Peer Renamed")

    def test_paging_backwards_returns_only_older_messages(self) -> None:
        with build_reader() as reader:
            found = reader.messages(DIRECT_ROOM, before_id=5001)
        self.assertEqual([message.id for message in found], [5000])

    def test_following_forwards_returns_only_newer_messages(self) -> None:
        with build_reader() as reader:
            found = reader.messages(DIRECT_ROOM, after_id=5001)
        self.assertEqual([message.id for message in found], [5003])

    def test_counting_excludes_deleted_messages(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.count_messages(DIRECT_ROOM), 3)

    def test_the_newest_id_is_available_without_reading_bodies(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.newest_log_id(DIRECT_ROOM), 5003)

    def test_the_newest_id_ignores_a_deleted_row(self) -> None:
        """The follow cursor must name a message the reader can actually return.

        Every other query excludes deleted rows, so a newest id that counts
        one hands the watcher a position it will never read a message at.
        """
        from kanao import paths
        from kanao.reader import Reader
        from tests.fixtures import build_profile

        cache, ring = build_profile()
        reader = Reader(keyring=ring, cache=cache)
        self.addCleanup(reader.close)
        connection = cache.databases[paths.chat_log_name(DIRECT_ROOM)]
        connection.execute(
            "INSERT INTO chatLogs VALUES (5004, ?, 1, 'withdrawn', NULL, 1800005004, 1)",
            (PEER_ID,),
        )
        connection.commit()

        self.assertEqual(reader.newest_log_id(DIRECT_ROOM), 5003)

    def test_identifiers_past_two_to_the_fifty_third_survive_as_strings(self) -> None:
        with build_reader() as reader:
            found = reader.messages(HUGE_ROOM)
        rendered = found[0].as_json()
        self.assertEqual(rendered["id"], str(HUGE_ID))
        self.assertEqual(rendered["roomId"], str(HUGE_ROOM))
        self.assertEqual(int(rendered["id"]), HUGE_ID)

    def test_reading_a_room_without_a_key_explains_which_key_is_missing(self) -> None:
        with build_reader() as reader:
            with self.assertRaises(KanaoError) as raised:
                reader.messages(UNREADABLE_ROOM)
        self.assertEqual(raised.exception.code, ErrorCode.KEY_UNAVAILABLE)


class SearchTests(unittest.TestCase):
    def test_finds_matches_across_readable_rooms(self) -> None:
        with build_reader() as reader:
            found = list(reader.search("group hello"))
        self.assertEqual([message.room_id for message in found], [GROUP_ROOM])

    def test_skips_rooms_whose_key_is_missing_instead_of_failing(self) -> None:
        with build_reader() as reader:
            self.assertEqual(list(reader.search("nothing matches this")), [])

    def test_a_limit_of_zero_searches_nothing(self) -> None:
        with build_reader() as reader:
            self.assertEqual(list(reader.search("first", limit=0)), [])

    def test_deleted_messages_are_not_searchable(self) -> None:
        with build_reader() as reader:
            self.assertEqual(list(reader.search("removed")), [])


class StatusTests(unittest.TestCase):
    def test_status_reports_a_ready_profile(self) -> None:
        with build_reader() as reader:
            status = reader.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["detail"], "ready")

    def test_status_explains_a_closed_client_instead_of_failing(self) -> None:
        from tests.fixtures import build_profile

        cache, _keyring = build_profile()
        from kanao.reader import Reader

        closed = Keyring(user_directory=PROFILE, pid=None, keys={})
        with Reader(user_directory=PROFILE, cache=cache, keyring=closed) as reader:
            status = reader.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["readableRooms"], 0)
        self.assertIn("not running", str(status["detail"]))

    def test_a_closed_client_names_the_reason_a_read_fails(self) -> None:
        from kanao.reader import Reader
        from tests.fixtures import build_profile

        cache, _keyring = build_profile()
        closed = Keyring(user_directory=PROFILE, pid=None, keys={})
        with Reader(user_directory=PROFILE, cache=cache, keyring=closed) as reader:
            with self.assertRaises(KanaoError) as raised:
                reader.rooms()
        self.assertEqual(raised.exception.code, ErrorCode.KAKAO_NOT_RUNNING)


class MembershipFeedTests(unittest.TestCase):
    """A feed row is the client's bookkeeping, and must not read as speech.

    Its ``message`` column holds JSON. One live open chat had 364 of these in
    its newest 400 records, so passing that through as message text buries the
    conversation on screen and hands an agent a wall of ``{"feedType": …}`` to
    reason over.
    """

    def rows(self):
        from tests.test_names import feed

        return [
            feed(20, 1_700_000_000, (PEER_ID, "예전 이름")),
            (21, PEER_ID, 1, "사람이 쓴 줄", None, 1_700_000_100, 0),
        ]

    def test_a_feed_carries_no_message_text(self) -> None:
        with build_reader(messages=self.rows()) as reader:
            found = {message.id: message for message in reader.messages(DIRECT_ROOM)}
        self.assertEqual(found[20].text, "")
        self.assertEqual(found[21].text, "사람이 쓴 줄")

    def test_a_feed_is_named_rather_than_left_as_a_type_number(self) -> None:
        with build_reader(messages=self.rows()) as reader:
            rendered = reader.messages(DIRECT_ROOM)[0].as_json()
        self.assertEqual(rendered["kind"], "feed")
        self.assertEqual(rendered["text"], "")

    def test_the_names_a_feed_records_are_still_read(self) -> None:
        """The history comes from the database, not from the message text."""
        with build_reader(messages=self.rows()) as reader:
            history = reader.name_history(DIRECT_ROOM)
        self.assertIn(PEER_ID, history)
        self.assertIn("예전 이름", [record.name for record in history[PEER_ID]])

    def test_a_feed_cannot_be_found_by_searching_for_its_json(self) -> None:
        with build_reader(messages=self.rows()) as reader:
            self.assertEqual(list(reader.search("feedType")), [])


if __name__ == "__main__":
    unittest.main()
