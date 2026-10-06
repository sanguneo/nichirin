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


_REVISION_COLUMN = "revision"
"""Raised each time the sender edits a message; absent from an older log."""
_META_TABLE = "chatLogMeta"
_HISTORY_TABLE = "chatLogHistory"
"""Every version of an edited message, the first at revision 0.

Measured: of 128 edited rows, 96 had history and every one of those held the
current revision beside the earlier ones; the other 32 had none at all.
"""
_HIDDEN_FEEDS = (_ERASED_FEED, model.EDIT_FEED)
"""Feeds that stand for a change to another message, marked where it happened."""
_ATTACHMENT_TYPES = (
    model.PHOTO_MESSAGE,
    model.ALBUM_MESSAGE,
    model.VIDEO_MESSAGE,
    model.FILE_MESSAGE,
)
_LINK_TYPES = (model.TEXT_MESSAGE, model.REPLY_MESSAGE)


_IN_CHUNK = 500
"""How many ids one ``IN`` list carries, well inside SQLite's variable limit."""

_FULL_TEXT_CANDIDATES = 5
"""At most this many messages are named when several begin with a notice's preview."""

_ROOM_IMAGES = 4
"""How many member pictures a room's icon is composed from, as the client does.

Only for a room without a picture of its own: ``chatRoomList``'s
``chatRoomImageUrl`` and ``chatRoomFullImageUrl`` were empty in all 273 rooms
of a live account, but a room a member gave a photo keeps it in ``chatMetas``
(:data:`_ROOM_PICTURE_META`), and an open chat in its link.
"""
_ROOM_PICTURE_META = 4
"""The ``chatMetas`` type holding a room's own photo, ``{imageUrl, fullImageUrl}``.

Measured: 3 group rooms carried one. Type 8 is a channel's chat background
(``chatBackgroundImageUrl``, 22 rows) and is not the room's icon.
"""
_PEER_ROOMS = (model.DIRECT_CHAT, model.OPEN_DIRECT_CHAT, model.CHANNEL_CHAT)
"""Rooms that are one counterpart, whose picture is that counterpart's."""
_IMAGE_HOST_SUFFIX = ".kakaocdn.net"
"""Measured hosts: ``chat.kakaocdn.net`` and ``p.kakaocdn.net``.

A profile picture is an address the client wrote, and a remote caller's
browser is what will fetch it; only the client's own CDN over HTTPS is passed
on, so a row that says anything else is treated as no picture at all.
"""


def _image_address(value: object) -> Optional[str]:
    """Return *value* when it is an HTTPS address on the client's CDN."""
    if not isinstance(value, str) or not value.startswith("https://"):
        return None
    host = value[len("https://") :].split("/", 1)[0].split(":", 1)[0].lower()
    return value if host.endswith(_IMAGE_HOST_SUFFIX) else None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _profile_images(users: sqlite3.Connection) -> dict[int, str]:
    """Map user ids to their small profile picture, where the client has one.

    Read beside the name map and from the same snapshot, so a picture is the
    one that account carries now, exactly like its name. ``talkUser`` holds
    ``profileImageUrl`` (measured: 15,787 of 17,756 rows, ~7 KB each); the
    ``Profile`` rows keep theirs as JSON under ``thumbnailUrl``. As with names,
    ``talkUser`` wins. A client whose tables lack the columns has no pictures,
    which is an answer, not an error.
    """
    found: dict[int, str] = {}
    if "profileImageInfo" in _columns(users, "Profile"):
        for user_id, info in users.execute("SELECT userId, profileImageInfo FROM Profile"):
            parsed = model.parse_attachment(info)
            address = _image_address(parsed.get("thumbnailUrl"))
            if address is not None:
                found[int(user_id)] = address
    if "profileImageUrl" in _columns(users, "talkUser"):
        for user_id, value in users.execute("SELECT userId, profileImageUrl FROM talkUser"):
            address = _image_address(value)
            if address is not None:
                found[int(user_id)] = address
    return found


