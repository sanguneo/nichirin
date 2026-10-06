"""Reader tests: who the user is, what a room is called, and what it contains."""

from __future__ import annotations

import json
import sqlite3
import unittest
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from kanao import paths, reader as reader_module
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


class NewcomerNameTests(unittest.TestCase):
    """Somebody who joins after the room was read is not left as a number.

    The name map is read once per reader and the interface keeps its reader
    for as long as it runs, so a newcomer stayed ``User <id>`` until the room
    was opened again — when a fresh read found them.
    """

    NEWCOMER = 555000111

    def rows(self, feed_name: str | None = None) -> list[tuple]:
        rows = [(5200, self.NEWCOMER, 1, "안녕하세요", None, 1_700_001_000, 0)]
        if feed_name is not None:
            feed = {"feedType": 4, "members": [{"userId": self.NEWCOMER, "nickName": feed_name}]}
            rows.insert(0, (5199, self.NEWCOMER, 0, json.dumps(feed), None, 1_700_000_990, 0))
        return rows

    def newest(self, reader: Reader) -> str:
        return reader.messages(DIRECT_ROOM, limit=50)[-1].sender

    def test_a_name_that_arrives_later_is_found_on_the_next_read(self) -> None:
        from kanao import paths

        with build_reader(messages=self.rows()) as reader:
            self.assertEqual(self.newest(reader), f"User {self.NEWCOMER}")
            users = reader._cache.databases[paths.USER_DATABASE]  # type: ignore[attr-defined]
            users.execute(
                "INSERT INTO talkUser (userId, type, nickName) VALUES (?, 1000, ?)",
                (self.NEWCOMER, "새로 온 사람"),
            )
            users.commit()
            reader._names_read_at -= reader_module.NAME_RETRY_SECONDS
            self.assertEqual(self.newest(reader), "새로 온 사람")

    def test_the_join_feed_names_somebody_the_profile_does_not(self) -> None:
        with build_reader(messages=self.rows(feed_name="입장한 닉")) as reader:
            self.assertEqual(self.newest(reader), "입장한 닉")

    def test_the_map_is_not_reread_for_every_line(self) -> None:
        with build_reader(messages=self.rows()) as reader:
            reader.names()
            before = reader._names
            self.newest(reader)
            self.assertIs(reader._names, before, "the map was read again inside the retry window")


class SearchPositionTests(unittest.TestCase):
    """Where the client's own search lists a room, predicted from the catalog.

    The search matches titles and members' names in recency order, so a
    one-to-one room sat third below two open chats that only had that person
    as a member — and opening the first result opened the wrong room.
    """

    PERSON = 606060

    def reader(self) -> Reader:
        reader = build_reader()
        self.addCleanup(reader.close)
        rooms = reader.rooms()
        # Two busier rooms with the person as a member, then their own room.
        reader._names = {self.PERSON: "합성 이름"}
        reader._members = {rooms[0].id: [self.PERSON], rooms[1].id: [self.PERSON]}
        self.rooms = [
            replace(rooms[0], title="첫 번째 단톡"),
            replace(rooms[1], title="두 번째 단톡"),
            replace(rooms[2], title="합성 이름"),
            *rooms[3:],
        ]
        return reader

    def test_rooms_matched_by_a_member_come_first(self) -> None:
        reader = self.reader()
        self.assertEqual(reader.search_position(self.rooms[2].id, rooms=self.rooms), 2)

    def test_a_room_matched_by_its_title_alone_is_first(self) -> None:
        reader = self.reader()
        reader._members = {}
        self.assertEqual(reader.search_position(self.rooms[2].id, rooms=self.rooms), 0)

    def test_a_room_the_search_would_not_match_has_no_position(self) -> None:
        reader = self.reader()
        self.assertIsNone(
            reader.search_position(self.rooms[2].id, query="없는 말", rooms=self.rooms)
        )

    def test_the_room_is_matched_by_the_title_the_client_lists_it_under(self) -> None:
        # The self-chat is 나와의 채팅 in the catalog and the account's name in
        # the client, and only the second is something its search matches (#87).
        reader = self.reader()
        reader._members = {}
        target = self.rooms[2].id
        self.assertEqual(
            reader.search_position(target, query="계정 이름", rooms=self.rooms, listed="계정 이름"),
            0,
        )
        self.assertIsNone(reader.search_position(target, rooms=self.rooms, listed="계정 이름"))

    def test_given_the_rooms_it_reads_no_catalog(self) -> None:
        reader = self.reader()
        reader.rooms = lambda limit=None: self.fail("the catalog was read")  # type: ignore[method-assign]
        reader._catalog = lambda: self.fail("the catalog was read")  # type: ignore[method-assign]
        self.assertEqual(reader.search_position(self.rooms[2].id, rooms=self.rooms), 2)


