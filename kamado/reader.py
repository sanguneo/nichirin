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
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from . import model, names as names_module, paths
from .errors import ErrorCode, KamadoError
from .keyring import Account, Keyring
from .model import Friend, Message, Room
from .names import NameLedger, NameRecord
from .snapshot import Fingerprint, SnapshotCache, SnapshotSource

_SELF_CHAT_TITLE = "나와의 채팅"
_GROUP_TITLE_MEMBERS = 3

ROOM_TABLE = "NTChatRoom"
MESSAGE_TABLE = "NTChatMessage"
NAME_RETRY_SECONDS = 5.0

_IN_CHUNK = 500
"""How many ids one ``IN`` list carries, well inside SQLite's variable limit."""


def _version(row: Sequence[Any]) -> int:
    """A digest of the columns one message is built from.

    Only compared with another taken in the same process, so Python's own
    hash is enough; it is not a checksum and is never stored.
    """
    return hash(tuple(row))
"""How long an unknown sender waits before the name map is read again."""
USER_TABLE = "NTUser"
LINK_TABLE = "NTOpenLink"
CONTEXT_TABLE = "NTChatContext"
META_TABLE = "NTChatMeta"
_META_NAME_TYPE = 3
"""The meta row carrying the name the client shows for a room.

Type 1 is the room's notice and the rest is call and counter bookkeeping, so
the type is what separates a name from the other things kept beside it.
"""

_ATTACHMENT_COLUMNS = ("attachment", "attachement", "attachments", "extra")
"""What the attachment column may be called; the Windows client misspelled it."""

_THREAD_COLUMNS = ("threadId",)
"""Where a newer client points a reply at the message it answers.

The older shape kept the reply's own copy of what it answered inside the
attachment. This one writes an ordinary text row and records only the id, so
the quote has to be read back out of the conversation.
"""

_DELETED_COLUMNS = ("deleted", "isDeleted")
_ERASED_TYPE_BIT = 0x4000
"""Added to a message's type when it is deleted for everyone.

The row keeps its text — a line typed and deleted went on showing as that
line — and ``deleted`` stays 0. Measured over the newest 20,000 rows: every
one of the 255 messages a deletion feed points at carried this bit. The
Windows build found the same bit on the same kind of row, alongside a flag
column this schema does not have.
"""
_ERASED_FEED = 14
"""The feed a deletion writes: ``{"feedType": 14, "logId": …, "hidden": true}``.

It is the client recording that it hid a message — ``byHost`` when a host did
it — so it is bookkeeping about a row that is no longer shown, and showing it
put a bare ``[feed 14]`` where the message had been.
"""
_MESSAGE_TEXT_COLUMNS = ("message", "text")
_SENT_AT_COLUMNS = ("sentAt", "sendAt", "createdAt")

