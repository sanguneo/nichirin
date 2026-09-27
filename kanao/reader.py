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
from typing import Any, Callable, Iterator, Optional, Sequence

from . import model, names as names_module, paths
from .errors import ErrorCode, KanaoError
from .keyring import Keyring
from .model import Friend, Message, Room
from .names import NameLedger, NameRecord
from .snapshot import SnapshotCache, SnapshotSource

_SELF_CHAT_TITLE = "나와의 채팅"
_GROUP_TITLE_MEMBERS = 3

_THREAD_COLUMN = "threadId"
UNLOCK_ATTEMPTS = 8
UNLOCK_SECONDS = 0.25
"""How long :meth:`Reader.unlock` waits between scans for a newly opened room."""
NAME_RETRY_SECONDS = 5.0
"""How long an unknown sender waits before the name map is read again."""
_ERASED_TYPE_BIT = 0x4000
"""Added to a message's type when it is deleted for everyone (16385, 16386).

The flag column below marks the same rows and a few this bit does not; the
bit is what the macOS schema, which has no such column, carries as well.
"""
_ERASED_FEED = 14
"""The feed a deletion writes, ``{"feedType": 14, "logId": …, "hidden": true}``.

Measured on the macOS build, where all 255 messages such feeds pointed at
carried the bit above; shown, it put a bare ``[feed 14]`` beside the gap.
"""
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
        self._names_source: Optional[sqlite3.Connection] = None
        """The user database snapshot the name map was read from.

        The cache hands back the same connection until that database changes,
        so a different one is the signal that a rename may have landed.
        """
        self._feed_names: dict[int, dict[int, str]] = {}
        self._feeds_lack: dict[int, set[int]] = {}
        self._members: Optional[dict[int, list[int]]] = None
        self._own_id: Optional[int] = None
        self._own_id_resolved = False
        self._selections: dict[int, str] = {}
        self._schema_sources: dict[int, sqlite3.Connection] = {}
        """Which snapshot of each room's log its schema was read from."""
        self._live_conditions: dict[int, str] = {}
        self._kept_conditions: dict[int, str] = {}

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
        chose for themselves, matching what the client shows. The map follows
        the user database: read again when that database has changed since,
        and kept otherwise, so the name is the one that user carries now.
        """
        self._revalidate_names()
        return self._known_names()

    def _revalidate_names(self) -> None:
        """Forget the name map if the user database moved since it was read.

        Asked once per read rather than once per message: a check is a
        fingerprint of the file, and a window is hundreds of messages. A
        rename writes ``talkUser`` and nothing else, so a map kept for the
        reader's whole life showed someone who renamed under the name they
        had when the interface opened.

        A failure to look is not a refresh. The map that was read stays in
        use and stays marked as read from the older snapshot, so the next
        read asks again; without any map, the failure is the caller's.
        """
        if self._names is None:
            return
        try:
            users = self._users()
        except KanaoError:
            return
        if users is not self._names_source:
            self._names = None

    def _known_names(self) -> dict[int, str]:
        """The name map as last read, reading it only when there is none."""
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
            self._names_source = users
            self._names_read_at = time.monotonic()
            if self._ledger is not None:
                # Once per snapshot of the user database: the map is kept
                # until that changes, and only a name that differs from its
                # last sighting is appended.
                self._ledger.observe(found)
        return self._names

    def unlock(
        self,
        room_id: int,
        attempts: int = UNLOCK_ATTEMPTS,
        settle: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Wait for the client to load a room's key, after its window opened.

        The key reaches memory only once the client opens the room's log,
        which it does on its own schedule after the window appears, so the
        heap is scanned again until the log pairs or the attempts run out.
        True when the room is readable now.
        """
        name = paths.chat_log_name(room_id)
        pause = settle if settle is not None else (lambda: time.sleep(UNLOCK_SECONDS))
        for attempt in range(attempts):
            self._keyring = Keyring.discover(self.user_directory, rescan=True)
            if self._keyring.has(name):
                return True
            if attempt + 1 < attempts:
                pause()
        return False

    def _sender_name(self, room_id: int, user_id: int) -> str:
        """Name *user_id*, looking again before settling for a number.

        The name map is read once per reader, and the interface keeps one
        reader for as long as it runs — so somebody who joined an open chat
        after the room was opened stayed ``User <id>`` until the room was
        opened again, when a fresh read found them. An unknown id now has
        the map read again, at most every :data:`NAME_RETRY_SECONDS`, and
        failing that the name the room's own join feed recorded for them.
        """
        found = self._known_names().get(user_id)
        if found is not None:
            return found
        if time.monotonic() - self._names_read_at >= NAME_RETRY_SECONDS:
            self._names = None
            found = self._known_names().get(user_id)
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
        # Settled only once the catalog has actually answered. Marking it
        # first let one BUSY read stand for "there is no account" for the
        # rest of the reader's life, and fromMe, mentionsMe and repliesToMe
        # all quietly went false with it.
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
                self._own_id_resolved = True
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
                self._own_id_resolved = True
                return self._own_id
        # The catalog answered and nothing in it proves who the account is.
        self._own_id_resolved = True
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
            return self._known_names().get(int(peer_id), title or f"User {peer_id}")
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
            names = self._known_names()
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
        # Every row is fetched before any title is composed. Composing one
        # can open the user database, and a cache whose budget cannot hold
        # both evicts the catalog - closing the connection this loop would
        # still be reading from. Least recently used is not "no longer used".
        rows = self._catalog().execute(sql, parameters).fetchall()
        self._revalidate_names()
        found: list[Room] = []
        for row in rows:
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

    def memberships(self, refresh: bool = False) -> dict[int, list[int]]:
        """Every room's active members, read once and kept until *refresh*.

        Kept because a send must not read the catalog: sending is what makes
        its snapshot stale, and a stale catalog costs a whole decryption.
        """
        if self._members is None or refresh:
            members: dict[int, list[int]] = {}
            for chat_id, user_id in self._catalog().execute(
                "SELECT chatId, userId FROM chatMembers WHERE COALESCE(isActive, 1) = 1"
            ):
                members.setdefault(int(chat_id), []).append(int(user_id))
            self._members = members
        return self._members

    def search_position(
        self,
        room_id: int,
        query: Optional[str] = None,
        rooms: Optional[Sequence[Room]] = None,
    ) -> Optional[int]:
        """Where the client's room search should list *room_id*, from zero.

        The search matches a room's title and the names of its members, and
        lists what it matches in the room list's own recency order — so a
        one-to-one room with somebody sat third, below two open chats that
        merely had them as a member, and opening the first result opened the
        wrong room. Checked live against that search: predicted third, shown
        third. None when the room would not be matched at all.
        """
        if rooms is None:
            rooms = self.rooms()
        target = next((room for room in rooms if room.id == room_id), None)
        if target is None:
            return None
        needle = (query if query is not None else target.title).casefold()
        if not needle:
            return None
        names = self.names()
        members = self.memberships()
        position = 0
        for room in rooms:
            matched = needle in room.title.casefold() or any(
                needle in names.get(user_id, "").casefold()
                for user_id in members.get(room.id, ())
            )
            if room.id == room_id:
                return position if matched else None
            if matched:
                position += 1
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
        self._schema_for(room_id, connection)
        return self._selections[room_id]

    def _live(self, room_id: int, connection: sqlite3.Connection) -> str:
        """The condition a row must meet to be a message nobody took back.

        Two columns say a message was deleted, and the newer one is absent
        from an older log, so it is asked for the way ``threadId`` is.
        """
        self._schema_for(room_id, connection)
        return self._live_conditions[room_id]

    def _kept(self, room_id: int, connection: sqlite3.Connection) -> str:
        """Like :meth:`_live`, but keeping messages deleted for everyone."""
        self._schema_for(room_id, connection)
        return self._kept_conditions[room_id]

    def _schema_for(self, room_id: int, connection: sqlite3.Connection) -> None:
        """Read a room's columns again whenever its snapshot is a new one.

        The schema is a property of the file, and the cache hands back a new
        connection exactly when the file changed, so that is the lifetime the
        answer is kept for. Asking costs one ``PRAGMA``; what it prevents is a
        column the client added mid-session being named as ``NULL`` forever.
        """
        if self._schema_sources.get(room_id) is not connection:
            self._read_schema(room_id, connection)
            self._schema_sources[room_id] = connection

    def _read_schema(self, room_id: int, connection: sqlite3.Connection) -> None:
        columns = {column[1] for column in connection.execute("PRAGMA table_info(chatLogs)")}
        thread = _THREAD_COLUMN if _THREAD_COLUMN in columns else "NULL"
        erased = f"(COALESCE(type, 0) & {_ERASED_TYPE_BIT}) != 0"
        if _ERASED_COLUMN in columns:
            erased = f"(COALESCE({_ERASED_COLUMN}, 0) != 0 OR {erased})"
        self._selections[room_id] = (
            f"logId, authorId, type, message, attachement, sendAt, {thread}, {erased}"
        )
        # A deletion writes a type-14 feed pointing at what it hid; the
        # placeholder stands where the message was, so the feed never shows.
        kept = (
            "COALESCE(deleted, 0) = 0 AND "
            f"CASE WHEN type = {model.FEED_MESSAGE} AND json_valid(message) "
            f"THEN json_extract(message, '$.feedType') END IS NOT {_ERASED_FEED}"
        )
        self._kept_conditions[room_id] = kept
        self._live_conditions[room_id] = f"{kept} AND NOT {erased}"

    def messages(
        self,
        room_id: int,
        limit: int = 40,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
        include_erased: bool = False,
    ) -> list[Message]:
        """Return up to *limit* messages, oldest first.

        *include_erased* keeps messages deleted for everyone, text withheld,
        so the interface can say "삭제된 메시지입니다" where the client does.
        Every other caller leaves them out, as it leaves out deleted rows.

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
        conditions = [
            self._kept(room_id, connection) if include_erased else self._live(room_id, connection)
        ]
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
        self._revalidate_names()
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

    def newest_log_id(self, room_id: int, include_erased: bool = False) -> Optional[int]:
        """Return the newest readable message id, without reading bodies.

        Deleted rows are excluded like everywhere else: this id is a position
        a follower resumes from, and naming a row no query will ever return
        is how a cursor comes to point at nothing. *include_erased* answers
        for the rows :meth:`messages` returns when asked for the same.

        Walked down the primary key rather than taken with ``MAX``: with a
        condition attached SQLite cannot use its min/max shortcut and scans
        the whole room, where this stops at the first row that qualifies.
        """
        connection = self._logs(room_id)
        condition = (
            self._kept(room_id, connection) if include_erased else self._live(room_id, connection)
        )
        row = connection.execute(
            f"SELECT logId FROM chatLogs WHERE {condition} ORDER BY logId DESC LIMIT 1"
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _message(self, room_id: int, row: "sqlite3.Row | tuple[Any, ...]") -> Message:
        log_id, author, type_code, text, attachment, sent_at, thread, erased = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        if erased:
            # What is left of it is encrypted, or on another client the very
            # words that were taken back; neither is shown, only the gap.
            return Message(
                id=int(log_id),
                room_id=room_id,
                sender_id=sender_id,
                sender=self._sender_name(room_id, sender_id),
                text="",
                type=kind,
                sent_at=None if sent_at is None else int(sent_at),
                from_me=self.own_user_id() is not None and sender_id == self.own_user_id(),
                erased=True,
            )
        known = self._known_names()
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
            video=model.video_in(kind, extra),
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
        _log_id, author, type_code, text, _attachment, _sent_at, _thread, _erased = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        return model.Quote(
            message_id=source,
            sender_id=sender_id,
            sender=self._known_names().get(sender_id),
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
        # rooms() has just looked at the user database, so the map it left is
        # the current one for every row below.
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