class ErasedMessageTests(unittest.TestCase):
    """A message deleted for everyone keeps ``deleted = 0`` and an encrypted text.

    The client flags it in ``is_encrypt_delete_msg`` instead, and the text it
    leaves behind is base64 that nobody wrote. Filtering on ``deleted`` alone
    put that in the conversation as if it had been typed.
    """

    ERASED_TEXT = "c3ludGhldGljIGVyYXNlZCByZW1haW5kZXI="

    def setUp(self) -> None:
        from kanao import paths
        from kanao.reader import Reader
        from tests.fixtures import build_profile

        cache, ring = build_profile()
        self.addCleanup(cache.close)
        self.reader = Reader(keyring=ring, cache=cache)
        self.addCleanup(self.reader.close)
        connection = cache.databases[paths.chat_log_name(DIRECT_ROOM)]
        connection.execute("ALTER TABLE chatLogs ADD COLUMN is_encrypt_delete_msg INTEGER")
        connection.execute(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, "
            "deleted, is_encrypt_delete_msg) VALUES (5004, ?, 16385, ?, NULL, 1800005004, 0, 1)",
            (PEER_ID, self.ERASED_TEXT),
        )
        connection.commit()

    def test_an_erased_message_is_not_part_of_the_conversation(self) -> None:
        found = self.reader.messages(DIRECT_ROOM, limit=50)
        self.assertNotIn(5004, [message.id for message in found])
        self.assertNotIn(self.ERASED_TEXT, [message.text for message in found])

    def test_a_follower_is_never_pointed_at_an_erased_message(self) -> None:
        self.assertEqual(self.reader.newest_log_id(DIRECT_ROOM), 5003)

    def test_an_erased_message_is_not_counted(self) -> None:
        from tests.fixtures import build_reader

        with build_reader() as plain:
            before = plain.count_messages(DIRECT_ROOM)
        self.assertEqual(self.reader.count_messages(DIRECT_ROOM), before)

    def test_the_interface_is_given_the_gap_but_never_the_text(self) -> None:
        found = self.reader.messages(DIRECT_ROOM, limit=50, include_erased=True)
        erased = [message for message in found if message.id == 5004]
        self.assertEqual(len(erased), 1)
        self.assertTrue(erased[0].erased)
        self.assertEqual(erased[0].text, "")
        self.assertNotIn(self.ERASED_TEXT, [message.text for message in found])

    def test_the_gap_never_reaches_the_json(self) -> None:
        found = self.reader.messages(DIRECT_ROOM, limit=50, include_erased=True)
        rendered = [message.as_json() for message in found]
        self.assertTrue(all("erased" not in one for one in rendered))

    def test_the_type_bit_alone_marks_a_deletion_too(self) -> None:
        from kanao import paths

        connection = self.reader._cache.databases[paths.chat_log_name(DIRECT_ROOM)]  # type: ignore[attr-defined]
        connection.execute(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, "
            "deleted, is_encrypt_delete_msg) VALUES (5005, ?, 16385, 'x', NULL, 1800005005, 0, 0)",
            (PEER_ID,),
        )
        connection.commit()
        ids = [message.id for message in self.reader.messages(DIRECT_ROOM, limit=50)]
        self.assertNotIn(5005, ids)

    def test_the_feed_a_deletion_writes_is_never_shown(self) -> None:
        from kanao import paths

        connection = self.reader._cache.databases[paths.chat_log_name(DIRECT_ROOM)]  # type: ignore[attr-defined]
        connection.execute(
            "INSERT INTO chatLogs (logId, authorId, type, message, attachement, sendAt, deleted) "
            "VALUES (5006, ?, 0, ?, NULL, 1800005006, 0)",
            (PEER_ID, json.dumps({"feedType": 14, "logId": 5004, "hidden": True})),
        )
        connection.commit()
        for include in (False, True):
            ids = [m.id for m in self.reader.messages(DIRECT_ROOM, limit=50, include_erased=include)]
            self.assertNotIn(5006, ids)

    def test_an_erased_message_is_not_found_by_search(self) -> None:
        found = list(self.reader.search("c3ludGhldGlj"))
        self.assertEqual(found, [])


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

    A number is passed through as a number unless its meaning was checked
    against a live screen — so far only a join and a leave; otherwise only
    what the client wrote in words — a cover reason, a nickname — is quoted.
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
        self.assertEqual(said, {5100: "합성 닉님이 입장하셨습니다."})

    def test_several_who_join_together_are_named_together(self) -> None:
        said = self.feeds(
            {"feedType": 4, "members": [{"nickName": "하나"}, {"nickName": "둘"}]}
        )
        self.assertEqual(said, {5100: "하나님, 둘님이 입장하셨습니다."})

    def test_a_crowd_is_cut_short_and_counted(self) -> None:
        said = self.feeds(
            {"feedType": 4, "members": [{"nickName": n} for n in ("가", "나", "다", "라", "마")]}
        )
        self.assertEqual(said, {5100: "가님, 나님, 다님 외 2명이 입장하셨습니다."})

    def test_a_leave_is_said_in_words(self) -> None:
        said = self.feeds({"feedType": 2, "member": {"nickName": "떠난 이름"}})
        self.assertEqual(said, {5100: "떠난 이름님이 퇴장하셨습니다."})

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


