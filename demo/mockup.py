"""A furnished, entirely invented profile, for screenshots of the interface.

Nothing here touches the signed-in account: the databases are built in memory,
the "key ring" hands back connections to them, and no name, message or room in
this file belongs to a real person. That is the whole point — a screenshot of
the real thing would publish somebody's conversation.

    python demo/mockup.py rooms      the conversation list
    python demo/mockup.py chat       a conversation, with a reply and a mention
    python demo/mockup.py digest     /sum 30m over the same room
    python demo/mockup.py friends    the friend list
    python demo/mockup.py real       /real over two accounts sharing a nickname
    python demo/mockup.py filter     the list narrowed by /
    python demo/mockup.py sent       a message the client has confirmed back
    python demo/mockup.py unseen     scrolled up while arrivals land below
    python demo/mockup.py cli        the JSON an agent is handed
    python demo/mockup.py --print chat   the frame as text, for a terminal-less check

The window opens at whatever size the console already has; 110x30 shows the
interface the way it is meant to look.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kamado import paths  # noqa: E402
from kamado.errors import ErrorCode, KamadoError  # noqa: E402
from kamado.keyring import Keyring  # noqa: E402
from kamado.reader import Reader  # noqa: E402

KST = timezone(timedelta(hours=9))
PROFILE = Path("C:/mockup-profile")

ME = 70001
JISOO, MINJUN, SEOYEON, DOYUN, HAJUN, YERIN = 70002, 70003, 70004, 70005, 70006, 70007
COPYCAT = 70008
"""A second account currently wearing the same nickname as another member.