LOG_META_TABLE = "NTChatLogMeta"
"""Per-message side rows; type 2 is a message's 공감."""
_REVISION_COLUMN = "revision"
"""Raised when the sender edits a message: 45 of 63,766 rows measured at 1."""
_LOCAL_PATH_COLUMN = "localFilePath"
"""Where the client saved a file, photo or video somebody chose to keep.

An absolute path the user picked (Downloads, Desktop, …), set on 36 rows and
still present for one of them, so it is a hint to be checked, never trusted.
"""
_NOTICE_META_TYPE = 1
_HIDDEN_FEEDS = (_ERASED_FEED, model.EDIT_FEED)
"""Feeds that stand for a change to another message, marked where it happened."""
_ATTACHMENT_TYPES = (
    model.PHOTO_MESSAGE,
    model.MULTI_PHOTO_MESSAGE,
    model.VIDEO_MESSAGE,
    model.FILE_MESSAGE,
)
_LINK_TYPES = (model.TEXT_MESSAGE, model.REPLY_MESSAGE)


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
        cache: Optional[SnapshotSource] = None,
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
        # A cache that was handed in belongs to whoever built it, and closing it
        # would shut every snapshot they are still using -- including the ones
        # that make a second reader cheap, which is the whole reason to share it.
        self._owns_cache = cache is None
        self._keyring: Optional[Keyring] = keyring
        self._ledger = ledger
        self._connection = connection
        self._names: Optional[dict[int, str]] = None
        self._names_read_at = 0.0
        self._feed_names: dict[int, dict[int, str]] = {}
        self._feeds_lack: dict[int, set[int]] = {}
        self._own_id: Optional[int] = None
        self._own_id_resolved = False
        self._columns: dict[str, set[str]] = {}
        # The snapshot the name map and the column sets were read from. The
        # cache hands back the same connection until the database changes, so
        # a different one is the signal that a rename or a schema change may
        # have landed; both are then asked again, once per snapshot rather
        # than once per message.
        self._snapshot: Optional[sqlite3.Connection] = None
        self.reveal_covered = False
        """Whether a message SafeBot covered keeps its text.

        Off by default, the way the client shows it; the interface's
        ``/safebot`` and the commands' ``--show-covered`` turn it on.
        """

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
        if self._owns_cache:
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
            connection = self._cache.connect(account.database, account.geometry)
        except sqlite3.DatabaseError:
            # If a malformed snapshot was encountered mid-write, invalidate and retry once cleanly.
            self._cache.invalidate(account.database)
            connection = self._cache.connect(account.database, account.geometry)
        if connection is not self._snapshot:
            # Only a snapshot that opened replaces the old one: a failed
            # connect raised above and left the previous map in place.
            self._snapshot = connection
            self._columns = {}
            self._names = None
        return connection

    def columns(self, table: str) -> set[str]:
        """Return the columns a table has, asked once per table per snapshot."""
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
            raise KamadoError(
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
        except KamadoError as error:
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

        # Sampled before counting: a count is filed under the state it was
        # counted from, never under one that arrived while it was counted.
        sample = self._status_key(ring.account)
        cached_rooms = self._cached_status_rooms(sample)
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
            self._save_status_rooms(sample, rooms)
        except KamadoError as error:
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

    @staticmethod
    def _status_key(account: Account) -> dict[str, object]:
        """What a cached room count is valid for: this account, this file, this state.

        The log is part of the state. A room added or removed while the
        client runs lands in the write-ahead log and leaves the database
        file's stat alone, so a key on the file alone kept answering the old
        count; and a second account's database with an equal stat would have
        been handed the first one's.
        """
        database = account.database
        sample = Fingerprint.of(database)
        return {
            "account": str(account.user_id),
            "database": str(database),
            "fingerprint": [*sample.database, *sample.log],
        }

    def _cached_status_rooms(self, key: dict[str, object]) -> Optional[int]:
        path = self._status_cache_file()
        try:
            stored = json.loads(path.read_text("utf-8"))
            if not isinstance(stored, dict):
                return None
            if any(stored.get(name) != value for name, value in key.items()):
                return None
            rooms = stored.get("rooms")
            # A file that says something else is a miss, not an answer.
            if not isinstance(rooms, int) or isinstance(rooms, bool) or rooms < 0:
                return None
            return rooms
        except (OSError, ValueError):
            return None

    def _save_status_rooms(self, key: dict[str, object], rooms: int) -> None:
        path = self._status_cache_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**key, "rooms": rooms}), encoding="utf-8")
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
        for themselves, matching what the client shows. The map follows the
        snapshot it was read from, so somebody who renamed is shown under the
        name they carry now rather than the one they had when the reader
        opened. :meth:`connection` forgets it when the snapshot moves, and
        every read asks for its connection before it names anybody, so a
        check here would only repeat that stat once per message.
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
        self._names_read_at = time.monotonic()
        if self._ledger is not None:
            # Once per snapshot: the map is kept until the database changes,
            # and only a name that differs from its last sighting is appended.
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
        # Settled only once the database has actually answered. Marking it
        # first let one failed read stand for "there is no account" for the
        # rest of the reader's life, and fromMe, mentionsMe and repliesToMe
        # all quietly went false with it.
        if "userId" in self.columns(CONTEXT_TABLE):
            row = self.connection().execute(
                f"SELECT userId FROM {CONTEXT_TABLE} WHERE userId IS NOT NULL LIMIT 1"
            ).fetchone()
            if row is not None and row[0]:
                self._own_id = int(row[0])
                self._own_id_resolved = True
                return self._own_id

        # Failing that, the account that derived this database's filename is
        # the account signed into it.
        if self._keyring is not None and self._keyring.account is not None:
            self._own_id = self._keyring.account.user_id
        self._own_id_resolved = True
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

    def _meta_names(self) -> dict[int, str]:
        """Map each room to the name it was given in the client, where there is one.

        A rename does not touch the room row: ``chatName`` stays null and the
        name goes to the meta table, which is why a named group otherwise came
        out titled after the people in it — and why sending to it aimed at a
        window title the client never shows. ``NTChatRoom.metaRevisions`` names
        the revision in force, and a rename only ever writes a higher one, so
        reading them in order and letting the last win lands on the same name
        without parsing that blob for every room.
        """
        columns = self.columns(META_TABLE)
        if not {"chatId", "type", "content"} <= columns:
            return {}
        order = " ORDER BY revision" if "revision" in columns else ""
        return {
            int(chat_id): str(content).strip()
            for chat_id, content in self.connection().execute(
                f"SELECT chatId, content FROM {META_TABLE} WHERE type = ?{order}",
                (_META_NAME_TYPE,),
            )
            if chat_id and str(content or "").strip()
        }

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
        """Return the users this room lists, as far as the client records them.

        The macOS client keeps a group's members as an archived array on the
        room row rather than in a join table, so this is a decode rather than a
        query, and it is the display list — enough to name a room, not a roll.
        """
        columns = self.columns(ROOM_TABLE)
        column = _first_present(columns, ("displayMemberIds", "memberIds"))
        if column is None:
            return set()
        row = self.connection().execute(
            f"SELECT {column} FROM {ROOM_TABLE} WHERE chatId = ?", (room_id,)
        ).fetchone()
        if row is None:
            return set()
        return set(_plist_user_ids(row[0]))

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
        metas = self._meta_names()
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
                        room_id, kind, stored, peer_id, link, member_ids, links,
                        metas, known, own
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
        metas: dict[int, str],
        known: dict[int, str],
        own: Optional[int],
    ) -> str:
        """Name a room the way the client shows it, in the client's own order."""
        title = str(stored or "").strip()
        if title:
            return title
        named = metas.get(room_id)
        if named:
            return named
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
        # A renamed room keeps its name in the meta table instead, and that is
        # the name its window carries.
        return self._meta_names().get(room_id)

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

    def _message_query(self, include_erased: bool = False) -> tuple[str, str]:
        """Return the column list for a message row and the deleted predicate."""
        columns = self._require_table(MESSAGE_TABLE)
        text = _first_present(columns, _MESSAGE_TEXT_COLUMNS) or "NULL"
        attachment = _first_present(columns, _ATTACHMENT_COLUMNS) or "NULL"
        sent_at = _first_present(columns, _SENT_AT_COLUMNS) or "NULL"
        thread = _first_present(columns, _THREAD_COLUMNS) or "NULL"
        deleted = _first_present(columns, _DELETED_COLUMNS)
        conditions = [f"COALESCE({deleted}, 0) = 0"] if deleted else []
        erased = f"(COALESCE(type, 0) & {_ERASED_TYPE_BIT}) != 0"
        if not include_erased:
            conditions.append(f"NOT {erased}")
        if text != "NULL":
            # CASE keeps json_extract away from a body that is not JSON, which
            # would fail the whole statement rather than the one row. A
            # deletion writes a type-14 feed and an edit a type-25 one; the
            # mark stands where the message is, so neither feed ever shows.
            hidden = ", ".join(str(code) for code in _HIDDEN_FEEDS)
            conditions.append(
                f"COALESCE(CASE WHEN type = {model.FEED_MESSAGE} AND json_valid({text}) "
                f"THEN json_extract({text}, '$.feedType') END, -1) NOT IN ({hidden})"
            )
        predicate = " AND ".join(conditions)
        revision = (
            f"COALESCE({_REVISION_COLUMN}, 0)" if _REVISION_COLUMN in columns else "0"
        )
        # Read with the row it belongs to, so a 공감 added later changes the
        # row's version and the screen rebuilds it like any other change.
        # The table's key is (logId, chatId, type), so this is one lookup.
        meta = self.columns(LOG_META_TABLE)
        reactions = (
            f"(SELECT content FROM {LOG_META_TABLE} "
            f"WHERE {LOG_META_TABLE}.logId = {MESSAGE_TABLE}.logId "
            f"AND {LOG_META_TABLE}.chatId = {MESSAGE_TABLE}.chatId "
            f"AND {LOG_META_TABLE}.type = {model.REACTION_META})"
            if {"logId", "chatId", "type", "content"} <= meta
            else "NULL"
        )
        return (
            f"logId, authorId, type, {text}, {attachment}, {sent_at}, {thread}, {erased}, "
            f"{revision}, {reactions}",
            predicate,
        )

    def messages(
        self,
        room_id: int,
        limit: int = 40,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
        include_erased: bool = False,
    ) -> list[Message]:
        """Return up to *limit* messages, oldest first.

        *before_id* pages backwards through history; *after_id* collects what
        has arrived since a known message, which is how following works.

        Which end the limit cuts from is decided by that difference.  Asked
        for a window, the newest messages are the ones worth having.  Asked
        for what arrived since a known message, they are not: taking the
        newest page there skips everything between the cursor and that page,
        and a caller that then advances its cursor never sees the difference
        again.  KakaoTalk backfills from the server after a reconnection, so a
        room really can gain thousands of rows between two polls.

        Ordered by ``logId`` rather than by the timestamp: it is the key the
        client indexes, and it is monotonic per room.
        """
        selected, deleted = self._message_query(include_erased)
        conditions = ["chatId = ?", deleted]
        parameters: list[object] = [room_id]
        if before_id is not None:
            conditions.append("logId < ?")
            parameters.append(before_id)
        if after_id is not None:
            conditions.append("logId > ?")
            parameters.append(after_id)
        forwards = after_id is not None and before_id is None
        sql = (
            f"SELECT {selected} FROM {MESSAGE_TABLE} WHERE {' AND '.join(conditions)} "
            f"ORDER BY logId {'ASC' if forwards else 'DESC'} LIMIT ?"
        )
        parameters.append(limit)

        rows = self.connection().execute(sql, parameters).fetchall()
        if not forwards:
            rows = list(reversed(rows))
        return [self._message(room_id, row) for row in rows]

    def count_messages(self, room_id: int) -> int:
        """Return how many undeleted messages are stored locally for a room."""
        _selected, deleted = self._message_query()
        row = self.connection().execute(
            f"SELECT COUNT(*) FROM {MESSAGE_TABLE} WHERE chatId = ? AND {deleted}",
            (room_id,),
        ).fetchone()
        return int(row[0])

    def newest_log_id(self, room_id: int, include_erased: bool = False) -> Optional[int]:
        """Return the newest locally stored message id, without reading bodies.

        Deleted rows are excluded, as they are everywhere else: this id is
        where following resumes, and one that names a withdrawn message names
        a position no query will ever return a message at. *include_erased*
        answers for the rows :meth:`messages` returns when asked for the same.
        """
        _selected, deleted = self._message_query(include_erased)
        row = self.connection().execute(
            f"SELECT MAX(logId) FROM {MESSAGE_TABLE} WHERE chatId = ? AND {deleted}",
            (room_id,),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def row_versions(
        self, room_id: int, low: int, high: int, include_erased: bool = False
    ) -> dict[int, int]:
        """Say what each row between two ids currently holds, without reading it.

        A message already on screen can change without a new one arriving:
        deleted for everyone, it keeps its id and has its type and text
        rewritten, and a follower that only asks for higher ids never looks at
        it again. The version is a digest of the columns a message is built
        from, so a row that changed has a different one; a row no longer
        returned at all - deleted, or a feed that is never shown - is absent.

        One range read and no names, quotes or JSON, which is what makes
        asking after every move of the database affordable.
        """
        selected, deleted = self._message_query(include_erased)
        rows = self.connection().execute(
            f"SELECT {selected} FROM {MESSAGE_TABLE} "
            f"WHERE chatId = ? AND {deleted} AND logId BETWEEN ? AND ?",
            (room_id, low, high),
        ).fetchall()
        return {int(row[0]): _version(row) for row in rows}

    def messages_at(
        self,
        room_id: int,
        log_ids: Sequence[int],
        include_erased: bool = False,
    ) -> tuple[list[Message], dict[int, int]]:
        """Read these messages again, oldest first, leaving out any now gone.

        The counterpart of :meth:`row_versions`: once a caller knows which of
        its rows changed, only those are rebuilt. Each message comes back with
        the version of the very row it was built from, so a change landing
        between two reads is caught by the next comparison rather than filed
        as already seen.
        """
        if not log_ids:
            return [], {}
        selected, deleted = self._message_query(include_erased)
        wanted = sorted(set(log_ids))
        rows: list[Any] = []
        connection = self.connection()
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            places = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    f"SELECT {selected} FROM {MESSAGE_TABLE} WHERE chatId = ? AND {deleted} "
                    f"AND logId IN ({places}) ORDER BY logId",
                    [room_id, *chunk],
                ).fetchall()
            )
        versions = {int(row[0]): _version(row) for row in rows}
        return [self._message(room_id, row) for row in rows], versions

    def _sender_name(self, room_id: int, user_id: int) -> str:
        """Name *user_id*, looking again before settling for a number.

        The name map is read once per reader, and the interface keeps one
        reader for as long as it runs — so somebody who joined an open chat
        after the room was opened stayed ``User <id>`` until the room was
        opened again, when a fresh read found them. Found on the Windows
        build. An unknown id now has the map read again, at most every
        :data:`NAME_RETRY_SECONDS`, and failing that the name the room's own
        join feed recorded, reading the feeds once per stranger rather than
        once per line.
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
            ordered = sorted(
                names_module.from_feeds(self.feed_rows(room_id)),
                key=lambda record: record.at or 0,
            )
            feeds = {record.user_id: record.name for record in ordered}
            self._feed_names[room_id] = feeds
            if user_id not in feeds:
                lacking.add(user_id)
        return feeds.get(user_id) or f"User {user_id}"

    def _message(self, room_id: int, row: "sqlite3.Row | tuple[Any, ...]") -> Message:
        (
            log_id,
            author,
            type_code,
            text,
            attachment,
            sent_at,
            thread,
            erased,
            revision,
            reacted,
        ) = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        if erased:
            # The client keeps the very words that were taken back; they are
            # dropped here, so nothing downstream can show them by mistake.
            own = self.own_user_id()
            return Message(
                id=int(log_id),
                room_id=room_id,
                sender_id=sender_id,
                sender=self._sender_name(room_id, sender_id),
                text="",
                type=kind,
                sent_at=None if sent_at is None else int(sent_at),
                from_me=own is not None and sender_id == own,
                erased=True,
            )
        known = self.names()
        own = self.own_user_id()
        # The attachment is read for what it records about *other* messages —
        # who this one named, and which one it answered. It is never turned
        # into message text, which would show words nobody typed.
        extra = model.parse_attachment(attachment)
        mentions = model.mentions_in(extra, known.get)
        reply = model.quoted_in(extra, known.get) or self._threaded_quote(
            room_id, int(log_id), thread
        )
        photo = model.photo_in(extra, kind)
        covered = model.covered_in(extra)
        return Message(
            id=int(log_id),
            room_id=room_id,
            sender_id=sender_id,
            sender=self._sender_name(room_id, sender_id),
            # A membership feed's message column is the client's own JSON,
            # not words anybody typed, so it is not passed off as text.
            text=""
            if kind == model.FEED_MESSAGE or (covered and not self.reveal_covered)
            else (text if isinstance(text, str) and text else ""),
            type=kind,
            sent_at=None if sent_at is None else int(sent_at),
            from_me=own is not None and sender_id == own,
            mentions=mentions,
            mentions_all=model.everyone_mentioned(extra),
            mentions_me=own is not None and any(one.user_id == own for one in mentions),
            reply=reply,
            replies_to_me=own is not None and reply is not None and reply.sender_id == own,
            photo=photo,
            video=model.video_in(kind, extra),
            reactions=model.reactions_in(reacted),
            edited=bool(revision),
            covered=covered,
        )

    def _threaded_quote(
        self, room_id: int, log_id: int, thread: Any
    ) -> Optional[model.Quote]:
        """Read back the message a threaded reply answers.

        Where the attachment carried the reply's own copy of what it answered,
        a thread carries only the id — so this is what that message says
        *now*, and it goes when the message goes. A thread naming a row this
        database does not hold quotes nothing rather than an empty line
        attributed to nobody; the reply still reads as an ordinary message,
        which is what it looks like in the client too.
        """
        try:
            source = int(thread)
        except (TypeError, ValueError):
            return None
        # A row that names itself is answering nothing, and quoting it would
        # stack a message above a copy of itself.
        if source <= 0 or source == log_id:
            return None
        selected, deleted = self._message_query()
        row = self.connection().execute(
            f"SELECT {selected} FROM {MESSAGE_TABLE} "
            f"WHERE chatId = ? AND logId = ? AND {deleted}",
            (room_id, source),
        ).fetchone()
        if row is None:
            return None
        _log_id, author, type_code, text, attachment, *_rest = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        hidden = (
            model.covered_in(model.parse_attachment(attachment)) is not None
            and not self.reveal_covered
        )
        return model.Quote(
            message_id=source,
            sender_id=sender_id,
            sender=self.names().get(sender_id),
            # A feed's text is the client's own JSON, which is not something
            # anybody said; it is dropped here exactly as it is for a message.
            # A covered message stays covered when somebody quotes it.
            text=""
            if kind == model.FEED_MESSAGE or hidden or not isinstance(text, str)
            else text,
            type=kind,
        )

    # -- what a room carries besides its messages ----------------------------

    def _live_row(self, room_id: int, log_id: int) -> Optional[Sequence[Any]]:
        """One message row as the commands see it, or None when it is not there."""
        selected, deleted = self._message_query()
        return self.connection().execute(
            f"SELECT {selected} FROM {MESSAGE_TABLE} "
            f"WHERE chatId = ? AND logId = ? AND {deleted}",
            (room_id, log_id),
        ).fetchone()

    def revisions(self, room_id: int, log_id: int) -> list[model.Revision]:
        """Every earlier version of one message this database kept: none.

        The Windows client keeps them in ``chatLogHistory``. This one has no
        such table — of the 45 rows edited here, none had a version anywhere
        but the row itself, whose text is already the newest — so an edited
        message answers with an empty list, which the contract reads as
        "never recorded". Still asked only of a message that is there: one
        deleted for everyone is ``MESSAGE_NOT_FOUND``, as on Windows.
        """
        if self._live_row(room_id, log_id) is None:
            raise KamadoError(
                ErrorCode.MESSAGE_NOT_FOUND, f"no readable message {log_id} in room {room_id}"
            )
        return []

    def notice(self, room_id: int) -> Optional[model.Notice]:
        """The notice pinned in a room, or None when it has none.

        ``NTChatMeta`` type 1: ``content`` is the notice's text, ``authorId``
        who pinned it and ``updatedAt`` seconds. There is no board table in
        this database, so there is nothing for a post to win over.
        """
        columns = self.columns(META_TABLE)
        if not {"chatId", "type", "content"} <= columns:
            return None
        author = "authorId" if "authorId" in columns else "NULL"
        moment = "updatedAt" if "updatedAt" in columns else "NULL"
        order = " ORDER BY revision DESC" if "revision" in columns else ""
        row = self.connection().execute(
            f"SELECT {author}, content, {moment} FROM {META_TABLE} "
            f"WHERE chatId = ? AND type = ?{order} LIMIT 1",
            (room_id, _NOTICE_META_TYPE),
        ).fetchone()
        if row is None or not isinstance(row[1], str) or not row[1].strip():
            return None
        author_id = model._identifier(row[0]) or None
        return model.Notice(
            text=row[1],
            kind="text",
            author_id=author_id,
            author=None if author_id is None else self.names().get(author_id),
            posted_at=model.instant_of(row[2]),
            source="legacy",
        )

    def attachments(
        self,
        room_id: int,
        kind: Optional[str] = None,
        limit: int = 50,
        before_id: Optional[int] = None,
    ) -> list[model.Attachment]:
        """Gather what a room's messages carried: photos, videos, files, links.

        *limit* counts messages, newest first, and the answer is oldest first
        like every other list here; *before_id* pages further back. A link is
        an address inside the text, so a covered message offers none while
        it stays covered.
        """
        if kind is not None and kind not in model.ATTACHMENT_KINDS:
            raise KamadoError(
                ErrorCode.INVALID_REQUEST,
                f"kind must be one of {', '.join(model.ATTACHMENT_KINDS)}",
            )
        selected, deleted = self._message_query()
        text = _first_present(self.columns(MESSAGE_TABLE), _MESSAGE_TEXT_COLUMNS)
        wanted: dict[str, tuple[int, ...]] = {
            "photo": (model.PHOTO_MESSAGE, model.MULTI_PHOTO_MESSAGE),
            "video": (model.VIDEO_MESSAGE,),
            "file": (model.FILE_MESSAGE,),
        }
        types = _ATTACHMENT_TYPES if kind is None else wanted.get(kind, ())
        clauses = []
        if types:
            clauses.append(f"type IN ({', '.join(str(code) for code in types)})")
        if kind in (None, "link") and text is not None:
            clauses.append(
                f"(type IN ({', '.join(str(code) for code in _LINK_TYPES)}) "
                f"AND {text} LIKE '%http%')"
            )
        if not clauses:
            return []
        sql = (
            f"SELECT {selected} FROM {MESSAGE_TABLE} "
            f"WHERE chatId = ? AND {deleted} AND ({' OR '.join(clauses)})"
        )
        parameters: list[object] = [room_id]
        if before_id is not None:
            sql += " AND logId < ?"
            parameters.append(before_id)
        sql += " ORDER BY logId DESC LIMIT ?"
        parameters.append(limit)
        rows = list(reversed(self.connection().execute(sql, parameters).fetchall()))
        found: list[model.Attachment] = []
        for row in rows:
            for item in self._attachments_of(room_id, row):
                if kind is None or item.kind == kind:
                    found.append(item)
        return found

    def _attachments_of(
        self, room_id: int, row: Sequence[Any], local_path: Optional[str] = None
    ) -> list[model.Attachment]:
        log_id, author, type_code, text, attachment, sent_at, *_rest = tuple(row)
        sender_id = int(author or 0)
        kind = int(type_code or 0)
        extra = model.parse_attachment(attachment)
        sender = self._sender_name(room_id, sender_id)
        moment = None if sent_at is None else int(sent_at)

        def item(
            what: str,
            url: str,
            name: Optional[str] = None,
            size: Optional[int] = None,
            count: int = 1,
            expires_at: Optional[int] = None,
        ) -> model.Attachment:
            return model.Attachment(
                message_id=int(log_id),
                kind=what,
                sender_id=sender_id,
                sender=sender,
                sent_at=moment,
                url=url,
                name=name,
                size=size,
                count=count,
                expires_at=expires_at,
                local_path=local_path,
            )

        photo = model.photo_in(extra, kind)
        if photo is not None:
            return [item("photo", photo.url, count=photo.count)]
        video = model.video_in(kind, extra)
        if video is not None:
            return [
                item("video", video.url, size=video.size or None, expires_at=video.expires_at)
            ]
        carried = model.file_in(kind, extra)
        if carried is not None:
            url, name, size, expires = carried
            return [item("file", url, name=name, size=size, expires_at=expires)]
        if model.covered_in(extra) is not None and not self.reveal_covered:
            return []
        if kind in _LINK_TYPES and isinstance(text, str):
            return [item("link", address) for address in model.links_in(text)]
        return []

    def file_at(self, room_id: int, log_id: int) -> model.Attachment:
        """The file one message carries, with where the client saved it, or ``MESSAGE_NOT_FOUND``."""
        row = self._live_row(room_id, log_id)
        local: Optional[str] = None
        if row is not None and _LOCAL_PATH_COLUMN in self.columns(MESSAGE_TABLE):
            saved = self.connection().execute(
                f"SELECT {_LOCAL_PATH_COLUMN} FROM {MESSAGE_TABLE} WHERE chatId = ? AND logId = ?",
                (room_id, log_id),
            ).fetchone()
            if saved is not None and isinstance(saved[0], str) and saved[0].strip():
                local = saved[0]
        carried = [] if row is None else self._attachments_of(room_id, row, local)
        if not carried or carried[0].kind != "file":
            raise KamadoError(
                ErrorCode.MESSAGE_NOT_FOUND,
                f"message {log_id} in room {room_id} is not a readable file",
            )
        return carried[0]

    def feed_details(self, room_id: int, log_ids: Sequence[int]) -> dict[int, str]:
        """Describe each of these feeds from what its own row carries.

        The JSON stays out of the message text, where it would be read as
        words somebody typed; this is the short account of it, for the rows
        being looked at rather than for a room's whole history. Parsing is
        done here rather than in SQL because a row with no text is not always
        a feed — a photo has none either — and asking SQLite's json_extract
        about one of those fails the whole statement instead of that row.
        """
        if not log_ids:
            return {}
        columns = self.columns(MESSAGE_TABLE)
        text = _first_present(columns, _MESSAGE_TEXT_COLUMNS)
        if text is None or "logId" not in columns:
            return {}
        places = ",".join("?" for _ in log_ids)
        found: dict[int, str] = {}
        for log_id, body in self.connection().execute(
            f"SELECT logId, {text} FROM {MESSAGE_TABLE} "
            f"WHERE chatId = ? AND type = ? AND logId IN ({places})",
            (room_id, model.FEED_MESSAGE, *log_ids),
        ):
            detail = model.feed_detail(body)
            if detail:
                found[int(log_id)] = detail
        return found

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
            found = self._message(int(row[0]), row[1:])
            if found.covered and not self.reveal_covered:
                # A match whose text is withheld would still say what it
                # contained, which is the thing the covering hides.
                continue
            yield found

    def query(self, sql: str, room_id: Optional[int] = None) -> list[dict[str, object]]:
        """Run one read-only statement against the decrypted database."""
        connection = self.connection()
        connection.execute("PRAGMA query_only = ON")
        try:
            cursor = connection.execute(sql)
        except sqlite3.Error as error:
            raise KamadoError(ErrorCode.INVALID_REQUEST, str(error)) from error
        columns = [description[0] for description in (cursor.description or ())]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