def _new_snapshot(connection: sqlite3.Connection) -> sqlite3.Connection:
    """A second connection holding the same database, as a re-read would be.

    The real cache hands back a new connection exactly when the file changed,
    so a swapped-in copy is what "this database moved" looks like to a reader.
    """
    copy = sqlite3.connect(":memory:")
    connection.backup(copy)
    return copy


class IdentityRetryTests(unittest.TestCase):
    """A failure to look for the account is not proof that there is none."""

    def test_a_failed_first_look_is_asked_again(self) -> None:
        reader = build_reader()
        self.addCleanup(reader.close)
        databases = reader._cache.databases
        catalog = databases.pop(paths.CATALOG_DATABASE)
        with self.assertRaises(KanaoError):
            reader.own_user_id()
        databases[paths.CATALOG_DATABASE] = catalog

        self.assertEqual(reader.own_user_id(), OWN_ID)
        by_id = {message.id: message for message in reader.messages(DIRECT_ROOM)}
        self.assertTrue(by_id[5001].from_me)

    def test_an_answer_is_kept_once_the_catalog_has_given_one(self) -> None:
        with build_reader() as reader:
            reader.own_user_id()
            opened = len(reader._cache.opened)
            reader.own_user_id()
            self.assertEqual(len(reader._cache.opened), opened)


class NameLifetimeTests(unittest.TestCase):
    """The name shown is the one the user carries now, not at startup."""

    def rename(self, reader: Reader, user_id: int, name: str) -> None:
        databases = reader._cache.databases
        users = _new_snapshot(databases[paths.USER_DATABASE])
        users.execute(
            "UPDATE talkUser SET friendNickName = ? WHERE userId = ?", (name, user_id)
        )
        users.commit()
        databases[paths.USER_DATABASE].close()
        databases[paths.USER_DATABASE] = users

    def senders(self, reader: Reader) -> dict[int, str]:
        return {message.id: message.sender for message in reader.messages(DIRECT_ROOM)}

    def test_a_known_user_who_renamed_is_shown_under_the_new_name(self) -> None:
        with build_reader() as reader:
            self.assertEqual(self.senders(reader)[5000], "Peer Renamed")
            self.rename(reader, PEER_ID, "Peer Again")
            self.assertEqual(self.senders(reader)[5000], "Peer Again")
            self.assertEqual(reader.names()[PEER_ID], "Peer Again")

    def test_an_unchanged_user_database_is_not_read_again(self) -> None:
        with build_reader() as reader:
            first = reader.names()
            reader.messages(DIRECT_ROOM)
            reader.rooms()
            self.assertIs(reader.names(), first)

    def test_a_failed_look_keeps_the_names_it_had_and_asks_again(self) -> None:
        with build_reader() as reader:
            self.senders(reader)
            databases = reader._cache.databases
            users = databases.pop(paths.USER_DATABASE)
            self.assertEqual(self.senders(reader)[5000], "Peer Renamed")
            databases[paths.USER_DATABASE] = users
            self.rename(reader, PEER_ID, "Peer Again")
            self.assertEqual(self.senders(reader)[5000], "Peer Again")


class SchemaLifetimeTests(unittest.TestCase):
    def test_a_column_the_log_gains_is_read_on_the_next_snapshot(self) -> None:
        """A room's columns are asked for again once its file has changed."""
        from tests.fixtures import LOG_SCHEMA, _connect

        old_schema = LOG_SCHEMA.replace(", threadId INTEGER", "")
        rows = [
            (5000, PEER_ID, 1, "first", None, 1_700_000_000, 0),
            (5001, OWN_ID, 1, "answer", None, 1_700_000_100, 0),
        ]
        with build_reader() as reader:
            databases = reader._cache.databases
            name = paths.chat_log_name(DIRECT_ROOM)
            databases[name].close()
            databases[name] = _connect(old_schema, {"chatLogs": rows})
            self.assertIsNone(reader.messages(DIRECT_ROOM)[-1].reply)

            databases[name].close()
            databases[name] = _connect(
                LOG_SCHEMA, {"chatLogs": [row + (5000,) for row in rows[1:]] + rows[:1]}
            )
            reply = reader.messages(DIRECT_ROOM)[-1].reply
            self.assertIsNotNone(reply)
            self.assertEqual(reply.message_id, 5000)


