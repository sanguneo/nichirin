"""Reader tests: who the user is, what a room is called, and what it contains."""

from __future__ import annotations

import json
import unittest
import unittest.mock

from kamado.errors import ErrorCode, KamadoError
from kamado.keyring import Keyring
from kamado.reader import Reader

from tests.fixtures import (
    DIRECT_ROOM,
    FRIEND_ID,
    GROUP_ROOM,
    HUGE_ID,
    HUGE_ROOM,
    OWN_ID,
    PEER_ID,
    CONTAINER,
    DATABASE,
    OPEN_ROOM,
    SELF_ROOM,
    STRANGER_ID,
    NAMED_ROOM,
    build_profile,
    build_reader,
)


def room_of(reader, room_id):
    """The fixture's room, said as a failure when the fixture has lost it.

    `Reader.room` may answer None — a room really can be missing — and these
    tests are about what a room is *called*, not about whether the fixture
    still holds one.
    """
    found = reader.room(room_id)
    assert found is not None, f"the fixture has no room {room_id}"
    return found


class IdentityTests(unittest.TestCase):
    def test_the_signed_in_account_is_stated_by_the_context(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.own_user_id(), OWN_ID)

    def test_the_context_wins_over_an_unrelated_account(self) -> None:
        from dataclasses import replace

        with build_reader() as reader:
            reader._keyring = replace(reader.keyring, account=replace(reader.account, user_id=STRANGER_ID))
            self.assertEqual(reader.own_user_id(), OWN_ID)

    def test_context_identifies_the_account_without_a_self_chat(self) -> None:
        with build_reader(with_self_room=False) as reader:
            self.assertEqual(reader.own_user_id(), OWN_ID)

    def test_resolving_the_account_is_only_done_once(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.own_user_id(), OWN_ID)
            reader.connection().execute("DELETE FROM NTChatContext")
            reader._keyring = Keyring(CONTAINER, [DATABASE], None)
            self.assertEqual(reader.own_user_id(), OWN_ID)

    def test_missing_context_falls_back_to_the_derived_account(self) -> None:
        with build_reader() as reader:
            reader.connection().execute("DELETE FROM NTChatContext")
            self.assertEqual(reader.own_user_id(), OWN_ID)


class NameTests(unittest.TestCase):
    def test_a_name_the_user_assigned_beats_the_contacts_own_nickname(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.names()[PEER_ID], "Peer Renamed")

    def test_the_signed_in_account_is_named_from_ntuser(self) -> None:
        """The macOS user table includes the owner as well as contacts."""
        with build_reader() as reader:
            self.assertEqual(reader.names()[OWN_ID], "My Own Name")

    def test_a_contact_with_no_name_at_all_is_left_out_rather_than_invented(self) -> None:
        with build_reader() as reader:
            reader.connection().execute(
                "UPDATE NTUser SET nickName = NULL, displayName = NULL, friendNickName = NULL WHERE userId = ?",
                (STRANGER_ID,),
            )
            self.assertNotIn(STRANGER_ID, reader.names())


