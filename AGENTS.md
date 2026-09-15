# AGENTS.md — working on kakaoread

Windows-only KakaoTalk reader, friend browser and sender in zero-dependency
Python 3.10+. Reads stay local; the explicit send command drives the client.
For *using* the tool as an agent, read `skill/SKILL.md`; this file is about
changing it.

## Commands

```powershell
python -m unittest discover -t . -s tests    # the whole suite
python -m kakaoread status                   # does anything work right now
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
| `reader.py` | Queries and name resolution |
| `context.py` | The single call an agent makes |
| `watch.py` | Stat-gated follow loop |
| `cli.py` | Argument parsing and the JSON envelope |
| `winauto.py` | Exact window targeting, draft protection, background text sending |
| `tui/theme.py` | OmO dark-theme tokens, 256-colour fallback, speaker tints |
| `tui/layout.py` | Column-accurate width, truncation, wrapping, footer plan |
| `tui/console.py` | Console mode, UTF-8 code page, alt screen, key input |
| `tui/digest.py` | Time slicing and extractive slice description |
| `tui/app.py` | Screen state, rendering, key handling |

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
  found are cached per pid in `%LOCALAPPDATA%\kakaoread\contexts.json` and
  re-read in 0.8 ms. **Only addresses are stored — never a key or a salt**, and
  every cached address is decoded and followed exactly like a fresh candidate,
  so a moved heap yields fewer contexts and falls back to a full scan.
- **The profile is walked, not globbed.** `rglob("*")` stats every entry in a
  profile that also holds media and caches: 1.36 s for 298 databases against
  92.8 ms with `os.walk`. `test_paths.py` pins the resulting order, which folds
  case because Windows path comparison does.
- Together: `status` 2.43 s → 0.30 s, `rooms` 2.54 s → 0.63 s, `friends` 2.06 s
  → 0.38 s, with byte-identical output.

### Consistency

- `build_image` compares the source **byte for byte** after reading. A stat
  check is not enough: a same-length rewrite inside one file-time tick leaves
  `st_mtime_ns` identical. `test_detects_a_same_size_rewrite_even_when_stat_reports_no_change`
  pins this.
- Caching and following *do* use `os.stat`, where a stale answer costs one
  extra poll rather than a wrong result.
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
- `talkUser` holds **contacts only** — never the signed-in account. Without
  `Profile`, every message the user sent shows a bare numeric sender.
- **The signed-in account is the sole member of the `MemoChat` room.**
  `Profile` has several rows and its `hasMeBadge` flag marks a *different*
  account; neither identifies the user.
- Local history is substantial: one observed room held 445,714 messages. Do not
  assume only a handful of messages are cached.

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
- Speaker tints share one base grey and differ only slightly. Hue alone stops
  separating past roughly twenty speakers, so a three-step lightness cycle is
  applied as well; `test_tui_layout.py` pins both the subtlety and the
  separability, and they pull against each other.
- `/sum` is extractive on purpose. There is no model in this process, so a
  slice reports counts, participants, keywords and representative lines, and
  never claims to summarise.
- The client writes placeholder words such as `사진` into the text column for
  attachments; they are filtered out of keywords or they dominate every slice.
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
- **A send must not re-read the catalog.** `reader.rooms()` measured 3.0 s
  when the snapshot had gone stale, and sending a message is exactly what
  makes it stale, so the duplicate-title census comes from the room list the
  screen already loaded rather than from a fresh read.
- **Polling yields to typing.** A poll that finds something re-decrypts the
  room's whole log — 0.62 s and 0.94 s measured on two live rooms — and this
  loop is single-threaded, so `_poll_due` defers while keys are still arriving
  and `_next_poll_at` waits behind an expensive room instead of in front of it.
- **The screen is narrower than the buffer, twice over.** `shutil` reports the
  console buffer, which is wider than the window showing it, so the visible
  region (`srWindow`) is used instead. On top of that Windows Terminal paints
  its scrollbar over the last cells rather than shrinking what it reports, so
  `right_margin()` leaves one column unused; `KAKAOREAD_TUI_MARGIN` tunes it,
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
- **The message line is open on the left and marked on the right.** The send
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

## Friends and sending

- Friends are `talkUser.type = 1`; other known users are not friends.
  Phone numbers are withheld unless `--with-phone` is explicit. Friend ids
  and direct-chat ids are serialized as strings. TUI `f` opens the friend list.
- Send requires a uniquely resolved catalog room and an exact matching open
  window. Duplicate catalog titles remain ambiguous even with `--room-id`.
- The Korean client's empty `RICHEDIT50W` returns `메시지 입력`, not `""`.
  This literal is reserved as the empty hint. Other drafts must be preserved.
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
- **Scanning rows opens real rooms, and opening a room marks it read.** That
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
- Stubs must fail the way production fails. `StubCache` raises `KakaoReadError`
  because the real cache does — a stub raising `FileNotFoundError` once made a
  test pass against an error type production never produces.
- When patching a module attribute, bind the original **before** patching or
  the wrapper will call itself.

## Deliberately absent

Login automation, protocol reimplementation, history harvesting, attachment
sending, automatic chat-window opening, and macOS support.
The supported write operation is text sent through an existing client window.