class RowVersionTests(unittest.TestCase):
    """How a caller holding messages learns that one of them changed."""

    def test_a_version_moves_when_a_row_changes_and_only_then(self) -> None:
        with build_reader() as reader:
            before = reader.row_versions(DIRECT_ROOM, 5000, 5003)
            self.assertEqual(set(before), {5000, 5001, 5003})
            self.assertEqual(reader.row_versions(DIRECT_ROOM, 5000, 5003), before)
            log = reader._cache.databases[paths.chat_log_name(DIRECT_ROOM)]
            log.execute("UPDATE chatLogs SET message = 'edited' WHERE logId = 5001")
            log.commit()
            after = reader.row_versions(DIRECT_ROOM, 5000, 5003)
        self.assertNotEqual(after[5001], before[5001])
        self.assertEqual(after[5000], before[5000])

    def test_versions_taken_with_messages_match_the_range_read(self) -> None:
        with build_reader() as reader:
            versions: dict[int, int] = {}
            reader.messages(DIRECT_ROOM, versions=versions)
            self.assertEqual(versions, reader.row_versions(DIRECT_ROOM, 5000, 5003))

    def test_a_row_no_longer_returned_has_no_version(self) -> None:
        with build_reader() as reader:
            log = reader._cache.databases[paths.chat_log_name(DIRECT_ROOM)]
            log.execute("UPDATE chatLogs SET deleted = 1 WHERE logId = 5000")
            log.commit()
            self.assertNotIn(5000, reader.row_versions(DIRECT_ROOM, 5000, 5003))
            self.assertEqual(
                [message.id for message in reader.messages_at(DIRECT_ROOM, [5000, 5003])],
                [5003],
            )


class _EvictingCache:
    """A cache that holds one snapshot and closes the rest, like a tight budget.

    Each connect of a database that is not already open hands out a fresh
    copy and closes whatever falls out, exactly as ``SnapshotCache`` closes an
    evicted entry - so a caller still iterating a closed one fails the way it
    would in production.
    """

    def __init__(self, sources: dict[str, sqlite3.Connection], capacity: int = 1) -> None:
        self.sources = sources
        self.capacity = capacity
        self.open: "OrderedDict[str, sqlite3.Connection]" = OrderedDict()

    def connect(self, database, key, page_size=4096, reserve=80):
        name = Path(database).as_posix()
        source = next(
            (candidate for candidate in self.sources if name.endswith(candidate)), None
        )
        if source is None:
            raise KanaoError(ErrorCode.KEY_UNAVAILABLE, f"no key for {name}")
        if source in self.open:
            self.open.move_to_end(source)
            return self.open[source]
        self.open[source] = _new_snapshot(self.sources[source])
        while len(self.open) > self.capacity:
            _name, evicted = self.open.popitem(last=False)
            evicted.close()
        return self.open[source]

    def close(self) -> None:
        for connection in self.open.values():
            connection.close()
        for connection in self.sources.values():
            connection.close()


class NestedReadTests(unittest.TestCase):
    """A read that opens another database must not lose the one it is reading."""

    def reader(self) -> Reader:
        stub, keyring = build_profile()
        cache = _EvictingCache(stub.databases)
        reader = Reader(user_directory=PROFILE, cache=cache, keyring=keyring)
        reader._owns_cache = True
        self.addCleanup(reader.close)
        return reader

    def test_rooms_are_all_listed_when_the_cache_holds_one_database(self) -> None:
        reader = self.reader()
        rooms = reader.rooms()
        self.assertEqual(
            {room.id for room in rooms},
            {DIRECT_ROOM, GROUP_ROOM, UNREADABLE_ROOM, HUGE_ROOM, SELF_ROOM},
        )
        titles = {room.id: room.title for room in rooms}
        self.assertEqual(titles[DIRECT_ROOM], "Peer Renamed")

    def test_messages_are_all_read_when_the_cache_holds_one_database(self) -> None:
        reader = self.reader()
        found = reader.messages(GROUP_ROOM)
        self.assertEqual([message.id for message in found][:2], [6001, 6002])
        self.assertTrue(any(message.reply is not None for message in found))


if __name__ == "__main__":
    unittest.main()
