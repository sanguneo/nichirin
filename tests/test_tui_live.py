"""Live-refresh tests.

The behaviour being protected is not just "new messages appear" but "new
messages appear without stealing the viewport". Someone reading back through
history while a group chat is busy must not be dragged to the bottom every
time somebody types.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kamado import paths
from kamado.model import ERASED_PLACEHOLDER
from tests.platform import require_posix_console

require_posix_console()

from kamado.tui.app import App
from kamado.tui.console import Key
from kamado.tui.layout import strip_ansi
from kamado.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, OWN_ID, PEER_ID, build_reader_on_disk, touch_log

WIDTH = 80


class LiveTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reader, self.cache = build_reader_on_disk(self.root)
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app.load_rooms()
        room = next(room for room in self.app.rooms if room.id == DIRECT_ROOM)
        self.app.open_room(room)

    def chat(self):
        """The open conversation, failing the test rather than the attribute.

        `App.chat` is a property over the tab list and is None when nothing
        is open, which is a state these tests never mean to be in — saying so
        here turns "no conversation is open" into the failure it is, instead
        of an AttributeError several lines later.
        """
        chat = self.app.chat
        if chat is None:
            self.fail("no conversation is open")
        return chat

    def arrive(self, log_id: int, text: str, room_id: int = DIRECT_ROOM, author: int = PEER_ID) -> None:
        connection = self.cache.database
        connection.execute(
            "INSERT INTO NTChatMessage "
            "(logId, chatId, authorId, type, message, attachment, sentAt, deleted) "
            "VALUES (?, ?, ?, 1, ?, NULL, ?, 0)",
            (log_id, room_id, author, text, 1_800_000_000 + log_id),
        )
        connection.commit()
        touch_log(self.root, room_id, b"log" + bytes(log_id % 251))

    def body(self) -> str:
        """The whole frame, for asserting that something reached the screen."""
        return "\n".join(strip_ansi(line) for line in self.app.render(WIDTH, 24))

    def viewport(self) -> str:
        """Only the scrolling region.

        The status line and footer are *expected* to change when messages
        arrive — that is how the reader learns something is waiting — so a
        viewport-stability assertion must not include them. The composer sits
        in the same excluded tail: what is being typed is not the transcript.
        """
        frame = self.app.render(WIDTH, 24)
        return "\n".join(strip_ansi(line) for line in frame[3:-5])


class ArrivalTests(LiveTests):
    def test_an_idle_poll_reports_nothing_to_redraw(self) -> None:
        self.assertFalse(self.app.refresh(WIDTH))

    def test_a_new_message_appears_without_reopening_the_room(self) -> None:
        self.arrive(5200, "LIVE_SENTINEL")
        self.assertNotIn("LIVE_SENTINEL", self.body())

        self.assertTrue(self.app.refresh(WIDTH))
        self.assertIn("LIVE_SENTINEL", self.body())

    def test_the_same_message_is_never_appended_twice(self) -> None:
        self.arrive(5201, "ONCE_ONLY")
        self.app.refresh(WIDTH)
        before = len(self.chat().messages)

        touch_log(self.root, DIRECT_ROOM, b"touched-again")
        self.app.refresh(WIDTH)

        self.assertEqual(len(self.chat().messages), before)
        self.assertEqual(self.body().count("ONCE_ONLY"), 1)

    def test_several_arrivals_all_show_up(self) -> None:
        for index in range(3):
            self.arrive(5300 + index, f"BURST{index}")
        self.app.refresh(WIDTH)
        body = self.body()
        for index in range(3):
            self.assertIn(f"BURST{index}", body)

    def test_a_new_speaker_is_given_a_tint_on_arrival(self) -> None:
        self.arrive(5400, "새 사람", author=999_111)
        self.app.refresh(WIDTH)
        newcomer = self.chat().messages[-1].sender
        self.assertIsNotNone(self.app.palette.name_colour(newcomer))


class ViewportTests(LiveTests):
    def test_a_reader_at_the_bottom_follows_the_conversation(self) -> None:
        self.assertEqual(self.chat().offset, 0)
        self.arrive(5500, "AT_BOTTOM")
        self.app.refresh(WIDTH)

        self.assertEqual(self.chat().offset, 0)
        self.assertIn("AT_BOTTOM", self.body())
        self.assertEqual(self.chat().unseen, 0)

    def test_a_reader_scrolled_up_keeps_seeing_the_same_lines(self) -> None:
        for index in range(30):
            self.arrive(5600 + index, f"HISTORY{index}")
        self.app.refresh(WIDTH)

        self.chat().offset = 8
        before = self.viewport()

        self.arrive(5700, "SHOULD_NOT_JUMP")
        self.app.refresh(WIDTH)

        self.assertEqual(self.viewport(), before, "the viewport moved under the reader")
        self.assertNotIn("SHOULD_NOT_JUMP", self.viewport())

    def test_a_reader_scrolled_up_is_told_how_much_is_waiting(self) -> None:
        self.chat().offset = 5
        self.arrive(5800, "WAITING_ONE")
        self.arrive(5801, "WAITING_TWO")
        self.app.refresh(WIDTH)

        self.assertEqual(self.chat().unseen, 2)
        self.assertIn("새 메시지 2개", self.body())
        self.assertNotIn("WAITING_TWO", self.viewport(), "arrivals must stay below the fold")

    def test_returning_to_the_bottom_clears_the_backlog_marker(self) -> None:
        self.chat().offset = 5
        self.arrive(5900, "PENDING")
        self.app.refresh(WIDTH)
        self.assertEqual(self.chat().unseen, 1)

        self.app.handle(Key.END, "")

        self.assertEqual(self.chat().offset, 0)
        self.assertEqual(self.chat().unseen, 0)
        self.assertIn("PENDING", self.body())


class RoomListTests(LiveTests):
    def setUp(self) -> None:
        super().setUp()
        self.app.chat = None

    def test_an_unchanged_catalog_does_not_redraw(self) -> None:
        self.assertFalse(self.app.refresh(WIDTH))

    def test_the_highlighted_room_stays_highlighted_across_a_refresh(self) -> None:
        self.app.selected = 1
        chosen = self.app.rooms[1].id

        catalog = self.cache.database
        catalog.execute("UPDATE NTChatRoom SET countOfNewMessage = 9 WHERE chatId = ?", (GROUP_ROOM,))
        catalog.commit()
        self.app.refresh(WIDTH)

        self.assertEqual(self.app.rooms[self.app.selected].id, chosen)

    def test_updated_unread_counts_reach_the_screen(self) -> None:
        catalog = self.cache.database
        catalog.execute("UPDATE NTChatRoom SET countOfNewMessage = 7 WHERE chatId = ?", (DIRECT_ROOM,))
        catalog.commit()

        self.assertTrue(self.app.refresh(WIDTH))
        self.assertIn("7 unread", self.body())

    def test_viewing_room_clears_unread_in_room_list(self) -> None:
        catalog = self.cache.database
        catalog.execute("UPDATE NTChatRoom SET countOfNewMessage = 5 WHERE chatId = ?", (DIRECT_ROOM,))
        catalog.commit()

        self.assertTrue(self.app.refresh(WIDTH))
        direct_line = next(line for line in self.body().splitlines() if "Peer Renamed" in line and "Friend Nick" not in line)
        self.assertIn("5 unread", direct_line)

        # Now user opens the room in kamado
        room = next(r for r in self.app.rooms if r.id == DIRECT_ROOM)
        self.app.open_room(room)
        self.app.refresh(WIDTH)
        self.assertIsNotNone(self.app.chat)

        # Switch back to room list
        self.app.picking = True
        direct_line = next(line for line in self.body().splitlines() if "Peer Renamed" in line and "Friend Nick" not in line)
        self.assertNotIn("unread", direct_line)

    def test_new_messages_after_viewing_increment_unread(self) -> None:
        catalog = self.cache.database
        catalog.execute("UPDATE NTChatRoom SET countOfNewMessage = 5 WHERE chatId = ?", (DIRECT_ROOM,))
        catalog.commit()
        self.app.refresh(WIDTH)

        # Open and view the room
        room = next(r for r in self.app.rooms if r.id == DIRECT_ROOM)
        self.app.open_room(room)
        self.app.refresh(WIDTH)

        # Close the tab
        chat = self.app.chat
        assert chat is not None
        self.app._leave_room(chat)
        self.assertEqual(len(self.app.chats), 0)

        # 2 new messages arrive while outside the room
        catalog.execute("UPDATE NTChatRoom SET countOfNewMessage = 7 WHERE chatId = ?", (DIRECT_ROOM,))
        catalog.commit()
        self.app.refresh(WIDTH)

        direct_line = next(line for line in self.body().splitlines() if "Peer Renamed" in line and "Friend Nick" not in line)
        self.assertIn("2 unread", direct_line)

    def test_arrival_while_picking_shows_in_room_list(self) -> None:
        # Open and view the room
        room = next(r for r in self.app.rooms if r.id == DIRECT_ROOM)
        self.app.open_room(room)
        self.app.refresh(WIDTH)

        # Enter room picker
        self.app.picking = True
        self.arrive(9300, "NEW_MESSAGE")
        self.app.refresh(WIDTH)

        direct_line = next(line for line in self.body().splitlines() if "Peer Renamed" in line and "Friend Nick" not in line)
        self.assertIn("1 unread", direct_line)


class DigestTests(LiveTests):
    def test_arrivals_do_not_disturb_an_open_digest(self) -> None:
        for index in range(6):
            self.arrive(9000 + index, f"주제 배포 회의 {index}")
        self.app.refresh(WIDTH)
        self.app._run_command("/sum 30m")
        before = self.viewport()

        self.arrive(9100, "AFTER_DIGEST")
        self.app.refresh(WIDTH)

        self.assertEqual(self.viewport(), before, "the digest was rebuilt underneath the reader")
        self.assertIn("AFTER_DIGEST", [m.text for m in self.chat().messages])


class ChangedInPlaceTests(LiveTests):
    """A message already on screen can change without a new one arriving.

    Deleted for everyone, it keeps its id and has its type rewritten, and the
    watcher only asks for ids above the newest it has seen - so the text
    stayed on screen, in the digest and in the export, until the room was
    opened again.
    """

    def change(self, sql: str, parameters: tuple) -> None:
        connection = self.cache.database
        connection.execute(sql, parameters)
        connection.commit()
        touch_log(self.root, DIRECT_ROOM, b"changed " + sql.encode()[:40] + repr(parameters).encode())

    def erase(self, log_id: int) -> None:
        """What this client does: the row and its text stay, the type is flagged."""
        self.change("UPDATE NTChatMessage SET type = type | 16384 WHERE logId = ?", (log_id,))

    def test_a_message_deleted_for_everyone_is_replaced_by_the_placeholder(self) -> None:
        self.assertIn("third", self.body())
        self.erase(5003)
        self.assertTrue(self.app.refresh(WIDTH))
        body = self.body()
        self.assertNotIn("third", body)
        self.assertIn(ERASED_PLACEHOLDER, body)
        self.assertTrue(next(m for m in self.chat().messages if m.id == 5003).erased)

    def test_the_feed_a_deletion_writes_does_not_show_beside_it(self) -> None:
        self.erase(5003)
        connection = self.cache.database
        connection.execute(
            "INSERT INTO NTChatMessage "
            "(logId, chatId, authorId, type, message, attachment, sentAt, deleted) "
            "VALUES (5004, ?, ?, 0, ?, NULL, 1800005004, 0)",
            (DIRECT_ROOM, PEER_ID, json.dumps({"feedType": 14, "logId": 5003, "hidden": True})),
        )
        connection.commit()
        touch_log(self.root, DIRECT_ROOM, b"feed fourteen")
        self.app.refresh(WIDTH)
        body = self.body()
        self.assertNotIn("third", body)
        self.assertNotIn("feed 14", body)
        self.assertEqual(body.count(ERASED_PLACEHOLDER), 1)

    def test_a_row_deleted_outright_leaves_the_conversation(self) -> None:
        self.change("UPDATE NTChatMessage SET deleted = 1 WHERE logId = ?", (5000,))
        self.assertTrue(self.app.refresh(WIDTH))
        self.assertNotIn(5000, [message.id for message in self.chat().messages])
        self.assertNotIn("first", self.body())

    def test_text_replaced_at_the_same_id_is_shown_as_it_is_now(self) -> None:
        self.change("UPDATE NTChatMessage SET message = 'REWRITTEN' WHERE logId = ?", (5000,))
        self.app.refresh(WIDTH)
        self.assertIn("REWRITTEN", self.body())
        self.assertNotIn("first", self.body())

    def test_a_database_that_moved_without_changing_a_held_row_redraws_nothing(self) -> None:
        self.body()
        rendered = self.chat().rendered
        touch_log(self.root, DIRECT_ROOM, b"moved, nothing changed")
        self.assertFalse(self.app.refresh(WIDTH))
        self.body()
        self.assertEqual(self.chat().revision, 0)
        self.assertIs(self.chat().rendered, rendered, "the transcript was rebuilt")

    def test_a_deletion_below_the_viewport_does_not_move_what_is_being_read(self) -> None:
        for index in range(30):
            self.arrive(5600 + index, f"HISTORY{index}")
        self.app.refresh(WIDTH)
        self.chat().offset = 8
        before = self.viewport()

        self.change("UPDATE NTChatMessage SET deleted = 1 WHERE logId = ?", (5629,))
        self.assertTrue(self.app.refresh(WIDTH))
        self.assertEqual(self.viewport(), before, "the viewport moved under the reader")

    def test_a_deletion_inside_the_viewport_leaves_the_rest_in_place(self) -> None:
        for index in range(30):
            self.arrive(5600 + index, f"HISTORY{index}")
        self.app.refresh(WIDTH)
        self.chat().offset = 12
        before = self.viewport()
        self.assertIn("HISTORY15", before)
        self.change("UPDATE NTChatMessage SET message = 'REPLACED' WHERE logId = ?", (5615,))
        self.assertTrue(self.app.refresh(WIDTH))
        self.assertEqual(
            self.viewport(),
            before.replace("HISTORY15", "REPLACED "),
            "only the changed line may differ",
        )

    def test_a_digest_is_cut_again_without_the_deleted_line(self) -> None:
        for index in range(6):
            self.arrive(9200 + index, f"주제 배포 회의 {index}")
        self.app.refresh(WIDTH)
        self.app._run_command("/sum 30m")

        def said() -> list[str]:
            return [message.text for piece in self.chat().slices for message in piece.messages]

        self.assertIn("주제 배포 회의 5", said())
        self.assertIn("6 messages", self.viewport())

        self.erase(9205)
        self.app.refresh(WIDTH)
        self.assertNotIn("주제 배포 회의 5", said())
        self.assertIn("5 messages", self.viewport())

    def test_a_digest_leaves_out_what_was_already_deleted(self) -> None:
        for index in range(3):
            self.arrive(9300 + index, f"요약 대상 {index}")
        self.erase(9302)
        self.app.refresh(WIDTH)
        self.app._run_command("/sum 30m")
        said = [message.text for piece in self.chat().slices for message in piece.messages]
        self.assertNotIn("요약 대상 2", said)


class TabArrivalTests(LiveTests):
    """One poll answers for every open conversation.

    The messages come back mixed, so the risk a second tab introduces is not
    missing an arrival but filing it under the wrong room — which would show a
    line in a conversation nobody wrote it in.
    """

    def setUp(self) -> None:
        super().setUp()
        self.app.open_room(next(room for room in self.app.rooms if room.id == GROUP_ROOM))
        self.direct, self.group = self.app.chats

    def texts(self, chat) -> list[str]:
        return [message.text for message in chat.messages]

    def test_each_message_lands_in_the_room_it_was_written_in(self) -> None:
        self.arrive(5400, "FOR_THE_DIRECT_ROOM", room_id=DIRECT_ROOM)
        self.arrive(6400, "FOR_THE_GROUP", room_id=GROUP_ROOM)

        self.assertTrue(self.app.refresh(WIDTH))

        self.assertIn("FOR_THE_DIRECT_ROOM", self.texts(self.direct))
        self.assertNotIn("FOR_THE_GROUP", self.texts(self.direct))
        self.assertIn("FOR_THE_GROUP", self.texts(self.group))
        self.assertNotIn("FOR_THE_DIRECT_ROOM", self.texts(self.group))

    def test_a_room_without_the_keyboard_still_receives(self) -> None:
        """The unfocused tab is watched, not merely displayed."""
        self.assertIs(self.app.chat, self.group)
        self.arrive(5401, "WHILE_LOOKING_ELSEWHERE", room_id=DIRECT_ROOM)
        self.assertTrue(self.app.refresh(WIDTH))
        self.assertIn("WHILE_LOOKING_ELSEWHERE", self.texts(self.direct))

    def test_a_poll_that_finds_nothing_redraws_nothing(self) -> None:
        self.assertFalse(self.app.refresh(WIDTH))


if __name__ == "__main__":
    unittest.main()
