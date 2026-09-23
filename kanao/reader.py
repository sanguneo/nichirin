"""Query the decrypted databases, doing the least work that answers the question.

Everything is lazy.  Building the key ring means scanning the client's heap,
and opening a snapshot means decrypting a whole file, so neither happens until
a caller actually needs it.  That is what lets ``status`` explain that
KakaoTalk is closed instead of failing to start for the same reason.

The queries target the schema this client really uses rather than a list of
guessed column aliases.  Two details from that schema shape the code:

* ``chatLogs`` has no index on ``sendAt`` but its primary key is ``logId``,
  and over the newest 5000 rows of a live room the two orders agreed exactly.
  Ordering by ``logId`` therefore reads an index instead of sorting hundreds of
  thousands of rows.
* The signed-in user is the sole member of the ``MemoChat`` room.  The
  ``Profile`` table holds several rows and its ``hasMeBadge`` flag marks a
  different account entirely, so neither is used to identify the account.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from . import model, names as names_module, paths
from .errors import ErrorCode, KanaoError
from .keyring import Keyring
from .model import Friend, Message, Room
from .names import NameLedger, NameRecord
from .snapshot import SnapshotCache, SnapshotSource

_SELF_CHAT_TITLE = "나와의 채팅"
_GROUP_TITLE_MEMBERS = 3

_THREAD_COLUMN = "threadId"
NAME_RETRY_SECONDS = 5.0
"""How long an unknown sender waits before the name map is read again."""
_ERASED_COLUMN = "is_encrypt_delete_msg"
"""Set on a message deleted for everyone, whose text the client then encrypts.

Such a row keeps ``deleted = 0``, so filtering on that alone showed the
encrypted remainder as if somebody had typed it. Measured over the newest
4,000 rows of every readable room: the 60 rows with this flag were exactly the
60 whose text was base64, and none of the 12,033 without it was.
"""
"""Where a newer client points a reply at the message it answers.