class RoomTests(unittest.TestCase):
    def test_rooms_are_listed_most_recently_active_first(self) -> None:
        with build_reader() as reader:
            order = [room.id for room in reader.rooms()]
        self.assertEqual(order[0], DIRECT_ROOM)
        self.assertEqual(order.index(DIRECT_ROOM) < order.index(GROUP_ROOM), True)

    def test_one_decrypted_database_makes_every_room_readable(self) -> None:
        with build_reader() as reader:
            rooms = reader.rooms()
        self.assertEqual(len(rooms), 6)
        self.assertTrue(all(room.readable for room in rooms))

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
            self.assertEqual(room_of(reader, NAMED_ROOM).title, "Named Group")

    def test_a_room_renamed_in_the_client_takes_that_name(self) -> None:
        """A rename never touches the room row; the name goes to the meta table.

        Left to the room row, a two-person group comes out titled after the one
        other person in it — the same name as the direct chat with that person,
        which is a collision the send path then has to refuse, and a window
        title the client never shows.
        """
        with build_reader(metas=[(GROUP_ROOM, 3, 10, "합성 스터디방")]) as reader:
            named = reader.room(GROUP_ROOM)
        assert named is not None
        self.assertEqual(named.title, "합성 스터디방")

    def test_the_newest_revision_of_a_name_is_the_one_shown(self) -> None:
        metas = [(GROUP_ROOM, 3, 10, "옛 이름"), (GROUP_ROOM, 3, 20, "새 이름")]
        with build_reader(metas=metas) as reader:
            renamed = reader.room(GROUP_ROOM)
        assert renamed is not None
        self.assertEqual(renamed.title, "새 이름")

    def test_a_notice_is_not_a_name(self) -> None:
        """Type 1 is the room's notice, kept in the same table as its name."""
        with build_reader(metas=[(GROUP_ROOM, 1, 10, "공지입니다")]) as reader:
            noticed = reader.room(GROUP_ROOM)
        assert noticed is not None
        self.assertIn("Peer Renamed", noticed.title)

    def test_the_stored_title_falls_back_to_the_name_the_window_carries(self) -> None:
        with build_reader(metas=[(GROUP_ROOM, 3, 10, "합성 스터디방")]) as reader:
            self.assertEqual(reader.stored_title(GROUP_ROOM), "합성 스터디방")

    def test_a_feed_says_what_its_own_row_carries(self) -> None:
        """An open chat's SafeBot writes the reason it covered something.

        The type number is not confirmed against a live client, so it stays a
        number; the cover is a string the client wrote, so it is quoted.
        """
        covering = json.dumps(
            {
                "feedType": 26,
                "coverType": "openchat_blind",
                "chatLogInfos": [{"logId": 7001, "type": 1}],
            }
        )
        messages = [(7002, DIRECT_ROOM, PEER_ID, 0, covering, None, 1_700_000_001, 0)]
        with build_reader(messages=messages) as reader:
            self.assertEqual(
                reader.feed_details(DIRECT_ROOM, [7002]), {7002: "26 openchat_blind"}
            )

    def test_a_membership_feed_says_who_it_names(self) -> None:
        joining = json.dumps(
            {"feedType": 4, "members": [{"userId": PEER_ID, "nickName": "합성 닉"}]}
        )
        messages = [(7002, DIRECT_ROOM, PEER_ID, 0, joining, None, 1_700_000_001, 0)]
        with build_reader(messages=messages) as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, [7002]), {7002: "4 합성 닉"})

    def test_a_feed_with_nothing_to_add_adds_nothing(self) -> None:
        hiding = json.dumps({"feedType": 21, "logId": 7001, "hidden": True})
        messages = [(7002, DIRECT_ROOM, PEER_ID, 0, hiding, None, 1_700_000_001, 0)]
        with build_reader(messages=messages) as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, [7002]), {7002: "21"})

    def test_a_row_that_is_not_json_is_left_alone(self) -> None:
        """A photo shows no text either, and what it stores is not JSON."""
        messages = [(7002, DIRECT_ROOM, PEER_ID, 0, "not json", None, 1_700_000_001, 0)]
        with build_reader(messages=messages) as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, [7002]), {})

    def test_nothing_asked_about_is_nothing_read(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.feed_details(DIRECT_ROOM, []), {})

    def test_the_self_chat_is_named_as_such(self) -> None:
        with build_reader() as reader:
            self.assertEqual(room_of(reader, SELF_ROOM).title, "나와의 채팅")

    def test_room_kinds_are_stable_names_an_agent_can_branch_on(self) -> None:
        with build_reader() as reader:
            kinds = {room.id: room.kind for room in reader.rooms()}
        self.assertEqual(kinds[DIRECT_ROOM], "direct")
        self.assertEqual(kinds[GROUP_ROOM], "group")
        self.assertEqual(kinds[SELF_ROOM], "self")
        self.assertEqual(kinds[OPEN_ROOM], "open-group")

    def test_room_type_code_is_reported_without_deciding_the_kind(self) -> None:
        with build_reader() as reader:
            reader.connection().execute("UPDATE NTChatRoom SET type = 999 WHERE chatId = ?", (DIRECT_ROOM,))
            room = room_of(reader, DIRECT_ROOM)
        self.assertEqual(room.kind, "direct")
        self.assertEqual(room.as_json()["typeCode"], 999)

    def test_an_unknown_room_id_is_absent_rather_than_fabricated(self) -> None:
        with build_reader() as reader:
            self.assertIsNone(reader.room(424242))


