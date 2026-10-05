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
import os
import sys
from typing import Any, Optional, Sequence

from . import __version__
from . import digest as digest_defaults
from .context import DEFAULT_LIMIT, read_context
from .errors import ErrorCode, KamadoError, as_envelope, success
from .model import ATTACHMENT_KINDS
from .names import NameLedger
from .reader import Reader
from .watch import DEFAULT_INTERVAL_SECONDS, Watcher

DIGEST_LIMIT = digest_defaults.DEFAULT_LIMIT
"""How many of the newest messages a digest slices, matching the screen's /sum.

Taken from :mod:`kamado.digest` rather than repeated, so the flag's default and
the server's default cannot drift apart.
"""
"""How many of the newest messages a digest slices, matching the screen's /sum."""


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


def _non_negative_interval(value: str) -> float:
    parsed = float(value)
    if parsed < 0 or parsed != parsed or parsed == float("inf"):
        raise argparse.ArgumentTypeError("must be 0 or a positive finite number")
    return parsed


def _add_room_selector(parser: argparse.ArgumentParser, required: bool) -> None:
    target = parser.add_mutually_exclusive_group(required=required)
    target.add_argument("--room", help="Conversation title, exact match preferred")
    target.add_argument("--room-id", type=int, help="Numeric conversation id")


def _add_cover_toggle(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--show-covered",
        action="store_true",
        help="Include the text of messages an open chat's SafeBot covered (hidden by default)",
    )


