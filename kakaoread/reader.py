"""Query the decrypted database, doing the least work that answers the question.

Everything is lazy.  Resolving the account means two PBKDF2 derivations, and
opening a snapshot means decrypting a whole file, so neither happens until a
caller actually needs it.  That is what lets ``status`` explain a missing
permission instead of failing to start for the same reason.

Three things about the macOS client's schema shape this file:

* **There is one database.**  Rooms, messages, users and open links are tables
  in a single file rather than a catalog plus a database per room, so there is
  no per-room key to be missing and every stored room is readable.
* **The signed-in account states itself.**  ``NTChatContext`` holds the user
  id, which the Windows schema never did — that build had to infer the account
  from whoever sat alone in the self-chat.
* **A room's kind is read from its shape, not from its type code.**  The type
  column is an integer here rather than a name, and an integer whose meaning
  this program has not confirmed is not something to brand a conversation
  with.  Whether a room carries an open-chat link, names a single counterpart,
  or holds nobody but the account itself are facts with no such ambiguity, so
  the kind is derived from those and the raw code is passed through untouched
  for a caller that knows better.

Column names are looked up rather than assumed for the same reason: this
schema is the client's, it has changed before, and a query naming a column
that has been renamed fails the whole conversation instead of the one field.
"""

from __future__ import annotations

import json
import plistlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import model, names as names_module, paths
from .errors import ErrorCode, KakaoReadError
from .keyring import Account, Keyring
from .model import Friend, Message, Room
from .names import NameLedger, NameRecord
from .snapshot import SnapshotCache

_SELF_CHAT_TITLE = "나와의 채팅"
_GROUP_TITLE_MEMBERS = 3

ROOM_TABLE = "NTChatRoom"
MESSAGE_TABLE = "NTChatMessage"
USER_TABLE = "NTUser"
LINK_TABLE = "NTOpenLink"
CONTEXT_TABLE = "NTChatContext"

_ATTACHMENT_COLUMNS = ("attachment", "attachement", "attachments", "extra")
"""What the attachment column may be called; the Windows client misspelled it."""

_DELETED_COLUMNS = ("deleted", "isDeleted")
_MESSAGE_TEXT_COLUMNS = ("message", "text")
_SENT_AT_COLUMNS = ("sentAt", "sendAt", "createdAt")


def _first_present(available: set[str], candidates: Iterable[str]) -> Optional[str]:
    """Return the first candidate this table actually has."""
    return next((name for name in candidates if name in available), None)


