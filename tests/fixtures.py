"""A synthetic KakaoTalk profile, shaped like the real one.

The column names and room-type strings here were taken from a live client, so
a query that works against these fixtures is querying the real schema.  No real
conversation, contact, or key is involved.

Decryption is deliberately out of the picture: it has its own tests against
genuinely encrypted fixtures, and stubbing the snapshot layer here keeps these
tests about the part that decides what an agent is told.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from kakaoread import paths
from kakaoread.errors import ErrorCode, KakaoReadError
from kakaoread.keyring import Keyring
from kakaoread.reader import Reader

PROFILE = Path("C:/synthetic-profile")
OWN_ID = 80133957
PEER_ID = 111222333
FRIEND_ID = 444555666
STRANGER_ID = 777888999
HUGE_ID = 9007199254740993  # Past 2**53, where naive JSON parsing loses precision.

DIRECT_ROOM = 1001
GROUP_ROOM = 1002
SELF_ROOM = 1003
UNREADABLE_ROOM = 1004
HUGE_ROOM = HUGE_ID

USER_SCHEMA = """
CREATE TABLE talkUser (
    userId INTEGER, nickName TEXT, friendNickName TEXT,
    profileImageUrl TEXT, statusMessage TEXT
);
CREATE TABLE Profile (userId INTEGER, nickname TEXT, hasMeBadge INTEGER);
"""

CATALOG_SCHEMA = """
CREATE TABLE chatRoomList (
    chatId INTEGER, type TEXT, chatRoomTitle TEXT, useCustomChatRoomTitle INTEGER,
    activeMembersCount INTEGER, newMessageCount INTEGER, lastLogId INTEGER,
    lastUpdatedAt INTEGER, directChatMemberId INTEGER
);
CREATE TABLE chatMembers (chatId INTEGER, userId INTEGER, isActive INTEGER);
"""

LOG_SCHEMA = """
CREATE TABLE chatLogs (
    logId INTEGER PRIMARY KEY, authorId INTEGER, type INTEGER, message TEXT,
    attachement TEXT, sendAt INTEGER, deleted INTEGER
);
"""


def _connect(script: str, rows: Optional[dict[str, list[tuple]]] = None) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(script)
    for table, values in (rows or {}).items():
        if not values:
            continue
        placeholders = ", ".join("?" * len(values[0]))
        connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", values)
    connection.commit()
    return connection


class StubCache:
    """Stands in for SnapshotCache, handing back prepared plaintext databases."""

    def __init__(self, databases: dict[str, sqlite3.Connection]) -> None:
        self.databases = databases
        self.opened: list[str] = []

    def connect(self, path: Path, key: bytes, page_size: int = 4096, reserve: int = 80):
        name = Path(path).as_posix()
        for candidate, connection in self.databases.items():
            if name.endswith(candidate):
                self.opened.append(candidate)
                return connection
        # Match what the real cache raises when a database cannot be opened,
        # so tests cannot pass against an error type production never produces.
        raise KakaoReadError(ErrorCode.KEY_UNAVAILABLE, f"no key for {name}")

    def close(self) -> None:
        for connection in self.databases.values():
            connection.close()


def build_profile(
    *,
    with_self_room: bool = True,
    messages: Optional[list[tuple]] = None,
) -> tuple[StubCache, Keyring]:
    """Return a stub cache and key ring describing one synthetic profile."""
    users = _connect(
        USER_SCHEMA,
        {
            "talkUser": [
                (PEER_ID, "peer-nickname", "Peer Renamed", None, None),
                (FRIEND_ID, "Friend Nick", None, None, None),
                (STRANGER_ID, None, None, None, None),
                (HUGE_ID, "Huge Id User", None, None, None),
            ],
            # The signed-in account appears only here, never in talkUser.
            "Profile": [
                (OWN_ID, "My Own Name", 0),
                (STRANGER_ID, "Decoy With Badge", 1),
            ],
        },
    )

    rooms = [
        (DIRECT_ROOM, "DirectChat", "", 0, 2, 0, 5003, 3000, PEER_ID),
        (GROUP_ROOM, "MultiChat", "", 0, 3, 2, 6001, 2000, 0),
        (UNREADABLE_ROOM, "MultiChat", "Named Group", 1, 4, 0, 7001, 1500, 0),
        (HUGE_ROOM, "DirectChat", "", 0, 2, 0, HUGE_ID, 1000, HUGE_ID),
    ]
    members = [
        (DIRECT_ROOM, OWN_ID, 1),
        (DIRECT_ROOM, PEER_ID, 1),
        (GROUP_ROOM, OWN_ID, 1),
        (GROUP_ROOM, PEER_ID, 1),
        (GROUP_ROOM, FRIEND_ID, 1),
        (UNREADABLE_ROOM, OWN_ID, 1),
        (HUGE_ROOM, OWN_ID, 1),
        (HUGE_ROOM, HUGE_ID, 1),
    ]
    if with_self_room:
        rooms.append((SELF_ROOM, "MemoChat", "", 0, 1, 0, 8001, 500, 0))
        members.append((SELF_ROOM, OWN_ID, 1))

    catalog = _connect(CATALOG_SCHEMA, {"chatRoomList": rooms, "chatMembers": members})

    if messages is None:
        messages = [
            (5000, PEER_ID, 1, "first", None, 1_700_000_000, 0),
            (5001, OWN_ID, 1, "second", None, 1_700_000_100, 0),
            (5002, PEER_ID, 1, "removed", None, 1_700_000_200, 1),
            (5003, PEER_ID, 1, "third", None, 1_700_000_300, 0),
        ]
    direct_log = _connect(LOG_SCHEMA, {"chatLogs": messages})
    group_log = _connect(
        LOG_SCHEMA,
        {"chatLogs": [(6001, FRIEND_ID, 1, "group hello", None, 1_700_000_400, 0)]},
    )
    huge_log = _connect(
        LOG_SCHEMA,
        {"chatLogs": [(HUGE_ID, HUGE_ID, 1, "huge", None, 1_700_000_500, 0)]},
    )

    databases = {
        paths.USER_DATABASE: users,
        paths.CATALOG_DATABASE: catalog,
        paths.chat_log_name(DIRECT_ROOM): direct_log,
        paths.chat_log_name(GROUP_ROOM): group_log,
        paths.chat_log_name(HUGE_ROOM): huge_log,
    }
    key = bytes(32)
    keyring = Keyring(
        user_directory=PROFILE,
        pid=4321,
        keys={name: key for name in databases} | {Path(name).name: key for name in databases},
    )
    return StubCache(databases), keyring


def build_reader(**kwargs) -> Reader:
    """Return a Reader wired to a synthetic profile."""
    cache, keyring = build_profile(**kwargs)
    return Reader(user_directory=PROFILE, cache=cache, keyring=keyring)


def build_reader_on_disk(root: Path, **kwargs) -> tuple[Reader, StubCache]:
    """Materialise the profile's file layout so stat-based watching has files.

    The files hold placeholder bytes rather than databases: the stub cache
    still supplies the connections, while the paths give the watcher something
    real to observe.
    """
    import dataclasses

    cache, keyring = build_profile(**kwargs)
    for name in cache.databases:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
        wal = path.with_name(path.name + "-wal")
        wal.write_bytes(b"log")
    on_disk = dataclasses.replace(keyring, user_directory=root)
    return Reader(user_directory=root, cache=cache, keyring=on_disk), cache


def touch_log(root: Path, room_id: int, payload: bytes) -> None:
    """Change a room's log on disk so its fingerprint moves."""
    path = root / paths.chat_log_name(room_id)
    wal = path.with_name(path.name + "-wal")
    wal.write_bytes(payload)