def _room_pictures(catalog: sqlite3.Connection) -> dict[int, str]:
    """Rooms a member gave a photo of their own, by room id."""
    tables = {
        str(name)
        for (name,) in catalog.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "chatMetas" not in tables:
        return {}
    found: dict[int, str] = {}
    for room_id, content in catalog.execute(
        "SELECT chatId, content FROM chatMetas WHERE type = ?", (_ROOM_PICTURE_META,)
    ).fetchall():
        address = _image_address(model.parse_attachment(content).get("imageUrl"))
        if address is not None:
            found[int(room_id)] = address
    return found


def _link_pictures(links: sqlite3.Connection) -> dict[int, str]:
    """Open chat links' own pictures, by link id."""
    if "linkImageUrl" not in _columns(links, "openLinkList"):
        return {}
    return {
        int(link_id): address
        for link_id, value in links.execute(
            "SELECT linkId, linkImageUrl FROM openLinkList"
        ).fetchall()
        if link_id is not None and (address := _image_address(value)) is not None
    }


def _room_images(
    room_type: str,
    peer: object,
    members: Sequence[int],
    own: Optional[int],
    images: dict[int, str],
    picture: Optional[str] = None,
) -> tuple[str, ...]:
    """The pictures a room's icon is drawn from, best first; empty for none.

    A room with a *picture* of its own — a photo a member set, or an open
    chat's link picture — is that picture alone, as the client shows it.
    Otherwise one counterpart — a 1:1, an open 1:1, a channel — is that
    counterpart's picture, the chat with oneself is the account's own, and any
    other room is its active members' other than the account, in the catalog's
    order, up to :data:`_ROOM_IMAGES`; a member without a picture is skipped.
    """
    if picture is not None:
        return (picture,)
    if room_type == model.SELF_CHAT:
        address = None if own is None else images.get(own)
        return () if address is None else (address,)
    if room_type in _PEER_ROOMS and isinstance(peer, int) and peer > 0:
        address = images.get(peer)
        return () if address is None else (address,)
    return tuple(
        images[member] for member in members if member != own and member in images
    )[:_ROOM_IMAGES]


def _version(row: "sqlite3.Row | tuple[Any, ...]") -> int:
    """A digest of the columns one message is built from.

    Only compared with another taken in the same process, so Python's own
    hash is enough; it is not a checksum and is never stored.
    """
    return hash(tuple(row))


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
        self._images: dict[int, str] = {}
        """Profile pictures, read with :attr:`_names` and replaced with it."""
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
        self._tables: dict[int, set[str]] = {}
        self.reveal_covered = False
        """Whether a message SafeBot covered keeps its text.

        Off by default, the way the client shows it; the interface's
        ``/safebot`` and the commands' ``--show-covered`` turn it on.
        """

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
            self._images = _profile_images(users)
            self._names = found
            self._names_source = users
            self._names_read_at = time.monotonic()
            if self._ledger is not None:
                # Once per snapshot of the user database: the map is kept
                # until that changes, and only a name that differs from its
                # last sighting is appended.
                self._ledger.observe(found)
        return self._names

    def _known_images(self) -> dict[int, str]:
        """The profile pictures of the name map's snapshot, reading it if needed."""
        self._known_names()
        return self._images

    def _link_pictures(self) -> dict[int, str]:
        """Open chat links' pictures; none when that database cannot be read.

        The client opens this database on its own schedule, so its key may not
        be in memory yet. A missing key costs the open rooms their picture —
        they fall back to their members' — and never the room list.
        """
        name = paths.OPEN_LINK_DATABASE
        try:
            if not self.keyring.has(name):
                return {}
            links = self._cache.connect(self.keyring.path_for(name), self._key_for(name))
            return _link_pictures(links)
        except (KanaoError, sqlite3.DatabaseError):
            return {}

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
        catalog = self._catalog()
        rows = catalog.execute(sql, parameters).fetchall()
        # An open room's picture is its link's; an older catalog has no link column.
        links_of: dict[int, int] = {}
        if "linkId" in _columns(catalog, "chatRoomList"):
            links_of = {
                int(room_id): int(link_id)
                for room_id, link_id in catalog.execute(
                    "SELECT chatId, linkId FROM chatRoomList WHERE linkId > 0"
                ).fetchall()
            }
        room_pictures = _room_pictures(catalog)
        # Memberships and the account are read from the catalog before the
        # other databases are opened for pictures, for the reason given above.
        memberships = self.memberships(refresh=True)
        own = self.own_user_id()
        link_pictures = self._link_pictures() if links_of else {}
        self._revalidate_names()
        images = self._known_images()
        found: list[Room] = []
        for row in rows:
            room_id, room_type, title, custom, members, unread, last_log, updated, peer = row
            room_id = int(room_id)
            picture = room_pictures.get(room_id)
            if picture is None and str(room_type or "") == model.OPEN_GROUP_CHAT:
                link = links_of.get(room_id)
                picture = None if link is None else link_pictures.get(link)
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
                    images=_room_images(
                        str(room_type or ""),
                        peer,
                        memberships.get(room_id, ()),
                        own,
                        images,
                        picture,
                    ),
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
        listed: Optional[str] = None,
    ) -> Optional[int]:
        """Where the client's room search should list *room_id*, from zero.

        The search matches a room's title and the names of its members, and
        lists what it matches in the room list's own recency order — so a
        one-to-one room with somebody sat third, below two open chats that
        merely had them as a member, and opening the first result opened the
        wrong room. Checked live against that search: predicted third, shown
        third. None when the room would not be matched at all.

        *listed* is the title the client lists the room under when that is
        not the catalog's: the self-chat is 나와의 채팅 here and the account's
        name there, and only the second is something its search matches (#87).
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
            title = listed if listed and room.id == room_id else room.title
            matched = needle in title.casefold() or any(
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
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self._tables[room_id] = tables
        thread = _THREAD_COLUMN if _THREAD_COLUMN in columns else "NULL"
        erased = f"(COALESCE(type, 0) & {_ERASED_TYPE_BIT}) != 0"
        if _ERASED_COLUMN in columns:
            erased = f"(COALESCE({_ERASED_COLUMN}, 0) != 0 OR {erased})"
        revision = (
            f"COALESCE({_REVISION_COLUMN}, 0)" if _REVISION_COLUMN in columns else "0"
        )
        # Read with the row it belongs to, so a 공감 added later changes the
        # row's version and the screen rebuilds it like any other change.
        # The table's key is (logId, type), so this is one index lookup.
        reactions = (
            f"(SELECT content FROM {_META_TABLE} WHERE {_META_TABLE}.logId = chatLogs.logId "
            f"AND {_META_TABLE}.type = {model.REACTION_META})"
            if _META_TABLE in tables
            else "NULL"
        )
        self._selections[room_id] = (
            f"logId, authorId, type, message, attachement, sendAt, {thread}, {erased}, "
            f"{revision}, {reactions}"
        )
        # A deletion writes a type-14 feed pointing at what it hid, and an
        # edit a type-25 one; the mark stands where the message is, so
        # neither feed ever shows.
        hidden = ", ".join(str(code) for code in _HIDDEN_FEEDS)
        kept = (
            "COALESCE(deleted, 0) = 0 AND "
            f"COALESCE(CASE WHEN type = {model.FEED_MESSAGE} AND json_valid(message) "
            f"THEN json_extract(message, '$.feedType') END, -1) NOT IN ({hidden})"
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
        versions: Optional[dict[int, int]] = None,
    ) -> list[Message]:
        """Return up to *limit* messages, oldest first.

        *versions*, when given, is filled with each returned row's version -
        see :meth:`row_versions` - taken from the very rows the messages were
        built from, so a caller holding them can later tell which changed.

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
        if versions is not None:
            versions.update({int(row[0]): _version(row) for row in rows})
        self._revalidate_names()
        return [self._message(room_id, row) for row in rows]

    def row_versions(
        self, room_id: int, low: int, high: int, include_erased: bool = False
    ) -> dict[int, int]:
        """Say what each row between two ids currently holds, without reading it.

        A message already on screen can change without a new one arriving:
        deleted for everyone, it keeps its id and has its text replaced, and
        a follower that only asks for higher ids never looks at it again. The
        version is a digest of the columns a message is built from, so a row
        that changed has a different one; a row no longer returned at all -
        deleted, or a feed that is never shown - is simply absent.

        One range read on the primary key and no names, quotes or JSON, which
        is what makes asking after every change of the log affordable.
        """
        connection = self._logs(room_id)
        condition = (
            self._kept(room_id, connection) if include_erased else self._live(room_id, connection)
        )
        rows = connection.execute(
            f"SELECT {self._selection(room_id, connection)} FROM chatLogs "
            f"WHERE {condition} AND logId BETWEEN ? AND ?",
            (low, high),
        ).fetchall()
        return {int(row[0]): _version(row) for row in rows}

    def messages_at(
        self,
        room_id: int,
        log_ids: Sequence[int],
        include_erased: bool = False,
        versions: Optional[dict[int, int]] = None,
    ) -> list[Message]:
        """Read these messages again, oldest first, leaving out any now gone.

        The counterpart of :meth:`row_versions`: once a caller knows which of
        its rows changed, only those are rebuilt.
        """
        if not log_ids:
            return []
        connection = self._logs(room_id)
        condition = (
            self._kept(room_id, connection) if include_erased else self._live(room_id, connection)
        )
        wanted = sorted(set(log_ids))
        rows: list[Any] = []
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            places = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    f"SELECT {self._selection(room_id, connection)} FROM chatLogs "
                    f"WHERE {condition} AND logId IN ({places}) ORDER BY logId",
                    chunk,
                ).fetchall()
            )
        if versions is not None:
            versions.update({int(row[0]): _version(row) for row in rows})
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
                sender_image=self._known_images().get(sender_id),
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
            photo=model.photo_in(kind, extra),
            video=model.video_in(kind, extra),
            reactions=model.reactions_in(reacted),
            edited=bool(revision),
            covered=covered,
            sender_image=self._known_images().get(sender_id),
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
            sender=self._known_names().get(sender_id),
            # A feed's text is the client's own JSON, which is not something
            # anybody said; it is dropped here exactly as it is for a message.
            # A covered message stays covered when somebody quotes it.
            text=""
            if kind == model.FEED_MESSAGE or hidden or not isinstance(text, str)
            else text,
            type=kind,
        )

    def revisions(self, room_id: int, log_id: int) -> list[model.Revision]:
        """Every version of one message, oldest first.

        Asked only of a message that is still there: one deleted for everyone
        keeps its history in this table, and reading it back would undo the
        deletion. A covered message keeps its versions covered too. A message
        the client never recorded history for answers with none.
        """
        connection = self._logs(room_id)
        row = connection.execute(
            f"SELECT {self._selection(room_id, connection)} FROM chatLogs "
            f"WHERE logId = ? AND {self._live(room_id, connection)}",
            (log_id,),
        ).fetchone()
        if row is None:
            raise KanaoError(
                ErrorCode.MESSAGE_NOT_FOUND, f"no readable message {log_id} in room {room_id}"
            )
        if _HISTORY_TABLE not in self._tables.get(room_id, set()):
            return []
        hidden = (
            model.covered_in(model.parse_attachment(row[4])) is not None
            and not self.reveal_covered
        )
        return [
            model.Revision(
                revision=int(revision or 0),
                text="" if hidden or not isinstance(text, str) else text,
                type=int(type_code or 0),
            )
            for revision, type_code, text in connection.execute(
                f"SELECT revision, type, message FROM {_HISTORY_TABLE} "
                "WHERE logId = ? ORDER BY revision",
                (log_id,),
            )
        ]

    def _full_text(
        self, room_id: int, preview: str
    ) -> tuple[Optional[tuple[int, str, bool]], tuple[int, ...]]:
        """The message a cut-off notice was pinned from, by its words.

        The post records no message id, so the only link is the text: a live
        text or reply message beginning with the whole stored preview. Exactly
        one such message is the answer; two or more are returned as candidates
        and none is picked. A covered message is not offered unless covered
        messages are shown.

        Asked only of a room whose key is already in memory. The notice is a
        catalog read that never opens a room, and finding its text must not
        start doing so — a room that cannot be read answers with nothing.
        """
        name = paths.chat_log_name(room_id)
        try:
            if not self.keyring.has(name):
                return None, ()
            connection = self._logs(room_id)
            rows = connection.execute(
                f"SELECT logId, message, attachement FROM chatLogs "
                f"WHERE {self._live(room_id, connection)} AND type IN (?, ?) "
                "AND length(message) >= ? AND substr(message, 1, ?) = ? "
                "ORDER BY logId DESC LIMIT ?",
                (
                    model.TEXT_MESSAGE,
                    model.REPLY_MESSAGE,
                    len(preview),
                    len(preview),
                    preview,
                    _FULL_TEXT_CANDIDATES + 1,
                ),
            ).fetchall()
        except (KanaoError, sqlite3.DatabaseError):
            return None, ()
        usable = []
        for log_id, message, attachment in rows:
            extra = model.parse_attachment(attachment)
            if not isinstance(message, str):
                continue
            if not self.reveal_covered and model.covered_in(extra) is not None:
                continue
            # A long message keeps only its first 3,999 characters in the row.
            cut = len(message) >= model.LONG_MESSAGE_CHARACTERS and "path" in extra
            usable.append((int(log_id), message, cut))
        if len(usable) == 1:
            return usable[0], ()
        return None, tuple(sorted(log_id for log_id, _message, _cut in usable))

    def _shared_text(
        self, room_id: int, post_id: Optional[str], preview: str
    ) -> Optional[tuple[int, str, bool]]:
        """The share of a cut-off notice's post: its id, its text, whether that is cut.

        A board post is matched to its share by the post id, and a notice
        row that records none (the older row) by its words. Either way the
        share has to begin with the whole stored preview, because a post
        edited after it was shared still carries the old text in its share.
        When the words alone are the link, two shares saying different
        things are no answer. The same room key rule as :meth:`_full_text`.
        """
        name = paths.chat_log_name(room_id)
        try:
            if not self.keyring.has(name):
                return None
            connection = self._logs(room_id)
            rows = connection.execute(
                f"SELECT logId, attachement FROM chatLogs "
                f"WHERE {self._live(room_id, connection)} AND type = ? ORDER BY logId DESC",
                (model.BOARD_SHARE_MESSAGE,),
            ).fetchall()
        except (KanaoError, sqlite3.DatabaseError):
            return None
        found: list[tuple[int, str]] = []
        for log_id, attachment in rows:
            extra = model.parse_attachment(attachment)
            if not self.reveal_covered and model.covered_in(extra) is not None:
                continue
            shared_post, text = model.board_share(extra)
            if text is None or not text.startswith(preview):
                continue
            if post_id is not None and shared_post != post_id:
                continue
            found.append((int(log_id), text))
        if not found or len({text for _log_id, text in found}) > 1:
            return None
        log_id, text = found[0]
        return log_id, text, len(text) >= model.BOARD_SHARE_CHARACTERS

    def _rest_of(
        self, room_id: int, post_id: Optional[str], preview: str
    ) -> dict[str, Any]:
        """Whatever this room holds of a notice past its cut, as Notice fields.

        The share and the pinned message are both asked, and the longer text
        wins. Equal lengths go to the share, which is linked by an id where
        the message is linked only by its words.
        """
        found, candidates = self._full_text(room_id, preview)
        shared = self._shared_text(room_id, post_id, preview)
        chosen, source = (found, "message") if found is not None else (None, None)
        if shared is not None and (chosen is None or len(shared[1]) >= len(chosen[1])):
            chosen, source = shared, "share"
        return {
            "full_text": None if chosen is None else chosen[1],
            "full_text_message_id": None if chosen is None else chosen[0],
            "full_text_truncated": chosen is not None and chosen[2],
            "full_text_candidates": candidates,
            "full_text_source": source,
        }

    def notice(self, room_id: int) -> Optional[model.Notice]:
        """The notice pinned in a room, or None when it has none.

        A board post wins over the older notice row; see :class:`model.Notice`
        for why. Both live in the catalog, so this needs no room key; what
        lies past a cut is looked for only in a room whose key is in memory.
        """
        catalog = self._catalog()
        tables = {
            str(row[0])
            for row in catalog.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        known = self._known_names()
        if "moimMetas" in tables:
            row = catalog.execute(
                "SELECT content FROM moimMetas WHERE chatId = ? AND type = 1", (room_id,)
            ).fetchone()
            post = model.parse_attachment(row[0]) if row is not None else {}
            if post.get("notice") is True:
                kind = str(post.get("type") or "text").lower()
                words = post.get("content") if kind != "schedule" else post.get("subject")
                text = words if isinstance(words, str) else ""
                author = model._identifier(post.get("owner_id"))
                # The client keeps only a post's first 100 characters; a longer
                # one is cut there, and its whole text may still be the message
                # that was pinned.
                truncated = kind == "text" and len(text) >= model.BOARD_PREVIEW_CHARACTERS
                post_id = post.get("id")
                rest = (
                    self._rest_of(room_id, post_id if isinstance(post_id, str) else None, text)
                    if truncated
                    else {}
                )
                return model.Notice(
                    text=text,
                    kind=kind,
                    author_id=author,
                    author=None if author is None else known.get(author),
                    posted_at=model.instant_of(post.get("created_at")),
                    source="board",
                    truncated=truncated,
                    **rest,
                )
        if "chatMetas" in tables:
            row = catalog.execute(
                "SELECT authorId, content, updateAt FROM chatMetas WHERE chatId = ? AND type = 1",
                (room_id,),
            ).fetchone()
            if row is not None and isinstance(row[1], str) and row[1].strip():
                author = model._identifier(row[0])
                # The older row is cut at the same 100 characters as a post.
                truncated = len(row[1]) >= model.BOARD_PREVIEW_CHARACTERS
                return model.Notice(
                    text=row[1],
                    kind="text",
                    author_id=author,
                    author=None if author is None else known.get(author),
                    posted_at=model.instant_of(row[2]),
                    source="legacy",
                    truncated=truncated,
                    **(self._rest_of(room_id, None, row[1]) if truncated else {}),
                )
        return None

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
            raise KanaoError(
                ErrorCode.INVALID_REQUEST,
                f"kind must be one of {', '.join(model.ATTACHMENT_KINDS)}",
            )
        connection = self._logs(room_id)
        wanted: dict[str, tuple[int, ...]] = {
            "photo": (model.PHOTO_MESSAGE, model.ALBUM_MESSAGE),
            "video": (model.VIDEO_MESSAGE,),
            "file": (model.FILE_MESSAGE,),
        }
        types = (
            _ATTACHMENT_TYPES if kind is None else wanted.get(kind, ())
        )
        clauses = []
        if types:
            clauses.append(f"type IN ({', '.join(str(code) for code in types)})")
        if kind in (None, "link"):
            clauses.append(
                f"(type IN ({', '.join(str(code) for code in _LINK_TYPES)}) "
                "AND message LIKE '%http%')"
            )
        sql = (
            f"SELECT {self._selection(room_id, connection)} FROM chatLogs "
            f"WHERE {self._live(room_id, connection)} AND ({' OR '.join(clauses)})"
        )
        parameters: list[object] = []
        if before_id is not None:
            sql += " AND logId < ?"
            parameters.append(before_id)
        sql += " ORDER BY logId DESC LIMIT ?"
        parameters.append(limit)
        rows = list(reversed(connection.execute(sql, parameters).fetchall()))
        self._revalidate_names()
        found: list[model.Attachment] = []
        for row in rows:
            for item in self._attachments_of(room_id, row):
                if kind is None or item.kind == kind:
                    found.append(item)
        return found

    def _attachments_of(
        self, room_id: int, row: "sqlite3.Row | tuple[Any, ...]"
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
            )

        photo = model.photo_in(kind, extra)
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
        """The file one message carries, or ``MESSAGE_NOT_FOUND``."""
        connection = self._logs(room_id)
        row = connection.execute(
            f"SELECT {self._selection(room_id, connection)} FROM chatLogs "
            f"WHERE logId = ? AND {self._live(room_id, connection)}",
            (log_id,),
        ).fetchone()
        carried = [] if row is None else self._attachments_of(room_id, row)
        if not carried or carried[0].kind != "file":
            raise KanaoError(
                ErrorCode.MESSAGE_NOT_FOUND,
                f"message {log_id} in room {room_id} is not a readable file",
            )
        return carried[0]

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
                found = self._message(room.id, row)
                if found.covered and not self.reveal_covered:
                    # A match whose text is withheld would still say what it
                    # contained, which is the thing the covering hides.
                    continue
                yield found
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
