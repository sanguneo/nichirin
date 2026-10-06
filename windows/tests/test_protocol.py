"""The envelopes this CLI emits, pinned as files.

`PROTOCOL.md` describes the shapes; these are one real sample of each, so a
consumer written against them — the HTTP wrapper, an agent — finds out here
when one changes rather than in production. Adding a key is allowed and is
meant to show up as a diff worth approving; removing or renaming one is a
breaking change and this is where it first fails.

Everything is generated from `tests/fixtures.py`, which is synthetic. No real
account is ever read, which is also why the samples can be committed.

Regenerate after an intended change::

    python -m tests.test_protocol --write
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from typing import Any
from contextlib import redirect_stdout
from pathlib import Path

from kanao import cli
from kanao.errors import KanaoError, as_envelope
from kanao.watch import Watcher

from tests.fixtures import DIRECT_ROOM, build_reader

ENVELOPES = Path(__file__).resolve().parent / "envelopes"
FOLLOW_INTERVAL = 1.0


def _command(*argv: str) -> dict:
    """Run one command and return the envelope it printed, errors included."""
    with build_reader() as reader:
        arguments = cli.build_parser().parse_args(argv)
        printed = io.StringIO()
        try:
            with redirect_stdout(printed):
                cli.run(arguments, reader)
        except KanaoError as error:
            return as_envelope(error)
        return json.loads(printed.getvalue())


def _follow_lines() -> tuple[dict, dict]:
    """Build the two `follow` lines the way ``cli._follow`` builds them.

    Captured rather than run: following does not end on its own. The message
    line reuses the same ``Message.as_json()`` every other command uses, so it
    is pinned for real; the ``watching`` line is the one shape here that is
    written twice, which `PROTOCOL.md` says out loud.
    """
    with build_reader() as reader:
        watcher = Watcher(reader, room_ids=[DIRECT_ROOM], interval=FOLLOW_INTERVAL)
        watcher.prime()
        watching = {
            "ok": True,
            "event": "watching",
            "rooms": [str(room_id) for room_id in watcher.watched_rooms()],
            "intervalSeconds": FOLLOW_INTERVAL,
        }
        newest = reader.messages(DIRECT_ROOM, limit=1)[-1]
        return watching, {"ok": True, "event": "message", **newest.as_json()}


def build_envelopes() -> dict[str, Any]:
    """One sample of every shape a consumer has to parse."""
    room = str(DIRECT_ROOM)
    watching, message = _follow_lines()
    return {
        "rooms": _command("rooms", "--readable-only"),
        "context": _command("context", "--room-id", room),
        "messages": _command("messages", "--room-id", room, "--limit", "3"),
        "digest": _command("digest", "--room-id", room, "--span", "2h"),
        "follow_watching": watching,
        "follow_message": message,
        "error_chat_not_found": _command("context", "--room", "존재하지 않는 방"),
        "error_invalid_request": _command(
            "digest", "--room-id", room, "--span", "13h"
        ),
    }


def _serialize(payload: object) -> str:
    """Written the way `cli._emit` writes an envelope, plus a trailing line."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_envelopes() -> list[Path]:
    ENVELOPES.mkdir(exist_ok=True)
    written = []
    for name, payload in build_envelopes().items():
        path = ENVELOPES / f"{name}.json"
        path.write_text(_serialize(payload), encoding="utf-8", newline="\n")
        written.append(path)
    return written


class EnvelopeTests(unittest.TestCase):
    """Each shape still matches the sample a consumer was written against."""

    def test_every_sample_matches_what_the_command_prints_now(self) -> None:
        for name, payload in build_envelopes().items():
            with self.subTest(envelope=name):
                path = ENVELOPES / f"{name}.json"
                self.assertTrue(
                    path.exists(),
                    f"{path.name} is missing; run python -m tests.test_protocol --write",
                )
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    json.loads(json.dumps(payload)),
                    f"{name} changed; approve it by running "
                    "python -m tests.test_protocol --write",
                )

    def test_building_twice_gives_the_same_thing(self) -> None:
        """A sample that moves on its own would fail for whoever ran it next."""
        self.assertEqual(
            json.dumps(build_envelopes(), sort_keys=True, default=str),
            json.dumps(build_envelopes(), sort_keys=True, default=str),
        )


class ContractTests(unittest.TestCase):
    """The rules `PROTOCOL.md` states, asserted rather than only written down."""

    def test_every_envelope_carries_ok(self) -> None:
        for name, payload in build_envelopes().items():
            with self.subTest(envelope=name):
                self.assertIsInstance(payload, dict)
                self.assertIsInstance(payload["ok"], bool)

    def test_a_failure_carries_a_code_and_a_message(self) -> None:
        for name in ("error_chat_not_found", "error_invalid_request"):
            with self.subTest(envelope=name):
                payload = build_envelopes()[name]
                self.assertFalse(payload["ok"])
                self.assertIn("code", payload["error"])
                self.assertIn("message", payload["error"])

    def test_every_identifier_is_a_string(self) -> None:
        envelopes = build_envelopes()
        for room in envelopes["rooms"]["rooms"]:
            self.assertIsInstance(room["id"], str)
        for message in envelopes["context"]["messages"]:
            self.assertIsInstance(message["id"], str)
            self.assertIsInstance(message["roomId"], str)
            self.assertIsInstance(message["senderId"], str)

    def test_a_follow_message_is_an_ordinary_message_plus_two_keys(self) -> None:
        """The stream carries no shape of its own beyond the event wrapper."""
        envelopes = build_envelopes()
        streamed = set(envelopes["follow_message"])
        listed = set(envelopes["messages"]["messages"][-1])
        self.assertEqual(streamed - listed, {"ok", "event"})
        self.assertEqual(listed - streamed, set())

    def test_messages_are_oldest_first(self) -> None:
        listed = build_envelopes()["messages"]["messages"]
        self.assertEqual([m["id"] for m in listed], sorted(m["id"] for m in listed))


if __name__ == "__main__":
    if "--write" in sys.argv:
        for path in write_envelopes():
            print(f"wrote {path.name}")
    else:
        unittest.main()
