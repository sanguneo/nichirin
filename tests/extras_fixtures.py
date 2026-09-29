"""A synthetic database carrying what the newer client writes beside a message.

``tests/fixtures.py`` models the schema every read needs. This adds what 공감,
edits, notices, attachments and SafeBot are read from: ``NTChatLogMeta``, the
``revision`` and ``localFilePath`` columns on ``NTChatMessage``, and the
``authorId``/``updatedAt`` columns of a notice row in ``NTChatMeta``. Column
names are the live client's; every value is made up.
"""

from __future__ import annotations

import json
from typing import Optional

from kamado.reader import Reader

from tests.fixtures import (
    FRIEND_ID,
    GROUP_ROOM,
    OWN_ID,
    PEER_ID,
    PHOTO_ATTACHMENT,
    build_reader,
)

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

SAFEBOT = json.dumps({"safeBot": True, "lastFeedId": 3})
REACTIONS = json.dumps(
    {
        "rx": [
            {"k": 2, "c": 3, "o": "1200509_021", "a": {"ko": "사랑"}},
            {"k": 2, "c": 1, "o": "1200509_038", "a": {"ko": "엄지척"}},
            {"k": 2, "c": 0, "o": "1200509_001", "a": {"ko": "아무도"}},
        ]
    }
)
NOTICE_TEXT = "이번 주 모임은 토요일입니다"
NOTICE_AT = 1_780_000_000


def _file_attachment() -> str:
    return json.dumps(
        {
            "url": FILE_URL,
            "name": FILE_NAME,
            "size": FILE_SIZE,
            "s": FILE_SIZE,
            "expire": FILE_EXPIRES,
            "k": "synthetic",
            "cs": "synthetic",
        }
    )


def build_extras_reader(local_path: Optional[str] = None) -> Reader:
    """A reader whose group room carries every newer table and column.

    *local_path* is what the file message's ``localFilePath`` says, so a test
    can point it at a file it made or at one that is not there.
    """
    reader = build_reader()
    database = reader._cache.database  # type: ignore[attr-defined]
    database.executescript(
        """
        ALTER TABLE NTChatMessage ADD COLUMN revision INTEGER;
        ALTER TABLE NTChatMessage ADD COLUMN localFilePath TEXT;
        ALTER TABLE NTChatMeta ADD COLUMN authorId INTEGER;
        ALTER TABLE NTChatMeta ADD COLUMN updatedAt INTEGER;
        CREATE TABLE NTChatLogMeta (
            logId INTEGER NOT NULL DEFAULT 0, chatId INTEGER NOT NULL DEFAULT 0,
            linkId INTEGER NOT NULL DEFAULT 0, type INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0, content TEXT, extra TEXT,
            extraRevision INTEGER NOT NULL DEFAULT 0, extraState INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (logId, chatId, type)
        );
        """
    )
    t = 1_700_001_000
    edit_feed = json.dumps(
        {"feedType": 25, "logId": EDITED, "hidden": True, "targetRevision": 1}
    )
    rows = [
        # logId, chatId, authorId, type, message, attachment, sentAt, deleted,
        # threadId, revision, localFilePath
        (REACTED, GROUP_ROOM, PEER_ID, 1, "좋은 아침", None, t, 0, None, 0, None),
        (EDITED, GROUP_ROOM, FRIEND_ID, 1, "고친 문장", "{}", t + 10, 0, None, 1, None),
        (EDIT_FEED, GROUP_ROOM, FRIEND_ID, 0, edit_feed, None, t + 11, 0, None, 0, None),
        (COVERED, GROUP_ROOM, FRIEND_ID, 1, "가려진 말", SAFEBOT, t + 20, 0, None, 0, None),
        (
            LINKS, GROUP_ROOM, PEER_ID, 1,
            "여기 https://example.com/a 그리고 https://example.com/b.",
            None, t + 30, 0, None, 0, None,
        ),
        (FILE, GROUP_ROOM, FRIEND_ID, 18, "", _file_attachment(), t + 40, 0, None, 0, local_path),
        (PHOTO, GROUP_ROOM, PEER_ID, 2, "사진", PHOTO_ATTACHMENT, t + 50, 0, None, 0, None),
        # Deleted for everyone: the bit is added to the type and the text stays.
        (ERASED, GROUP_ROOM, OWN_ID, 16385, "지운 말", None, t + 60, 0, None, 1, None),
        (
            COVERED_LINK, GROUP_ROOM, PEER_ID, 1, "https://covered.example/x",
            SAFEBOT, t + 70, 0, None, 0, None,
        ),
        (REPLY_TO_COVERED, GROUP_ROOM, PEER_ID, 1, "그거 뭐였어요", None, t + 80, 0, COVERED, 0, None),
    ]
    database.executemany(
        "INSERT INTO NTChatMessage (logId, chatId, authorId, type, message, attachment, "
        "sentAt, deleted, threadId, revision, localFilePath) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    database.execute(
        "INSERT INTO NTChatLogMeta (logId, chatId, type, content, extra) VALUES (?, ?, 2, ?, ?)",
        (REACTED, GROUP_ROOM, REACTIONS, json.dumps({"myRx": []})),
    )
    # The same id in another room is another message's 공감, not this one's.
    database.execute(
        "INSERT INTO NTChatLogMeta (logId, chatId, type, content) VALUES (?, ?, 2, ?)",
        (LINKS, GROUP_ROOM + 1000, REACTIONS),
    )
    database.executemany(
        "INSERT INTO NTChatMeta (chatId, type, revision, content, authorId, updatedAt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(GROUP_ROOM, 1, 7, NOTICE_TEXT, PEER_ID, NOTICE_AT)],
    )
    database.commit()
    return reader
