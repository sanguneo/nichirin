"""A synthetic KakaoTalk database, shaped like the real one.

The table and column names here follow the macOS client's schema, so a query
that works against these fixtures is querying the real shape.  No real
conversation, contact, or key is involved.

Decryption is deliberately out of the picture: it has its own tests against
genuinely encrypted fixtures, and stubbing the snapshot layer here keeps these
tests about the part that decides what an agent is told.

One structural difference from the Windows fixtures this was forked from: there
is a single database rather than a catalog plus one file per room, so a room is
no longer "unreadable" for want of a key.  What used to be the unreadable room
is now the one with a custom title, which is the other thing it was testing.
"""

from __future__ import annotations

import json
import plistlib
import sqlite3
from pathlib import Path
from typing import Optional

from kakaoread.cipher import Geometry
from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.keyring import Account, Keyring
from kakaoread.reader import Reader

from . import support

CONTAINER = Path("/synthetic/container")
DATABASE_NAME = "a" * 78
DATABASE = CONTAINER / DATABASE_NAME

OWN_ID = 80133957
PEER_ID = 111222333
FRIEND_ID = 444555666
STRANGER_ID = 777888999
HUGE_ID = 9007199254740993  # Past 2**53, where naive JSON parsing loses precision.

DIRECT_ROOM = 1001
GROUP_ROOM = 1002
SELF_ROOM = 1003
NAMED_ROOM = 1004
OPEN_ROOM = 1005
HUGE_ROOM = HUGE_ID

OPEN_LINK_ID = 5150
OPEN_LINK_NAME = "합성 오픈채팅"
NAMED_ROOM_TITLE = "Named Group"

FRIEND_TYPE = 1
NON_FRIEND_TYPE = 1000

SCHEMA = """
CREATE TABLE NTChatRoom (
    chatId INTEGER PRIMARY KEY, type INTEGER, chatName TEXT,
    activeMembersCount INTEGER, countOfNewMessage INTEGER, lastLogId INTEGER,
    lastUpdatedAt INTEGER, directChatMemberUserId INTEGER, linkId INTEGER,
    displayMemberIds BLOB
);
CREATE TABLE NTChatMessage (
    logId INTEGER PRIMARY KEY, chatId INTEGER, authorId INTEGER, type INTEGER,
    message TEXT, attachment TEXT, sentAt INTEGER, deleted INTEGER
);
CREATE TABLE NTUser (
    userId INTEGER, linkId INTEGER, type INTEGER, nickName TEXT,
    displayName TEXT, friendNickName TEXT, phoneNumber TEXT, statusMessage TEXT,
    directChatId INTEGER, favorite INTEGER
);
CREATE TABLE NTOpenLink (linkId INTEGER, linkName TEXT);
CREATE TABLE NTChatContext (userId INTEGER);
"""

REPLY_TYPE = 26
"""The client's type for a message written against another one."""

MENTION_OF_OWNER = json.dumps({"mentions": [{"at": [0], "len": 12, "user_id": OWN_ID}]})
"""A mention as the client writes it: an id and where the name sits."""

MENTION_OF_EVERYONE = json.dumps({"all_mention": {"at": 0}})
"""An @전체, which records no ids at all — only that the token was used."""

REPLY_TO_OWNER = json.dumps(
    {
        "src_logId": 6002,
        "src_userId": OWN_ID,
        "src_type": 1,
        "src_message": "내가 남긴 줄",
        "src_linkId": 0,
    }
)
"""A reply carrying its own copy of the message it answers."""


def members_blob(user_ids: list[int]) -> bytes:
    """Archive a member list the way the client stores one: a binary plist."""
    return plistlib.dumps(user_ids, fmt=plistlib.FMT_BINARY)


def _connect(rows: Optional[dict[str, list[tuple]]] = None) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    for table, values in (rows or {}).items():
        if not values:
            continue
        placeholders = ", ".join("?" * len(values[0]))
        connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", values)
    connection.commit()
    return connection


class StubCache:
    """Stands in for SnapshotCache, handing back a prepared plaintext database."""

    def __init__(self, database: sqlite3.Connection, path: Path = DATABASE) -> None:
        self.database = database
        self.path = path
        self.opened: list[str] = []

    def connect(self, path: Path, geometry: Geometry) -> sqlite3.Connection:
        if Path(path).name != self.path.name:
            # Match what the real cache raises when a database cannot be
            # opened, so tests cannot pass against an error type production
            # never produces.
            raise KakaoReadError(ErrorCode.KEY_UNAVAILABLE, f"no key for {path}")
        self.opened.append(Path(path).name)
        return self.database

    def close(self) -> None:
        self.database.close()


