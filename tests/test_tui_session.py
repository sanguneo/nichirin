"""The interface's session: two switches and the open tabs, kept between launches.

Closing the screen used to throw everything away: the times came back, SafeBot
covered again, and every tab had to be found again. The App writes nothing
unless it is handed a file, so these tests hand it one in a temporary
directory; nothing here reads or writes the person's own.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanao.cli import build_parser
from kanao.tui.session import DEFAULT_ACCOUNT, SessionState, load_session, save_session
from tests.platform import import_app

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, OWN_ID, UNREADABLE_ROOM, build_reader

GONE_ROOM = 424242


class TemporaryFile(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "kanao-cli" / "tui_state.json"


class StoreTests(TemporaryFile):
    def test_a_saved_session_reads_back_as_it_was_written(self) -> None:
        state = SessionState(
            show_timestamps=False,
            reveal_covered=True,
            open_room_ids=[2**60, 7],
            focused_room_id=7,
        )
        save_session(self.path, "42", state)
        self.assertEqual(load_session(self.path, "42"), state)

    def test_ids_are_written_as_strings(self) -> None:
        save_session(self.path, "42", SessionState(open_room_ids=[2**60], focused_room_id=2**60))
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["version"], 1)
        self.assertEqual(written["accounts"]["42"]["open_room_ids"], [str(2**60)])
        self.assertEqual(written["accounts"]["42"]["focused_room_id"], str(2**60))

    def test_a_missing_or_broken_file_is_a_first_launch(self) -> None:
        self.assertEqual(load_session(self.path, "42"), SessionState())
        self.path.parent.mkdir(parents=True)
        for broken in ("{ not json", "[1, 2]", '{"accounts": []}', '{"accounts": {"42": "x"}}'):
            self.path.write_text(broken, encoding="utf-8")
            self.assertEqual(load_session(self.path, "42"), SessionState(), broken)

    def test_each_account_keeps_its_own_session(self) -> None:
        first = SessionState(open_room_ids=[1])
        second = SessionState(show_timestamps=False, open_room_ids=[2])
        save_session(self.path, "1", first)
        save_session(self.path, "2", second)
        self.assertEqual(load_session(self.path, "1"), first)
        self.assertEqual(load_session(self.path, "2"), second)
        self.assertEqual(load_session(self.path, DEFAULT_ACCOUNT), SessionState())

    def test_what_cannot_be_read_is_left_at_its_default(self) -> None:
        state = SessionState.from_json(
            {
                "show_timestamps": "false",
                "reveal_covered": 1,
                "open_room_ids": [True, -3, "abc", "12", 12, 30],
                "focused_room_id": "x",
            }
        )
        self.assertEqual(state, SessionState(open_room_ids=[12, 30]))

    def test_a_refused_move_is_raised_and_leaves_nothing_behind(self) -> None:
        # A directory where the file belongs refuses the move over it, the
        # way another interface holding the file open does on Windows.
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            save_session(self.path, "42", SessionState())
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], [self.path.name])


class _NoDesktop:
    """A sender that fails the test if restoring ever reaches for the client."""

    def __getattr__(self, name: str):
        raise AssertionError(f"restoring a session asked the client to {name}")


class AppCase(TemporaryFile):
    def setUp(self) -> None:
        super().setUp()
        self.App = import_app()

    def app_for(self, reader, session_file=None):
        from kanao.tui.theme import Theme

        app = self.App(reader, theme=Theme(truecolor=True, enabled=False), session_file=session_file)
        app._sender = _NoDesktop()  # type: ignore[assignment]
        app.load_rooms()
        return app

    def fixture_reader(self):
        reader = build_reader()
        self.addCleanup(reader.close)
        return reader

    def saved(self) -> SessionState:
        return load_session(self.path, str(OWN_ID))

    def tabs(self, app) -> list[int]:
        return [chat.room.id for chat in app.chats]


class RestoreTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self.app_for(self.fixture_reader(), self.path)

    def test_saved_tabs_come_back_in_order_with_the_focused_one_in_front(self) -> None:
        state = SessionState(
            show_timestamps=False,
            open_room_ids=[GROUP_ROOM, DIRECT_ROOM],
            focused_room_id=GROUP_ROOM,
        )
        save_session(self.path, str(OWN_ID), state)
        self.app.restore_session()
        self.assertEqual(self.tabs(self.app), [GROUP_ROOM, DIRECT_ROOM])
        assert self.app.chat is not None
        self.assertEqual(self.app.chat.room.id, GROUP_ROOM)
        self.assertFalse(self.app.show_timestamps)
        self.assertEqual(self.saved(), state)

    def test_a_locked_or_gone_room_is_skipped_without_the_client(self) -> None:
        self.assertIn(UNREADABLE_ROOM, [room.id for room in self.app.rooms if not room.readable])
        save_session(
            self.path,
            str(OWN_ID),
            SessionState(
                open_room_ids=[UNREADABLE_ROOM, GONE_ROOM, DIRECT_ROOM],
                focused_room_id=UNREADABLE_ROOM,
            ),
        )
        self.app.restore_session()
        self.assertEqual(self.tabs(self.app), [DIRECT_ROOM])
        self.assertIsNone(self.app.pending_unlock)
        self.assertEqual(
            self.saved(), SessionState(open_room_ids=[DIRECT_ROOM], focused_room_id=DIRECT_ROOM)
        )

    def test_the_session_is_filed_under_the_account_the_catalog_names(self) -> None:
        save_session(self.path, DEFAULT_ACCOUNT, SessionState(open_room_ids=[DIRECT_ROOM]))
        self.app.restore_session()
        self.assertEqual(self.tabs(self.app), [])
        self.assertEqual(self.saved(), SessionState())

    def test_restoring_writes_once_with_every_tab_back(self) -> None:
        save_session(self.path, str(OWN_ID), SessionState(open_room_ids=[GROUP_ROOM, DIRECT_ROOM]))
        with mock.patch("kanao.tui.app.write_session") as written:
            self.app.restore_session()
        self.assertEqual(written.call_count, 1)
        self.assertEqual(written.call_args.args[2].open_room_ids, [GROUP_ROOM, DIRECT_ROOM])

    def test_every_change_is_written_down(self) -> None:
        from kanao.tui.console import TOGGLE_TIMESTAMP

        self.app.restore_session()
        room = {room.id: room for room in self.app.rooms}
        self.app.open_room(room[DIRECT_ROOM])
        self.assertEqual(self.saved().open_room_ids, [DIRECT_ROOM])

        self.app.open_room(room[GROUP_ROOM], beside=True)
        self.assertEqual(self.saved().open_room_ids, [DIRECT_ROOM, GROUP_ROOM])
        self.assertEqual(self.saved().focused_room_id, GROUP_ROOM)

        self.app._focus_tab(0)
        self.assertEqual(self.saved().focused_room_id, DIRECT_ROOM)

        self.app.handle("char", TOGGLE_TIMESTAMP)
        self.assertFalse(self.saved().show_timestamps)

        self.app._run_command("/safebot")
        self.assertTrue(self.saved().reveal_covered)

        self.app._run_command("/close")
        self.assertEqual(self.saved().open_room_ids, [GROUP_ROOM])
        self.assertEqual(self.saved().focused_room_id, GROUP_ROOM)

        self.app._run_command("/back")
        self.assertEqual(self.saved().open_room_ids, [])
        self.assertIsNone(self.saved().focused_room_id)

    def test_a_draft_never_reaches_the_file(self) -> None:
        from kanao.tui.console import Key

        self.app.restore_session()
        self.app.open_room(next(room for room in self.app.rooms if room.id == DIRECT_ROOM))
        for character in "보내지 않은 초안":
            self.app.handle("char", character)
        self.app.handle(Key.ESCAPE, "")
        self.assertEqual(self.app.drafts[DIRECT_ROOM], "보내지 않은 초안")
        self.assertNotIn("초안", self.path.read_text(encoding="utf-8"))

    def test_a_failed_save_is_said(self) -> None:
        self.app.restore_session()
        self.app.open_room(next(room for room in self.app.rooms if room.id == DIRECT_ROOM))
        chat = self.app.chat
        assert chat is not None
        with mock.patch("kanao.tui.app.write_session", side_effect=PermissionError("in use")):
            self.app._toggle_timestamps(chat)
        self.assertIn("세션을 저장하지 못했습니다", chat.notice)


class WithoutAFileTests(AppCase):
    def test_nothing_is_read_or_written(self) -> None:
        save_session(
            self.path,
            str(OWN_ID),
            SessionState(show_timestamps=False, open_room_ids=[DIRECT_ROOM]),
        )
        before = self.path.read_bytes()
        app = self.app_for(self.fixture_reader())
        app.restore_session()
        self.assertEqual(self.tabs(app), [])
        self.assertTrue(app.show_timestamps)
        app.open_room(next(room for room in app.rooms if room.id == GROUP_ROOM))
        assert app.chat is not None
        app._toggle_timestamps(app.chat)
        self.assertEqual(self.path.read_bytes(), before)


class SafeBotTests(AppCase):
    def covered_text(self, reveal: bool) -> str:
        from tests.extras_fixtures import COVERED, build_extras_reader

        reader = build_extras_reader()
        self.addCleanup(reader.close)
        save_session(
            self.path,
            str(OWN_ID),
            SessionState(reveal_covered=reveal, open_room_ids=[GROUP_ROOM]),
        )
        app = self.app_for(reader, self.path)
        app.restore_session()
        self.assertEqual(reader.reveal_covered, reveal)
        assert app.chat is not None
        return next(message.text for message in app.chat.messages if message.id == COVERED)

    def test_the_saved_switch_reaches_the_reader_before_the_tabs_are_read(self) -> None:
        self.assertEqual(self.covered_text(reveal=True), "가려진 말")
        self.assertNotEqual(self.covered_text(reveal=False), "가려진 말")


class CommandTests(unittest.TestCase):
    def test_fresh_is_off_unless_asked(self) -> None:
        parser = build_parser()
        self.assertTrue(parser.parse_args(["tui", "--fresh"]).fresh)
        self.assertFalse(parser.parse_args(["tui"]).fresh)

    def test_the_command_hands_the_screen_its_file_unless_fresh(self) -> None:
        import_app()
        from kanao import cli
        from kanao.tui.session import session_path

        handed = []
        with mock.patch("kanao.tui.console.supports_tui", return_value=True), mock.patch(
            "kanao.tui.console.Console"
        ), mock.patch("kanao.tui.theme.detect_dark_mode", return_value=True), mock.patch(
            "kanao.tui.app.App"
        ) as app:
            for fresh in (False, True):
                cli._tui(mock.Mock(), fresh=fresh)
                handed.append(app.call_args.kwargs["session_file"])
        self.assertEqual(handed, [session_path(), None])


if __name__ == "__main__":
    unittest.main()
