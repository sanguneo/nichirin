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
  92.8 ms with `os.walk`. `test_paths.py` pins the resulting order, which folds
  case because Windows path comparison does.
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

## Terminal interface

- The palette is OmO's `dark` theme, duplicated rather than imported, because
  this project must not read another program's install.
- **Never measure text with `len()`.** Hangul is double-width and every line
  carries escape sequences; `layout.display_width` is the only correct ruler,
  and a mistake here silently shifts every column rather than raising.
- The console must be switched to code page 65001 before drawing or Hangul is
  replaced, and virtual terminal processing must be enabled or escapes print
  literally. Both are restored on exit along with the alternate screen.
- Frames are rebuilt whole and written once. Partial redraws flicker and strand
  stale cells.
- **The frame is cut and filled to the screen by `Console.draw`, not by its
  callers.** One line wider than the console wraps, and every row after it lands
  a row late and at the wrong column: driving a real console left 23 rows
  holding the previous frame where two were expected, which is exactly the chat
  remnants that showed under the room list. Narrow lines are padded, so no cell
  survives a frame it does not belong to.
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
- **Polling yields to typing.** A poll that finds something used to re-decrypt
  the room's whole log — 0.62 s and 0.94 s measured on two live rooms — and
  this loop is single-threaded, so `_poll_due` defers while keys are still
  arriving and `_next_poll_at` waits behind an expensive room instead of in
  front of it. The log-only overlay is what let `REFRESH_SECONDS` drop from
  0.35 to 0.15; the deferral stays, because an arrival is still the most
  expensive thing that can happen between two keystrokes.
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
- When patching a module attribute, bind the original **before** patching or
  the wrapper will call itself.

## Deliberately absent

Login automation, protocol reimplementation, history harvesting, attachment
sending, automatic chat-window opening, and macOS support.
The supported write operation is text sent through an existing client window.
