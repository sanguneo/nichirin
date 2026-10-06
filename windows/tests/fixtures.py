"""A synthetic KakaoTalk profile, shaped like the real one.

The column names and room-type strings here were taken from a live client, so
a query that works against these fixtures is querying the real schema.  No real
conversation, contact, or key is involved.

Decryption is deliberately out of the picture: it has its own tests against
genuinely encrypted fixtures, and stubbing the snapshot layer here keeps these
tests about the part that decides what an agent is told.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from kanao import paths
from kanao.errors import ErrorCode, KanaoError
from kanao.keyring import Keyring
from kanao.reader import Reader

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
    userId INTEGER, type INTEGER, nickName TEXT, friendNickName TEXT,
    phoneNumber TEXT, statusMessage TEXT, directChatId INTEGER, favorite INTEGER
);
CREATE TABLE Profile (userId INTEGER, nickname TEXT, hasMeBadge INTEGER);
"""

PICTURE_COLUMNS = """
ALTER TABLE talkUser ADD COLUMN profileImageUrl TEXT;
ALTER TABLE Profile ADD COLUMN profileImageInfo TEXT;
"""
"""The two columns a live client keeps profile pictures in, added on request.

Left out by default, so every other test also runs against a client whose
tables have no picture at all — which must answer, not fail.
"""

PEER_PICTURE = "https://p.kakaocdn.net/synthetic/peer.jpg"
FRIEND_PICTURE = "https://chat.kakaocdn.net/synthetic/friend.jpg"
OWN_PICTURE = "https://p.kakaocdn.net/synthetic/own.jpg"
ROOM_PICTURE = "https://chat.kakaocdn.net/synthetic/room.jpg"
LINK_PICTURE = "https://open.kakaocdn.net/synthetic/link.jpg"
"""Made-up addresses on the hosts measured live."""

OPEN_ROOM = 1005
OPEN_LINK = 900
PICTURE_CATALOG = """
ALTER TABLE chatRoomList ADD COLUMN linkId INTEGER;
CREATE TABLE chatMetas (
    chatId INTEGER, type INTEGER, revision INTEGER, authorId INTEGER,
    content TEXT, updateAt INTEGER, extra TEXT, noticeRevision INTEGER
);
"""
OPEN_LINK_SCHEMA = """
CREATE TABLE openLinkList (linkId INTEGER, linkName TEXT, linkImageUrl TEXT);
"""

FRIEND_TYPE = 1
NON_FRIEND_TYPE = 1000
SELF_STORED_TITLE = "계정본인이름"

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
    attachement TEXT, sendAt INTEGER, deleted INTEGER, threadId INTEGER
);
"""

REPLY_TYPE = 26
"""The client's type for a message written against another one."""

MENTION_OF_OWNER = json.dumps({"mentions": [{"at": [0], "len": 12, "user_id": OWN_ID}]})
"""A mention as the client writes it: an id and where the name sits."""

PHOTO_TYPE = 2
VIDEO_TYPE = 3
PHOTO_ATTACHMENT = json.dumps(
    {
        "url": "https://talk.kakaocdn.net/synthetic/full.jpg",
        "thumbnailUrl": "https://talk.kakaocdn.net/synthetic/thumb.jpg",
        "w": 400,
        "h": 300,
    }
)
"""A made-up address on the real CDN host, so the whitelist is exercised."""

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


def _connect(script: str, rows: Optional[dict[str, list[tuple]]] = None) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(script)
    for table, values in (rows or {}).items():
        if not values:
            continue
        # Rows name the columns they fill, left to right, so a row may stop
        # short of a column the client added later — and one row carrying that
        # column does not oblige every other row to restate it.
        width = max(len(value) for value in values)
        names = [column[1] for column in connection.execute(f"PRAGMA table_info({table})")]
        filled = ", ".join(names[:width])
        placeholders = ", ".join("?" * width)
        connection.executemany(
            f"INSERT INTO {table} ({filled}) VALUES ({placeholders})",
            [tuple(value) + (None,) * (width - len(value)) for value in values],
        )
    connection.commit()
    return connection