Two accounts can share a display name down to the character; they cannot share
an id, which is the whole reason the name history exists.
"""

CREW_ROOM = 90001
JISOO_ROOM = 90002
SELF_ROOM = 90003
WORK_ROOM = 90004
STUDY_ROOM = 90005
MINJUN_ROOM = 90006
CHANNEL_ROOM = 90007
SEOYEON_ROOM, DOYUN_ROOM, HAJUN_ROOM, YERIN_ROOM = 90008, 90009, 90010, 90011

USER_SCHEMA = """
CREATE TABLE talkUser (
    userId INTEGER, type INTEGER, nickName TEXT, friendNickName TEXT,
    phoneNumber TEXT, statusMessage TEXT, directChatId INTEGER, favorite INTEGER
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

TEXT, PHOTO, REPLY = 1, 2, 26


def at(hour: int, minute: int, days_ago: int = 0) -> int:
    """A time today (or *days_ago* days back) in the user's zone, as an epoch."""
    now = datetime.now(KST) - timedelta(days=days_ago)
    return int(now.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


def mentions(*user_ids: int) -> str:
    return json.dumps({"mentions": [{"at": [0], "len": 4, "user_id": one} for one in user_ids]})


def everyone() -> str:
    return json.dumps({"all_mention": {"at": 0}})


def answering(log_id: int, user_id: int, text: str, naming: tuple[int, ...] = ()) -> str:
    """A reply, optionally naming somebody in the same breath — both happen."""
    attachment = {
        "src_logId": log_id,
        "src_userId": user_id,
        "src_type": TEXT,
        "src_message": text,
        "src_linkId": 0,
    }
    if naming:
        attachment["mentions"] = [{"at": [0], "len": 2, "user_id": one} for one in naming]
    return json.dumps(attachment)


def renamed(log_id: int, sent_at: int, user_id: int, was: str) -> tuple:
    """A membership feed, which is the only place the client writes a nickname."""
    body = json.dumps(
        {"feedType": 4, "members": [{"userId": user_id, "nickName": was}]},
        ensure_ascii=False,
    )
    # Authored by the member it names, the way the live client writes it.
    return (log_id, user_id, 0, body, None, sent_at, 0)


def connect(script: str, rows: dict[str, list[tuple]]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.executescript(script)
    for table, values in rows.items():
        if values:
            placeholders = ", ".join("?" * len(values[0]))
            connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", values)
    connection.commit()
    return connection


class Cache:
    """Stands in for the snapshot cache, handing back the invented databases."""

    def __init__(self, databases: dict[str, sqlite3.Connection]) -> None:
        self.databases = databases

    def connect(self, path: Path, key: bytes, page_size: int = 4096, reserve: int = 80):
        name = Path(path).as_posix()
        for candidate, connection in self.databases.items():
            if name.endswith(candidate):
                return connection
        raise KamadoError(ErrorCode.KEY_UNAVAILABLE, f"no key for {name}")

    def close(self) -> None:
        for connection in self.databases.values():
            connection.close()


CREW_LOG = [
    (5010, SEOYEON, TEXT, "다들 이번 주는 좀 쉬셨어요? 저는 무릎이 좀 아파서 쉬었어요", None, at(18, 44), 0),
    (5011, JISOO, TEXT, "이번 주말 코스 어디로 갈까요? 지난번 코스는 좀 짧았던 것 같아요", None, at(19, 2), 0),
    (5012, MINJUN, PHOTO, "", None, at(19, 3), 0),
    (5013, MINJUN, TEXT, "한강 잠수교에서 반포까지 한 바퀴 어떨까요", None, at(19, 3), 0),
    (5014, SEOYEON, TEXT, "좋아요! 몇 시 출발이에요?", None, at(19, 5), 0),
    (
        5015,
        JISOO,
        REPLY,
        "7시에 모이면 해 뜨는 것 보면서 뛸 수 있어요",
        answering(5014, SEOYEON, "좋아요! 몇 시 출발이에요?"),
        at(19, 6),
        0,
    ),
    (5016, DOYUN, TEXT, "@나 지난번 페이스 기록 좀 공유해줄 수 있어요?", mentions(ME), at(19, 10), 0),
    (5017, ME, TEXT, "네, 정리해서 오늘 밤에 올릴게요", None, at(19, 12), 0),
    (5018, HAJUN, TEXT, "저도 그 기록 궁금했어요 ㅎㅎ", None, at(19, 14), 0),
    (5019, JISOO, TEXT, "@전체 내일 새벽에 비 예보 있어요, 우비 챙겨오세요", everyone(), at(19, 20), 0),
    (
        5020,
        MINJUN,
        REPLY,
        "비 오면 실내 트랙으로 바꾸는 건 어때요?",
        answering(5019, JISOO, "@전체 내일 새벽에 비 예보 있어요, 우비 챙겨오세요"),
        at(19, 22),
        0,
    ),
    (5021, SEOYEON, TEXT, "트랙도 좋아요. 대신 끝나고 국밥은 꼭 먹어야 해요", None, at(19, 24), 0),
    (5022, DOYUN, TEXT, "국밥 찬성입니다", None, at(19, 25), 0),
    # A reply that also names somebody: one row carrying both facts, which is
    # what a window of four messages has to be able to report.
    (
        5023,
        SEOYEON,
        REPLY,
        "@나 기록 올려주시면 저도 페이스 맞춰볼게요",
        answering(5017, ME, "네, 정리해서 오늘 밤에 올릴게요", naming=(ME,)),
        at(19, 27),
        0,
    ),
]

WORK_LOG = [
    (6010, YERIN, TEXT, "배포 스크립트 리뷰 부탁드려요", None, at(11, 20), 0),
    (6011, ME, TEXT, "확인하고 오후에 코멘트 남길게요", None, at(11, 32), 0),
]

JISOO_LOG = [
    (7010, JISOO, TEXT, "주말에 시간 괜찮으면 커피 한 잔 해요", None, at(17, 12), 0),
]

SELF_LOG = [
    (8010, ME, TEXT, "러닝화 새로 사기 · 무릎 보호대 · 스트레칭 영상 저장", None, at(9, 5), 0),
]

# Two accounts answering to "지수" in one room, which is what impersonation
# looks like from here: the feed is the only record that one of them used to
# be called something else.
STUDY_LOG = [
    renamed(9001, at(9, 10, 6), COPYCAT, "런닝맨"),
    renamed(9002, at(9, 11, 6), JISOO, "지수"),
    (9010, HAJUN, TEXT, "이번 주 스터디는 목요일 저녁 8시입니다", None, at(20, 25), 0),
    (9011, JISOO, TEXT, "자료는 어디서 받나요?", None, at(20, 30), 0),
    (9012, COPYCAT, TEXT, "제가 링크 보내드릴게요, 개인으로 연락주세요", None, at(20, 32), 0),
    (9013, YERIN, TEXT, "어… 방금 두 분 다 지수님이신가요?", None, at(20, 33), 0),
]


def build_reader() -> Reader:
    """Assemble the invented profile and hand back a reader over it."""
    users = connect(
        USER_SCHEMA,
        {
            "talkUser": [
                (JISOO, 1, "지수", None, None, "오늘도 10km", JISOO_ROOM, 1),
                (MINJUN, 1, "민준", None, None, "사진 찍는 러너", MINJUN_ROOM, 1),
                (SEOYEON, 1, "서연", None, None, "천천히 오래", SEOYEON_ROOM, 0),
                (DOYUN, 1, "도윤", None, None, None, DOYUN_ROOM, 0),
                (HAJUN, 1, "하준", None, None, "스터디 모집 중", HAJUN_ROOM, 0),
                (YERIN, 1, "예린", None, None, "출근 중", YERIN_ROOM, 0),
                # Not a friend, just a co-member — and currently named 지수.
                (COPYCAT, 1000, "지수", None, None, None, None, 0),
            ],
            "Profile": [(ME, "나", 0)],
        },
    )

    rooms = [
        # No emoji in a title: the console advances the two columns they
        # measure but paints over the next cell, and the smear survives.
        (CREW_ROOM, "OM", "주말 러닝크루", 1, 42, 3, 5023, at(19, 27), 0),
        (JISOO_ROOM, "DirectChat", "", 0, 2, 1, 7010, at(17, 12), JISOO),
        (WORK_ROOM, "MultiChat", "프로덕트 · 배포 준비", 1, 8, 0, 6011, at(11, 32), 0),
        (STUDY_ROOM, "OM", "주말 알고리즘 스터디 모집", 1, 128, 12, 9013, at(20, 33), 0),
        (SELF_ROOM, "MemoChat", "나", 0, 1, 0, 8010, at(9, 5), 0),
        (MINJUN_ROOM, "DirectChat", "", 0, 2, 0, None, at(8, 40, 1), MINJUN),
        (CHANNEL_ROOM, "PlusChat", "동네 러닝 소식", 1, 2, 0, None, at(19, 0, 2), 0),
        # Rooms the client has not opened this session: catalogued, unreadable,
        # and therefore absent from the list — which is the honest state.
        (SEOYEON_ROOM, "DirectChat", "", 0, 2, 0, None, at(21, 10, 1), SEOYEON),
        (DOYUN_ROOM, "DirectChat", "", 0, 2, 0, None, at(13, 5, 2), DOYUN),
        (HAJUN_ROOM, "DirectChat", "", 0, 2, 0, None, at(9, 40, 3), HAJUN),
        (YERIN_ROOM, "DirectChat", "", 0, 2, 0, None, at(15, 22, 4), YERIN),
    ]
    members = [
        (SELF_ROOM, ME, 1),
        (JISOO_ROOM, ME, 1),
        (JISOO_ROOM, JISOO, 1),
        (MINJUN_ROOM, ME, 1),
        (MINJUN_ROOM, MINJUN, 1),
        *[(CREW_ROOM, one, 1) for one in (ME, JISOO, MINJUN, SEOYEON, DOYUN, HAJUN)],
        *[(WORK_ROOM, one, 1) for one in (ME, YERIN)],
        *[(STUDY_ROOM, one, 1) for one in (ME, HAJUN, YERIN, JISOO, COPYCAT)],
    ]
    catalog = connect(CATALOG_SCHEMA, {"chatRoomList": rooms, "chatMembers": members})

    databases = {
        paths.USER_DATABASE: users,
        paths.CATALOG_DATABASE: catalog,
        paths.chat_log_name(CREW_ROOM): connect(LOG_SCHEMA, {"chatLogs": CREW_LOG}),
        paths.chat_log_name(WORK_ROOM): connect(LOG_SCHEMA, {"chatLogs": WORK_LOG}),
        paths.chat_log_name(JISOO_ROOM): connect(LOG_SCHEMA, {"chatLogs": JISOO_LOG}),
        paths.chat_log_name(SELF_ROOM): connect(LOG_SCHEMA, {"chatLogs": SELF_LOG}),
        paths.chat_log_name(STUDY_ROOM): connect(LOG_SCHEMA, {"chatLogs": STUDY_LOG}),
    }
    # One key object per database, the way a real ring pairs them: the footer
    # counts distinct keys, and a shared object would report a single one.
    invented = {name: bytes([index + 1] * 32) for index, name in enumerate(databases)}
    keyring = Keyring(
        user_directory=PROFILE,
        pid=1234,
        keys=invented | {Path(name).name: key for name, key in invented.items()},
    )
    # ledger stays unset: this reader must never write a name anywhere.
    return Reader(user_directory=PROFILE, cache=Cache(databases), keyring=keyring)


def arrange(app, scene: str) -> None:
    """Put the interface in the state a screenshot wants to show."""
    app.load_rooms()
    if scene == "rooms":
        app.selected = next(
            index for index, one in enumerate(app.rooms) if one.id == CREW_ROOM
        )
        app.status = "카카오톡이 연 적 있는 방만 읽을 수 있습니다"
        return
    if scene == "friends":
        app.show_friends = True
        app.load_friends()
        return
    if scene == "real":
        # The question a reader actually has: which of these two is which.
        study = next(one for one in app.rooms if one.id == STUDY_ROOM)
        app.open_room(study)
        app._run_command("/real 지수")
        return
    if scene == "filter":
        app.filtering = True
        app._narrow("러닝")
        return
    room = next(one for one in app.rooms if one.id == CREW_ROOM)
    app.open_room(room)
    chat = app.chat
    assert chat is not None
    if scene == "digest":
        app._run_command("/sum 30m")
        return
    if scene == "sent":
        # What was sent is marked, not announced: the tick sits against the
        # right border of the box the message was written in.
        chat.delivered = True
        return
    if scene == "unseen":
        # Scrolled up in the history while messages keep arriving: the view
        # holds its place and says how many landed below it.
        chat.offset = 5
        chat.unseen = 3
        return
    chat.draft = "7시 좋아요, 저는 잠수교 쪽에서 합류할게요"
    chat.caret = len(chat.draft)


def show_cli(reader) -> None:
    """Print what an agent is handed, which is the other half of the tool.

    Two short commands rather than one long one: a forty-message ``context``
    is a hundred lines of JSON, which is the right answer for a program and
    the wrong one for a screenshot. Neither envelope here is abridged.
    """
    from kamado.errors import success
    from kamado.names import steps_with_current

    print("$ kamado status")
    print(json.dumps(success(reader.status()), ensure_ascii=False, indent=2, sort_keys=True))

    history = reader.name_history(STUDY_ROOM)
    current = reader.names()
    users = [
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
        for user_id in reader.users_named(STUDY_ROOM, "지수", history)
    ]
    print()
    print(f'$ kamado names --room-id {STUDY_ROOM} "지수"')
    print(
        json.dumps(
            success({"users": users, "count": len(users)}),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: list[str]) -> int:
    scenes = ("rooms", "chat", "digest", "friends", "filter", "sent", "unseen", "real")
    printing = "--print" in argv
    wanted = next((one for one in argv if one in scenes or one == "cli"), "chat")

    from kamado.tui.app import App
    from kamado.tui.theme import Theme

    with build_reader() as reader:
        if wanted == "cli":
            show_cli(reader)
            return 0
        if printing:
            app = App(reader, theme=Theme(truecolor=True, enabled=True))
            arrange(app, wanted)
            for line in app.render(110, 30):
                print(line)
            return 0

        from kamado.tui.console import Console

        # Forced rather than detected: a screenshot should show the palette the
        # interface was designed in, whatever the host terminal advertises.
        app = App(reader, theme=Theme(truecolor=True, enabled=True))
        arrange(app, wanted)
        with Console() as console:
            app.run(console)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
