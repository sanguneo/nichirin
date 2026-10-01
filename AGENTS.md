# AGENTS.md — working on kanao-cli

Windows-only KakaoTalk reader, friend browser and sender in zero-dependency
Python 3.10+. Reads stay local; the explicit send command drives the client.
For *using* the tool as an agent, read `skill/SKILL.md`; this file is about
changing it.

## Commands

```powershell
python -m unittest discover -t . -s tests    # the whole suite
python -m kanao status                       # does anything work right now
python install.py --target <dir> --force     # install the skill
```

Discovery needs `-t .` so `tests` stays an importable package. `python -m
unittest discover tests` breaks the fixture imports.

## Review and contribution discipline

See `CONTRIBUTING.md` for the contribution workflow and `improvement.md` for
the dated review and proposed fixes. A planned invariant is not a claim that
the current implementation already satisfies it.

- A review-only request produces findings and plans, not product changes.
  Respect the requested verification budget; prefer source/caller/test evidence
  and small deterministic counterexamples over a full suite or live automation.
  Label executed evidence, source deductions, and platform-dependent hypotheses
  separately. Do not invent incident rates or speedups.
- Bind a cached image to the source state it actually read. A later stat is
  not evidence that an earlier image includes a later commit. Stat equality is
  not byte equality, and a WAL checksum is not page authentication.
- Preserve per-room log-ID order and monotone resume cursors. Publish a cursor
  only after its covered output. Do not mark a failed read or identity lookup
  as completed.
- State cache ownership, borrowed-connection lifetime, metadata invalidation,
  and what a memory budget counts. A limit per page is not a limit on the
  accumulated result or process memory.
- Keep external message/name/title/URL text separate from renderer-generated
  terminal controls. Content changes at the same message ID must participate
  in any transcript-cache invalidation design.
- A sibling port carries its assumptions, callers, contract, and regressions,
  not just its function body. Record both source revisions. Windows has
  per-room databases, heap keys and Win32 input; macOS has one account database,
  derived keys and AX input. Verify local files instead of assuming an upstream
  feature or shared specification is already present.
- Keep review artifacts synthetic and secret-free. Existing user work is not
  part of a contribution. Commit and push only when requested.

## Layout

| Module | Responsibility |
| --- | --- |
| `cng.py` | AES-256-CBC through Windows CNG; `PreparedKey` holds one key handle |
| `page.py` | SQLCipher page ↔ plain SQLite page |
| `wal.py` | Committed prefix of a write-ahead log |
| `process.py` | Read-only heap scan for SQLCipher `codec_ctx` |
| `paths.py` | Signed-in profile discovery |
| `keyring.py` | Pair each key with the database sharing its salt |
| `snapshot.py` | Decrypt, verify, cache, retry |
| `names.py` | Nickname history: membership feeds plus an observed ledger |
| `reader.py` | Queries and name resolution |
| `context.py` | The single call an agent makes |
| `watch.py` | Stat-gated follow loop |
| `cli.py` | Argument parsing and the JSON envelope |
| `winauto.py` | Exact window targeting, draft replacement, background text sending |
| `wic.py` | Any image's bytes → pixels, through the codecs Windows already has |
| `digest.py` | The digest the command and the server both ask for |
| `files.py` | A file message's bytes: the client's saved copy, or its address |
| `serve/config.py` | Constants, token and interval validation, error-to-status map |
| `serve/handler.py` | Auth, routing, and every route that answers and closes |
| `serve/stream.py` | The SSE connection's own lifetime, and the watcher it owns |
| `serve/backends.py` | Reader, Sender and Watcher wiring for a live host |
| `serve/server.py` | Binding the socket and serving until interrupted |
| `tui/theme.py` | OmO dark-theme tokens, 256-colour fallback, speaker tints |
| `tui/layout.py` | Column-accurate width, truncation, wrapping, footer plan |
| `tui/console.py` | Console mode, UTF-8 code page, alt screen, key input |
| `tui/digest.py` | Time slicing and extractive slice description |
| `tui/images.py` | Which protocol draws, how a picture is sized, and the CDN fetch |
| `tui/sixel.py` | PNG → pixels and pixels → sixel, pure standard library |
| `tui/app.py` | Screen state, rendering, key handling |
| `tui/session.py` | The switches and open tabs kept between launches |
| `kakaoread/` | The old name, aliased onto `kanao`; no behaviour of its own |

Dependencies run strictly downward; nothing below `reader.py` imports anything
above it.

## Facts that are easy to get wrong

These were established by measurement or by reading a live client. Changing
code that depends on them without re-checking will produce quiet wrong answers.

### Decryption

- The key exists **only in the running client's memory**, and only for a
  database it has opened. A room KakaoTalk never displayed cannot be read.
- `codec_ctx` layout (64-bit): iterations `0x04` = 256000, key size `0x10` = 32,
  page size `0x1C` = 4096, reserve `0x24` = 80, salt pointer `0x40`, read
  context `0x60`, and the key pointer at read context `+0x08`.
- A database is matched to a key by the 16-byte salt that both the file header
  and the in-memory context carry. Never match by filename.
- **Prepare the key once per database, not per page.** Per-page CNG setup
  measured 182.2 µs/page against 7.6 µs/page with the handle reused.

### Cost of a single command

Every command pays discovery before it answers, and both halves were slow:

- **The heap scan is remembered, not repeated.** `scan_process` measured 426 ms
  on a quiet client and 2.8 s when its working set was large. The addresses it
  found are cached per pid in `%LOCALAPPDATA%\kanao-cli\contexts.json` and
  re-read in 0.8 ms. **Only addresses are stored — never a key or a salt**, and
  every cached address is decoded and followed exactly like a fresh candidate,
  so a moved heap yields fewer contexts and falls back to a full scan.
- **A remembered set cannot know about a room opened after it.** It is
  trusted when it pairs as many databases as before, which it still does
  when the client has opened one more since — so that room read as
  `KEY_UNAVAILABLE` until the client restarted. Found by sending to a room
  that had just been opened for the send: remembered 32, fresh 33.
  `Reader._key_for` rescans once, ignoring the remembered set, when it lacks
  the database asked for.
- **The profile is walked, not globbed.** `rglob("*")` stats every entry in a
  profile that also holds media and caches: 1.36 s for 298 databases against
  92.8 ms with `os.walk`. `test_paths.py` pins the resulting order, which the
  walk folds case for **itself**: `Path` comparison folds on Windows and
  nowhere else, so leaving it to the platform gave one answer here and another
  on the macOS sibling's machine — same order on Windows, and now the same
  order everywhere.
- Together: `status` 2.43 s → 0.30 s, `rooms` 2.54 s → 0.63 s, `friends` 2.06 s
  → 0.38 s, with byte-identical output.
- **A message that arrives is not a reason to decrypt the file again.** SQLite
  appends committed frames to `-wal` and leaves the `.edb` alone, so
  `SnapshotCache` keeps the decrypted base pages beside the snapshot and
  `update_image_with_wal` lays only the log over them. Measured on live rooms:
  a 48.3 MB log with a 4.1 MB `-wal` fell from 431.5 ms to 100.2 ms, and the
  29.9 MB catalog from 222.3 ms to 36.7 ms — and the overlay was **byte-identical
  to the full decryption** in both. The base pages are charged to the cache
  budget too, because an entry that quietly costs twice what the cache believes
  is how a budget stops meaning anything.
- **The budget follows the database, because a fixed one is a bet on the
  account being small.** An entry costs about twice its image, so a fixed
  256 MiB starts shedding base pages at roughly a 128 MiB database — and a
  shed entry turns the next arriving message back into a full decryption.
  This account is already past it: a **73.0 MB** room beside a **56.6 MB** one
  is 259 MB of entries before the catalog is counted, and the screen opens
  three panes. The budget is now `min(max(256 MiB, largest image × 3),
  physical memory / 4)`, derived on each use because the size it follows is
  not known until something has been read. A stated `budget_bytes` is honoured
  exactly, and a machine that will not say how much memory it has keeps the
  fixed number it always had.
- **The image reaches SQLite as memory, and that is worth a whole Python
  version.** `_connect` hands the decrypted bytes to `sqlite3`'s `deserialize`,
  which exists from 3.11; before that the image has to be written to a
  temporary file and backed up into memory. On the 29.9 MB catalog that is
  **5.4 ms against 240.9 ms**, per database opened, and a room log is larger
  than the catalog. It is also the difference between the plaintext never
  touching the disk and it being written and unlinked.
- **The decryption is checked with `PRAGMA quick_check`, not `integrity_check`.**
  Both walk every page and every B-tree, which is what a wrong key fails; the
  full check additionally verifies index sort order against the rows indexed,
  and on the 48.3 MB room that was 337.4 ms against 87.0 ms. The overlay path
  skips even that: those base pages were checked when they were decrypted and
  every log frame carries its own checksum, so it connects and reads
  `sqlite_master` once instead.

### Consistency

- `build_image` compares the source **byte for byte** after reading. A stat
  check is not enough: a same-length rewrite inside one file-time tick leaves
  `st_mtime_ns` identical. `test_detects_a_same_size_rewrite_even_when_stat_reports_no_change`
  pins this.
- Caching and following *do* use `os.stat`, where a stale answer costs one
  extra poll rather than a wrong result.
- **What licenses reusing the base pages is the log's generation, not a stat.**
  `BasePages.generation` is the 32-byte log header those pages were decrypted
  against, and the salt pair in it names the current run of the log. In
  write-ahead logging mode nothing but a checkpoint ever writes the `.edb`,
  and a checkpoint copies frames the log still holds — so while the generation
  stands, every page it may have written underneath us is a page the overlay
  applies anyway and the image comes out the same. A checkpoint that *restarts*
  the log is the one that would strand pages in a file no longer being read,
  and restarting is exactly what changes that salt. A page-1 sentinel was tried
  first and is weaker: it is a guess about which pages a checkpoint touches,
  where this is a proof about which pages the log still carries.
- **The overlay is given up on, never defended.** A new generation, a log that
  moved mid-read, a page the log grew the database to but never wrote, or an
  image SQLite will not read `sqlite_master` from all fall back to a full
  decryption rather than surfacing an error, because the caller asked a
  question the data can still answer. A new generation raises `LogRestarted`
  and is **not retried**: backing off (0.60 s in all) cannot put stranded
  pages back, so it goes straight to the full read. Only a log still moving
  is retried.
