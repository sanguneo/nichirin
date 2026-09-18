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
| `digest.py` | The digest the command and the server both ask for |
| `serve/config.py` | Constants, token and interval validation, error-to-status map |
| `serve/handler.py` | Auth, routing, and every route that answers and closes |
| `serve/stream.py` | The SSE connection's own lifetime, and the watcher it owns |
| `serve/backends.py` | Reader, Sender and Watcher wiring for a live host |
| `serve/server.py` | Binding the socket and serving until interrupted |
| `tui/theme.py` | OmO dark-theme tokens, 256-colour fallback, speaker tints |
| `tui/layout.py` | Column-accurate width, truncation, wrapping, footer plan |
| `tui/console.py` | Console mode, UTF-8 code page, alt screen, key input |
| `tui/digest.py` | Time slicing and extractive slice description |
| `tui/app.py` | Screen state, rendering, key handling |
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
  question the data can still answer.
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
  `deleted`.
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
  reconnection, writing far more than the 500-row page in one go.
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
- **A reader hanging up is SSE's success case.** `socketserver` prints a whole
  traceback for the `ConnectionResetError` that a closed `curl -N` raises on
  the next write, so `QuietDisconnects` swallows exactly the four ordinary
  disconnect errors and escalates everything else. The suite went from printing
  tracebacks on every stream test to none.
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
- **Several conversations can be open, and only one has the keyboard.**
  `App.chats` is the list and `App.chat` is a property over it, which is why
  everything acting on "the" conversation still says `self.chat` and never had
  to learn about panes. A pane needs `MIN_PANE_COLUMNS` (44) to read as a
  conversation at all — Hangul is two columns a character — so the window
  decides how many are drawn: two from 89 columns, three from 134. A room that
  stops fitting is **hidden, not closed**, because the reader opened it and a
  window getting smaller is not a decision to let one go; the header row then
  appears even over a single pane, or nothing on screen would say there is
  another room to `tab` to. `esc` closes the focused pane, not the screen.
- **Each pane keeps its own watcher, unlike the macOS build.** There one
  fingerprint covers the whole account, so sharing a watcher is what stops
  panes multiplying the most expensive thing that build does. Here a room *is*
  its own database and a poll is one stat of its log, so panes cost exactly
  what they read — and an off-screen pane is still followed, or its unseen
  count would start from whenever the window happened to get wider.
- **A message belongs to the room it arrived in.** `_confirm_delivery` takes
  the conversation as an argument rather than reading the focused one, or a
  tick would land in whichever pane the keyboard was in when the client wrote
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
  per (width, message count). Rebuilding it on every keystroke measured 71 ms
  per key at the default 400-message window and 142 ms at 800, which is what
  made input stutter; cached, a keystroke costs 0.17 ms. Anything that changes
  a loaded message must change that key or the screen will lie.
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
  Native message calls have a timeout; a hung client must not hang the CLI.
- `submitted` means queue acceptance, never confirmed network delivery.
  Read back a new matching from-me record before claiming delivery. Never
  automatically retry an uncertain send.
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
  the suite is 767 tests with a skip count of zero, and that is the check that
  a guard has not quietly turned something off.

## What was taken from the macOS sister project, and what was not

`kamado-cli` is the macOS port and the two repositories drift. Work crosses
over by hand, so the reasons for *not* crossing something over are recorded
here — otherwise the same question gets re-answered every time the gap is
noticed again.

Taken:

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

Not taken, and why:

- **Showing photos in the conversation** (`kamado-cli#32`, still open there).
  Neither half of it exists on this side. The transport is the **kitty
  graphics protocol**, which Windows Terminal does not implement — what it
  gained in 1.22 is sixel, a different protocol — so the escape layer would be
  written from scratch rather than ported. The conversion is `sips`, which is
  a macOS binary, so WebP and JPEG would need another answer too. And the
  feature is the first thing in either repo that makes the *tool* talk to the
  network: a photo is a CDN address in the database, not pixels, so showing
  one means fetching it. This repo's description is "네트워크 0회" and its
  README says the client does the network talking, so that is a promise to
  change deliberately rather than a detail to inherit.
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
sending, automatic chat-window opening, and macOS support.
The supported write operation is text sent through an existing client window.

**Calling an agent from inside the interface.** `/ask` shipped in 0.9.0 and
was removed in 0.10.0, on both sides. The direction was inverted: the agent
drives this CLI — `context`, `digest`, `follow` are the interface built for it
— rather than this tool driving an agent. Which agent, what it is shown and how
it is asked are the agent's decisions, and a `/ask` inside the screen made the
tool make all three. It also broke "no network" by proxy: the core opened no
socket, but the `omo` it spawned did. This tool spawns no process.
