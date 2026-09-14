"""The command line, where every command answers in the same shape.

Success is ``{"ok": true, ...}`` and failure is ``{"ok": false, "error":
{"code", "message"}}`` for *every* command, so an agent branches on one
contract instead of learning which commands print JSON and which print prose to
stderr.  Exit status is 0 for success, 1 for a handled failure, and argparse's
2 for a malformed invocation.

Rooms are selected by ``--room`` (title) or ``--room-id`` (identifier) and
never by a value that could be either, because silently deciding that a room
titled "123" meant room 123 is exactly the class of guess this tool refuses.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from typing import Any, Optional, Sequence

from . import __version__
from .context import DEFAULT_LIMIT, read_context
from .errors import ErrorCode, KakaoReadError, as_envelope, success
from .reader import Reader
from .watch import DEFAULT_INTERVAL_SECONDS, Watcher


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _interval(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _add_room_selector(parser: argparse.ArgumentParser, required: bool) -> None:
    target = parser.add_mutually_exclusive_group(required=required)
    target.add_argument("--room", help="Conversation title, exact match preferred")
    target.add_argument("--room-id", type=int, help="Numeric conversation id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kakaoread",
        description="Read local KakaoTalk conversations on Windows (read-only)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="Report access without requiring it to work")
    commands.add_parser("verify", help="Prove that decryption currently works")

    rooms = commands.add_parser("rooms", help="List conversations, most recent first")
    rooms.add_argument("--limit", type=_positive)
    rooms.add_argument(
        "--readable-only", action="store_true", help="Only rooms whose key is loaded"
    )

    context = commands.add_parser("context", help="Read one conversation with coverage")
    _add_room_selector(context, required=True)
    context.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)

    messages = commands.add_parser("messages", help="Read a page of messages")
    _add_room_selector(messages, required=True)
    messages.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    messages.add_argument("--before-id", type=int, help="Page backwards from this id")

    friends = commands.add_parser("friends", help="List the signed-in user's friends")
    friends.add_argument("--favorites", action="store_true", help="Only 즐겨찾기")
    friends.add_argument(
        "--with-phone",
        action="store_true",
        help="Include phone numbers (withheld by default)",
    )

    search = commands.add_parser("search", help="Search readable conversations")
    search.add_argument("keyword")
    search.add_argument("--limit", type=_positive, default=20)

    follow = commands.add_parser("follow", help="Stream new messages as NDJSON")
    follow.add_argument("--room-id", type=int, action="append", dest="room_ids")
    follow.add_argument("--interval", type=_interval, default=DEFAULT_INTERVAL_SECONDS)

    query = commands.add_parser("query", help="Run one read-only SQL statement")
    query.add_argument("sql")
    query.add_argument("--room-id", type=int, help="Query a room's log instead of the catalog")

    schema = commands.add_parser("schema", help="List tables and columns")
    schema.add_argument("--room-id", type=int)

    commands.add_parser("tui", help="Browse conversations in a terminal interface")

    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _selected_room(reader: Reader, arguments: argparse.Namespace):
    from .context import _resolve

    target = arguments.room_id if arguments.room_id is not None else arguments.room
    return _resolve(reader, target)


def run(arguments: argparse.Namespace, reader: Reader) -> int:
    """Execute one command and return its exit status."""
    if arguments.command == "status":
        _emit(success(reader.status()))
        return 0

    if arguments.command == "verify":
        _emit(success(reader.verify()))
        return 0

    if arguments.command == "rooms":
        rooms = reader.rooms(limit=None if arguments.readable_only else arguments.limit)
        if arguments.readable_only:
            rooms = [room for room in rooms if room.readable]
            if arguments.limit is not None:
                rooms = rooms[: arguments.limit]
        _emit(success({"rooms": [room.as_json() for room in rooms], "count": len(rooms)}))
        return 0

    if arguments.command == "context":
        target = arguments.room_id if arguments.room_id is not None else arguments.room
        _emit(read_context(reader, target, arguments.limit))
        return 0

    if arguments.command == "messages":
        room = _selected_room(reader, arguments)
        found = reader.messages(room.id, limit=arguments.limit, before_id=arguments.before_id)
        _emit(
            success(
                {
                    "room": room.as_json(),
                    "messages": [message.as_json() for message in found],
                    "count": len(found),
                }
            )
        )
        return 0

    if arguments.command == "friends":
        friends = reader.friends(favorites_only=arguments.favorites)
        _emit(
            success(
                {
                    "friends": [friend.as_json(include_phone=arguments.with_phone) for friend in friends],
                    "count": len(friends),
                }
            )
        )
        return 0

    if arguments.command == "search":
        rooms = reader.rooms()
        unreadable = sum(1 for room in rooms if not room.readable)
        found = list(reader.search(arguments.keyword, limit=arguments.limit))
        _emit(
            success(
                {
                    "matches": [message.as_json() for message in found],
                    "count": len(found),
                    "unsearchableRooms": unreadable,
                    "note": "Rooms KakaoTalk has not opened have no key and were not searched; "
                    "an empty result is not proof a conversation never happened.",
                }
            )
        )
        return 0

    if arguments.command == "tui":
        return _tui(reader)

    if arguments.command == "follow":
        return _follow(reader, arguments)

    if arguments.command == "query":
        rows = reader.query(arguments.sql, room_id=arguments.room_id)
        _emit(success({"rows": rows, "count": len(rows)}))
        return 0

    if arguments.command == "schema":
        tables = reader.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
            room_id=arguments.room_id,
        )
        described: dict[str, Any] = {}
        for table in tables:
            name = str(table["name"])
            escaped = name.replace('"', '""')
            described[name] = [
                {"name": column["name"], "type": column["type"]}
                for column in reader.query(
                    f'PRAGMA table_info("{escaped}")', room_id=arguments.room_id
                )
            ]
        _emit(success({"tables": described}))
        return 0

    raise KakaoReadError(ErrorCode.INVALID_REQUEST, f"unsupported command {arguments.command}")


def _tui(reader: Reader) -> int:
    """Hand the terminal to the browser, restoring it however it ends."""
    from .tui.app import App
    from .tui.console import Console, supports_tui

    if not supports_tui():
        raise KakaoReadError(
            ErrorCode.INVALID_REQUEST,
            "the terminal interface needs an interactive Windows console; "
            "run it directly rather than through a pipe",
        )
    with Console() as console:
        App(reader).run(console)
    return 0


def _follow(reader: Reader, arguments: argparse.Namespace) -> int:
    """Stream one JSON object per line so a consumer can read incrementally."""
    watcher = Watcher(reader, room_ids=arguments.room_ids, interval=arguments.interval)
    watcher.prime()
    watched = watcher.watched_rooms()
    print(
        json.dumps(
            {
                "ok": True,
                "event": "watching",
                "rooms": [str(room_id) for room_id in watched],
                "intervalSeconds": arguments.interval,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        for message in watcher.follow():
            print(
                json.dumps(
                    {"ok": True, "event": "message", **message.as_json()}, ensure_ascii=False
                ),
                flush=True,
            )
    except KeyboardInterrupt:
        return 0
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, run one command, and report failures in the same shape."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    arguments = build_parser().parse_args(argv)
    try:
        with Reader() as reader:
            return run(arguments, reader)
    except KeyboardInterrupt:
        return 130
    except BaseException as error:  # noqa: BLE001 - reported, never swallowed
        if isinstance(error, SystemExit):
            raise
        _emit(as_envelope(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