- **An image is filed under the fingerprint taken before it was read.**
  `connect` used to sample again afterwards, so a commit that landed while the
  image was being checked sat beside an image that did not hold it, and the
  next call handed the older image back for as long as the room stayed quiet.
  Both paths now store the first sample; a commit that did make it into the
  read costs one extra read. That is all it claims: equal stats still say
  nothing about equal bytes.
- **`ReadDirectoryChangesW` does not work here**, and neither does C#'s
  `FileSystemWatcher` which wraps it: zero events across 30 seconds of live
  observation, because NTFS defers directory-entry updates while SQLite holds
  the log handle open. Stat polling caught every change.

### Schema

Taken from a live client, not guessed.

- Catalog is `chat_data/chatListInfo.edb`, table **`chatRoomList`**, with
  `chatId`, `type`, `chatRoomTitle`, `useCustomChatRoomTitle`,
  `activeMembersCount`, `newMessageCount`, `lastLogId`, `lastUpdatedAt`,
  `directChatMemberId`.
- Room `type` values: `DirectChat`, `MultiChat`, `MemoChat`, `PlusChat`, `OM`,
  `OD`.
- `chatMembers(chatId, userId, isActive)` resolves group membership directly —
  there is no binary plist to parse.
- Messages live in `chat_data/chatLogs_<roomId>.edb`, table `chatLogs`, columns
  `logId`, `authorId`, `type`, `message`, `attachement` (sic), `sendAt`,
  `deleted` and `threadId` — the last being where a reply now names the
  message it answers, and the only one that may be missing from an older log.
- **Order by `logId`, not `sendAt`.** `chatLogs` has no index on `sendAt`, so
  ordering by it scans and sorts every row; `logId` is the primary key. Over
  the newest 5,000 rows of a live room the two orders agreed exactly.
- **Which end the limit cuts from depends on the question.** A window wants
  the newest rows, so `messages` orders descending. Catch-up does not: asked
  for what arrived after a cursor, taking the newest page skips everything
  between the cursor and that page, and a follower that then advances its
  cursor never sees the difference again. `messages` therefore orders
  *ascending* when `after_id` is given without `before_id`, and
  `Watcher._catch_up` pages until a short page arrives. This is not a
  theoretical burst: the client backfills from the server after a
  reconnection, writing far more than the 500-row page in one go. It pages
  only **up to the newest id it started with**: each page is read from
  whatever snapshot the cache holds by then, so a room written faster than it
  is paged would keep every page full forever. What lies past the ceiling is
  the next poll's. That bounds pages, not rows held.
- **A room's messages leave a poll in log-id order, whatever their clocks
  say.** Sorting a poll by `(sentAt, id)` put a room's 11 before its 10
  whenever 11 carried the earlier time, and the stream published 10 as that
  room's cursor. Rooms are merged by time comparing only each room's next
  message, and the stream's cursor is compared as a number and only advances.
- **A failed read must not update the follow fingerprint.** `Watcher.poll`
  decides whether to open a room by stat'ing its log, so recording the
  fingerprint before the read lets a failure stand in for a success: the next
  poll finds the log unchanged, skips the room, and whatever arrived during
  the failure is never reported. The fingerprint is stored only after the
  read returns.
- `talkUser` holds **contacts only** — never the signed-in account. Without
  `Profile`, every message the user sent shows a bare numeric sender.
- **The signed-in account is the sole member of the `MemoChat` room.**
  `Profile` has several rows and its `hasMeBadge` flag marks a *different*
  account; neither identifies the user.
- Local history is substantial: one observed room held 445,714 messages. Do not
  assume only a handful of messages are cached.
- **A `type = 0` row is the client's bookkeeping, and its `message` is JSON.**
  One live open chat had **364 of these in its newest 400 records**; another
  had 37. Passing that column through as message text filled the screen with
  `{"feedType": 2, …}` and handed an agent the same wall to reason over, so
  the text is dropped and the kind is `feed`. `names.from_feeds` reads those
  rows straight from the database, so the name history is unaffected, and
  `search` excludes them because an ordinary word would otherwise match a row
  that shows no text at all. Live feeds carry a real `authorId`, so they are
  attributed like any other row.

- **A message deleted for everyone is not `deleted = 1`.** The client keeps
  the row with `deleted = 0`, sets `is_encrypt_delete_msg = 1` and replaces
  the text with base64, often adding `0x4000` to the type (16385, 16386). The
  screen showed that base64 as something the user had typed. Over the newest
  4,000 rows of every readable room the flag and the base64 coincided
  exactly: 60 rows each way, none of 12,033 others. `Reader._live` adds the
  flag to every "not deleted" condition, asking the schema for it the way
  `threadId` is asked for.
- **A newcomer is looked for again, not left as a number.** The interface
  keeps one reader for its whole life, so somebody who joined an open chat
  after the room was opened showed as `User <id>` until the room was
  reopened. `Reader._sender_name` re-reads the map for an unknown id at most
  every `NAME_RETRY_SECONDS`, then falls back to the nickname the room's join
  feed recorded, reading the feeds once per stranger rather than once per line.
- **The name map follows the user database, and the columns follow the log.**
  A map read once per reader kept a known user's old name after they renamed.
  `names()` is now checked once per read against the user database snapshot
  it was read from — the cache hands back a new connection exactly when that
  file changed — and a failure to check keeps the old map without marking it
  refreshed. A room's columns are re-asked the same way per log snapshot.
  Memberships keep their explicit refresh, because a send must not read the
  catalog. `own_user_id` is settled only once the catalog has answered: one
  `BUSY` used to stand for "no account" for the reader's whole life.
- **Fetch before composing.** `rooms()` iterated the catalog cursor while
  titles were composed, and composing one can open the user database; a cache
  too small for both evicted the catalog under the loop. Least recently used
  is not the same as no longer used.
- **The interface shows where a deleted message was; the commands do not.**
  `Reader.messages(include_erased=True)` keeps a deleted row with its text
  withheld and `Message.erased` set, and only the interface's reads and its
  watchers ask for that, so the screen says "삭제된 메시지입니다" where the
  client does. Every command, `serve` and the shared `openapi.json` still
  leave the row out, and `as_json` never mentions the field. The type-14 feed
  a deletion writes is hidden in both, because the placeholder already stands
  where the message was. A row is erased when `is_encrypt_delete_msg` is set
  *or* the type carries `0x4000`, the bit the macOS schema shares.
- **Two feed types are said in words.** `4` is a join and `2` a leave —
  read off a live open chat beside the client's own screen, 736 and 161 rows,
  every one naming a member. `model.FEED_KINDS` holds only those; every other
  code stays a number until it is checked the same way.
  They are shown as the client's own sentence, "이름님이 입장하셨습니다." —
  three names at most, then "외 N명" — and not as a bracketed tag.

### What else a room records: 공감, edits, notices, files, SafeBot

Read off this client on 2026-09-28, nine readable rooms, schema and counts
only. The idea came from reviewing `piratetalk-web`, whose read side does the
same with the same tables; its send side injects a DLL and none of that
crossed over.

- **공감 live in `chatLogMeta`, keyed `(logId, type)`, `type = 2`.** 746 rows,
  each `{"rx": [{"k", "c", "o", "a": {"ko"}}]}`: `c` how many, `o` the mini
  emoticon, `a.ko` the word the client shows (사랑 333, 엄지척 300, 웃음 44 …).
  `k` was 2 on all 840 entries and is not relied on. **Who reacted is not
  stored.** The content is read by a correlated subquery *inside* the message
  selection, so a 공감 added later changes the row's version and the screen
  rebuilds that row like any other change; it is one primary-key lookup. The
  table is asked for, not assumed, like `threadId`.
- **An edit raises `chatLogs.revision` and keeps every version in
  `chatLogHistory(logId, revision, …)`.** 128 edited rows: 96 had history and
  every one held revision 0 through the current one; 32 had none. The edit also
  writes a `feedType 25` feed (140 of them, all `hidden: true`) that is hidden
  like the deletion feed 14 — the mark stands on the message. **History is
  refused for a message deleted for everyone**: 4 erased rows had history, and
  returning it would undo the deletion.
- **The notice is in the catalog twice, and the board post wins.**
  `moimMetas.type = 1` is a 톡게시판 post (`notice: true`, `type`
  TEXT/IMAGE/SCHEDULE, `created_at` ISO with `Z`); `chatMetas.type = 1` is the
  older plain-text row. In all four rooms holding both, the older row was
  2019–2023 and the post 2025–2026, and the texts never matched — so the older
  row is read only when there is no post. Reading it needs no room key.
- **A file is `type = 18`** with `url`, `name`, `size` (also `s`), `expire` in
  ms and a checksum `cs`, on `talk.kakaocdn.net` or `dn.talk.kakao.com`. A live
  address answered a ranged GET with 206; expired ones 410 and 403. So
  `files.open_file` decides expiry from `expire` before any request, prefers a
  copy in `Documents\카카오톡 받은 파일` matching **name and size**, and fetches
  only from the whitelist. The name is somebody else's text: `safe_name` keeps
  its last path component, and `Content-Disposition` carries it
  percent-encoded in `filename*` with a printable-ASCII fallback. **This is the
  second network exception**, beside `Ctrl+P`: only `GET /file`, only on
  request. Measured live: a 5,491-byte file streamed whole, `remote`.
- **SafeBot marks the message itself: `{"safeBot": true}` in the attachment**,
  11 rows; `type & 0x2000` (which piratetalk also checks) was 0 rows here and is
  not used. The row keeps its text, so hiding it is this tool's decision:
  `Reader.reveal_covered` is off by default, as the client shows it, and
  `/safebot` or `--show-covered` turns it on. Off, the text is withheld from
  the message, a quote of it, its links and the search — a match with empty
  text would still say what it contained. The six `openchat_blind` cover feeds
  seen all pointed at rows already `is_encrypt_delete_msg = 1`, which show as
  deleted anyway.