DAEMON_COMMANDS = ("status", "verify", "rooms", "context", "messages", "friends", "query", "send")
"""The commands :meth:`daemon.DaemonServer._dispatch` answers.

Everything else runs here. Forwarding by exclusion handed the daemon every
command nobody had listed — ``digest``, ``search``, ``names`` and the three
room reads below — and it answered each with ``unknown daemon command``, which
``main`` printed as the result rather than running the command itself.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kamado",
        description="Read KakaoTalk conversations and friends, and send through the macOS client",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="Report access without requiring it to work")
    commands.add_parser("verify", help="Prove that decryption currently works")

    rooms = commands.add_parser("rooms", help="List conversations, most recent first")
    rooms.add_argument("--limit", type=_positive)
    rooms.add_argument(
        "--readable-only", action="store_true", help="Only rooms that can be decrypted"
    )

    context = commands.add_parser("context", help="Read one conversation with coverage")
    _add_room_selector(context, required=True)
    context.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    _add_cover_toggle(context)

    messages = commands.add_parser("messages", help="Read a page of messages")
    _add_room_selector(messages, required=True)
    messages.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    messages.add_argument("--before-id", type=int, help="Page backwards from this id")
    _add_cover_toggle(messages)

    digest = commands.add_parser(
        "digest", help="Slice recent conversation into time windows and describe each"
    )
    _add_room_selector(digest, required=True)
    digest.add_argument("--span", default="30m", help="Slice length such as 30m or 2h")
    digest.add_argument(
        "--limit",
        type=_positive,
        default=DIGEST_LIMIT,
        help="How many of the newest messages to slice",
    )
    digest.add_argument(
        "--highlights", type=_positive, default=3, help="Representative lines per slice"
    )
    _add_cover_toggle(digest)

    notice = commands.add_parser("notice", help="Show the notice pinned in a room")
    _add_room_selector(notice, required=True)

    attachments = commands.add_parser(
        "attachments", help="List the photos, videos, files and links a room carried"
    )
    _add_room_selector(attachments, required=True)
    attachments.add_argument("--kind", choices=ATTACHMENT_KINDS, help="Only one kind")
    attachments.add_argument(
        "--limit",
        type=_positive,
        default=50,
        help="How many of the newest messages to look through",
    )
    attachments.add_argument("--before-id", type=int, help="Page backwards from this id")
    _add_cover_toggle(attachments)

    revisions = commands.add_parser(
        "revisions", help="Show the earlier versions of an edited message"
    )
    _add_room_selector(revisions, required=True)
    revisions.add_argument("--message-id", type=int, required=True)
    _add_cover_toggle(revisions)

    friends = commands.add_parser("friends", help="List the signed-in user's friends")
    friends.add_argument("--favorites", action="store_true", help="Only 즐겨찾기")
    friends.add_argument(
        "--with-phone",
        action="store_true",
        help="Include phone numbers (withheld by default)",
    )

    send = commands.add_parser("send", help="Send text through an open KakaoTalk chat window")
    _add_room_selector(send, required=True)
    send.add_argument("text", help="Message text (quote whitespace and newlines)")
    send.add_argument("--dry-run", action="store_true", help="Resolve and check without typing")
    send.add_argument(
        "--open",
        action="store_true",
        dest="open_room",
        help="Open the room from the list first when its window is closed",
    )

    names = commands.add_parser(
        "names", help="Show what names a member of a room has gone by"
    )
    _add_room_selector(names, required=True)
    names.add_argument(
        "who",
        nargs="?",
        help="A nickname; every user who has ever carried it is returned",
    )
    names.add_argument("--user-id", type=int, help="Look one user up by identifier")

    search = commands.add_parser("search", help="Search readable conversations")
    search.add_argument("keyword")
    search.add_argument("--limit", type=_positive, default=20)
    _add_cover_toggle(search)

    follow = commands.add_parser("follow", help="Stream new messages as NDJSON")
    follow.add_argument("--room-id", type=int, action="append", dest="room_ids")
    follow.add_argument("--interval", type=_interval, default=DEFAULT_INTERVAL_SECONDS)
    _add_cover_toggle(follow)

    query = commands.add_parser("query", help="Run one read-only SQL statement")
    query.add_argument("sql")
    query.add_argument("--room-id", type=int, help="Query a room's log instead of the catalog")

    schema = commands.add_parser("schema", help="List tables and columns")
    schema.add_argument("--room-id", type=int)

    daemon = commands.add_parser("daemon", help="Manage resident background query daemon")
    daemon_sub = daemon.add_subparsers(dest="daemon_action", required=True)
    daemon_start = daemon_sub.add_parser("start", help="Start background daemon")
    daemon_start.add_argument("--foreground", action="store_true", help="Run in foreground")
    daemon_sub.add_parser("stop", help="Stop background daemon")
    daemon_sub.add_parser("status", help="Check daemon status")

    tui_cmd = commands.add_parser("tui", help="Browse conversations in a terminal interface")
    tui_cmd.add_argument(
        "--fresh",
        action="store_true",
        help="Start fresh without restoring previous session tabs or preferences",
    )

    serve = commands.add_parser(
        "serve",
        help="Expose send, reads, file downloads and a message stream over HTTP + SSE",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--token",
        default=None,
        help="Bearer token (defaults to the KAMADO_TOKEN environment variable)",
    )
    serve.add_argument(
        "--min-interval",
        type=_non_negative_interval,
        default=2.0,
        help="Minimum seconds between two sends; 0 disables (default %(default)s)",
    )
    serve.add_argument(
        "--poll-interval",
        type=_interval,
        default=1.0,
        help="Seconds between watcher polls on /stream (default %(default)s)",
    )

    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _digest(reader: Reader, arguments: argparse.Namespace) -> dict[str, Any]:
    """Hand a caller the same extractive digest the screen's /sum builds.

    The digest itself is :func:`kamado.digest.build`, so the command and the
    HTTP server ask one implementation the same question; this is the argument
    handling around it.
    """
    from .digest import build

    target = arguments.room_id if arguments.room_id is not None else arguments.room
    return success(
        build(
            reader,
            target,
            span=arguments.span,
            limit=arguments.limit,
            highlights=arguments.highlights,
        )
    )


def _selected_room(reader: Reader, arguments: argparse.Namespace):
    from .context import _resolve

    target = arguments.room_id if arguments.room_id is not None else arguments.room
    return _resolve(reader, target)


def run(arguments: argparse.Namespace, reader: Reader) -> int:
    """Execute one command and return its exit status."""
    reader.reveal_covered = bool(getattr(arguments, "show_covered", False))
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

    if arguments.command == "digest":
        _emit(_digest(reader, arguments))
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

    if arguments.command == "names":
        return _names(reader, arguments)

    if arguments.command == "notice":
        room = _selected_room(reader, arguments)
        pinned = reader.notice(room.id)
        _emit(
            success(
                {"room": room.as_json(), "notice": None if pinned is None else pinned.as_json()}
            )
        )
        return 0

    if arguments.command == "attachments":
        room = _selected_room(reader, arguments)
        carried = reader.attachments(
            room.id, kind=arguments.kind, limit=arguments.limit, before_id=arguments.before_id
        )
        _emit(
            success(
                {
                    "room": room.as_json(),
                    "attachments": [item.as_json() for item in carried],
                    "count": len(carried),
                }
            )
        )
        return 0

    if arguments.command == "revisions":
        room = _selected_room(reader, arguments)
        versions = reader.revisions(room.id, arguments.message_id)
        _emit(
            success(
                {
                    "room": room.as_json(),
                    "messageId": str(arguments.message_id),
                    "revisions": [version.as_json() for version in versions],
                    "count": len(versions),
                }
            )
        )
        return 0

    if arguments.command == "send":
        return _send(reader, arguments)

    if arguments.command == "serve":
        from . import paths, snapshot
        from .serve import (
            UploadStage,
            make_live_account_reader,
            make_live_attachments_provider,
            make_live_context_provider,
            make_live_digest_provider,
            make_live_file_provider,
            make_live_notice_provider,
            make_live_revisions_provider,
            make_live_rooms_provider,
            make_live_send,
            make_live_upload,
            make_live_watcher_factory,
            run as serve_run,
            validate_token,
        )

        token = validate_token(arguments.token or os.environ.get("KAMADO_TOKEN"))
        if arguments.host in ("0.0.0.0", "::", ""):
            print(
                "warning: serving on a wildcard address exposes the bearer "
                "token to every machine that can reach this port. Put a "
                "reverse proxy in front or use SSH port forwarding.",
                file=sys.stderr,
                flush=True,
            )
        # A long-running server pays the temporary-file path on every refresh,
        # and a previous run killed by launchd may have left its copies behind.
        snapshot.warn_if_plaintext_fallback()
        snapshot.sweep_orphans()
        # Texts and files share one line, so they share the warm reader too.
        send_reader = make_live_account_reader()
        return serve_run(
            host=arguments.host,
            port=arguments.port,
            send=make_live_send(send_reader),
            token=token,
            min_interval=arguments.min_interval,
            rooms_provider=make_live_rooms_provider(lambda: Reader()),
            context_provider=make_live_context_provider(lambda: Reader()),
            digest_provider=make_live_digest_provider(lambda: Reader()),
            watcher_factory=make_live_watcher_factory(
                lambda: Reader(), interval=arguments.poll_interval
            ),
            poll_interval=arguments.poll_interval,
            notice_provider=make_live_notice_provider(lambda: Reader()),
            attachments_provider=make_live_attachments_provider(lambda: Reader()),
            revisions_provider=make_live_revisions_provider(lambda: Reader()),
            file_provider=make_live_file_provider(lambda: Reader()),
            upload=make_live_upload(send_reader),
            upload_stage=UploadStage(paths.state_directory() / "uploads"),
        )

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
                    "note": "Search covers the messages KakaoTalk has cached locally; the "
                    "client keeps only recent history on this machine, so an empty result "
                    "is not proof a conversation never happened.",
                }
            )
        )
        return 0

    if arguments.command == "tui":
        # Deliberately not handed this reader: see :func:`_tui`.
        return _tui(arguments)

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

    if arguments.command == "daemon":
        from .daemon import DaemonServer

        server = DaemonServer()
        if arguments.daemon_action == "start":
            return server.start(foreground=arguments.foreground)
        if arguments.daemon_action == "stop":
            return server.stop()
        if arguments.daemon_action == "status":
            return server.status()

    raise KamadoError(ErrorCode.INVALID_REQUEST, f"unsupported command {arguments.command}")


def _names(reader: Reader, arguments: argparse.Namespace) -> int:
    """Answer who has gone by what, for one user or for a whole room.

    Three questions, one command.  An identifier asks for one member's history.
    A nickname asks the reverse question — which accounts have worn this name —
    and more than one answer is the point rather than an ambiguity to refuse,
    because that is what one account imitating another looks like from here.
    With neither, the room's renamers are listed, which is the only view that
    is useful without already knowing who to suspect.
    """
    from .names import steps_with_current

    room = _selected_room(reader, arguments)
    history = reader.name_history(room.id)
    current = reader.names()

    if arguments.user_id is not None:
        selected = [int(arguments.user_id)]
    elif arguments.who:
        selected = reader.users_named(room.id, arguments.who, history)
    else:
        selected = sorted(user_id for user_id, steps in history.items() if len(steps) > 1)

    found = [
        {
            "userId": str(user_id),
            "current": current.get(user_id),
            "history": [
                record.as_json()
                for record in steps_with_current(
                    user_id, history.get(user_id, ()), current.get(user_id)
                )
            ],
        }
        for user_id in selected
    ]
    _emit(
        success(
            {
                "room": room.as_json(),
                "users": found,
                "count": len(found),
                "note": "The client records a name only when someone joins, leaves or is "
                "invited; a plain rename is recorded here only from the first read after "
                "it happened.",
            }
        )
    )
    return 0


def _send(reader: Reader, arguments: argparse.Namespace) -> int:
    """Resolve the catalog before targeting an exact client window."""
    from .macauto import send_to_room

    target = arguments.room_id if arguments.room_id is not None else arguments.room
    result = send_to_room(
        reader,
        target,
        arguments.text,
        dry_run=arguments.dry_run,
        open_room=arguments.open_room,
    )
    _emit(success(result))
    return 0


def _tui(arguments: Optional[argparse.Namespace] = None) -> int:
    """Hand the terminal to the browser, restoring it however it ends.

    The interface's reader is opened *by the worker, on the worker's thread*,
    and closed by it there. It is deliberately not the reader this command was
    given: that one is closed by :func:`main` on this thread when the browser
    returns, and quitting while a decryption is still running is exactly the
    case where that close would land on a database another thread is reading.
    Two readers is the price of one owner, and the one this command was handed
    has read nothing by the time the browser starts.

    The console is entered first so that it is left last: the database is
    given up while the screen still holds the last frame, rather than after
    the terminal has been handed back to a shell that then appears to hang.
    """
    from .tui.app import App
    from .tui.console import Console, supports_tui
    from .tui.worker import DatabaseWorker

    if not supports_tui():
        raise KamadoError(
            ErrorCode.INVALID_REQUEST,
            "the terminal interface needs an interactive terminal; "
            "run it directly rather than through a pipe",
        )
    # The ledger is switched on here for the same reason main switches it on:
    # this is certainly reading the live database, and a rename is invisible
    # unless something writes down what it saw.
    restore = not bool(arguments and getattr(arguments, "fresh", False))
    with Console() as console, DatabaseWorker(
        open_reader=lambda: Reader(ledger=NameLedger())
    ) as owner:
        App(db=owner, restore=restore).run(console)
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

    # If the background daemon is running and this is a query command, forward it!
    # ``serve`` joins this list for the same reason ``follow`` is on it: the
    # daemon answers one query and returns, and a server that is supposed to
    # hold a socket open would exit having printed the daemon's answer.
    if arguments.command in DAEMON_COMMANDS:
        from .daemon import is_daemon_running, send_to_daemon

        if is_daemon_running():
            res = send_to_daemon(
                arguments.command,
                vars(arguments),
                repeatable=arguments.command != "send",
            )
            if res is not None:
                _emit(res)
                return 0 if res.get("ok") else 1

    try:
        # The real entry point is where the name ledger is switched on: a
        # rename is invisible unless something writes down what it saw, and
        # this is the one place that is certainly reading the live databases.
        with Reader(ledger=NameLedger()) as reader:
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