def _plist_user_ids(raw: object) -> list[int]:
    """Read the user ids out of a binary-plist member list.

    The client stores a group's display members as an archived array.  A plain
    array of numbers reads straight out; a keyed archive keeps them among its
    ``$objects``, so the integers are taken from there rather than trying to
    unarchive Objective-C objects this program has no use for.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw[:6] == b"bplist":
        return []
    try:
        loaded = plistlib.loads(bytes(raw))
    except Exception:  # noqa: BLE001 - a foreign format failing is not our failure
        return []
    if isinstance(loaded, list):
        return [int(item) for item in loaded if isinstance(item, int)]
    if isinstance(loaded, dict):
        objects = loaded.get("$objects")
        if isinstance(objects, list):
            return [item for item in objects if isinstance(item, int) and item > 0]
    return []


class Reader:
    """Read-only access to the signed-in KakaoTalk account on this Mac."""

    def __init__(
        self,
        cache: Optional[SnapshotCache] = None,
        keyring: Optional[Keyring] = None,
        ledger: Optional[NameLedger] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Build a reader without touching the disk or the client.

        *cache* and *keyring* exist so callers can supply an already-resolved
        account instead of paying for the derivation twice, and *connection* so
        tests can drive the query logic against a fixture database directly.

        *ledger* is opt-in on purpose.  Recording names is the only thing here
        that writes real conversation data to disk, so a reader built without
        one — every reader in the test suite — cannot touch the user's history.
        """
        self._cache = cache if cache is not None else SnapshotCache()
        self._keyring: Optional[Keyring] = keyring
        self._ledger = ledger
        self._connection = connection
        self._names: Optional[dict[int, str]] = None
        self._own_id: Optional[int] = None
        self._own_id_resolved = False
        self._columns: dict[str, set[str]] = {}

    @property
    def keyring(self) -> Keyring:
        """Resolve the account once, on first use."""
        if self._keyring is None:
            self._keyring = Keyring.discover()
        return self._keyring

    @property
    def account(self) -> Account:
        return self.keyring.require()

    @property
    def database(self) -> Path:
        return self.account.database

    def close(self) -> None:
        self._cache.close()

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    # -- the one database ----------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """Return the decrypted database, decrypting only when it has changed."""
        if self._connection is not None:
            return self._connection
        account = self.account
        try:
            return self._cache.connect(account.database, account.geometry)
        except sqlite3.DatabaseError:
            # If a malformed snapshot was encountered mid-write, invalidate and retry once cleanly.
            self._cache.invalidate(account.database)
            return self._cache.connect(account.database, account.geometry)

    def columns(self, table: str) -> set[str]:
        """Return the columns a table has, asked once per table per process."""
        known = self._columns.get(table)
        if known is None:
            known = {
                str(row[1])
                for row in self.connection().execute(f"PRAGMA table_info({table})")
            }
            self._columns[table] = known
        return known

    def _require_table(self, table: str) -> set[str]:
        columns = self.columns(table)
        if not columns:
            raise KakaoReadError(
                ErrorCode.READ_FAILED,
                f"this database has no {table} table, so the client's schema has "
                "changed in a way this build does not understand",
                table=table,
            )
        return columns

    # -- diagnostics ---------------------------------------------------------

    def status(self) -> dict[str, object]:
        """Describe access without requiring any of it to be working."""
        try:
            ring = self.keyring
        except KakaoReadError as error:
            return {
                "ready": False,
                "account": None,
                "database": None,
                "rooms": 0,
                "detail": error.message,
            }

        if ring.account is None:
            detail = (
                "no KakaoTalk database was found in this machine's container"
                if not ring.databases
                else "a database is present but no account on this machine opens it"
            )
            return {
                "ready": False,
                "account": None,
                "database": None,
                "rooms": 0,
                "detail": detail,
            }

        cached_rooms = self._cached_status_rooms(ring.account.database)
        if cached_rooms is not None:
            return {
                "ready": True,
                "account": ring.account.user,
                "database": ring.account.database.name,
                "rooms": cached_rooms,
                "detail": "ready",
            }

        try:
            rooms = self.count_rooms()
            self._save_status_rooms(ring.account.database, rooms)
        except KakaoReadError as error:
            return {
                "ready": False,
                "account": ring.account.user,
                "database": ring.account.database.name,
                "rooms": 0,
                "detail": error.message,
            }
        return {
            "ready": True,
            "account": ring.account.user,
            "database": ring.account.database.name,
            "rooms": rooms,
            "detail": "ready",
        }

    def _status_cache_file(self) -> Path:
        return paths.state_directory() / "status.json"

    def _cached_status_rooms(self, database: Path) -> Optional[int]:
        path = self._status_cache_file()
        try:
            stored = json.loads(path.read_text("utf-8"))
            if not isinstance(stored, dict):
                return None
            fp = stored.get("fingerprint")
            current = (
                database.stat().st_mtime_ns,
                database.stat().st_size,
            )
            if fp == list(current) or fp == tuple(current):
                return int(stored["rooms"])
        except (OSError, ValueError, KeyError):
            return None
        return None

    def _save_status_rooms(self, database: Path, rooms: int) -> None:
        path = self._status_cache_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            current = (database.stat().st_mtime_ns, database.stat().st_size)
            path.write_text(
                json.dumps({"fingerprint": current, "rooms": rooms}),
                encoding="utf-8",
            )
        except OSError:
            pass

    def count_rooms(self) -> int:
        self._require_table(ROOM_TABLE)
        row = self.connection().execute(f"SELECT COUNT(*) FROM {ROOM_TABLE}").fetchone()
        return int(row[0])

    def verify(self) -> dict[str, object]:
        """Prove decryption works by opening the database and counting its rooms."""
        return {"verified": True, "rooms": self.count_rooms(), "readableRooms": self.count_rooms()}

    # -- people --------------------------------------------------------------

    def names(self) -> dict[int, str]:
        """Map every known user id to the best available display name.

        A name the signed-in user assigned wins over the name the contact chose
        for themselves, matching what the client shows.
        """
        if self._names is not None:
            return self._names

        columns = self.columns(USER_TABLE)
        found: dict[int, str] = {}
        if "userId" in columns:
            preferred = [
                name
                for name in ("friendNickName", "displayName", "nickName")
                if name in columns
            ]
            if preferred:
                sql = f"SELECT userId, {', '.join(preferred)} FROM {USER_TABLE}"
                for row in self.connection().execute(sql):
                    user_id = row[0]
                    if user_id is None:
                        continue
                    name = next(
                        (str(value).strip() for value in row[1:] if str(value or "").strip()),
                        "",
                    )
                    if name:
                        found[int(user_id)] = name
        self._names = found
        if self._ledger is not None:
            # Once per process: the map is memoised above, and only a name that
            # differs from its last sighting is appended.
            self._ledger.observe(found)
        return found

    def own_user_id(self) -> Optional[int]:
        """Identify the signed-in account, or None when it cannot be proven.

        The client writes it down here, so unlike the Windows build there is
        nothing to infer: the account is stated, and the derived one from the
        container's filename agrees with it.
        """
        if self._own_id_resolved:
            return self._own_id
        self._own_id_resolved = True

        if "userId" in self.columns(CONTEXT_TABLE):
            row = self.connection().execute(
                f"SELECT userId FROM {CONTEXT_TABLE} WHERE userId IS NOT NULL LIMIT 1"
            ).fetchone()
            if row is not None and row[0]:
                self._own_id = int(row[0])
                return self._own_id

        # Failing that, the account that derived this database's filename is
        # the account signed into it.
        if self._keyring is not None and self._keyring.account is not None:
            self._own_id = self._keyring.account.user_id
        return self._own_id

    def friends(self, favorites_only: bool = False) -> list[Friend]:
        """Return the signed-in user's friend list from the local database."""
        columns = self._require_table(USER_TABLE)
        wanted = {
            "id": "userId",
            "friend_nick": "friendNickName",
            "nick": _first_present(columns, ("nickName", "displayName")),
            "phone": _first_present(columns, ("phoneNumber", "phone")),
            "status": _first_present(columns, ("statusMessage", "message")),
            "direct": _first_present(columns, ("directChatId", "chatId")),
            "favorite": _first_present(columns, ("favorite", "isFavorite")),
        }
        selected = [(key, name) for key, name in wanted.items() if name and name in columns]
        sql = f"SELECT {', '.join(name for _key, name in selected)} FROM {USER_TABLE}"

        conditions: list[str] = []
        parameters: list[object] = []
        # Taken from a live database: the column is `friendType`, and 1 is the
        # list the client shows as 친구. The other values are known
        # non-friends (1000, 699 rows), removed or one-way relationships
        # (-1, 323 rows) and a handful of special ones (9, -3, -4, -10) — the
        # signed-in account's own row among them, which is why filtering on
        # this is also what keeps the user out of their own friend list.
        friend_type = _first_present(columns, ("friendType", "type"))
        if friend_type:
            conditions.append(f"{friend_type} = ?")
            parameters.append(model.FRIEND_TYPE)
        if "linkId" in columns:
            # An open-chat identity is a different row for the same person and
            # is not a friend entry, so it is left out.
            conditions.append("COALESCE(linkId, 0) = 0")
        if favorites_only and wanted["favorite"]:
            conditions.append(f"{wanted['favorite']} = 1")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        friends: list[Friend] = []
        for row in self.connection().execute(sql, parameters):
            values = dict(zip((key for key, _name in selected), row))
            user_id = values.get("id")
            if user_id is None:
                continue
            nick = values.get("nick")
            friend_nick = values.get("friend_nick")
            name = (str(friend_nick or "") or str(nick or "")).strip() or f"User {user_id}"
            direct = values.get("direct")
            friends.append(
                Friend(
                    id=int(user_id),
                    name=name,
                    nickname=str(nick) if nick else None,
                    phone=str(values.get("phone")) if values.get("phone") else None,
                    status_message=str(values.get("status")) if values.get("status") else None,
                    direct_chat_id=int(direct) if direct else None,
                    favorite=bool(values.get("favorite")),
                )
            )
        friends.sort(key=lambda friend: (not friend.favorite, friend.name.casefold()))
        return friends

    # -- rooms ---------------------------------------------------------------

    def _link_names(self) -> dict[int, str]:
        """Map each open-chat link to its name, when this client keeps any."""
        columns = self.columns(LINK_TABLE)
        if "linkId" not in columns:
            return {}
        column = _first_present(columns, ("linkName", "name", "displayName"))
        if column is None:
            return {}
        return {
            int(link_id): str(name).strip()
            for link_id, name in self.connection().execute(
                f"SELECT linkId, {column} FROM {LINK_TABLE}"
            )
            if link_id and str(name or "").strip()
        }

    def member_ids(self, room_id: int) -> set[int]:
        """Return all known participants for a room.

        Combines display thumbnail members (displayMemberIds), open chat link
        participants (NTUser.linkId), and authors who have posted messages
        (NTChatMessage.authorId) so that mentions and name history cover the
        full roll rather than just the avatar sample.
        """
        columns = self.columns(ROOM_TABLE)
        column = _first_present(columns, ("displayMemberIds", "memberIds"))
        members: set[int] = set()

        if column is not None:
            has_link = "linkId" in columns
            query = (
                f"SELECT {column}, linkId FROM {ROOM_TABLE} WHERE chatId = ?"
                if has_link
                else f"SELECT {column} FROM {ROOM_TABLE} WHERE chatId = ?"
            )
            row = self.connection().execute(query, (room_id,)).fetchone()
            if row is not None:
                members.update(_plist_user_ids(row[0]))
                if has_link and row[1]:
                    try:
                        user_rows = self.connection().execute(
                            "SELECT userId FROM NTUser WHERE linkId = ?", (row[1],)
                        ).fetchall()
                        members.update(int(ur[0]) for ur in user_rows if ur[0])
                    except sqlite3.Error:
                        pass

        # Also include any author who has sent a message in this room
        try:
            msg_rows = self.connection().execute(
                f"SELECT DISTINCT authorId FROM {MESSAGE_TABLE} WHERE chatId = ?",
                (room_id,),
            ).fetchall()
            members.update(int(mr[0]) for mr in msg_rows if mr[0])
        except sqlite3.Error:
            pass

        return members

    def _kind_of(
        self, link_id: int, peer_id: int, member_ids: list[int], own: Optional[int]
    ) -> str:
        """Name a room by its shape rather than by an unconfirmed type code."""
        if link_id:
            return model.OPEN_DIRECT_CHAT if peer_id else model.OPEN_GROUP_CHAT
        if peer_id:
            return model.DIRECT_CHAT
        if own is not None and member_ids and set(member_ids) <= {own}:
            return model.SELF_CHAT
        return model.GROUP_CHAT

    def rooms(self, limit: Optional[int] = None) -> list[Room]:
        """Return conversations, most recently active first."""
        columns = self._require_table(ROOM_TABLE)
        name_column = _first_present(columns, ("chatName", "title", "chatRoomTitle"))
        members_column = _first_present(columns, ("displayMemberIds", "memberIds"))
        selected = [
            "chatId",
            "type" if "type" in columns else "NULL",
            name_column or "NULL",
            _first_present(columns, ("activeMembersCount", "memberCount")) or "NULL",
            _first_present(columns, ("countOfNewMessage", "newMessageCount", "unreadCount"))
            or "NULL",
            "lastLogId" if "lastLogId" in columns else "NULL",
            _first_present(columns, ("lastUpdatedAt", "updatedAt")) or "NULL",
            _first_present(columns, ("directChatMemberUserId", "directChatMemberId")) or "NULL",
            "linkId" if "linkId" in columns else "NULL",
            members_column or "NULL",
        ]
        sql = f"SELECT {', '.join(selected)} FROM {ROOM_TABLE}"
        order = _first_present(columns, ("lastUpdatedAt", "updatedAt"))
        if order:
            sql += f" ORDER BY {order} IS NULL, {order} DESC"
        parameters: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)

        own = self.own_user_id()
        links = self._link_names()
        known = self.names()

        found: list[Room] = []
        for row in self.connection().execute(sql, parameters):
            (
                room_id, type_code, stored, members, unread,
                last_log, updated, peer, link_id, raw_members,
            ) = row
            room_id = int(room_id)
            peer_id = int(peer or 0)
            link = int(link_id or 0)
            member_ids = _plist_user_ids(raw_members)
            kind = self._kind_of(link, peer_id, member_ids, own)
            found.append(
                Room(
                    id=room_id,
                    type=kind,
                    title=self._title(
                        room_id, kind, stored, peer_id, link, member_ids, links, known, own
                    ),
                    member_count=int(members or 0) or len(member_ids),
                    unread=int(unread or 0),
                    last_log_id=None if last_log is None else int(last_log),
                    last_updated_at=None if updated is None else int(updated),
                    readable=True,
                    type_code=None if type_code is None else int(type_code),
                )
            )
        return found

    def _title(
        self,
        room_id: int,
        kind: str,
        stored: object,
        peer_id: int,
        link_id: int,
        member_ids: list[int],
        links: dict[int, str],
        known: dict[int, str],
        own: Optional[int],
    ) -> str:
        """Name a room the way the client shows it, in the client's own order."""
        title = str(stored or "").strip()
        if title:
            return title
        if kind == model.SELF_CHAT:
            return _SELF_CHAT_TITLE
        if link_id and links.get(link_id):
            return links[link_id]
        if peer_id:
            return known.get(peer_id) or f"User {peer_id}"

        others = [member for member in member_ids if member != own]
        if others:
            shown = [known.get(member, f"User {member}") for member in others[:_GROUP_TITLE_MEMBERS]]
            if len(others) > _GROUP_TITLE_MEMBERS:
                shown.append(f"외 {len(others) - _GROUP_TITLE_MEMBERS}명")
            return ", ".join(shown)
        return f"Room {room_id}"

    def stored_title(self, room_id: int) -> Optional[str]:
        """Return the client's own stored title for a room, before resolution."""
        columns = self.columns(ROOM_TABLE)
        column = _first_present(columns, ("chatName", "title", "chatRoomTitle"))
        if column is None:
            return None
        row = self.connection().execute(
            f"SELECT {column} FROM {ROOM_TABLE} WHERE chatId = ?", (room_id,)
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

    def _message_query(self) -> tuple[str, str]:
        """Return the column list for a message row and the deleted predicate."""
        columns = self._require_table(MESSAGE_TABLE)
        text = _first_present(columns, _MESSAGE_TEXT_COLUMNS) or "NULL"
        attachment = _first_present(columns, _ATTACHMENT_COLUMNS) or "NULL"
        sent_at = _first_present(columns, _SENT_AT_COLUMNS) or "NULL"
        deleted = _first_present(columns, _DELETED_COLUMNS)
        predicate = f"COALESCE({deleted}, 0) = 0" if deleted else "1 = 1"
        return f"logId, authorId, type, {text}, {attachment}, {sent_at}", predicate

    def messages(
        self,
        room_id: int,
        limit: int = 40,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
    ) -> list[Message]:
        """Return up to *limit* messages ending at the newest, oldest first.

        *before_id* pages backwards through history; *after_id* collects what
        has arrived since a known message, which is how following works.

        Ordered by ``logId`` rather than by the timestamp: it is the key the
        client indexes, and it is monotonic per room.
        """
        selected, deleted = self._message_query()
        conditions = ["chatId = ?", deleted]
        parameters: list[object] = [room_id]
        if before_id is not None:
            conditions.append("logId < ?")
            parameters.append(before_id)
        if after_id is not None:
            conditions.append("logId > ?")
            parameters.append(after_id)
        sql = (
            f"SELECT {selected} FROM {MESSAGE_TABLE} WHERE {' AND '.join(conditions)} "
            "ORDER BY logId DESC LIMIT ?"
        )
        parameters.append(limit)

        rows = self.connection().execute(sql, parameters).fetchall()
        return [self._message(room_id, row) for row in reversed(rows)]

    def count_messages(self, room_id: int) -> int:
        """Return how many undeleted messages are stored locally for a room."""
        _selected, deleted = self._message_query()
        row = self.connection().execute(
            f"SELECT COUNT(*) FROM {MESSAGE_TABLE} WHERE chatId = ? AND {deleted}",
            (room_id,),
        ).fetchone()
        return int(row[0])

    def newest_log_id(self, room_id: int) -> Optional[int]:
        """Return the newest locally stored message id, without reading bodies."""
        self._require_table(MESSAGE_TABLE)
        row = self.connection().execute(
            f"SELECT MAX(logId) FROM {MESSAGE_TABLE} WHERE chatId = ?", (room_id,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _message(self, room_id: int, row: "sqlite3.Row | tuple[Any, ...]") -> Message:
        log_id, author, type_code, text, attachment, sent_at = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        known = self.names()
        own = self.own_user_id()
        # The attachment is read for what it records about *other* messages —
        # who this one named, and which one it answered. It is never turned
        # into message text, which would show words nobody typed.
        extra = model.parse_attachment(attachment)
        mentions = model.mentions_in(extra, known.get)
        reply = model.quoted_in(extra, known.get)
        return Message(
            id=int(log_id),
            room_id=room_id,
            sender_id=sender_id,
            sender=known.get(sender_id, f"User {sender_id}"),
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
        )

    def feed_rows(self, room_id: int) -> Iterator[tuple[Optional[int], str]]:
        """Yield this room's membership bookkeeping rows as ``(sentAt, json)``."""
        columns = self.columns(MESSAGE_TABLE)
        text = _first_present(columns, _MESSAGE_TEXT_COLUMNS)
        if text is None:
            return
        sent_at = _first_present(columns, _SENT_AT_COLUMNS) or "NULL"
        for moment, body in self.connection().execute(
            f"SELECT {sent_at}, {text} FROM {MESSAGE_TABLE} WHERE chatId = ? AND type = ?",
            (room_id, model.FEED_MESSAGE),
        ):
            if isinstance(body, str) and body:
                yield (None if moment is None else int(moment)), body

    # -- names over time -----------------------------------------------------

    def name_history(self, room_id: int) -> dict[int, list[NameRecord]]:
        """Return every name each user has been seen under in one room.

        Two sources, and they answer different questions.  The room's own feed
        rows reach back to whenever the client first logged a join, but record
        a name only at a join, leave or invite.  The ledger covers only what
        this tool has read since it was enabled, but catches a plain rename,
        which the client never writes down at all.
        """
        records = list(names_module.from_feeds(self.feed_rows(room_id)))
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

        The recorded history is searched together with the names the database
        shows right now, because the client logs a name at a join and not at a
        rename: the name somebody goes by today is routinely the one no feed
        ever captured.
        """
        history = self.name_history(room_id) if history is None else history
        current = self.names()
        needle = name.casefold().strip()
        universe = set(history) | self.member_ids(room_id)
        return sorted(
            set(names_module.carriers(history, name))
            | {user for user in universe if (current.get(user) or "").casefold() == needle}
        )

    # -- search and raw access ----------------------------------------------

    def search(self, keyword: str, limit: int = 20) -> Iterator[Message]:
        """Search every room at once, newest first, stopping at *limit*.

        One database means one query rather than one per room, and the room a
        hit belongs to comes back with it.
        """
        if limit <= 0:
            return
        selected, deleted = self._message_query()
        columns = self.columns(MESSAGE_TABLE)
        text = _first_present(columns, _MESSAGE_TEXT_COLUMNS)
        if text is None:
            return
        rows = self.connection().execute(
            # Feeds are excluded rather than filtered afterwards: their message
            # column is JSON, so a search for an ordinary word like "type"
            # would match rows that show no text at all.
            f"SELECT chatId, {selected} FROM {MESSAGE_TABLE} "
            f"WHERE {deleted} AND type != ? AND {text} LIKE ? "
            "ORDER BY logId DESC LIMIT ?",
            (model.FEED_MESSAGE, f"%{keyword}%", limit),
        ).fetchall()
        for row in rows:
            yield self._message(int(row[0]), row[1:])

    def query(self, sql: str, room_id: Optional[int] = None) -> list[dict[str, object]]:
        """Run one read-only statement against the decrypted database."""
        connection = self.connection()
        connection.execute("PRAGMA query_only = ON")
        try:
            cursor = connection.execute(sql)
        except sqlite3.Error as error:
            raise KakaoReadError(ErrorCode.INVALID_REQUEST, str(error)) from error
        columns = [description[0] for description in (cursor.description or ())]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