- **Both builds answer these reads since contract 0.16.0.** They entered
  `openapi.json` 0.15.0 marked `x-platform` Windows-only; `kamado-cli#44`
  measured the macOS schema and ported them, and the marks came off in both
  repositories (issue #5). Where the Mac stores less — no board table, so the
  notice is always `legacy`; no edit history, so `/revisions` is empty — the
  route's own description says so.
- **A link in the received-files folder is not a copy.** `local_copy` refuses
  a candidate that `is_symlink()`, because following one would stream whatever
  it points at to a remote `/file` caller. kamado checks the same and opens
  with `O_NOFOLLOW`; Windows has no such flag, and making a link here needs a
  privilege, so the one check is the rule both builds share.

### Being addressed: mentions and replies

Both live in `attachement`, which was previously read and thrown away. Measured
on a live client, newest 4,000 rows of each readable room.

- **A reply is `type = 26`** and its attachment carries `src_logId`,
  `src_userId`, `src_type`, `src_message` and `src_linkId` — all five on all
  108 replies seen, with `src_message` non-empty on every one. That text is the
  client's own copy taken when the reply was sent, so it survives the original
  being deleted and may disagree with what the original row says now.
- **Parsing is keyed on `src_logId`, not on the type.** What makes a message a
  reply here is that it recorded a source; reading the type instead would drop
  every quote the moment the client numbers one differently.
- **A reply is recorded two ways now, and the newer one carries no copy of
  what it answered.** The client moved a reply out of the attachment and onto
  a **`threadId`** column: an ordinary `type = 1` row that only points at the
  message it answers. Reading the attachment alone therefore lost every recent
  reply and the screen stopped drawing quote lines with nothing failing.
  Measured here on 2026-09-21, newest 4,000 rows: the busiest room held **346
  `threadId` replies against 26 `src_logId`**, and its newest 50 held **9
  against 1**; the newest `src_logId` anywhere on this account was **three days
  old** (2026-09-18). The same move was measured on the macOS side two days
  earlier, so it is a client change rather than a platform difference.
  `Reader._threaded_quote` reads the answered row back, which means a threaded
  quote says what that message says *now* and goes when that message goes.
  A thread naming a row this database does not hold — or one that was deleted
  — quotes nothing rather than an empty line attributed to nobody, and a row
  naming itself is not a reply to itself. Where both shapes are present the
  attachment's own copy wins, because it is what the sender was looking at.
  Live after the change: 9 quote lines across the account's newest-50 windows
  where there had been none, the busiest room's 50-message window costing
  36.7 ms with eight read-backs in it.
- **The column is asked for, not assumed.** Naming `threadId` in the `SELECT`
  fails the whole statement on a log written before the column existed, so
  `Reader._selection` reads the schema once per room and substitutes `NULL`.
  Unlike the macOS build, a room here *is* its own database, so the answered
  row is found by `logId` alone — `chatLogs` carries no `chatId` to narrow by.
- **A mention is an ordinary `type = 1` row** whose attachment holds
  `mentions`, each entry exactly `{"at": [offset], "len": n, "user_id": id}` —
  29 observed, every id an integer, every `at` a one-element list. `@전체` is
  the separate key `all_mention`, written as an **object** (`{"at": 1}`, 15
  observed) rather than a flag, so its presence is what is read.
- **The text carries only a name, so the id is the mention.** Same reason the
  sender column cannot be trusted below: the name in the message is what that
  account wore when it was sent, and anyone can wear it.
- Frequencies, per room over 4,000 rows: replies 51, 25 and 4; mention rows 10,
  15 and 1. Rare enough that a window of 40 usually has none, which is why
  these keys are omitted when absent rather than rendered as empty.

### Names, and why they cannot be trusted retroactively

- **Every name this tool shows is the name that user carries *now*.**
  `Profile` and `talkUser` are overwritten in place, so a message sent under an
  old nickname is displayed under the new one. In an open chat where people
  rename themselves to imitate each other, reading the sender column is how you
  attribute a conversation to the wrong person — which happened, and reached a
  summary that was sent to the room before it was caught.
- **`userId` never changes, so it is the only safe way to say "same person".**
  Two accounts can share a display name down to the character; they cannot
  share an id.
- **A rename writes nothing anywhere.** The client records a nickname only in a
  membership feed: `chatLogs` rows with `type = 0`, whose `message` is JSON like
  `{"feedType": 4, "members": [{"userId": …, "nickName": …}]}`. Only feed types
  4, 6, 2 and 11 carry names. Measured on one live open chat: 6,037 feed rows,
  156 of which named someone, covering 72 users and 18 distinct renames.
  Types 21 (4,756) and 14 (1,024) are the bulk and carry nothing.
- **No per-message column holds a sender name.** `supplement`, `attachement`
  and `referer` were all checked against 234,061 text rows: the string
  `nickName` appears **zero** times outside `type = 0`. There is no way to
  recover who a message was signed as when it was sent.
- Because of that, `names.py` keeps its own append-only ledger at
  `%LOCALAPPDATA%\kanao-cli\names.jsonl`. Unlike `contexts.json`, which stores
  no key, and `rows.json`, which stores a hash rather than a title, **this file
  holds real nicknames** — a name history cannot exist without them. It is
  opt-in at the constructor: `Reader(ledger=...)` is set only by `cli.main`, so
  no test can append fixture names to a real person's history.
- **The ledger is account-wide and the feeds are room-scoped**, so
  `name_history` narrows the ledger to `chatMembers` plus whoever the feeds
  name. Skipping that made "who else answers to this name" return strangers
  from other chats, which is indistinguishable from the impersonation the
  question is asking about: six answers where three were real.

## The remote server (`kanao serve`)

- **A remote caller is not a person typing, so the reads are bounded.** The
  commands cap nothing because whoever runs one is the cap. Over HTTP
  `limit` reaches SQLite, where a **negative limit means no limit at all** —
  `/digest?limit=-1` read an entire room, and one observed room holds 445,714
  messages. `limit` is 1–1000 and `highlights` 1–20, checked in
  `serve/config.py` **before the reader is opened**, and a `roomId` outside
  the 64-bit range is a 400 rather than an `OverflowError` out of the driver.
- **`openapi.json` is the same file in both repositories, byte for byte.**
  That is the claim it makes: one contract, two builds. Members that really
  differ carry `x-platform` — today the macOS-only `typeCode` on a room and
  the macOS-only `PERMISSION_REQUIRED` — and `tests/serve/test_openapi.py`
  pins *which* members may carry it, so a third divergence has to be declared
  rather than discovered. The same tests ask the running handler whether each
  documented route is routed and read the limits back out of `serve/config`
  and the room kinds out of `model.ROOM_KINDS`, because a specification
  nobody checks is a rumour: until contract 0.19.0 `Room.kind` listed
  `open`, `plus` and `unknown`, which no build sent, and missed `channel`,
  `open-group` and `open-direct`, which the builds do send. Writing it is what turned
  up `INPUT_NOT_FOUND` and `NO_USER_DATA` answering 500 here and 404/503
  there.
- **The send path keeps its snapshot cache and key ring; the read paths do
  not.** `make_live_send` used to build a bare `Reader` per request, so every
  `POST /send` found the account again and decrypted the catalog again before
  a character reached the input box — the rule *a send must not re-read the
  catalog* being broken by the one caller that sends most. Measured live at
  `--min-interval 0`, six requests: **478·469·505·458·474·457 ms → 469·100·110·
  109·113·153 ms**. The first is unchanged and the rest are the saving.
  Sharing only the cache is not enough; finding the account is most of a
  second by itself. Freshness is not what is traded away — `SnapshotCache.
  connect` re-reads the fingerprint on every call and overlays a log that
  moved — and the reader is still new per request, so nothing it memoises
  about names or columns leaks into the next one. **Do not widen this to the
  read routes**: they run concurrently and `SnapshotCache` takes no locks,
  where sends are one at a time under `send_lock`.
- **A `Reader` does not close a cache it was handed.** `_owns_cache` is
  `cache is None`. Without it the sharing above fails *silently*: the next
  reader simply decrypts again and answers correctly, only slowly. The test
  fixtures hand their stub cache over outright (`reader._owns_cache = True`),
  because nobody else holds it and 232 synthetic databases were left open
  when they did not.
- **A cached snapshot outlives the thread that decrypted it, so it is opened
  with `check_same_thread=False`.** Each request arrives on its own thread, so
  the second send reads a connection the first one opened; sqlite3 refuses
  that by default and the refusal is a request that **dies with no answer at
  all**. Found by sending twice against a live client — the suite had no
  threaded send — and pinned since by
  `test_a_cached_snapshot_is_readable_from_another_thread`. The images are
  read-only bytes already in memory and sends are serialized, so nothing here
  is concurrent. `kamado-cli` already carries the same flag, and its work
  order for this change did not mention it — sharing a cache across threads
  and opening the connections for it are one change, so port them together.
- **A send that arrives early waits its turn; it is not thrown away.** What
  `min_interval` protects is one input box, and a caller refused for being
  early can only sleep and retry, which is every client reimplementing the
  queue. `429 RATE_LIMITED` therefore means **the line is full**
  (`MAX_QUEUED_SENDS`, four counting the one being sent), and the queue is
  what gives that number a meaning. Live: two sends fired at the same instant
  both arrived, **exactly 2 s apart**, both read back as `fromMe`; and eight
  fired at once against the running server put **four in the line and turned
  four away**, each refusal answered in 5–7 ms having touched nothing.
- **The pacing is measured from when a send started, and only after it was
  delivered.** From the finish, the next caller pays again for time the last
  send already spent. The old limiter recorded the start too — but *before*
  the send, and for sends that never delivered: one `POST` to a room with no
  window left the next real send refused for two seconds. Measured, old
  against new: `[404, 429]` with nothing delivered, against `[404, 200]` with
  one. It also never fired at all when a send took longer than the interval,
  which is exactly when it was needed.
- **Ctrl-C answers whoever is still in line.** `ServeHTTPServer` sets
  `daemon_threads`, so shutting down would kill an accepted-but-unsent request
  without a word — queueing sends is what creates that state, and a queue that
  swallows one at exit is worse than the refusal it replaced. `run` closes the
  queue, and each waiter answers `503 BUSY` saying the message was not sent.
- **The stream's cursor names every room, because one id cannot stand for an
  account.** `/stream` follows all readable rooms, so the last message it sent
  says nothing about where the other rooms had reached — standard SSE
  `Last-Event-ID` would silently resume the wrong ones. The server emits
  `event: cursor` with `{roomId: logId}` *after* the messages it covers, and a
  client hands it back as repeated `since=<roomId>:<logId>`.
  `Watcher.resume` then seeds `_seen` and records **no** fingerprints, so the
  first poll pages each named room forward from where its reader stopped. A
  room the cursor does not name starts from now; that is a gap, and it is
  documented rather than papered over.
- **A cursor the server cannot honour is refused, not downgraded.** A watcher
  without `resume` answers 500 rather than quietly starting from now, because
  a caller that asked to be resumed and was not would believe it had missed
  nothing. Same reason the README no longer claims reconnection catches up by
  itself: it did not, and saying so was worse than the gap.
- **The connection has a deadline and the streams have a count.** Without the
  handler's `timeout` a client could promise a body in `Content-Length` and
  hold a thread for as long as it liked; it now answers `408` after 30 s. Each
  SSE connection owns a reader and a watcher for its whole life, so eight is
  the most that may be open and the ninth is `503 BUSY`.
- **A body refused unread is read anyway, or the refusal is lost.** The
  handler answers before reading the body whenever the body is what is wrong
  — no `Content-Length`, a bad one, too large, chunked — and for a POST's
  401 and 404, and HTTP/1.0 closes the connection after every answer. A body
  that lands on a socket already closed is answered with a reset, and on
  Windows the reset destroys the 400 the caller has not read yet: measured,
  `WinError 10053` on every try, for a 26-byte body as for a 300 KB one, and
  a body still arriving when the server closes was reset at 300 KB.
  `test_post_without_content_length_is_rejected` failed that way once in a
  full run. So `Handler.finish` half-closes and reads what is still coming
  until the caller closes, for at most `LINGER_SECONDS` and `LINGER_BYTES`.
  It does so only when a body may be left (`_body_left_unread`), because a
  lingering read on every request would hold the single-threaded test server
  until each client hung up. The regression test reads the server's side of
  the socket at its close rather than waiting for a reset: before the fix it
  failed 20 of 20, in all three ways the order of two threads allows.
- **A reader hanging up is SSE's success case.** `socketserver` prints a whole
  traceback for the `ConnectionResetError` that a closed `curl -N` raises on
  the next write, so `QuietDisconnects` swallows exactly the four ordinary
  disconnect errors and escalates everything else. The suite went from printing
  tracebacks on every stream test to none.
- **A remote read can ask for a room to be opened; it never does so by
  itself.** A room's key is in memory only once the client has opened it, so
  `/context` and `/digest` answered 503 `KEY_UNAVAILABLE` for every other
  room, and a remote caller had no way to ask what the interface and
  `messages --open` already could. `open=true` has the handler call
  `room_opener` — `backends.make_live_room_opener`, the same
  `opening.open_for_reading` the command uses — under `send_lock`, because it
  drives the one client, and then read once more. Without the flag the 503
  stands: opening marks the room read in the client, and a read must not do
  that as a side effect. A value that is neither true nor false is a 400,
  so a typo cannot open a room. Live, a locked room: 503 in 0.46 s, then
  200 in 3.27 s with `open=true`, then 200 in 0.55 s without it.
  `opening.py` exists because `serve` must not import `cli`.
- **The digest is `digest.py`, not `cli._digest`.** The server wanted what the
  command does, and reaching into `cli` for it meant assembling an
  `argparse.Namespace` to satisfy a function that never wanted one — and
  pointing `serve` at the layer above it. Both now call
  `digest.build(reader, target, …)`, and `cli.DIGEST_LIMIT` is that module's
  default rather than a second 400.
- **The threaded server is tested as the threaded server.** Everything in
  `tests/serve/` drives a single-threaded `HTTPServer`, which cannot answer a
  second request while a stream is open — so `test_server.py` runs the real
  `ServeHTTPServer`, where an open stream must not block `/rooms`, two sends
  genuinely race for the lock, and the slow-body deadline is observable.

## Terminal interface

- The palette is OmO's `dark` theme, duplicated rather than imported, because
  this project must not read another program's install.
- **The light palette is not the dark one inverted, and it is measured.** A
  tint that reads as calm on a dark field is simply missing on a white one, so
  the light speakers carry more saturation and far less lightness, and the
  yellow-to-cyan arc is darkened again because those hues lose contrast first.
  Every name and body colour clears **3.8:1** against `#eff1f5` and white — the
  worst measured 4.11:1 — with all 40 still distinct. `success` and `warning`
  are darker than the palette they came from (2.96:1 and 2.31:1 originally)
  because they are not decoration: they are the `✓`, the `?` and the `@` that
  say whether a message was delivered and whether it named you.
- **Only `cli` asks what the terminal looks like.** `Theme(dark=True)` is the
  default and `detect_dark_mode` is called once, in `_tui`, for the same
  reason the name ledger is opt-in: a default that reads the machine makes
  every test depend on the machine. Detection is `KANAO_THEME`, then
  `COLORFGBG`, then the Windows app theme in the registry, then dark — nothing
  can read Windows Terminal's actual background, which keeps its scheme to
  itself and reports the legacy attribute table whatever the scheme says.
- **A URL is clickable, and every line it reaches carries the whole target.**
  `wrap_with_links` binds an OSC 8 hyperlink per line, so a link split across
  two rows opens the same address from either half rather than a truncated one.
  The pieces are matched back to the source **character by character**, not by
  counting: wrapping drops the spaces it breaks on, so length arithmetic drifts
  by however many it dropped and silently binds the target to the wrong letters
  — a message with two spaces in it is enough. The escapes are zero-width and
  `_ANSI` knows them, or every column after a link would move by the length of
  the URL. Links are emitted only when the theme is enabled.
- **Never measure text with `len()`.** Hangul is double-width and every line
  carries escape sequences; `layout.display_width` is the only correct ruler,
  and a mistake here silently shifts every column rather than raising.
- **Somebody else's text is cleaned before it is styled.** Measuring past an
  escape is not disarming it: `truncate_to_width` carries escapes through on
  purpose, so a message or nickname holding `ESC[2J` reached the terminal
  whole. `layout.clean` replaces C0 with its control picture, DEL with its own
  and C1 with U+FFFD, one character for one, at every site that draws
  messages, names, titles, quotes, digests or notices. Stripping a finished
  frame instead would take the renderer's own colour and links with it. A URL
  ends at a control or its picture, and a target that is not printable is
  not linked. JSON is untouched; this is display policy.
- The console must be switched to code page 65001 before drawing or Hangul is
  replaced, and virtual terminal processing must be enabled or escapes print
  literally. Both are restored on exit along with the alternate screen.
- **Frames are rebuilt whole and written by the row that changed.** Every frame
  is still composed in full — a caller never decides what to repaint, which is
  what made partial redraws flicker and strand cells — but `Console.draw`
  remembers the rows it painted and writes only the ones that now differ, at
  absolute coordinates. A frame the screen already holds writes nothing. The
  loop redraws after every keystroke and again each time the key wait expires,
  so a whole frame meant 11 KB at 200x50 several times a second, each ending in
  a cursor move; a Korean IME anchors its composition to that caret and redraws
  it with the cells beneath, which is why typing Hangul lagged where ASCII did
  not. Measured against the fixture room: one keystroke 11,175 B → 275 B, an
  idle second 89,400 B → **0 B**. Replaying both paths through a terminal model
  at 120x40, 200x50 and 80x24 left every cell and the caret identical, which is
  the only thing that licenses writing less. The cache is given up on a resize
  and on re-entering the alternate screen, and it is sound only because
  `Console.draw` is the sole writer to that screen.
- **The frame is cut and filled to the screen by `Console.draw`, not by its
  callers.** One line wider than the console wraps, and every row after it lands
  a row late and at the wrong column: driving a real console left 23 rows
  holding the previous frame where two were expected, which is exactly the chat
  remnants that showed under the room list. Narrow lines are padded, so no cell
  survives a frame it does not belong to.
- **The minimum size is the layout's, not the paint's.** `Console.size` never
  reports below 20x8 so producers always have room to lay something out, but a
  window really can be smaller: at 30x5 it still answers 29x8. Painting eight
  rows into five scrolls the screen, which moves both the caret and the rows
  `draw` remembers, so the paint is clipped to `_visible_size` and the caret is
  clamped into it. A `\r`, `\n` or `\t` in a line is replaced with a space for
  the same reason at the other end: `layout.display_width` counts it as zero
  columns, so it pads to exactly the right width and then renders as a break
  that moves every row after it.
- **Wrapping is turned off through the console mode, not the escape.**
  `\x1b[?7l` is accepted and ignored here — a 140-column line still wrapped on a
  120-column screen — so `_wrap_free_mode` clears `ENABLE_WRAP_AT_EOL_OUTPUT` while
  keeping the escape-processing flags.
- **Measure glyphs in the console, not in a capture.** `ReadConsoleOutputCharacterW`
  over a whole screen returns characters, not cells, so one wide character
  shifts every row after it and a correct frame looks smeared. Read one row at a
  time. Every glyph the interface draws was checked against the cursor's own
  advance: box characters, `❯`, `•`, `◉`, `↑↓`, `✓`, `↳` one column, Hangul two.
- **The composer is an editor, not an append-only line.** `ChatState.caret` is an
  index into the draft, and anything drawn left of the text belongs to the
  caret's column too. Home and End move the caret while something is written and
  scroll the conversation when the box is empty.
- **Escape must not destroy a draft.** It leaves the room, and the draft is kept
  per room in `App.drafts` so reopening restores it with the caret at its end.
  `Ctrl+U` is the key that clears, and sending forgets the draft.
- **A newline reaches the box two ways.** Shift+Enter carries
  `dwControlKeyState`, and a paste arrives as ordinary keystrokes — so an enter
  with more character events already queued is a newline rather than a send.
  `PeekConsoleInputW` answers that without consuming the queue; counting any
  pending record would call a key release a paste.
- **Letters filter the lists only after `/`.** Typing must not take the letter
  shortcuts away from the room and friends views, so filtering is entered
  deliberately and left with escape.
- **A send is deferred by one frame.** `_submit` parks the text on
  `App.pending_send` and the loop paints before `flush_pending` runs: a send that
  has to open a room takes 1.7 s, and the screen would otherwise freeze with no
  explanation.
- **Row geometry scales with the screen.** The pitch, first centre and column
  were measured at 100%; `row_geometry(dpi)` scales them, because on a 150%
  laptop every row would otherwise land on someone else's conversation.
- **Delivery is matched by words, and doubt is admitted.** The client may tidy
  whitespace, so the comparison is word-wise; after 30 s unseen the mark becomes
  `?` rather than going on claiming to be queued.
- Speaker tints share one base grey and differ only slightly. Hue alone stops
  separating past roughly twenty speakers, so a three-step lightness cycle is
  applied as well; `test_tui_layout.py` pins both the subtlety and the
  separability, and they pull against each other.
- `/sum` is extractive on purpose. There is no model in this process, so a
  slice reports counts, participants, keywords and representative lines, and
  never claims to summarise.
- The client writes placeholder words such as `사진` into the text column for
  attachments; they are filtered out of keywords or they dominate every slice.
- **`/real` answers in one line, and its useful answer is the plural one.** A
  single chain says what somebody used to be called; two chains say which of
  two identical names is the impostor, and that is the question a reader
  actually has when they type it. The arguments are re-joined with a space
  because `Some Name (handle)` is one nickname, not two words.
- **The key loop must never block on input.** `Console.read_key` takes a
  timeout so `App.run` can call `App.refresh` between keystrokes; blocking on
  `getwch` was why the first version showed a frozen conversation until a key
  was pressed.
- Refreshing reuses `watch.Watcher`, so an unchanged room costs one stat
  (0.3 ms measured) and is never decrypted.
- The composer keeps focus for the whole time a room is open, so no letter is
  a shortcut there; `/older`, `/export` and `/quit` carry what `o`, `e` and `q`
  used to do. `test_tui_send.py` pins that a typed `q` does not quit.
- **Several conversations can be open as tabs, and the one in front has the
  whole width.** `App.chats` is the list and `App.chat` is a property over it,
  which is why everything acting on "the" conversation still says `self.chat`.
  Side-by-side panes gave each conversation half the screen from 89 columns,
  and half a width is where Hangul sentences break into fragments, so from
  `kamado-cli#43` the conversation in front is drawn across the screen and a
  strip of tabs (`layout.plan_tabs`) sits above it. The strip is drawn for a
  single room as well (`kamado-cli#55`): leaving it out moved the hints, the
  notice and the conversation down a row when a second room opened, and the
  title from the footer to the top, so the footer never names the room now.
  Tab and shift+tab still ask for a second tab, because they ask whether
  there is anywhere to go, not whether the strip is shown. The tab in front
  carries `FOCUS_MARK` as well as the highlight, because a plain terminal
  has no highlight; the others carry `+N` for what
  arrived behind them, and tabs that do not fit are counted at their edge.
  `tab` and `shift+tab` walk the ring — the console reports Shift+Tab as the
  `VK_TAB` record with `SHIFT_PRESSED`, decoded as `Key.BACKTAB` — and `esc`
  closes the tab in front, not the screen. Only the tab in front places
  pictures, so switching withdraws one and gives the frame up.
- **Each tab keeps its own watcher, unlike the macOS build.** There one
  fingerprint covers the whole account, so sharing a watcher is what stops
  tabs multiplying the most expensive thing that build does. Here a room *is*
  its own database and a poll is one stat of its log, so tabs cost exactly
  what they read — and a tab behind is still followed, or its count of
  arrivals would start from whenever it was brought forward.
- **The room list counts what this screen has not shown.** Reading here
  never lowers the client's `newMessageCount`, so a room read to its last
  line came back to the list still saying "5 unread" while its tab had said
  nothing. `App.seen_unreads` holds the count a room had reached when it was
  last seen here and the list subtracts it; an open tab answers with its own
  `unseen`, the `+N` the strip shows, and arrivals behind the list count
  there too. The catalog is not re-read while a tab is open, so closing one
  records the count it opened at plus `ChatState.arrived`, less the arrivals
  the reader never scrolled down to. A count that falls below the record is
  the client reading the room, and the record is dropped. Not measured here:
  whether the Windows client lowers the count when it reads a room, and
  whether it counts feed rows (`_counts_as_unread` assumes it does not).
  From `kamado-cli#51` (`03b6449`), whose catalog refresh marks the room in
  front as seen; here that refresh does not run while a tab is open, hence
  the arrival count.
- **A message belongs to the room it arrived in.** `_confirm_delivery` takes
  the conversation as an argument rather than reading the focused one, or a
  tick would land in whichever tab the keyboard was in when the client wrote
  the message back.
- **Console input comes from `ReadConsoleInputW`, never `msvcrt`.**
  `kbhit()` is true for key-up, focus, mouse and buffer-resize records while
  `getwch()` blocks on them, so the loop stalled until the next real key
  arrived: the screen sat one keystroke behind and backspace looked broken.
  Key-up and non-key records are dropped, and an astral character is rejoined
  from the two UTF-16 halves the console delivers it as.
- **The viewport anchors by a flag resolved at render time.**
  `ChatState.anchor_top` exists because the top is a distance from the bottom,
  and neither the body nor the screen height is known when the key is handled.
  A digest opens at its own first line; a conversation opens at its newest.
- **Typing must not redraw the conversation.** `_transcript_lines` is cached
  per (width, message count, `ChatState.revision`, open photos). Rebuilding it
  on every keystroke measured 71 ms per key at the default 400-message window
  and 142 ms at 800, which is what made input stutter; cached, a keystroke
  costs 0.17 ms. Anything that changes a loaded message must change that key
  or the screen will lie — which is what the revision is for.
- **A message on screen can change without a new one arriving.** Deleted for
  everyone, it keeps its id and has its text replaced, and the watcher asks
  only for ids above the newest — so the text stayed on screen, in the digest
  and in the export. `Watcher.moved` names rooms whose log moved; for those
  the interface compares the held rows' versions (`Reader.row_versions`, one
  primary-key range read, no names or quotes) and rebuilds only a row whose
  version changed (`Reader.messages_at`). A version and the message built from
  it always come from the same read. A digest is cut again without the
  deleted line, and erased messages are not counted in a digest at all.
