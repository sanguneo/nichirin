"""A synthetic profile carrying the tables the newer client writes.

``tests/fixtures.py`` models the schema every build has. This adds what the
reads for 공감, edits, notices, attachments and SafeBot need: ``chatLogMeta``,
``chatLogHistory`` and the ``revision`` column in the group's log, and
``chatMetas`` and ``moimMetas`` in the catalog. Column names are the live
client's; every value is made up.
"""

from __future__ import annotations

import json

from kanao import paths
from kanao.reader import Reader

from tests.fixtures import (
    DIRECT_ROOM,
    FRIEND_ID,
    GROUP_ROOM,
    OWN_ID,
    PEER_ID,
    PHOTO_ATTACHMENT,
    SELF_ROOM,
    _connect,
    build_reader,
)

LOG_SCHEMA = """
CREATE TABLE chatLogs (
    logId INTEGER PRIMARY KEY, authorId INTEGER, type INTEGER, message TEXT,
    attachement TEXT, sendAt INTEGER, deleted INTEGER, threadId INTEGER,
    is_encrypt_delete_msg INTEGER, revision INTEGER
);
CREATE TABLE chatLogMeta (
    logId INTEGER NOT NULL, revision INTEGER NOT NULL, extra_revision INTEGER NOT NULL,
    type INTEGER NOT NULL, extra_state INTEGER NOT NULL, content TEXT, extra TEXT,
    PRIMARY KEY (logId, type)
);
CREATE TABLE chatLogHistory (
    logId INTEGER, revision INTEGER, authorId INTEGER, type INTEGER, message TEXT,
    attachment TEXT, referer TEXT, PRIMARY KEY (logId, revision)
);
"""

REACTED = 9001
EDITED = 9002
EDIT_FEED = 9003
COVERED = 9004
LINKS = 9005
FILE = 9006
PHOTO = 9007
ERASED = 9008
COVERED_LINK = 9009
REPLY_TO_COVERED = 9010

FILE_URL = "https://talk.kakaocdn.net/synthetic/report.pdf"
FILE_NAME = "보고서.pdf"
FILE_SIZE = 2048
FILE_EXPIRES = 1_900_000_000_000
"""Milliseconds, like the client writes; a test's clock decides if it has passed."""

SAFEBOT = json.dumps({"safeBot": True})
REACTIONS = json.dumps(
    {
        "rx": [
            {"k": 2, "c": 3, "o": "1200509_021", "a": {"ko": "사랑"}},
            {"k": 2, "c": 1, "o": "1200509_038", "a": {"ko": "엄지척"}},
            {"k": 2, "c": 0, "o": "1200509_001", "a": {"ko": "아무도"}},
        ]
    }
)

BOARD_NOTICE = "새 공지입니다"
LEGACY_NOTICE = "예전 공지"


def _group_log():
    t = 1_700_001_000
    return _connect(
        LOG_SCHEMA,
        {
            "chatLogs": [
                (REACTED, PEER_ID, 1, "좋은 아침", None, t, 0, None, 0, 0),
                (EDITED, FRIEND_ID, 1, "고친 문장", None, t + 10, 0, None, 0, 2),
                (
                    EDIT_FEED,
                    FRIEND_ID,
                    0,
                    json.dumps({"feedType": 25, "hidden": True, "logId": EDITED, "targetRevision": 2}),
                    None,
                    t + 11,
                    0,
                    None,
                    0,
                    0,
                ),
                (COVERED, FRIEND_ID, 1, "가려진 말", SAFEBOT, t + 20, 0, None, 0, 0),
                (
                    LINKS,
                    PEER_ID,
                    1,
                    "여기 https://example.com/a 그리고 https://example.com/b.",
                    None,
                    t + 30,
                    0,
                    None,
                    0,
                    0,
                ),
                (
                    FILE,
                    FRIEND_ID,
                    18,
                    "",
                    json.dumps(
                        {
                            "url": FILE_URL,
                            "name": FILE_NAME,
                            "size": FILE_SIZE,
                            "s": FILE_SIZE,
                            "expire": FILE_EXPIRES,
                        }
                    ),
                    t + 40,
                    0,
                    None,
                    0,
                    0,
                ),
                (PHOTO, PEER_ID, 2, "사진", PHOTO_ATTACHMENT, t + 50, 0, None, 0, 0),
                (ERASED, OWN_ID, 16385, "c29tZXRoaW5n", None, t + 60, 0, None, 1, 1),
                (COVERED_LINK, PEER_ID, 1, "https://covered.example/x", SAFEBOT, t + 70, 0, None, 0, 0),
                (REPLY_TO_COVERED, PEER_ID, 1, "그거 뭐였어요", None, t + 80, 0, COVERED, 0, 0),
            ],
            "chatLogMeta": [(REACTED, 1, 0, 2, 0, REACTIONS, None)],
            "chatLogHistory": [
                (EDITED, 0, FRIEND_ID, 1, "처음 문장", None, None),
                (EDITED, 1, FRIEND_ID, 1, "중간 문장", None, None),
                (EDITED, 2, FRIEND_ID, 1, "고친 문장", None, None),
                (ERASED, 0, OWN_ID, 1, "지운 말의 원문", None, None),
            ],
        },
    )


def _add_notices(catalog) -> None:
    catalog.executescript(
        """
        CREATE TABLE chatMetas (
            chatId INTEGER, type INTEGER, revision INTEGER, authorId INTEGER,
            content TEXT, updateAt INTEGER, extra TEXT, noticeRevision INTEGER
        );
        CREATE TABLE moimMetas (
            chatId INTEGER, type INTEGER, update_revision INTEGER, badge_revision INTEGER,
            last_seen_revision INTEGER, last_seen_notice_revision INTEGER,
            content TEXT, extra TEXT
        );
        """
    )
    catalog.executemany(
        "INSERT INTO chatMetas (chatId, type, authorId, content, updateAt) VALUES (?, ?, ?, ?, ?)",
        [
            (GROUP_ROOM, 1, PEER_ID, LEGACY_NOTICE, 1_600_000_000),
            (DIRECT_ROOM, 1, PEER_ID, LEGACY_NOTICE, 1_600_000_000),
            (GROUP_ROOM, 3, PEER_ID, "방 이름", 1_600_000_000),
        ],
    )
    catalog.executemany(
        "INSERT INTO moimMetas (chatId, type, content) VALUES (?, ?, ?)",
        [
            (
                GROUP_ROOM,
                1,
                json.dumps(
                    {
                        "notice": True,
                        "type": "TEXT",
                        "content": BOARD_NOTICE,
                        "owner_id": FRIEND_ID,
                        "created_at": "2026-01-04T03:00:00.000Z",
                    }
                ),
            ),
            (SELF_ROOM, 1, json.dumps({"notice": False, "type": "TEXT", "content": "공지 아님"})),
        ],
    )
    catalog.commit()


def build_extras_reader() -> Reader:
    """A reader whose group room and catalog carry the newer tables."""
    reader = build_reader()
    databases = reader._cache.databases  # type: ignore[attr-defined]
    databases[paths.chat_log_name(GROUP_ROOM)].close()
    databases[paths.chat_log_name(GROUP_ROOM)] = _group_log()
    _add_notices(databases[paths.CATALOG_DATABASE])
    return reader
