"""Reader tests: who the user is, what a room is called, and what it contains."""

from __future__ import annotations

import json
import unittest

from kanao import reader as reader_module
from kanao.errors import ErrorCode, KanaoError
from kanao.keyring import Keyring
from kanao.reader import Reader

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
    build_profile,
    build_reader,
)


def room_of(reader: Reader, room_id: int):
    """The fixture's room, said as a failure when the fixture has lost it.

    `Reader.room` may answer None — a room really can be missing — and these
    tests are about what a room is *called*, not about whether the fixture
    still holds one.
    """
    found = reader.room(room_id)
    assert found is not None, f"the fixture has no room {room_id}"
    return found


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
            self.assertEqual(room_of(reader, DIRECT_ROOM).title, "Peer Renamed")

    def test_a_group_room_without_a_title_lists_its_other_members(self) -> None:
        with build_reader() as reader:
            title = room_of(reader, GROUP_ROOM).title
        self.assertIn("Peer Renamed", title)
        self.assertIn("Friend Nick", title)
        self.assertNotIn("My Own Name", title)

    def test_a_custom_group_title_is_used_as_stored(self) -> None:
        with build_reader() as reader:
            self.assertEqual(room_of(reader, UNREADABLE_ROOM).title, "Named Group")

    def test_the_self_chat_is_named_as_such(self) -> None:
        with build_reader() as reader:
            self.assertEqual(room_of(reader, SELF_ROOM).title, "나와의 채팅")

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
        self.addCleanup(cache.close)
        reader = Reader(keyring=ring, cache=cache)
        self.addCleanup(reader.close)
        connection = cache.databases[paths.chat_log_name(DIRECT_ROOM)]
        connection.execute(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted) "
            "VALUES (5004, ?, 1, 'withdrawn', NULL, 1800005004, 1)",
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
        self.addCleanup(cache.close)
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
        self.addCleanup(cache.close)
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


class FeedDetailTests(unittest.TestCase):
    """A feed can say which kind it is without its JSON reaching the screen.

    The number is passed through as a number because no live client has ever
    confirmed what a `feedType` means; only what the client wrote in words —
    a cover reason, a nickname — is quoted.
    """

    def feeds(self, payload: dict) -> dict[int, str]:
        messages = [(5100, PEER_ID, 0, json.dumps(payload), None, 1_700_000_900, 0)]
        with build_reader(messages=messages) as reader:
            return reader.feed_details(DIRECT_ROOM, [5100])

    def test_a_covered_message_says_what_covered_it(self) -> None:
        """An open chat's SafeBot records the reason it hid something."""
        said = self.feeds(
            {
                "feedType": 26,
                "coverType": "openchat_blind",
                "chatLogInfos": [{"logId": 5099, "type": 1}],
            }
        )
        self.assertEqual(said, {5100: "26 openchat_blind"})

    def test_a_membership_feed_says_who_it_names(self) -> None:
        said = self.feeds(
            {"feedType": 4, "members": [{"userId": PEER_ID, "nickName": "합성 닉"}]}
        )
        self.assertEqual(said, {5100: "4 합성 닉"})

    def test_a_single_member_is_read_like_a_list_of_one(self) -> None:
        """The client writes `member` as often as `members` — 1,209 rows of it."""
        said = self.feeds({"feedType": 6, "member": {"nickName": "혼자 온 이름"}})
        self.assertEqual(said, {5100: "6 혼자 온 이름"})

    def test_a_feed_with_nothing_to_add_adds_only_its_number(self) -> None:
        said = self.feeds({"feedType": 21, "logId": 5099, "hidden": True})
        self.assertEqual(said, {5100: "21"})

    def test_a_row_that_is_not_json_is_left_alone(self) -> None:
        """A photo shows no text either, and what it stores is not JSON.

        Asking SQLite's json_extract about one of those fails the statement
        rather than the row, which is why the parsing is done in Python.
        """
        messages = [(5100, PEER_ID, 0, "not json at all", None, 1_700_000_900, 0)]
        with build_reader(messages=messages) as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, [5100]), {})

    def test_nothing_asked_about_is_nothing_read(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, []), {})

    def test_an_ordinary_message_is_not_described_as_a_feed(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, [5000]), {})


class CacheOwnershipTests(unittest.TestCase):
    """Who closes the snapshot cache — and who must not."""

    def test_a_cache_the_reader_was_handed_outlives_it(self) -> None:
        """A shared cache is the whole saving on the server's send path.

        Closing a borrowed one breaks nothing visible: the next reader simply
        decrypts the database again and answers correctly. It is only ever
        slower, which is exactly why it needs a test rather than a reviewer.
        """
        cache, keyring = build_profile()
        self.addCleanup(cache.close)
        with Reader(user_directory=PROFILE, cache=cache, keyring=keyring) as reader:
            self.assertTrue(reader.rooms())
        with Reader(user_directory=PROFILE, cache=cache, keyring=keyring) as second:
            self.assertTrue(second.rooms())

    def test_a_cache_the_reader_built_itself_is_still_closed_by_it(self) -> None:
        class _Recording:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        built: list = []
        original = reader_module.SnapshotCache

        def _record():
            built.append(_Recording())
            return built[-1]

        reader_module.SnapshotCache = _record
        try:
            reader = Reader(user_directory=PROFILE)
        finally:
            reader_module.SnapshotCache = original
        reader.close()
        self.assertTrue(built[0].closed)


if __name__ == "__main__":
    unittest.main()
