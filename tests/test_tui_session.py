from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kamado.cli import build_parser
from kamado.tui.theme import Theme
from kamado.tui.app import App
from kamado.tui.session import (
    TuiSessionState,
    load_session,
    save_session,
    state_file_path,
)
from tests.fixtures import DIRECT_ROOM, GROUP_ROOM
from tests.test_tui_worker import FakeSender, HEIGHT, WAIT, WIDTH, WorkerCase


class TuiSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"HOME": self.tmp.name}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_serialization_round_trip(self) -> None:
        state = TuiSessionState(
            show_timestamps=False,
            mouse_clicks=True,
            reveal_covered=True,
            open_room_ids=[100, 200],
            focused_room_id=200,
        )
        as_dict = state.to_dict()
        restored = TuiSessionState.from_dict(as_dict)
        self.assertEqual(state, restored)

    def test_load_defaults_when_file_missing(self) -> None:
        state = load_session(user_id=1234, environment=self.env)
        self.assertTrue(state.show_timestamps)
        self.assertEqual(state.open_room_ids, [])
        self.assertIsNone(state.focused_room_id)

    def test_save_and_load_isolated_by_user_id(self) -> None:
        state_a = TuiSessionState(
            show_timestamps=False,
            mouse_clicks=False,
            reveal_covered=True,
            open_room_ids=[101, 102],
            focused_room_id=102,
        )
        state_b = TuiSessionState(
            show_timestamps=True,
            mouse_clicks=True,
            reveal_covered=False,
            open_room_ids=[201],
            focused_room_id=201,
        )

        save_session(state_a, user_id=1000, environment=self.env)
        save_session(state_b, user_id=2000, environment=self.env)

        loaded_a = load_session(user_id=1000, environment=self.env)
        loaded_b = load_session(user_id=2000, environment=self.env)

        self.assertEqual(loaded_a, state_a)
        self.assertEqual(loaded_b, state_b)

    def test_corrupted_json_returns_defaults(self) -> None:
        path = state_file_path(self.env)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ broken json: true", encoding="utf-8")

        state = load_session(user_id=1000, environment=self.env)
        self.assertTrue(state.show_timestamps)
        self.assertEqual(state.open_room_ids, [])


class TuiAppSessionIntegrationTests(WorkerCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"HOME": self.tmp.name}
        self.sender = FakeSender()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def test_app_restores_preferences_and_tabs(self) -> None:
        user_id = 9999
        initial_state = TuiSessionState(
            show_timestamps=False,
            mouse_clicks=False,
            reveal_covered=False,
            open_room_ids=[DIRECT_ROOM, GROUP_ROOM],
            focused_room_id=GROUP_ROOM,
        )
        save_session(initial_state, user_id=user_id, environment=self.env)

        app = App(
            None,
            theme=Theme(truecolor=True, enabled=False),
            sender=self.sender,
            db=self.worker,
            restore=True,
            user_id=user_id,
            environment=self.env,
        )
        self.assertFalse(app.show_timestamps)
        self.assertFalse(app.mouse_clicks)
        self.assertEqual(app._pending_restore_room_ids, [DIRECT_ROOM, GROUP_ROOM])
        self.assertEqual(app._pending_restore_focus_id, GROUP_ROOM)

        app.load_rooms()
        self.assertTrue(app._pump(WIDTH, timeout=WAIT))

        while len(app.chats) < 2:
            self.assertTrue(app._pump(WIDTH, timeout=WAIT))

        self.assertEqual(len(app.chats), 2)
        self.assertEqual([c.room.id for c in app.chats], [DIRECT_ROOM, GROUP_ROOM])
        self.assertEqual(app.chat.room.id, GROUP_ROOM)

    def test_app_fresh_mode_ignores_saved_session(self) -> None:
        user_id = 9999
        saved_state = TuiSessionState(
            show_timestamps=False,
            open_room_ids=[DIRECT_ROOM],
            focused_room_id=DIRECT_ROOM,
        )
        save_session(saved_state, user_id=user_id, environment=self.env)

        app = App(
            None,
            theme=Theme(truecolor=True, enabled=False),
            sender=self.sender,
            db=self.worker,
            restore=False,
            user_id=user_id,
            environment=self.env,
        )
        self.assertTrue(app.show_timestamps)
        self.assertEqual(app._pending_restore_room_ids, [])

        app.load_rooms()
        self.assertTrue(app._pump(WIDTH, timeout=WAIT))
        self.assertEqual(len(app.chats), 0)

    def test_app_saves_session_on_toggle_and_tab_close(self) -> None:
        user_id = 9999
        app = App(
            None,
            theme=Theme(truecolor=True, enabled=False),
            sender=self.sender,
            db=self.worker,
            restore=True,
            user_id=user_id,
            environment=self.env,
        )
        app.load_rooms()
        self.assertTrue(app._pump(WIDTH, timeout=WAIT))

        app._toggle_timestamps()
        loaded = load_session(user_id=user_id, environment=self.env)
        self.assertFalse(loaded.show_timestamps)

        direct_room = next(r for r in app.rooms if r.id == DIRECT_ROOM)
        app.open_room(direct_room)
        while not app.chats:
            self.assertTrue(app._pump(WIDTH, timeout=WAIT))

        loaded = load_session(user_id=user_id, environment=self.env)
        self.assertEqual(loaded.open_room_ids, [DIRECT_ROOM])
        self.assertEqual(loaded.focused_room_id, DIRECT_ROOM)

        app.close_tab(app.chats[0]) if hasattr(app, "close_tab") else app._leave_room(app.chats[0])
        loaded = load_session(user_id=user_id, environment=self.env)
        self.assertEqual(loaded.open_room_ids, [])
        self.assertIsNone(loaded.focused_room_id)

    def test_cli_parser_fresh_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["tui", "--fresh"])
        self.assertTrue(args.fresh)

        default_args = parser.parse_args(["tui"])
        self.assertFalse(default_args.fresh)