class ResolutionTests(unittest.TestCase):
    def test_an_exact_title_wins_over_a_longer_one_containing_it(self) -> None:
        with build_reader() as reader:
            matches = reader.resolve("Named Group")
        self.assertEqual([room.id for room in matches], [NAMED_ROOM])

    def test_matching_ignores_case(self) -> None:
        with build_reader() as reader:
            self.assertEqual([room.id for room in reader.resolve("nAmEd gRoUp")], [NAMED_ROOM])

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

    def test_a_borrowed_cache_outlives_the_reader_that_used_it(self) -> None:
        """Whoever built the cache closes it, because sharing is the point.

        ``serve`` hands one cache to a reader per send. A reader that closed it
        on the way out would put the whole decryption back on every request --
        the 2.5 s that sharing exists to avoid -- and would do it silently,
        since the next reader simply decrypts again and answers correctly.
        """
        cache, keyring = build_profile()
        first = Reader(cache=cache, keyring=keyring)  # type: ignore[arg-type]
        self.assertTrue(first.rooms())
        first.close()

        second = Reader(cache=cache, keyring=keyring)  # type: ignore[arg-type]
        self.addCleanup(second.close)
        self.assertTrue(
            second.rooms(), "the first reader closed a cache it had only borrowed"
        )

    def test_a_reader_still_closes_the_cache_it_made_itself(self) -> None:
        import kamado.reader as reader_module

        original = reader_module.SnapshotCache  # bound before patching
        closed: list = []

        class _Recording(original):  # type: ignore[misc,valid-type]
            def close(self) -> None:
                closed.append(True)
                super().close()

        reader_module.SnapshotCache = _Recording
        self.addCleanup(setattr, reader_module, "SnapshotCache", original)

        Reader().close()
        self.assertEqual(closed, [True])

    def test_the_newest_id_is_available_without_reading_bodies(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.newest_log_id(DIRECT_ROOM), 5003)

    def test_the_newest_id_ignores_a_deleted_row(self) -> None:
        """The follow cursor must name a message the reader can actually return.

        Every other query excludes deleted rows, so a newest id that counts
        one hands the watcher a position it will never read a message at.
        """
        cache, keyring = build_profile()
        reader = Reader(cache=cache, keyring=keyring)
        self.addCleanup(reader.close)
        cache.database.execute(
            "INSERT INTO NTChatMessage "
            "(logId, chatId, authorId, type, message, attachment, sentAt, deleted) "
            "VALUES (5004, ?, ?, 1, 'withdrawn', NULL, ?, 1)",
            (DIRECT_ROOM, PEER_ID, 1_700_000_400),
        )
        cache.database.commit()

        self.assertEqual(reader.newest_log_id(DIRECT_ROOM), 5003)

    def test_catching_up_after_a_cursor_starts_at_the_oldest_unseen_message(self) -> None:
        """A page cut from the newest end would skip everything below it.

        Asked what arrived after a known message, a caller advances its
        cursor past whatever it is handed, so a page taken from the far end
        loses the gap between the cursor and that page for good.
        """
        cache, keyring = build_profile()
        reader = Reader(cache=cache, keyring=keyring)
        self.addCleanup(reader.close)
        cache.database.executemany(
            "INSERT INTO NTChatMessage "
            "(logId, chatId, authorId, type, message, attachment, sentAt, deleted) "
            "VALUES (?, ?, ?, 1, ?, NULL, ?, 0)",
            [
                (log_id, DIRECT_ROOM, PEER_ID, f"burst {log_id}", 1_700_001_000 + log_id)
                for log_id in range(5200, 5210)
            ],
        )
        cache.database.commit()

        found = reader.messages(DIRECT_ROOM, limit=4, after_id=5003)
        self.assertEqual([message.id for message in found], [5200, 5201, 5202, 5203])

    def test_identifiers_past_two_to_the_fifty_third_survive_as_strings(self) -> None:
        with build_reader() as reader:
            found = reader.messages(HUGE_ROOM)
        rendered = found[0].as_json()
        self.assertEqual(rendered["id"], str(HUGE_ID))
        self.assertEqual(rendered["roomId"], str(HUGE_ROOM))
        self.assertEqual(int(rendered["id"]), HUGE_ID)

    def test_a_stored_room_without_messages_reads_as_empty(self) -> None:
        with build_reader() as reader:
            self.assertEqual(reader.messages(NAMED_ROOM), [])


class SearchTests(unittest.TestCase):
    def test_finds_matches_across_readable_rooms(self) -> None:
        with build_reader() as reader:
            found = list(reader.search("group hello"))
        self.assertEqual([message.room_id for message in found], [GROUP_ROOM])

    def test_no_matching_text_returns_no_messages(self) -> None:
        with build_reader() as reader:
            self.assertEqual(list(reader.search("nothing matches this")), [])

    def test_a_limit_of_zero_searches_nothing(self) -> None:
        with build_reader() as reader:
            self.assertEqual(list(reader.search("first", limit=0)), [])

    def test_deleted_messages_are_not_searchable(self) -> None:
        with build_reader() as reader:
            self.assertEqual(list(reader.search("removed")), [])


class StatusTests(unittest.TestCase):
    def test_status_reports_a_ready_account_without_a_running_client(self) -> None:
        with build_reader() as reader:
            status = reader.status()
        self.assertEqual(status, {
            "ready": True, "account": str(OWN_ID), "database": DATABASE.name,
            "rooms": 6, "detail": "ready",
        })

    def test_status_explains_an_unresolved_account_instead_of_failing(self) -> None:
        with build_reader() as reader:
            reader._keyring = Keyring(CONTAINER, [DATABASE], None)
            status = reader.status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["rooms"], 0)
        self.assertIsNone(status["account"])
        self.assertIsNone(status["database"])

    def test_status_uses_cached_rooms_when_database_fingerprint_matches(self) -> None:
        import tempfile
        from pathlib import Path
        from tests.fixtures import build_reader_on_disk

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reader, _stub = build_reader_on_disk(root)
            first = reader.status()
            self.assertEqual(first["rooms"], 6)

            # Re-running status should hit cache without calling count_rooms
            with unittest.mock.patch.object(reader, "count_rooms", side_effect=AssertionError("called count_rooms")):
                second = reader.status()
            self.assertEqual(second, first)

    def test_an_unresolved_account_names_the_reason_a_read_fails(self) -> None:
        with build_reader() as reader:
            reader._keyring = Keyring(CONTAINER, [DATABASE], None)
            with self.assertRaises(KamadoError) as raised:
                reader.rooms()
        self.assertEqual(raised.exception.code, ErrorCode.KEY_UNAVAILABLE)


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
            (21, DIRECT_ROOM, PEER_ID, 1, "사람이 쓴 줄", None, 1_700_000_100, 0),
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