- **A late row inside the loaded range is an arrival too** (issue #13).
  The same range read also finds ids the tab does not hold, and
  `messages_at` builds those with their versions before they are inserted in
  log-id order. They update the digest, delivery mark and unseen counts without
  moving a scrolled viewport. Nothing below the oldest loaded id is read:
  only an explicit older page extends that range. Measured read-only on Windows
  on 2026-10-01 at kanao-cli `a5eb25b`: among the newest 4,000 rows of each of
  eight readable rooms, four rowid inversions across two rooms. The mechanism
  reported from kamado-cli `4f0314b` therefore occurs here too; the fixture
  regressions protect insertion, ordering, deduplication and the history bound.
  `follow` and `/stream` still resume only above their cursors; changing their
  shared contract is outside this fix.
- **A reply is stacked the way the client stacks it, and neither mark costs a
  column.** The replier is named once, on the row that carries the quotation:
  time, name, then the one line they were answering, cut because the message
  it quotes is routinely far outside the window. Their own words go on the
  next row, where the `↳` occupies the single column that already separated
  the name from the text — the mark belongs to the answer, not to the quote.
  The mention mark uses the two-column gap the head already had between the
  time and the name. Both are width-neutral on purpose: anything drawn left of
  a message moves the whole conversation sideways, which is invisible in a
  screenshot and shows up only as text landing one column late.
- **A send must not re-read the catalog.** `reader.rooms()` measured 3.0 s
  when the snapshot had gone stale, and sending a message is exactly what
  makes it stale, so the duplicate-title census comes from the room list the
  screen already loaded rather than from a fresh read.
- **A refusal for a duplicate title names the other room.** The way out of the
  collision is in the client — a title is the only thing a window can be
  addressed by, so one of the two rooms has to be renamed — and "there are 2"
  does not say which one that is. The other room is described by kind, size
  and last activity, from the list the screen already holds, so this costs
  nothing the send was not already carrying. Three titles collide across 272
  rooms on the account this was measured on, so it is not a hypothetical.
- **Polling yields to typing.** A poll that finds something used to re-decrypt
  the room's whole log — 0.62 s and 0.94 s measured on two live rooms — and
  this loop is single-threaded, so `_poll_due` defers while keys are still
  arriving and `_next_poll_at` waits behind an expensive room instead of in
  front of it. The log-only overlay is what let `REFRESH_SECONDS` drop from
  0.35 to 0.15; the deferral stays, because an arrival is still the most
  expensive thing that can happen between two keystrokes. Re-measured after
  the overlay: a poll that opens the room costs 171–355 ms, four times in 40 s
  on a live 33-member room, against 0.30 ms for one that finds nothing.
- **The keystroke clock cannot see Hangul being typed, so the draft box
  answers for it.** The IME holds every jamo and paints the half-made syllable
  at the terminal's own caret, so nothing reaches the loop until the syllable
  commits and `TYPING_GRACE_SECONDS` expires *inside* a word — which is why
  typing stuttered where the same keys in ASCII did not. Text already in the
  composer is the evidence the clock is missing, so it raises the grace to
  `COMPOSING_GRACE_SECONDS`. That is 1.5 s rather than the macOS port's 3.0 s
  because the hidden poll costs 171–355 ms here against 1.3–1.9 s there; a
  message left half-written still lets the room move again 1.5 s later.
- **The first frame is drawn before the first decryption.** Opening the
  interface means decrypting the catalog, which is the slowest thing this
  program does, and it used to happen before anything reached the screen — so
  the terminal sat blank with no explanation. `App.run` paints one frame
  carrying a loading status first; it costs nothing and names what is being
  waited on.
- **The screen is narrower than the buffer, twice over.** `shutil` reports the
  console buffer, which is wider than the window showing it, so the visible
  region (`srWindow`) is used instead. On top of that Windows Terminal paints
  its scrollbar over the last cells rather than shrinking what it reports, so
  `right_margin()` leaves one column unused; `KANAO_TUI_MARGIN` tunes it,
  and 0 takes them back on a terminal that does not need it.
- **Autowrap is off while the interface owns the screen.** A frame line is
  exactly as wide as the screen, and writing its last cell makes the terminal
  wrap; the frame's own newline then lands a row late, so every box below
  drifts and its right border falls off.
- **Nothing is drawn to stand in for the cursor.** The terminal's own caret is
  parked at the end of the draft, and a block drawn beside it read as a second
  cursor in the wrong place. Whatever sits left of the text is part of that
  caret's column: adding a mark slot there once left `caret` two columns
  behind, which is exactly how it looked.
- **The message box is closed on both sides and marked on the right.** The send
  mark — `*` queued, `✓` delivered — sits against the right border, where it
  cannot push the prompt or the text sideways. `✓` is width-neutral rather
  than ambiguous. Emoji were tried and removed: the console advances the two
  columns they measure, but the glyph paints over the next cell and the smear
  survives until something overwrites it.
- **What was sent is marked, not announced.** Typing clears a stale tick but
  never an unresolved `*`: hiding that would hide the one fact worth knowing,
  since a queued Enter is not a delivered message.
- **The caret belongs in the composer.** A Korean IME draws half-typed Hangul
  at the terminal caret, and a finished frame leaves the caret after the
  footer, so composition appeared beside the version string. `Console.draw`
  takes the cell to park it in, and `App.cursor` carries it.
- **The browser always opens the room it is sending to.** Someone typing into
  a conversation has already named their target, so the composer passes
  `open_if_needed`. The alias the window may carry is read once in `open_room`
  and kept on `ChatState` — reading it per send would put the 3.0 s catalog
  decrypt back on the path that was just cleared of it.
- **A sent message is never echoed into the transcript.** It arrives through
  the watcher like any other message, so the screen cannot show a line the
  client did not actually send.
- **Arrivals must not steal the viewport.** When `chat.offset` is zero the view
  follows the newest line; when the reader has scrolled up, the offset is
  advanced by exactly the number of lines appended so the same text stays put,
  and the count surfaces as `unseen` instead. `test_tui_live.py` asserts this
  against the scrolling region only — the status line and footer are *supposed*
  to change, and including them in that assertion is what made the first
  version of the test fail against correct behaviour.

## Conventions

- Every command returns `{"ok": true, ...}` or
  `{"ok": false, "error": {"code", "message"}}`. Exit 0, 1 for a handled
  failure, 2 for a malformed command. New commands join this contract.
- Every identifier crossing the boundary is a **string**; KakaoTalk ids exceed
  2^53.
- Messages are returned oldest-first everywhere.
- Failures carry an `ErrorCode`. "KakaoTalk is closed" and "that room was never
  opened" are different codes because they call for different actions.
- Nothing expensive happens in a constructor, so `status` can explain a closed
  client instead of failing for the same reason.
- Ambiguity is refused with candidates attached, never resolved by picking the
  first match.
- **A key that reports being addressed appears only when it applies.**
  `replyTo`, `mentions`, `mentionsAll`, `mentionsMe` and `repliesToMe` are
  absent on an ordinary message rather than rendered empty, because a window is
  forty messages and denying five things per line triples the answer. Their
  absence is the negative; `context` also summarises them once in `attention`.
- **The old name resolves to the same module objects, not to copies.** This
  project was `kakaoread`, and `python -m kakaoread`, the `kakaoread` script,
  `scripts/kakaoread.py` and `import kakaoread` all still work. The shim is a
  meta path finder that hands back the `kanao` module a name stands for, so
  `KanaoError` is one class under both names — loading a second copy instead
  would leave an `except` written against one name silently missing the other.
  Import rewrites `__spec__` on the way through and `exec_module` puts it back,
  so `kanao` never ends up describing itself as `kakaoread`.
  `kakaoread.__main__` is excluded from the alias because `python -m` asks the
  loader for a code object, which an alias has none of.

## Friends and sending

- Friends are `talkUser.type = 1`; other known users are not friends.
  Phone numbers are withheld unless `--with-phone` is explicit. Friend ids
  and direct-chat ids are serialized as strings. TUI `f` opens the friend list.
- Send requires a uniquely resolved catalog room and an exact matching open
  window. Duplicate catalog titles remain ambiguous even with `--room-id`.
- **An unfinished draft is cleared, not refused.** It used to be `BUSY`, which
  could only be resolved by walking to the client, and the caller had already
  named the room and the words. The clearing is reported — `clearedDraft` on a
  send, `wouldClearDraft` on a dry run — because losing somebody's half-typed
  line silently is the part that would be indefensible. `EM_SETSEL(0, -1)`
  already covered the whole box, so nothing about the write changed; the two
  guards around it went away, and the box is now read **once**, after the
  write, to verify what landed.
- The Korean client's empty `RICHEDIT50W` returns `메시지 입력`, not `""`.
  This literal is reserved as the empty hint, and is what an empty box reads
  as rather than as a draft.
- **A room can be opened without the mouse.** The room list is
  `ChatRoomListCtrl` inside the main window, an `EVA_VH_ListControl_Dblclk` that
  handles double-click in its own WndProc, so posting
  `WM_LBUTTONDOWN/UP/LBUTTONDBLCLK/UP` at a row opens that room. Posted mouse
  messages work here even though posted Enter does not reach the send path.
  The cursor is provably untouched: measured identical before, immediately
  after, and 0.4 s later.
- **Rows can only be addressed by coordinate, so a miss is ordinary.** The
  list is owner-drawn — rows are neither child windows nor UIA elements.
  Geometry measured at 100% scaling: origin (66,116) inside the main window,
  pitch 68 px, first row centre 29 px, x 134. Pinned rooms float to the top,
  so the catalog order is **not** the list order. `RoomOpener` therefore tries
  rows, reads the title of whatever window appears, closes anything that is
  not the target, and only a title that matches exactly is sent to.
- **Opening a room scrolls the list, so a row number is meaningless until it
  is reset.** A room found at row 0 one minute was missed across all eight
  rows the next, because the scan itself had moved the list. `scroll_to_top`
  wheels it back before **every** row, by posting `WM_MOUSEWHEEL`.
- **The desktop is put back the way it was found.** A room this opened is
  minimised, because nobody asked for it to be in front; a room the user
  already had open is never moved. A main window taken out of the tray goes
  back to the tray. Verified live from a cold tray state: the room ended
  minimised, the main window ended hidden, in 5.4 s.
- **A minimised window sends.** Proven end to end against the live client:
  the window stayed `iconic` through the send, the cursor did not move, the
  foreground did not change, and the message read back from the log. A
  hidden window also accepts text (written and read back), but `chat_windows`
  deliberately sees only visible ones, so hiding is not a supported parking
  spot — minimising is.
- **A room that opens is in front until it is put away, so do not wait.** The
  opener used to sleep a flat 1.2 s per row; it now watches for the window
  and minimises it the moment it appears, without activating it. What is
  left — measured 949 ms and 964 ms across two runs, unchanged by the
  minimise style — is KakaoTalk showing its own window, and nothing on this
  side preempts that. End to end: 2.8 s → 1.7 s.
- **Scanning rows opens real rooms, and opening a room marks it read.** That
  is the true cost of a miss, not the flicker, so the rows are aimed at
  rather than swept. A row that worked is remembered per room in
  `%LOCALAPPDATA%\kanao-cli\rows.json` under a hash of the title, never the
  title itself. Before that memory exists the guesses alternate between two
  truths — a room sits where the catalog's recency order puts it, or it is
  pinned and near the top — because exhausting the first hypothesis measured
  four wrong rooms opened for a pinned room. Alternating brought that to one,
  and the remembered row brings it to zero: measured 10.0 s and four rooms,
  then 5.3 s and one, then 2.9 s and none.
- **With a hint, two rows are guessed at, never eight.** The remembered row
  and the hinted one; after that the search box. Alternating across the top
  rows opened every one of them from the interface whenever its order
  disagreed with the screen's, which it does for every pinned room. The
  sweep survives only for a caller that has no hint at all.
- **The search lists members' rooms too, so the room is not always first.**
  KakaoTalk's room search matches a title *or* the name of anyone in the
  room, in the list's recency order. A one-to-one room sat third below two
  open chats that merely had that person as a member, so opening the first
  result opened a stranger's room and the send failed. `Reader.search_position`
  predicts the row from the catalog — titles, `chatMembers` and the name map —
  and only that row is opened: predicted third, shown third, and the room
  then opened with no other window appearing. The interface passes its own
  room list and the memberships it read with it, because a send must not read
  the catalog.
- **A room past the searched rows is searched for, not swept for.** With a
  catalog hint beyond `SEARCHED_ROWS`, every hint guess fell out of range and
  what remained was a sweep of the top rows — each a wrong room opened and
  marked read before failing anyway. Such a room now gets only its remembered
  row, then the client's search box: the text goes into the visible `Edit`
  (140x23) with `EM_REPLACESEL`, and the results are drawn in a **separate,
  previously hidden `SearchListCtrl`**, not in `ChatRoomListCtrl`. Clicking
  the room list after searching opened the unfiltered top room at 0.15 s and
  nothing at 1.0 s; clicking the result list's row 0 with the ordinary row
  geometry opened the room. The box is 0x0 until search is opened, so a closed
  one is opened by posting a click at the magnifier inside `ChatRoomListView`
  (217,25 at 100%) and closed again afterwards at its X (297,72). A bare
  down/up there does nothing: the view arms a button only after a
  `WM_MOUSEMOVE` over it, which is posted too, so the cursor still never
  moves. A search row the person had open is emptied but left open. Verified
  live from a tray-hidden main window: the room sent, its window ended
  minimised, the main window back in the tray and the search row closed.
- **A minimise sent the moment a window appears can be undone.** A room opened
  from the search results was left in front: the client's own show landed
  after the minimise. `RoomOpener._park` watches the window until it has
  stayed iconic for `PARKED_POLLS` looks and minimises it again if it rose.
- **The old note, kept for the shape of the cost:** That
  is the true cost of a miss, not the flicker. Keep `SEARCHED_ROWS` small and
  prefer rooms near the top of the list.
- **A main window in the tray is hidden, not destroyed.** It fails every
  visibility test but is still enumerable, so `show_main_window` finds it among
  all top-level windows and calls `ShowWindow`; that moves no cursor and posts
  no input. Verified live: hidden with `SW_HIDE`, then brought back by `--open`.
- **The window title is the UI's, not the catalog's.** They differ for the
  self-chat and for groups with no custom name. `reader.stored_title` returns
  the catalog's own `chatRoomTitle` — `나상권` for a self-chat shown as
  `나와의 채팅` — and the CLI passes it as an alias the window may carry.
- **Do not use WM_SETTEXT.** It leaves the client's send-enabled state wrong.
  EM_SETSEL + EM_REPLACESEL fires the ordinary change path. Post Enter with
  scan-code and key-transition bits; check both PostMessage return values.
- No SetForegroundWindow, SetFocus, SendInput, clipboard writes or mouse moves.
  The one exception gives focus *back*: opening a room lets the client
  activate its window, so `RoomOpener.open` notes the foreground window first
  and asks once for it again at the end. Windows refuses that request from a
  background process — accepted and ignored, measured — so what really keeps
  the keyboard where it was is how the room is put away. **A room is
  minimised by posting `WM_SYSCOMMAND`/`SC_MINIMIZE`**, the minimise button:
  the client handles it on its own thread after it has shown and activated
  the room, and activation goes back to the window behind. Minimising from
  here raced the client — done first, the client then activated a window
  already minimised, which held the keyboard with nothing on screen; done
  synchronously, it waited 0.98 s on a client busy showing that window. The
  main window taken from the tray is shown with `SW_SHOWNOACTIVATE` and put at
  the bottom of the Z order, or it was the window behind the room and took
  the keyboard. Measured live from a terminal, self-chat, main window up and
  in the tray: focus returned to the terminal both times, and opening the
  room fell from 3.3 s to 1.15–1.30 s (the search's settle 0.3 → 0.1 s, the
  parking watch 1.5 → 0.5 s, and no synchronous minimise).
  Native message calls have a timeout; a hung client must not hang the CLI.
- **`submitted` means the input box emptied, never confirmed network
  delivery.** Posting Enter only queues a keystroke, so `PostMessage`
  returning says nothing about whether the client acted on it; the box going
  empty is the client's own account of having taken the message. An
  unreadable box is an unknown outcome, not an empty one, and reporting an
  unknown as submitted is the failure `Sender._confirm_sent` exists to
  prevent. Read back a new matching from-me record before claiming delivery.
  Never automatically retry an uncertain send.
- **The confirmation is a poll, so a working client pays one read.** Measured
  live against the self-chat: the box was already empty on the **first** look,
  **13.7 ms** after the Enter was posted, with the whole send at 22.2 ms and
  the message read back as a new `fromMe` record. The three-second ceiling
  (`SUBMIT_ATTEMPTS` × `POLL_SECONDS`) is therefore what a *stuck* client
  costs, not what a send costs — which is why this is a poll and not a fixed
  settle. `openapi.json` needed no change: it already said `submitted` means
  the client accepted the text, and it is this build that had been reporting
  something weaker.
- Confirm recipient and content before sending to real people. Callers must
  keep at least two seconds between sends. Real tests use only self-chat or a
  room explicitly authorized by the user; unit tests never call the desktop.

## Testing rules

- **No real data, ever.** Not in fixtures, tests, logs, or committed output.
  Fixtures are synthetic; `tests/fixtures.py` mirrors the real schema.
- No sleeps and no timing assumptions. `Watcher.poll` exists separately from
  `Watcher.follow` precisely so the suite never waits on a clock; test `poll`.
- Stubs must fail the way production fails. `StubCache` raises `KanaoError`
  because the real cache does — a stub raising `FileNotFoundError` once made a
  test pass against an error type production never produces.
- **A stub that answers instantly is a timing production never has.**
  `tests/serve/test_send.py` gave the rate limiter 60 s and 3600 s intervals
  against a `StubSender` that returns in microseconds, so it passed on the one
  timing it could not fail; the variable that mattered — how long a send takes
  — was the thing the stub erased, and a real send was slower than the
  interval it was supposed to be held to. Pacing is driven by an injected
  clock now, and `_SendQueue._wait` is a method so a test can replace the one
  place it really blocks. An HTTP-level test that steps that clock must step
  it **further than the interval, derived from it** rather than written down,
  or the suite genuinely waits minutes.
- **A test that needs a queue full must fill the queue, not hold a request.**
  `QuietHTTPServer` answers one request at a time on purpose, so a send parked
  inside the handler cannot be overtaken by the request meant to be refused —
  the test deadlocks against its own harness. Reserve the places directly.
- When patching a module attribute, bind the original **before** patching or
  the wrapper will call itself.
- **A test that needs Windows says so and is skipped elsewhere.** Most of this
  suite is platform-neutral and gets run on the macOS sibling's machine while
  shared code is written there; before `tests/platform.py` that run answered
  with a wall of import errors, which says nothing except "not Windows" and
  hides the one real failure somebody just introduced. `require_windows_console`,
  `require_windows_crypto`, `require_windows_libraries` and `import_app` guard
  the modules that reach for `WinDLL`, and `tests/support.py` imports
  `ctypes.wintypes` **inside** `encrypt` for the same reason — reading it off
  Windows raises and would take down every module that imports the fixtures.
  The guards can be exercised from here rather than guessed at: `python -c
  "import ctypes; del ctypes.WinDLL; import unittest; unittest.main(
  module=None, argv=['x','discover','-t','.','-s','tests'], exit=False)"` runs
  the suite the way a host without those libraries sees it, and that is how
  the seven `Win32MessageTests` errors were found — they patch `ctypes.WinDLL`
  itself, and `patch` refuses an attribute that is not there. It does **not**
  model POSIX path comparison, which is the other half of what differs and is
  why the profile walk folds case itself. On Windows nothing is skipped:
  the suite is 1083 tests with a skip count of zero, and that is the check that
  a guard has not quietly turned something off.

## What was taken from the macOS sister project, and what was not

`kamado-cli` is the macOS port and the two repositories drift. Work crosses
over by hand, so the reasons for *not* crossing something over are recorded
here — otherwise the same question gets re-answered every time the gap is
noticed again.

Taken:

- **Tabs instead of side-by-side panes** (`kamado-cli#43`), from kamado-cli
  `5a99d28` onto this repository's `ee2d19a`. The strip planner's algorithm
  crossed unchanged; the rendering, the Shift+Tab decoding (a Win32 record
  here, CBT there) and the photo placement were fitted to this build, and the
  focused tab carries a mark as well as a highlight. Not taken: the shared
  watcher (see above) and tab clicks, which would need mouse reporting and
  take the console's own text selection away.
- **`LogRestarted`**, the macOS build's non-retried log restart, for the same
  reason it exists there.
- **Row-level repainting** (`kamado-cli#23`). Reviewed, merged there, ported
  here with the Windows frame's own per-row `CLEAR_LINE` and trailing
  `CLEAR_BELOW`, and re-measured on this side.
- **Clipping the paint to the physical window**, and scrubbing `\r`, `\n` and
  `\t` out of a line. Not a feature, a latent bug in both.
- **Clickable URLs** (`kamado-cli#22`), reimplemented. The upstream version
  maps wrapped lines back to the source with `len()` arithmetic, which drifts
  by every space the wrapper dropped; here the characters are matched directly.
- **The light palette and appearance detection** (`kamado-cli#20`), with the
  macOS `defaults read` probe replaced by the Windows app-theme registry value
  and the contrast re-measured rather than inherited.
- **Holding the poll while a message is being written** (`kamado-cli#25`). The
  IME is blind to the keystroke clock on both sides, so the mechanism crossed
  over unchanged; the constant did not. 3.0 s there buys back a 1.3–1.9 s
  poll, and the same poll measured 171–355 ms here, so it is 1.5 s.
- **Naming the room an ambiguous send is torn between** (`kamado-cli#26`).
  Ported as written — the census and the room list it describes from are the
  same on both sides — and it is live here: three titles collide across the
  272 rooms of the account it was checked against.
- **The session kept between launches** (`kamado-cli#57`, `#58`, issue #12),
  from kamado-cli `e106351` and `4bd41e3` onto this repository's `3b39119`.
  `tui/session.py` keeps `/time`, `/safebot` and the open tabs with their
  focus in `%LOCALAPPDATA%\kanao-cli\tui_state.json`, filed per account. It
  holds room ids and two booleans — no title, no name and no draft. Like the
  name ledger it is opt-in at the constructor: `App(session_file=...)` is set
  only by `cli._tui`, and `tui --fresh` passes none. The two fixes made over
  the PR there hold here by construction rather than by code: this screen has
  no worker queue, so saved tabs open one after another and none can be
  refused for a full queue; and the reader *is* the SafeBot state, so the
  switch is set on it before the first tab is read and the two cannot
  disagree. What did need fitting: the account is `Reader.own_user_id()`,
  asked once the catalog has been read rather than in the constructor; a
  saved room that is locked now is skipped, because opening it means the
  client bringing a window up and starting the screen must not do that — it
  drops out with the next write, like a room that has gone; restoring writes
  once, after the last tab; and the temporary file carries the process id,
  so two interfaces do not write into one, while a refused move (another
  process holding the file open) is said on the status line. Not taken: the
  mouse switch, which this build does not have.

Taken later, after being refused twice — **showing photos in the conversation**
(`kamado-cli#32`). Both of the reasons it was refused turned out to be
answerable, and the third was a decision the owner made rather than a fact:

- **The display layer is sixel, written here.** Windows Terminal implements
  sixel from 1.22, not kitty, so the escapes were not ported; the encoder is
  `tui/sixel.py`, pure standard library. It knows `WT_SESSION`, so on this
  platform the protocol is read out of the environment and nothing has to be
  asked. kitty and iTerm2 are still recognised for the terminals that have them.
- **The conversion is `wic.py`, not `sips`.** macOS converts every photo to PNG
  with a shell command that does not exist here. It is not needed either:
  sixel is pixels, so the CDN's JPEG goes to the Windows Imaging Component
  through `ctypes` — the same shape as the CNG binding — and comes back as
  pixels. Verified against the live install: PNG exact to the pixel, WebP
  360x360, GIF 248x176, JPEG 3840x2400.
- **"네트워크 0회" was changed deliberately, in the same commit as the code.**
  A photo is a CDN address, not pixels, so showing one means fetching it, and
  the local `.cng` cache is encrypted with a key that was hunted twice and not
  found (see the note below). The README, the badge and the one-line
  description now say *reads are local* and name the photo fetch as the single
  exception: one address, on a two-host whitelist, only when `Ctrl+P` is
  pressed.
- **A photo's address is in the JSON now; the picture is not.** It was kept
  out of `Message.as_json` while a picture could only be drawn in the
  interface. A remote viewer wanted one too, and the macOS build had been
  answering with `photo: {url, width, height, count}` without the shared
  `openapi.json` saying so. The key is now here in that shape, present only on
  a message that carries one, and `Photo` is documented in the contract. It is
  an address: nothing on this side fetches for it. Live: 34 of 1,025 recent
  messages carried one, one of them an album, every address on
  `talk.kakaocdn.net`.
- **The interaction is a key, not a click.** macOS opens a photo by clicking
  its row, which means asking the terminal for mouse reporting — and a terminal
  doing that stops doing its own text selection and link handling. The composer
  holds the keyboard the whole time a room is open, so a plain letter is not
  free either: `Ctrl+P` (and `/photo`) opens the newest picture and closes it
  again.
- **What the sixel path costs, and why.** Encoding one 240x240 picture measured
  **19.7 s** before it was fixed, of which quantisation was 19.0 s: median-cut
  searched the whole palette for every colour when it already knew which box
  each fell into, and it ran over all 57,505 distinct colours. Membership
  replaces the search, and above 4,096 colours the channels are rounded to four
  bits first. **19.7 s → 1.12 s**, and each built escape is cached per box.
  Inline protocols keep no image server-side, so a moved picture is drawn again
  and the frame is given up whole (`Console.forget`) — the rows it vacated are
  blank, and a blank row that is still blank is exactly the row the row-diff
  painter skips.

Not taken, and why:

  **Two of those three reasons have since been measured rather than assumed.**

  *Display is no longer the blocker.* Sixel is a real path on this platform —
  `Yeachan-Heo/gajae-code` draws images in Windows Terminal that way, choosing
  between kitty, iTerm and sixel at runtime — but it pays for it with a Rust
  `encodeSixel` and `Bun.Image` for decoding. Pure Python can encode sixel;
  what it cannot do is decode the WebP and JPEG this client stores, so the
  dependency-free version of that half is `windowscodecs.dll` through
  `ctypes`, the same shape as the CNG binding.

  *The pixels really are on this machine, and are really encrypted.*
  `chat_data\*.cng` is 12,068 files and `chat_data\cli\cli_<40 hex>` a further
  4,893. Entropy is 7.1–7.3 bits per byte at head, middle and tail, with no
  image magic in the first 4 KB — but the **first 16 bytes repeat**: 454 of
  800 sampled share one first block and the sample holds six distinct ones.
  Every file's length is a multiple of 16, in `OCH` and `chat_data\url_image_v2`
  as well, so it is a 16-byte block cipher.

  **The chaining is CBC, and that is measured rather than inferred.** Across
  111 files and 661,698 blocks: 20,906 same-position collision pairs, **all of
  them inside a shared prefix**; after the first block where two files differ
  they **never** collide again; **zero** repeated blocks within any file and
  **zero** collisions between different positions. ECB would do the opposite
  on all three counts. Chance would produce 6.4 × 10⁻²⁸ pairs. The shared
  prefixes are long — 399 file pairs agree for **37 blocks** and the longest
  runs **344 blocks (5,504 bytes)** — so these files begin with a great deal
  of identical plaintext.

  **Two things that are still open, and that the statistics cannot close.**
  How many keys there are: "one key with several distinct first plaintext
  blocks" and "one key or IV per header group" predict the same collision
  topology. And what the plaintext prefix *is*: the 137-file group maps to
  original URLs that are a mix of JPEG, PNG and WEBP, so the first block is
  **not** an image header — something of Kakao's own comes first. That matters
  for any key hunt: a candidate key cannot be judged by looking for image
  magic at offset 0, which is exactly how the search below scored itself.

  Four key sources have been
  tried and all failed: `jiru/kakaodecrypt`'s Android schedule across all 32
  `encType` values with this account's id; the 30 distinct SQLCipher keys this
  tool already extracts; the fixed-key 128-byte XOR below; and — the serious
  one — **every AES key schedule in the client's entire readable memory**
  (709,132,288 bytes, 56 schedules, 25 distinct keys, against 288 blobs, CBC
  and ECB, offsets 0/16/32, five IV strategies including one back-derived from
  an assumed PNG/JFIF header): **zero valid images**. That does not prove the
  key is absent — an unexpanded key, a non-AES cipher or an unguessed
  container would all survive it — but it does mean the cheap version of "read
  it locally" is dead, and a viewer that wants photos today has to fetch them.

- **Stickers and emoticons are the opposite case, and are worth taking.**
  `DigitalItem\e` holds them already: of 300 sampled, 248 `.png` are stored
  **in the clear**, and the 51 `.webp` and one `.gif` are obfuscated only by
  **XOR of the first 128 bytes** with an LFSR keystream from a fixed key —
  every one of them decoded to a valid image in the check. Ninety lines of
  pure standard library, no network, no CDN, no promise to renegotiate. The
  owner's `k-emo` project implements exactly this and is the reference; the
  algorithm, not the code, is what crosses over.
- **Opening a room through the client's search box** (`kamado-cli#29`). There
  the fix works because accessibility hands over the *name* of every row: type
  the room into the search field, read 648 row names, click the one that
  matches. Both halves of that are missing here. The room list is owner-drawn,
  so a row is neither a child window nor a UIA element and carries no name to
  match — and the list reports no scroll state either (`GetScrollInfo` on both
  `EVA_VH_ListControl_Dblclk` and its `_EVA_CustomScrollCtrl` answered
  `min=0 max=0 page=0 pos=0`), so even *whether* a search narrowed the list is
  unobservable. The main window does hold `Edit` controls that are plausibly
  the search fields — two of them, visible and enabled but sized 0×0 until
  search opens — so the typing half is probably reachable. It is the reading
  half that is not: the only way to learn which room a filtered row holds is
  to open it, which is the exact cost (a stranger's room opened and marked
  read) the change exists to avoid. What is already done here instead is the
  remembered row in `rows.json` plus closing a window whose title does not
  match, which brought a measured four wrong rooms to none.
- **Running through `uv` for speed**, which that README recommends. The
  speedup it reports is not uv's: macOS ships Python 3.9, so `_connect` there
  falls back to a temporary file, and `uv run --python 3.12` is simply the
  easiest way to get a 3.11+ interpreter onto a Mac. Windows ships no Python
  at all, so whatever is installed is what runs, and the fast path is already
  taken on anything past 3.10. Measured here, the launcher is a cost rather
  than a saving: `uv run --no-project python -c pass` 63.9 ms against 29.6 ms,
  and `status` 313.1 ms against 253.3 ms. With no dependencies to resolve,
  uv has nothing to win back — the README says "use 3.11+" instead, and names
  uv only as one way to get it.
- **Expiring the delivery tick after 1.6 s** (`kamado-cli#27`). There the tick
  is a whole row above the composer, and that row is shared: `_status_line`
  gives a delivery priority over the notice and the unseen count, so a tick
  that never expires hides them. Here the mark is a single cell on the
  composer's right border, where it displaces nothing and hides nothing —
  `delivered` is read in exactly one place, the box. A ✓ still standing an
  hour later is not stale either; it says the last thing sent did arrive,
  which stays true until the next send, and typing already clears it. Adding
  a clock would retire the one fact worth keeping for no room reclaimed.
- **Waiting longer for a split escape sequence** (`kamado-cli#28`). A macOS
  terminal delivers Escape and the arrow keys as bytes, so a lone `\x1b` is
  only telling apart from `\x1b[D` by waiting — and 30 ms was too short when
  the two halves arrived in separate reads, which quit the interface instead
  of moving the caret. This console reads `ReadConsoleInputW` records, so an
  arrow arrives as `VK_LEFT` and Escape as `VK_ESCAPE`: two different records,
  nothing to disambiguate and no delay to tune.
- **Moving the TUI's reads onto a worker thread** (`kamado-cli#39`). Half of
  that change crossed over and half of it cannot. The half taken is the cache
  budget above, which is a bug on any platform. The half left is
  `DatabaseWorker`, and its motivation is mostly **fork safety**: that build
  decrypts pages in forked children, and a fork from a threaded process hands
  the child locks with no thread left to release them. This build has no such
  fork — see the entry below on why parallel decryption was never taken — so
  the hazard does not exist here. What remains is jitter, and the numbers do
  not carry it: a poll that opens a room costs 171–355 ms here against
  1.3–1.9 s there, and `_poll_due` already defers it while somebody is typing.
  Against that, the port is a 500-line rewrite of `tui/app.py` plus a new
  417-line module.
- **Parallel page decryption across cores** (`kamado-cli#21`). The expensive
  case it speeds up barely happens here: the WAL overlay already means an
  arriving message costs 100.2 ms rather than 431.5 ms on a 48.3 MB room, and
  a full decryption is the rare path. Against that, Windows starts
  subprocesses by spawning, and a `PreparedKey` wraps a CNG handle that cannot
  cross a process boundary — so this is a redesign of the key handling, not a
  port, and nothing measured here says it would pay for itself.
- **Reading a room's name out of the meta table** (`kamado-cli#24`). On macOS a
  rename leaves `NTChatRoom.chatName` null and writes the name to `NTChatMeta`
  as `type = 3`, so the reader had to go and get it. The Windows client keeps
  the same meta table — `chatMetas`, same `type = 3`, 7 named rooms of 59 rows
  — but it also writes the name straight into `chatRoomList.chatRoomTitle`,
  and that column is filled for **all 272** rooms. Checked room by room: the
  title this reader composes differs from the stored one in exactly one room,
  the self-chat, which is the deliberate disagreement `stored_title` already
  documents. The meta name disagrees with the catalog in one room, where the
  meta row is from 2018 and its revision is far below the room's
  `lastSyncMetaRevision` — so on this side the meta table is the **stale**
  copy, and preferring it would introduce the bug the macOS change removes.
- **The resident daemon on a Unix domain socket.** There is no such socket to
  bind; it would be a named pipe and a different program, and the command
  costs that motivated it were already paid down by the context cache and the
  `os.walk` change.
- **The louder dark speaker tints** (`kamado-cli#19`, and the dark half of
  `#20`). That palette raises name saturation to 0.75 and drops lightness to
  0.72. This repo's dark tints are deliberately near-invisible — one base grey,
  a slight tint — and `test_tui_layout.py` pins the subtlety and the
  separability against each other on purpose. Taking the louder version would
  fail those tests by design rather than by accident, so only the light half
  crossed over. The dark tints are bit-identical to what they were: checked
  across 63 speakers, names and bodies, before and after.
- **The macOS window automation.** `winauto.py` is this side's equivalent and
  solves the same problems differently; there is nothing to share.

## Deliberately absent

Login automation, protocol reimplementation, history harvesting, attachment
sending, and macOS support.
The supported write operation is text sent through an existing client window.

**Opening a room so it can be read was absent, and is not any more.** A key
is in memory only for a room the client has opened, and on the account this
was decided on that was 6 of 271 rooms, so the interface listed six. The
owner chose to trade the cost — the client marks a room read when it opens —
for being able to read the rest: the list now shows every room, marks the
ones without a key `잠김`, and enter on one has the client open it (the same
remembered row, hinted row, then search as a send), waits for the key with
`Reader.unlock`, and shows it. `messages --open` does the same for a caller.
It only ever happens for a room somebody chose; nothing opens rooms in bulk,
and the window is left minimised rather than closed, because the key belongs
to the open log.

**Calling an agent from inside the interface.** `/ask` shipped in 0.9.0 and
was removed in 0.10.0, on both sides. The direction was inverted: the agent
drives this CLI — `context`, `digest`, `follow` are the interface built for it
— rather than this tool driving an agent. Which agent, what it is shown and how
it is asked are the agent's decisions, and a `/ask` inside the screen made the
tool make all three. It also broke "no network" by proxy: the core opened no
socket, but the `omo` it spawned did. This tool spawns no process.