class StubCache:
    """Stands in for SnapshotCache, handing back prepared plaintext databases."""

    def __init__(self, databases: dict[str, sqlite3.Connection]) -> None:
        self.databases = databases
        self.opened: list[str] = []

    def connect(self, database: Path, key: bytes, page_size: int = 4096, reserve: int = 80):
        name = Path(database).as_posix()
        for candidate, connection in self.databases.items():
            if name.endswith(candidate):
                self.opened.append(candidate)
                return connection
        # Match what the real cache raises when a database cannot be opened,
        # so tests cannot pass against an error type production never produces.
        raise KanaoError(ErrorCode.KEY_UNAVAILABLE, f"no key for {name}")

    def close(self) -> None:
        for connection in self.databases.values():
            connection.close()


def build_profile(
    *,
    with_self_room: bool = True,
    messages: Optional[list[tuple]] = None,
    pictures: bool = False,
) -> tuple[StubCache, Keyring]:
    """Return a stub cache and key ring describing one synthetic profile."""
    users = _connect(
        USER_SCHEMA + (PICTURE_COLUMNS if pictures else ""),
        {
            "talkUser": [
                # userId, type, nickName, friendNickName, phone, status, directChatId, favorite
                (PEER_ID, FRIEND_TYPE, "peer-nickname", "Peer Renamed", "01011112222", "hi", DIRECT_ROOM, 1),
                (FRIEND_ID, FRIEND_TYPE, "Friend Nick", None, None, None, None, 0),
                (STRANGER_ID, NON_FRIEND_TYPE, None, None, None, None, None, 0),
                (HUGE_ID, NON_FRIEND_TYPE, "Huge Id User", None, None, None, None, 0),
            ],
            # The signed-in account appears only here, never in talkUser.
            "Profile": [
                (OWN_ID, "My Own Name", 0),
                (STRANGER_ID, "Decoy With Badge", 1),
            ],
        },
    )
    if pictures:
        users.executemany(
            "UPDATE talkUser SET profileImageUrl = ? WHERE userId = ?",
            [
                (PEER_PICTURE, PEER_ID),
                (FRIEND_PICTURE, FRIEND_ID),
                # Neither is the client's CDN over HTTPS, so neither is passed on.
                ("http://p.kakaocdn.net/synthetic/plain.jpg", STRANGER_ID),
                ("https://example.invalid/synthetic/elsewhere.jpg", HUGE_ID),
            ],
        )
        users.execute(
            "UPDATE Profile SET profileImageInfo = ? WHERE userId = ?",
            (json.dumps({"thumbnailUrl": OWN_PICTURE, "originalUrl": ""}), OWN_ID),
        )
        users.commit()

    rooms = [
        (DIRECT_ROOM, "DirectChat", "", 0, 2, 0, 5003, 3000, PEER_ID),
        (GROUP_ROOM, "MultiChat", "", 0, 3, 2, 6005, 2000, 0),
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
        # The client stores the self-chat under the account's own name, which
        # is also what its window is titled; the reader shows 나와의 채팅.
        rooms.append((SELF_ROOM, "MemoChat", SELF_STORED_TITLE, 0, 1, 0, 8001, 500, 0))
        members.append((SELF_ROOM, OWN_ID, 1))

    if pictures:
        # An open group whose picture is its link's, not its members'.
        rooms.append((OPEN_ROOM, "OM", "Open Room", 1, 3, 0, 9001, 400, 0, OPEN_LINK))
        members.extend([(OPEN_ROOM, OWN_ID, 1), (OPEN_ROOM, FRIEND_ID, 1)])
    catalog = _connect(
        CATALOG_SCHEMA + (PICTURE_CATALOG if pictures else ""),
        {"chatRoomList": rooms, "chatMembers": members},
    )
    if pictures:
        catalog.executemany(
            "INSERT INTO chatMetas (chatId, type, content) VALUES (?, ?, ?)",
            [
                # A photo a member set for the named group.
                (UNREADABLE_ROOM, 4, json.dumps({"imageUrl": ROOM_PICTURE, "fullImageUrl": ROOM_PICTURE})),
                # A chat background is not the room's icon.
                (GROUP_ROOM, 8, json.dumps({"chatBackgroundImageUrl": ROOM_PICTURE})),
            ],
        )
        catalog.commit()

    if messages is None:
        messages = [
            (5000, PEER_ID, 1, "first", None, 1_700_000_000, 0),
            (5001, OWN_ID, 1, "second", None, 1_700_000_100, 0),
            (5002, PEER_ID, 1, "removed", None, 1_700_000_200, 1),
            (5003, PEER_ID, 1, "third", None, 1_700_000_300, 0),
        ]
    direct_log = _connect(LOG_SCHEMA, {"chatLogs": messages})
    # The group is where being addressed happens: a mention names an id the
    # text does not carry, and a reply carries the message it answers.
    group_log = _connect(
        LOG_SCHEMA,
        {
            "chatLogs": [
                (6001, FRIEND_ID, 1, "group hello", None, 1_700_000_400, 0),
                (6002, OWN_ID, 1, "내가 남긴 줄", None, 1_700_000_410, 0),
                (6003, PEER_ID, 1, "@My Own Name 이것 좀 봐줘", MENTION_OF_OWNER, 1_700_000_420, 0),
                (6004, FRIEND_ID, REPLY_TYPE, "그거 맞아요", REPLY_TO_OWNER, 1_700_000_430, 0),
                (6005, PEER_ID, 1, "@전체 공지입니다", MENTION_OF_EVERYONE, 1_700_000_440, 0),
                (6006, FRIEND_ID, PHOTO_TYPE, "사진", PHOTO_ATTACHMENT, 1_700_000_450, 0),
                (6007, FRIEND_ID, VIDEO_TYPE, "동영상", PHOTO_ATTACHMENT, 1_700_000_460, 0),
            ]
        },
    )
    self_log = _connect(
        LOG_SCHEMA,
        {"chatLogs": [(8001, OWN_ID, 1, "메모", None, 1_700_000_600, 0)]},
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
        paths.chat_log_name(SELF_ROOM): self_log,
    }
    if pictures:
        databases[paths.OPEN_LINK_DATABASE] = _connect(
            OPEN_LINK_SCHEMA, {"openLinkList": [(OPEN_LINK, "Open Room", LINK_PICTURE)]}
        )
    key = bytes(32)
    keyring = Keyring(
        user_directory=PROFILE,
        pid=4321,
        keys={name: key for name in databases} | {Path(name).name: key for name in databases},
    )
    return StubCache(databases), keyring


def build_reader(ledger=None, **kwargs) -> Reader:
    """Return a Reader wired to a synthetic profile.

    *ledger* stays None unless a test asks for one, so no test can append a
    synthetic name to the real history under ``%LOCALAPPDATA%``.
    """
    cache, keyring = build_profile(**kwargs)
    reader = Reader(user_directory=PROFILE, cache=cache, keyring=keyring, ledger=ledger)
    # A reader does not close a cache it was handed — the server's send path
    # shares one across requests. Nobody shares this one, so the reader is
    # given it outright; otherwise every test here leaves its synthetic
    # databases open (232 of them, counted).
    reader._owns_cache = True
    return reader


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
    reader = Reader(user_directory=root, cache=cache, keyring=on_disk)
    reader._owns_cache = True  # as in build_reader: nobody else holds this one
    return reader, cache


def touch_log(root: Path, room_id: int, payload: bytes) -> None:
    """Change a room's log on disk so its fingerprint moves."""
    path = root / paths.chat_log_name(room_id)
    wal = path.with_name(path.name + "-wal")
    wal.write_bytes(payload)
