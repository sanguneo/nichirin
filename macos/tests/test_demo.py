"""The demo profile still builds, and still renders every scene.

The mockup carries its own copy of the client's schema so it can be run on its
own, and that copy is exactly what rotted last time: the Windows original kept
a catalog plus a database per room, and when the runtime moved to the macOS
single-database shape the generator quietly stopped matching it.  Nothing
failed, because nothing ran it — the screenshots in the repository were simply
older than the code.

So these drive the real thing: the scenes are arranged through the same
``App`` the interface uses, and a schema drift shows up here as a failure
rather than as a stale picture nobody noticed.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("demo_mockup", ROOT / "demo" / "mockup.py")
assert _spec and _spec.loader
mockup = importlib.util.module_from_spec(_spec)
sys.modules["demo_mockup"] = mockup
_spec.loader.exec_module(mockup)

from tests.platform import require_posix_console

require_posix_console()

from kamado.tui.app import App  # noqa: E402
from kamado.tui.layout import display_width, strip_ansi  # noqa: E402
from kamado.tui.theme import Theme  # noqa: E402

SCENES = ("rooms", "chat", "tabs", "digest", "friends", "filter", "sent", "unseen", "real")

WIDTH, HEIGHT = 110, 30


def render(scene: str) -> list[str]:
    with mockup.build_reader() as reader:
        app = App(reader, theme=Theme(truecolor=True, enabled=True))
        mockup.arrange(app, scene)
        return app.render(WIDTH, HEIGHT)


class ProfileTests(unittest.TestCase):
    def test_the_invented_profile_reads_as_a_signed_in_account(self) -> None:
        with mockup.build_reader() as reader:
            status = reader.status()

        self.assertTrue(status["ready"])
        self.assertEqual(status["account"], str(mockup.ME))

    def test_every_room_is_readable_because_they_share_one_database(self) -> None:
        """The Windows original had rooms it held no key for; this shape cannot."""
        with mockup.build_reader() as reader:
            rooms = reader.rooms()

        self.assertTrue(rooms)
        self.assertTrue(all(room.readable for room in rooms))

    def test_the_rooms_carry_the_kinds_the_scenes_are_written_against(self) -> None:
        with mockup.build_reader() as reader:
            kinds = {room.id: room.type for room in reader.rooms()}

        self.assertEqual(kinds[mockup.CREW_ROOM], "OM")
        self.assertEqual(kinds[mockup.JISOO_ROOM], "DirectChat")
        self.assertEqual(kinds[mockup.WORK_ROOM], "MultiChat")
        self.assertEqual(kinds[mockup.SELF_ROOM], "MemoChat")

    def test_two_accounts_wear_the_same_nickname(self) -> None:
        """The whole point of the ``real`` scene: a name is not an identity."""
        with mockup.build_reader() as reader:
            found = reader.users_named(
                mockup.STUDY_ROOM, "지수", reader.name_history(mockup.STUDY_ROOM)
            )

        self.assertEqual(len(found), 2)


class SceneTests(unittest.TestCase):
    def test_every_scene_renders_a_full_frame(self) -> None:
        for scene in SCENES:
            with self.subTest(scene=scene):
                lines = render(scene)
                self.assertEqual(len(lines), HEIGHT)

    def test_no_line_overflows_the_grid(self) -> None:
        """A frame wider than the terminal wraps, and the screenshot tears."""
        for scene in SCENES:
            with self.subTest(scene=scene):
                for line in render(scene):
                    self.assertLessEqual(display_width(line), WIDTH)

    def test_the_conversation_shows_the_invented_people(self) -> None:
        body = strip_ansi("\n".join(render("chat")))

        self.assertIn("주말 러닝크루", body)
        self.assertIn("서연", body)

    def test_the_tab_scene_shows_the_strip_and_what_is_waiting_in_it(self) -> None:
        frame = [strip_ansi(line) for line in render("tabs")]

        self.assertIn("주말 러닝크루", frame[0])
        self.assertIn("프로덕트", frame[0])
        self.assertIn("+2", frame[0], "the waiting room is not counted")
        self.assertNotIn("주말 러닝크루", frame[-1], "the footer names a room the strip already did")

    def test_the_agent_scene_answers_with_both_accounts(self) -> None:
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mockup.build_reader() as reader, redirect_stdout(buffer):
            mockup.show_cli(reader)
        printed = buffer.getvalue()

        self.assertIn('"count": 2', printed)
        self.assertIn(str(mockup.COPYCAT), printed)


if __name__ == "__main__":
    unittest.main()