The older shape kept the reply's own copy of what it answered inside the
attachment.  This one writes an ordinary text row and records only the id, so
the quote has to be read back out of the conversation.
"""


class Reader:
    """Read-only access to one signed-in KakaoTalk profile."""

    def __init__(
        self,
        user_directory: Optional[Path] = None,
        cache: Optional[SnapshotSource] = None,
        keyring: Optional[Keyring] = None,
        ledger: Optional[NameLedger] = None,
    ) -> None:
        """Build a reader without touching the disk or the client.

        *cache* and *keyring* exist so callers can supply an already-scanned
        key ring instead of paying for a second heap scan, and so tests can
        drive the query logic against fixtures.

        *ledger* is opt-in on purpose.  Recording names is the only thing here
        that writes real conversation data to disk, so a reader built without
        one — every reader in the test suite — cannot touch the user's history.
        """
        self._user_directory = user_directory
        self._cache = cache if cache is not None else SnapshotCache()
        self._owns_cache = cache is None
        self._keyring: Optional[Keyring] = keyring
        self._ledger = ledger
        self._names: Optional[dict[int, str]] = None
        self._names_read_at = 0.0
        self._feed_names: dict[int, dict[int, str]] = {}
        self._feeds_lack: dict[int, set[int]] = {}
        self._own_id: Optional[int] = None
        self._own_id_resolved = False
        self._selections: dict[int, str] = {}
        self._live_conditions: dict[int, str] = {}

    @property
    def user_directory(self) -> Path:
        if self._user_directory is None:
            self._user_directory = paths.user_directory()
        return self._user_directory

    @property
    def keyring(self) -> Keyring:
        """Scan the running client once, on first use."""
        if self._keyring is None:
            self._keyring = Keyring.discover(self.user_directory)
        return self._keyring

    def _key_for(self, name: str) -> bytes:
        """The key for *name*, scanning again once if a remembered set lacks it.

        The remembered addresses cannot know about a room opened after they
        were recorded, so a miss against them is not yet an answer.
        """
        ring = self.keyring
        if not ring.has(name) and ring.remembered:
            self._keyring = ring = Keyring.discover(self.user_directory, rescan=True)
        return ring.key_for(name)

    def close(self) -> None:
        """Close what this reader built, and leave a borrowed cache alone.

        A cache passed in belongs to the caller — the server's send path keeps
        one alive across requests so a message costs a fingerprint check
        rather than a whole decryption.  Closing it here would look like it
        worked, because the next reader simply decrypts again and answers
        correctly; it would only ever be slow.
        """
        if self._owns_cache:
            self._cache.close()

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    # -- diagnostics ---------------------------------------------------------

    def status(self) -> dict[str, object]:
        """Describe access without requiring any of it to be working."""
        try:
            directory = self.user_directory
            profile: Optional[str] = str(directory)
        except KanaoError:
            return {
                "running": False,
                "profile": None,
                "decryptableDatabases": 0,
                "readableRooms": 0,
                "detail": "no signed-in KakaoTalk profile exists on this computer",
            }

        ring = self.keyring
        readable = len(ring.chat_ids())
        if not ring.running:
            detail = "KakaoTalk is not running, so no keys are in memory"
        elif not ring.keys:
            detail = "KakaoTalk is running but has not opened any database yet"
        else:
            detail = "ready"
        return {
            "running": ring.running,
            "profile": profile,
            "decryptableDatabases": len(ring),
            "readableRooms": readable,
            "detail": detail,
        }

    def verify(self) -> dict[str, object]:
        """Prove decryption works by opening the catalog and counting its rooms."""
        connection = self._catalog()
        rooms = connection.execute("SELECT COUNT(*) FROM chatRoomList").fetchone()[0]
        return {"verified": True, "rooms": int(rooms), "readableRooms": len(self.keyring.chat_ids())}

    # -- catalog -------------------------------------------------------------

    def _catalog(self) -> sqlite3.Connection:
        name = paths.CATALOG_DATABASE
        return self._cache.connect(self.keyring.path_for(name), self._key_for(name))

    def _users(self) -> sqlite3.Connection:
        name = paths.USER_DATABASE
        return self._cache.connect(self.keyring.path_for(name), self._key_for(name))

    def names(self) -> dict[int, str]:
        """Map every known user id to the best available display name.

        A name the signed-in user assigned wins over the name the contact
        chose for themselves, matching what the client shows.
        """
        if self._names is None:
            users = self._users()
            # talkUser holds contacts and never the signed-in account itself,
            # so without Profile every message the user sent would be
            # attributed to a bare numeric id.
            found: dict[int, str] = {
                int(user_id): str(nickname)
                for user_id, nickname in users.execute(
                    "SELECT userId, nickname FROM Profile WHERE nickname IS NOT NULL"
                )
            }
            found.update(
                {
                    int(user_id): str(friend or nick)
                    for user_id, friend, nick in users.execute(
                        "SELECT userId, friendNickName, nickName FROM talkUser"
                    )
                    if friend or nick
                }
            )
            self._names = found
            self._names_read_at = time.monotonic()
            if self._ledger is not None:
                # Once per process: the map is memoised above, and only a name
                # that differs from its last sighting is appended.
                self._ledger.observe(found)
        return self._names

    def _sender_name(self, room_id: int, user_id: int) -> str:
        """Name *user_id*, looking again before settling for a number.

        The name map is read once per reader, and the interface keeps one
        reader for as long as it runs — so somebody who joined an open chat
        after the room was opened stayed ``User <id>`` until the room was
        opened again, when a fresh read found them. An unknown id now has
        the map read again, at most every :data:`NAME_RETRY_SECONDS`, and
        failing that the name the room's own join feed recorded for them.
        """
        found = self.names().get(user_id)
        if found is not None:
            return found
        if time.monotonic() - self._names_read_at >= NAME_RETRY_SECONDS:
            self._names = None
            found = self.names().get(user_id)
            if found is not None:
                return found
        feeds = self._feed_names.get(room_id)
        lacking = self._feeds_lack.setdefault(room_id, set())
        if feeds is None or (user_id not in feeds and user_id not in lacking):
            # Newest last, so the name a person carried most recently wins.
            feeds = {
                record.user_id: record.name
                for record in names_module.from_feeds(self._logs(room_id))
            }
            self._feed_names[room_id] = feeds
            if user_id not in feeds:
                # Read once per stranger: a sender no feed names would
                # otherwise re-read every feed of the room for each line.
                lacking.add(user_id)
        return feeds.get(user_id) or f"User {user_id}"

    def name_history(self, room_id: int) -> dict[int, list[NameRecord]]:
        """Return every name each user has been seen under in one room.

        Two sources, and they answer different questions.  The room's own feed
        rows reach back to whenever the client first logged a join, but record
        a name only at a join, leave or invite.  The ledger covers only what
        this tool has read since it was enabled, but catches a plain rename,
        which the client never writes down at all.
        """
        records = list(names_module.from_feeds(self._logs(room_id)))
        if self._ledger is not None:
            # The ledger is account-wide while the feeds belong to one room, so
            # it is narrowed to this room's people. Without that, asking who
            # else answers to a name returned strangers from other chats, which
            # reads exactly like the impersonation the question is about.
            known = {record.user_id for record in records} | self.member_ids(room_id)
            records.extend(
                record for record in self._ledger.records() if record.user_id in known
            )
        return names_module.timeline(records)

    def users_named(
        self, room_id: int, name: str, history: Optional[dict[int, list[NameRecord]]] = None
    ) -> list[int]:
        """Return everyone in one room who has ever answered to *name*.

        The recorded history is searched together with the names the databases
        show right now, because the client logs a name at a join and not at a
        rename: the name somebody goes by today is routinely the one no feed
        ever captured.  Both halves stay inside this room's membership, so the
        answer is about the people in the conversation being read.
        """
        history = self.name_history(room_id) if history is None else history
        current = self.names()
        needle = name.casefold().strip()
        universe = set(history) | self.member_ids(room_id)
        return sorted(
            set(names_module.carriers(history, name))
            | {user for user in universe if (current.get(user) or "").casefold() == needle}
        )

    def member_ids(self, room_id: int) -> set[int]:
        """Return the users the catalog currently lists in a room."""
        return {
            int(row[0])
            for row in self._catalog().execute(
                "SELECT userId FROM chatMembers WHERE chatId = ?", (room_id,)
            )
        }

    def friends(self, favorites_only: bool = False) -> list["Friend"]:
        """Return the signed-in user's friend list from the local database.

        A friend is a ``talkUser`` row of type 1; the other types are known
        non-friends (group co-members) or special relationships. Favourites are
        listed first, then names, so the order matches what the client shows.
        """
        from .model import FRIEND_TYPE, Friend

        sql = (
            "SELECT userId, friendNickName, nickName, phoneNumber, statusMessage, "
            "directChatId, favorite FROM talkUser WHERE type = ?"
        )
        parameters: list[object] = [FRIEND_TYPE]
        if favorites_only:
            sql += " AND favorite = 1"

        friends: list[Friend] = []
        for row in self._users().execute(sql, parameters):
            user_id, friend_nick, nick, phone, status, direct_chat, favorite = row
            name = (friend_nick or nick or "").strip() or f"User {user_id}"
            friends.append(
                Friend(
                    id=int(user_id),
                    name=name,
                    nickname=str(nick) if nick else None,
                    phone=str(phone) if phone else None,
                    status_message=str(status) if status else None,
                    direct_chat_id=int(direct_chat) if direct_chat else None,
                    favorite=bool(favorite),
                )
            )
        friends.sort(key=lambda friend: (not friend.favorite, friend.name.casefold()))
        return friends

    def own_user_id(self) -> Optional[int]:
        """Identify the signed-in account, or None when it cannot be proven."""
        if self._own_id_resolved:
            return self._own_id
        self._own_id_resolved = True
        catalog = self._catalog()

        self_rooms = [
            int(row[0])
            for row in catalog.execute(
                "SELECT chatId FROM chatRoomList WHERE type = ?", (model.SELF_CHAT,)
            )
        ]
        for room_id in self_rooms:
            members = [
                int(row[0])
                for row in catalog.execute(
                    "SELECT userId FROM chatMembers WHERE chatId = ?", (room_id,)
                )
            ]
            if len(members) == 1:
                self._own_id = members[0]
                return self._own_id

        # Without a self-chat, the account is whoever is in a direct room
        # besides the recorded counterpart.
        for room_id, peer in catalog.execute(
            "SELECT chatId, directChatMemberId FROM chatRoomList "
            "WHERE type = ? AND directChatMemberId > 0 LIMIT 50",
            (model.DIRECT_CHAT,),
        ):
            members = {
                int(row[0])
                for row in catalog.execute(
                    "SELECT userId FROM chatMembers WHERE chatId = ?", (int(room_id),)
                )
            }
            remainder = members - {int(peer)}
            if len(remainder) == 1:
                self._own_id = remainder.pop()
                return self._own_id
        return None

    def _title(
        self,
        room_id: int,
        room_type: str,
        stored_title: Optional[str],
        custom: int,
        peer_id: Optional[int],
    ) -> str:
        title = (stored_title or "").strip()
        if title and (custom or room_type in (model.CHANNEL_CHAT, model.OPEN_GROUP_CHAT)):
            return title
        if room_type == model.SELF_CHAT:
            return _SELF_CHAT_TITLE
        if room_type in (model.DIRECT_CHAT, model.OPEN_DIRECT_CHAT) and peer_id:
            return self.names().get(int(peer_id), title or f"User {peer_id}")
        if title:
            return title

        own = self.own_user_id()
        members = [
            int(row[0])
            for row in self._catalog().execute(
                "SELECT userId FROM chatMembers WHERE chatId = ? AND isActive = 1", (room_id,)
            )
            if int(row[0]) != own
        ]
        if members:
            names = self.names()
            shown = [names.get(member, f"User {member}") for member in members[:_GROUP_TITLE_MEMBERS]]
            if len(members) > _GROUP_TITLE_MEMBERS:
                shown.append(f"외 {len(members) - _GROUP_TITLE_MEMBERS}명")
            return ", ".join(shown)
        return f"Room {room_id}"

    def rooms(self, limit: Optional[int] = None) -> list[Room]:
        """Return conversations, most recently active first."""
        sql = (
            "SELECT chatId, type, chatRoomTitle, useCustomChatRoomTitle, activeMembersCount, "
            "newMessageCount, lastLogId, lastUpdatedAt, directChatMemberId "
            "FROM chatRoomList ORDER BY lastUpdatedAt IS NULL, lastUpdatedAt DESC"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)

        readable = set(self.keyring.chat_ids())
        found: list[Room] = []
        for row in self._catalog().execute(sql, parameters):
            room_id, room_type, title, custom, members, unread, last_log, updated, peer = row
            room_id = int(room_id)
            found.append(
                Room(
                    id=room_id,
                    type=str(room_type or ""),
                    title=self._title(room_id, str(room_type or ""), title, int(custom or 0), peer),
                    member_count=int(members or 0),
                    unread=int(unread or 0),
                    last_log_id=None if last_log is None else int(last_log),
                    last_updated_at=None if updated is None else int(updated),
                    readable=room_id in readable,
                )
            )
        return found

    def stored_title(self, room_id: int) -> Optional[str]:
        """Return the catalog's own title for a room, before any resolution.

        A room's window carries this rather than the title composed here: the
        self-chat is stored under the account's own name while it is shown as
        "나와의 채팅", and a group with no custom title is named after its
        members by each side independently.
        """
        row = self._catalog().execute(
            "SELECT chatRoomTitle FROM chatRoomList WHERE chatId = ?", (room_id,)
        ).fetchone()
        stored = None if row is None else row[0]
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
        return None

    def room(self, room_id: int) -> Optional[Room]:
        """Return one conversation by id."""
        return next((found for found in self.rooms() if found.id == room_id), None)

    def resolve(self, name: str) -> list[Room]:
        """Return rooms matching *name*, preferring an exact title match.

        Returning every match rather than the first one is what lets a caller
        refuse an ambiguous request instead of reading an arbitrary room.
        """
        needle = name.casefold().strip()
        rooms = self.rooms()
        exact = [found for found in rooms if found.title.casefold() == needle]
        return exact or [found for found in rooms if needle in found.title.casefold()]

    # -- messages ------------------------------------------------------------

    def _logs(self, room_id: int) -> sqlite3.Connection:
        name = paths.chat_log_name(room_id)
        return self._cache.connect(self.keyring.path_for(name), self._key_for(name))

    def _selection(self, room_id: int, connection: sqlite3.Connection) -> str:
        """Name the columns one message is read from, for this room's log.

        ``threadId`` is where the client now records which message a reply
        answers, and a log written before it existed does not have the column
        at all — naming a column that is not there fails the whole statement
        rather than that one row, so the schema is asked instead of assumed.
        The answer is kept per room because it is a property of the file, and
        a client that gains the column mid-session is a client that has been
        reinstalled underneath a reader that is already running.
        """
        known = self._selections.get(room_id)
        if known is None:
            self._read_schema(room_id, connection)
            known = self._selections[room_id]
        return known

    def _live(self, room_id: int, connection: sqlite3.Connection) -> str:
        """The condition a row must meet to be a message nobody took back.

        Two columns say a message was deleted, and the newer one is absent
        from an older log, so it is asked for the way ``threadId`` is.
        """
        known = self._live_conditions.get(room_id)
        if known is None:
            self._read_schema(room_id, connection)
            known = self._live_conditions[room_id]
        return known

    def _read_schema(self, room_id: int, connection: sqlite3.Connection) -> None:
        columns = {column[1] for column in connection.execute("PRAGMA table_info(chatLogs)")}
        thread = _THREAD_COLUMN if _THREAD_COLUMN in columns else "NULL"
        self._selections[room_id] = f"logId, authorId, type, message, attachement, sendAt, {thread}"
        live = "COALESCE(deleted, 0) = 0"
        if _ERASED_COLUMN in columns:
            live += f" AND COALESCE({_ERASED_COLUMN}, 0) = 0"
        self._live_conditions[room_id] = live

    def messages(
        self,
        room_id: int,
        limit: int = 40,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
    ) -> list[Message]:
        """Return up to *limit* messages, oldest first.

        *before_id* pages backwards through history; *after_id* collects what
        has arrived since a known message, which is how following works.

        Which end the limit cuts from is decided by that difference.  Asked
        for a window, the newest messages are the ones worth having.  Asked
        for what arrived since a known message, they are not: taking the
        newest page there skips whatever sits between the cursor and it, and
        a caller that then advances its cursor never sees the difference
        again.  KakaoTalk backfills from the server after a reconnection, so
        a room really can gain thousands of rows between two polls.
        """
        connection = self._logs(room_id)
        sql = f"SELECT {self._selection(room_id, connection)} FROM chatLogs"
        parameters: list[object] = []
        conditions = [self._live(room_id, connection)]
        if before_id is not None:
            conditions.append("logId < ?")
            parameters.append(before_id)
        if after_id is not None:
            conditions.append("logId > ?")
            parameters.append(after_id)
        forwards = after_id is not None and before_id is None
        sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY logId {'ASC' if forwards else 'DESC'} LIMIT ?"
        parameters.append(limit)

        rows = connection.execute(sql, parameters).fetchall()
        if not forwards:
            rows = list(reversed(rows))
        return [self._message(room_id, row) for row in rows]

    def feed_details(self, room_id: int, log_ids: Sequence[int]) -> dict[int, str]:
        """Describe each of these feeds from what its own row carries.

        The JSON stays out of the message text, where it reads as words
        somebody typed and buries the conversation; this is the short account
        of it, asked for the rows being looked at rather than for a room's
        whole history.

        Parsing happens here rather than in SQL. A row with no text is not
        always a feed — a photo has none either — and handing one of those to
        SQLite's ``json_extract`` fails the whole statement instead of that
        row.
        """
        if not log_ids:
            return {}
        places = ",".join("?" for _ in log_ids)
        found: dict[int, str] = {}
        for log_id, body in self._logs(room_id).execute(
            f"SELECT logId, message FROM chatLogs WHERE type = ? AND logId IN ({places})",
            (model.FEED_MESSAGE, *log_ids),
        ):
            detail = model.feed_detail(body)
            if detail:
                found[int(log_id)] = detail
        return found

    def count_messages(self, room_id: int) -> int:
        """Return how many undeleted messages are stored locally for a room."""
        connection = self._logs(room_id)
        row = connection.execute(
            f"SELECT COUNT(*) FROM chatLogs WHERE {self._live(room_id, connection)}"
        ).fetchone()
        return int(row[0])

    def newest_log_id(self, room_id: int) -> Optional[int]:
        """Return the newest readable message id, without reading bodies.

        Deleted rows are excluded like everywhere else: this id is a position
        a follower resumes from, and naming a row no query will ever return
        is how a cursor comes to point at nothing.
        """
        connection = self._logs(room_id)
        row = connection.execute(
            f"SELECT MAX(logId) FROM chatLogs WHERE {self._live(room_id, connection)}"
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _message(self, room_id: int, row: "sqlite3.Row | tuple[Any, ...]") -> Message:
        log_id, author, type_code, text, attachment, sent_at, thread = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        known = self.names()
        own = self.own_user_id()
        # The attachment is read for what it records about *other* messages —
        # who this one named, and which one it answered. It is never turned
        # into message text, which would show words nobody typed.
        extra = model.parse_attachment(attachment)
        mentions = model.mentions_in(extra, known.get)
        # Where both shapes are present the attachment's own copy wins: it is
        # what the sender was actually looking at when they answered.
        reply = model.quoted_in(extra, known.get) or self._threaded_quote(
            room_id, int(log_id), thread
        )
        return Message(
            id=int(log_id),
            room_id=room_id,
            sender_id=sender_id,
            sender=self._sender_name(room_id, sender_id),
            # A membership feed's message column is the client's own JSON,
            # not words anybody typed, so it is not passed off as text.
            text=""
            if kind == model.FEED_MESSAGE
            else (text if isinstance(text, str) and text else ""),
            type=kind,
            sent_at=None if sent_at is None else int(sent_at),
            from_me=own is not None and sender_id == own,
            mentions=mentions,
            mentions_all=model.everyone_mentioned(extra),
            mentions_me=own is not None and any(one.user_id == own for one in mentions),
            reply=reply,
            replies_to_me=own is not None and reply is not None and reply.sender_id == own,
            photo=model.photo_in(kind, extra),
        )

    def _threaded_quote(
        self, room_id: int, log_id: int, thread: Any
    ) -> Optional[model.Quote]:
        """Read back the message a threaded reply answers.

        Where the attachment carried the reply's own copy of what it answered,
        a thread carries only the id — so this is what that message says
        *now*, and it goes when that message goes.  A thread naming a row this
        database does not hold quotes nothing rather than an empty line
        attributed to nobody; the reply still reads as an ordinary message,
        which is what it looks like in the client too.

        Unlike the macOS build, every room here is its own database, so the
        answered row is found by ``logId`` alone — ``chatLogs`` carries no
        ``chatId`` column to narrow by.
        """
        try:
            source = int(thread)
        except (TypeError, ValueError):
            return None
        # A row that names itself is answering nothing, and quoting it would
        # stack a message above a copy of itself.
        if source <= 0 or source == log_id:
            return None
        connection = self._logs(room_id)
        row = connection.execute(
            f"SELECT {self._selection(room_id, connection)} FROM chatLogs "
            f"WHERE logId = ? AND {self._live(room_id, connection)}",
            (source,),
        ).fetchone()
        if row is None:
            return None
        _log_id, author, type_code, text, _attachment, _sent_at, _thread = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        return model.Quote(
            message_id=source,
            sender_id=sender_id,
            sender=self.names().get(sender_id),
            # A feed's text is the client's own JSON, which is not something
            # anybody said; it is dropped here exactly as it is for a message.
            text="" if kind == model.FEED_MESSAGE or not isinstance(text, str) else text,
            type=kind,
        )

    def search(self, keyword: str, limit: int = 20) -> Iterator[Message]:
        """Search readable rooms, newest first, stopping once *limit* is reached.

        Rooms whose key is absent are skipped rather than failing the search,
        because a partial local cache is the normal state; the caller is told
        how many rooms were unreadable so a miss is never reported as proof.
        """
        if limit <= 0:
            return
        pattern = f"%{keyword}%"
        remaining = limit
        for room in self.rooms():
            if not room.readable or remaining <= 0:
                continue
            try:
                connection = self._logs(room.id)
            except KanaoError:
                continue
            rows = connection.execute(
                # Feeds are excluded rather than filtered afterwards: their
                # message column is JSON, so a search for an ordinary word
                # like "type" would match rows that show no text at all.
                f"SELECT {self._selection(room.id, connection)} FROM chatLogs "
                f"WHERE {self._live(room.id, connection)} AND type != ? AND message LIKE ? "
                "ORDER BY logId DESC LIMIT ?",
                (model.FEED_MESSAGE, pattern, remaining),
            ).fetchall()
            for row in rows:
                yield self._message(room.id, row)
                remaining -= 1

    def query(self, sql: str, room_id: Optional[int] = None) -> list[dict[str, object]]:
        """Run one read-only statement against the catalog or a room's log."""
        connection = self._catalog() if room_id is None else self._logs(room_id)
        connection.execute("PRAGMA query_only = ON")
        try:
            cursor = connection.execute(sql)
        except sqlite3.Error as error:
            raise KanaoError(ErrorCode.INVALID_REQUEST, str(error)) from error
        columns = [description[0] for description in (cursor.description or ())]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