def build_database(
    *, with_self_room: bool = True, messages: Optional[list[tuple]] = None
) -> sqlite3.Connection:
    """Return one synthetic database holding every room and message."""
    users = [
        # userId, linkId, type, nickName, displayName, friendNickName,
        # phone, status, directChatId, favorite
        (PEER_ID, 0, FRIEND_TYPE, "peer-nickname", "peer-display", "Peer Renamed",
         "01011112222", "hi", DIRECT_ROOM, 1),
        (FRIEND_ID, 0, FRIEND_TYPE, "Friend Nick", None, None, None, None, None, 0),
        (STRANGER_ID, 0, NON_FRIEND_TYPE, "Stranger", None, None, None, None, None, 0),
        (HUGE_ID, 0, NON_FRIEND_TYPE, "Huge Id User", None, None, None, None, None, 0),
        # Unlike the Windows client, this schema carries the signed-in account
        # in the same table as everybody else.
        (OWN_ID, 0, NON_FRIEND_TYPE, "My Own Name", None, None, None, None, None, 0),
    ]

    rooms = [
        (DIRECT_ROOM, 0, "", 2, 0, 5003, 3000, PEER_ID, 0,
         members_blob([OWN_ID, PEER_ID])),
        (GROUP_ROOM, 1, "", 3, 2, 6005, 2000, 0, 0,
         members_blob([OWN_ID, PEER_ID, FRIEND_ID])),
        (NAMED_ROOM, 1, NAMED_ROOM_TITLE, 4, 0, 7001, 1500, 0, 0,
         members_blob([OWN_ID, STRANGER_ID])),
        (OPEN_ROOM, 2, "", 9, 0, 9001, 1200, 0, OPEN_LINK_ID,
         members_blob([OWN_ID, STRANGER_ID])),
        (HUGE_ROOM, 0, "", 2, 0, HUGE_ID, 1000, HUGE_ID, 0,
         members_blob([OWN_ID, HUGE_ID])),
    ]
    if with_self_room:
        # The account alone in a room is what makes it the self-chat; the
        # client stores no title for it and the reader shows 나와의 채팅.
        rooms.append((SELF_ROOM, 5, "", 1, 0, 8001, 500, 0, 0, members_blob([OWN_ID])))

    if messages is None:
        messages = [
            (5000, DIRECT_ROOM, PEER_ID, 1, "first", None, 1_700_000_000, 0),
            (5001, DIRECT_ROOM, OWN_ID, 1, "second", None, 1_700_000_100, 0),
            (5002, DIRECT_ROOM, PEER_ID, 1, "removed", None, 1_700_000_200, 1),
            (5003, DIRECT_ROOM, PEER_ID, 1, "third", None, 1_700_000_300, 0),
        ]

    # The group is where being addressed happens: a mention names an id the
    # text does not carry, and a reply carries the message it answers.
    messages = list(messages) + [
        (6001, GROUP_ROOM, FRIEND_ID, 1, "group hello", None, 1_700_000_400, 0),
        (6002, GROUP_ROOM, OWN_ID, 1, "내가 남긴 줄", None, 1_700_000_410, 0),
        (6003, GROUP_ROOM, PEER_ID, 1, "@My Own Name 이것 좀 봐줘",
         MENTION_OF_OWNER, 1_700_000_420, 0),
        (6004, GROUP_ROOM, FRIEND_ID, REPLY_TYPE, "그거 맞아요",
         REPLY_TO_OWNER, 1_700_000_430, 0),
        (6005, GROUP_ROOM, PEER_ID, 1, "@전체 공지입니다",
         MENTION_OF_EVERYONE, 1_700_000_440, 0),
        (HUGE_ID, HUGE_ROOM, HUGE_ID, 1, "huge", None, 1_700_000_500, 0),
    ]
    if with_self_room:
        messages.append((8001, SELF_ROOM, OWN_ID, 1, "메모", None, 1_700_000_600, 0))

    return _connect(
        {
            "NTUser": users,
            "NTChatRoom": rooms,
            "NTChatMessage": messages,
            "NTOpenLink": [(OPEN_LINK_ID, OPEN_LINK_NAME)],
            "NTChatContext": [(OWN_ID,)],
        }
    )


def build_keyring(database: Path = DATABASE) -> Keyring:
    """Return a key ring naming one already-resolved synthetic account."""
    return Keyring(
        container=database.parent,
        databases=[database],
        account=Account(user_id=OWN_ID, database=database, geometry=support.geometry()),
    )


def build_profile(**kwargs) -> tuple[StubCache, Keyring]:
    """Return a stub cache and key ring describing one synthetic account."""
    return StubCache(build_database(**kwargs)), build_keyring()


def build_reader(ledger=None, **kwargs) -> Reader:
    """Return a Reader wired to a synthetic database.

    *ledger* stays None unless a test asks for one, so no test can append a
    synthetic name to the real history under Application Support.
    """
    cache, keyring = build_profile(**kwargs)
    return Reader(cache=cache, keyring=keyring, ledger=ledger)


def build_reader_on_disk(root: Path, **kwargs) -> tuple[Reader, StubCache]:
    """Materialise the file layout so stat-based watching has files to watch.

    The file holds placeholder bytes rather than a database: the stub cache
    still supplies the connection, while the path gives the watcher something
    real to observe.
    """
    database = root / DATABASE_NAME
    root.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"placeholder")
    database.with_name(database.name + "-wal").write_bytes(b"log")

    cache = StubCache(build_database(**kwargs), path=database)
    return Reader(cache=cache, keyring=build_keyring(database)), cache


def touch_log(root: Path, room_id: int, payload: bytes) -> None:
    """Change the database on disk so its fingerprint moves.

    The room id is accepted and ignored: on macOS every room shares one file,
    so any room's arrival moves the same fingerprint.
    """
    database = root / DATABASE_NAME
    database.with_name(database.name + "-wal").write_bytes(payload)
