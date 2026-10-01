# AGENTS.md — working on kamado-cli

macOS-only KakaoTalk reader, friend browser and sender in zero-dependency
Python 3.9+. Reads stay local; the explicit send command drives the client.
This file is about *changing* the tool; `skill/SKILL.md` is about using it.

Forked from the Windows-only `kanao-cli`. Every macOS fact below was
established against a live client rather than inherited; the Windows platform
layer (CNG, the heap scan, `winauto`) is gone, and reading and sending are
both verified working on this machine.

## Commands

```bash
python3 -m unittest discover -t . -s tests    # the whole suite
python3 -m kamado status                      # does anything work right now
python3 install.py --target <dir> --force     # install the skill
python3 demo/capture.py --all                 # re-render the demo screenshots
```

Discovery needs `-t .` so `tests` stays an importable package.

## Review and contribution discipline

See `CONTRIBUTING.md` for the contribution workflow and `improvement.md` for
the dated review and proposed fixes. Proposed invariants are requirements for
future changes, not evidence that this checkout already satisfies them.

- For review-only work, return source-grounded findings and plans rather than
  changing product code. Respect the user's verification budget. Distinguish
  executed evidence, source counterexamples, and Mac-only hypotheses; do not
  present Windows checks as APFS, CommonCrypto or Accessibility verification.
- Acquiring two files separately does not make their pair atomic. Bind the
  database, committed WAL generation, resulting image, and cache version at
  the acquisition boundary. A later live stat cannot certify an older clone.
- Failed reads and identity lookups must not finalize successful state.
  Metadata caches need a stated lifetime relative to the account and snapshot.
  A borrowed cache needs an explicit owner.
- Preserve room-local log-ID order and monotone, output-covered stream cursors.
  Define framing and byte bounds for stream sockets; one recv is not a message.
  A page limit does not bound accumulated rows or process memory.
- Keep the existing fork guard and single database-owner thread. Worker launch,
  failure, timeout and cleanup belong to the same change. A thread wrapper
  alone does not make a fork-based decryptor safe in a threaded server.
- Treat external message/name/title/URL text as data before adding terminal
  controls. Same-ID edits and deletions belong in transcript invalidation.
- A sibling port includes assumptions, callers, contract and regressions.
  Record both revisions and inspect the local checkout. Do not assume that an
  upstream PR, shared specification, or Windows feature is already installed.
  Keep the single account database, derived keys and AX boundaries explicit.
- Keep artifacts synthetic and secret-free, preserve unrelated user changes,
  and commit or push only when requested.

## Pull requests

- **The evidence is the review.** A pull request carries what it ran and what
  it measured — the suite's last line, the new test failing against the code
  before the fix, the before-and-after numbers with the data they were taken
  on, and what was and was not done against a live client. A reviewer should
  be able to check the claim without rebuilding the experiment, and a claim
  with nothing attached is not ready to open.
  `.github/pull_request_template.md` is that list.
- **Merge oldest first.** Pull requests land in the order they were opened,
  so the later one rebases onto the earlier. Landing them out of order makes
  the conflict somebody else's, and the branch that was reviewed is no longer
  the branch that merged.

## The two platform differences that drive the whole port

1. **The key is derived, not stolen.** Windows kept the SQLCipher key only in
   the running client's heap. macOS derives it from the machine's
   `IOPlatformUUID` and the signed-in `userId`, so reading works with
   KakaoTalk closed and there is no process scanning at all.
2. **There is one database, not one per room.** Windows had
   `chatLogs_<roomId>.edb` per conversation. macOS keeps every room, message,
   user and open link in a single SQLCipher file inside the app container.

## Permissions

Both are granted to the *terminal application*, not to this tool, and both
fail by hanging rather than by erroring.

- **Full Disk Access** — required to enumerate
  `~/Library/Containers/com.kakao.KakaoTalkMac`. Without it `os.listdir` on
  the container blocks indefinitely instead of raising `PermissionError`,
  which is why every container access is made under a timeout.
- **Accessibility** — required to drive the client for sending. It is granted
  to the *responsible process*, which is the terminal (or the app that
  launched the interpreter), not the `python3` binary; granting the wrong app
  leaves `AXIsProcessTrusted()` false with no other symptom.

## Facts established against a live client

These were proven by measurement or by reading the running client. Changing
code that depends on them without re-checking will quietly break.

### Reading

- The database is **one large SQLCipher file** (measured 119 MB) that the
  running client rewrites continuously. Reading it in place fails the
  byte-for-byte consistency check over and over — once measured 112 s to catch
  a clean moment. `snapshot.clone_snapshot` takes an **APFS `cp -c` clone**
  first (copy-on-write, O(1), frozen against the writer), which dropped a
  `status` from 112 s to 3.8 s. A non-APFS filesystem falls back to a plain
  copy.
- The tested client is **SQLCipher 3** (1024-byte pages, 48-byte reserve), not
  4; `cipher.detect` proves the profile against the file rather than assuming.
- The account `userId` is not stored in the clear. It is recovered as the
  **SHA-512 preimage** of a preference-key suffix
  (`DESIGNATEDFRIENDSREVISION:<sha512hex>` etc.), searched across cores and
  then cached in `account.json` — only the id, never a key.
- `NTUser` has no `type` column; a friend is `friendType = 1`. The message
  attachment column is spelled `attachment` here (the Windows client
  misspelled it `attachement`).
- **A reply is recorded two ways, and the newer one carries no copy of what it
  answered.** The old shape put the reply's own snapshot in the attachment
  (`src_logId`, `src_userId`, `src_message`), which is why a quote outlived the
  original being deleted. This client writes an ordinary `type = 1` row and
  points a **`threadId`** column at the message it answers — measured 248 of
  4,000 rows, while the newest `src_logId` anywhere was two days old and the
  busiest room's last 50 messages held three `threadId` replies and zero
  `src_logId`. Reading only the attachment therefore lost every recent reply,
  and the interface stopped drawing quote lines without anything failing.
  `Reader._threaded_quote` reads the answered row back instead, so a threaded
  quote says what that message says *now* rather than what it said when it was
  answered; a thread naming a row this database does not hold quotes nothing
  rather than an empty line, and a row naming itself is not a reply to itself.
  Where both shapes are present the attachment's own copy still wins, because
  it is what the sender was actually looking at.
- **Which end the limit cuts from depends on the question.** A window wants the
  newest rows, so `messages` orders descending. Catch-up does not: asked for
  what arrived after a cursor, taking the newest page skips everything between
  the cursor and that page, and a follower that then advances its cursor never
  sees the difference again. `messages` therefore orders *ascending* when
  `after_id` is given without `before_id`, and `Watcher._catch_up` pages until
  a short page arrives. The client backfills from the server after a
  reconnection, writing far more than the 500-row page in one go.
- **A failed read must not consume the change that prompted it.** One
  fingerprint covers the whole account here, so a pass that records it before
  reading lets a failed room stand in for a successful one: the next poll finds
  the database unchanged and returns immediately. `Watcher.poll` stores the
  fingerprint only when every watched room was read.
- **What licenses reusing the cached base pages is the log's generation, not a
  stat.** `BasePages.generation` is the 32-byte log header those pages were
  decrypted against, and its salt pair names the current run of the log. In WAL
  mode only a checkpoint writes the `.edb`, and a checkpoint copies frames the
  log still holds — so while the generation stands, every page written
  underneath us is one the overlay applies anyway. A checkpoint that *restarts*
  the log is the case that would strand pages, and restarting is exactly what
  changes that salt; it is `LogRestarted` rather than a plain
  `ConsistencyError` because backing off cannot undo it.
- **The overlay is given up on, never defended.** A new generation, a log that
  moved mid-read, a page the log grew the database to but never wrote, or an
  image SQLite will not read `sqlite_master` from all fall back to a full
  decryption — and the connection opened for a rejected image is closed on the
  way out, since neither the cache nor the caller ever learned about it.
- **Base pages are charged to the cache budget, and shed before a snapshot
  is.** Keeping them uncounted made an entry cost twice what the cache believed.
  Counting them alone halves how many snapshots fit and closes connections
  callers still hold, so `_make_room` drops base pages first, least recently
  used first, and evicts a snapshot only when giving up every set was still not
  enough.
- `build_image` and `update_image_with_wal` assemble their result through the
  same `_overlay_log`, so a commit that shrank the database truncates the image
  in both. They drifted apart once already, and only the incremental path was
  fixed.
- **Two clones are not one snapshot.** `clone_snapshot` clones the database and
  then its log, and a checkpoint that restarts the log between the two pairs a
  database from before it with a log from after it — a mix that passes every
  structural check. The live log header is read before cloning and the cloned
  log must still carry it; a restart always changes that salt. A plain copy
  (off APFS) is not frozen at any instant, so it is compared with the live file
  afterwards. Either failure is a `ConsistencyError` and is retried.
- **An image is filed under the fingerprint taken before it was read.**
  Sampling again afterwards put a commit that landed during the read beside an
  image that does not hold it, and the next call returned the older image for
  as long as the account stayed quiet. Both the full read and the overlay store
  the first sample. Equal stats still say nothing about equal bytes.
- **The forked decryption has a deadline and an undertaker.** `join` without a
  limit made a stuck child a command that never returned. The wait is bounded
  (`_PARALLEL_DEADLINE_SECONDS`), and every child that was started is
  terminated and collected before the shared segment is released and before
  the serial fallback begins.
- **The name map and the column sets follow the snapshot.** The cache hands
  back the same connection until the database changes, so `Reader.connection`
  forgets both when it sees a different one: a known user who renamed is shown
  under the new name, once per snapshot rather than once per message.
  `own_user_id` is settled only after a lookup answered; one `BUSY` used to
  stand for "no account" for the reader's whole life.
- **A room's messages leave a poll in log-id order, whatever their clocks
  say.** Sorting by `(sentAt, id)` put a room's 11 before its 10 whenever 11
  carried the earlier time, and `/stream` published 10 as the cursor. Rooms are
  merged by time comparing only each room's next message; the stream's cursor
  is compared as a number and only advances. An empty room is primed at 0, or
  its first message took the "never primed" path and was never reported, and a
  catch-up pages only up to the newest id it started with.
- **The daemon socket is framed.** A request and an answer are each one line.
  One `recv` is not one request: the server reads to the newline under
  `REQUEST_MAX_BYTES` and a deadline, and the client reads to the newline under
  `RESPONSE_MAX_BYTES`. An answer that is cut short, malformed or too large is
  `None` — the caller runs the command itself, as with no daemon — never a
  partial success.
- **Reading a preferences plist is a container access too.** Only listing the
  directory had a deadline. `read_preferences` lets `PermissionError` rise so
  the guard names the permission, and while one abandoned worker is still stuck
  `guarded` refuses at once rather than piling another beside it.

- **A message deleted for everyone keeps its text.** `deleted` stays 0 and
  0x4000 is added to the type (16385 for text, 16386 for a photo), and the
  client writes a `feedType` 14 row — `{"logId": …, "hidden": true}`, with
  `byHost` when a host did it — pointing at it. A line typed and deleted went
  on showing as that line, beside a bare `[feed 14]`. Over the newest 20,000
  rows all 255 messages a type-14 feed points at carried the bit. The
  deleted predicate from `_message_query` now excludes both, so a window, a
  search, a count and a follower's cursor all agree; the feed test goes
  through `CASE … json_valid` because `json_extract` on a body that is not
  JSON fails the statement rather than the row. The Windows build leaves the
  text encrypted instead and marks it with a column this schema lacks — the
  bit is what the two share.
- **The interface shows where a deleted message was; the commands do not.**
  `Reader.messages(include_erased=True)` keeps a deleted row with its text
  dropped in the reader — this client keeps the very words that were taken
  back — and `Message.erased` set. Only `tui/worker.py`'s reads and its
  watcher ask for that, so the screen says "삭제된 메시지입니다" where the
  client does; every command, `serve` and the shared `openapi.json` still
  leave the row out, and `as_json` never mentions the field.
- **A message on screen can change without a new one arriving.** Deleted for
  everyone it keeps its id, and the watcher asks only for ids above the newest,
  so the words stayed on screen, in the digest and in the export. The TUI's
  POLL job carries each tab's ids and known versions; for a room
  `Watcher.moved` names, the owner thread compares `Reader.row_versions` (one
  range read) and re-reads only changed or unknown rows with
  `Reader.messages_at`, which returns each message with the version of the row
  it was built from. `ChatState.revision` joins the transcript cache key, a
  digest is cut again, and a reader scrolled up keeps the last visible line
  pinned. A digest never counts an erased message.
- **Somebody else's text is cleaned before it is styled.** Measuring past an
  escape is not disarming it: `truncate_to_width` carries escapes through on
  purpose. `layout.clean` replaces C0 with its control picture, DEL with its
  own and C1 with U+FFFD, one character for one, at every site that draws
  messages, names, titles, quotes, digests, notices or the status line. A URL
  ends at a control or its picture. JSON is untouched; this is display policy.
- **Feed types 4 and 2 are said in words** (`model.FEED_KINDS`): a join and a
  leave, checked on the Windows build against the client's own screen; here
  309 and 183 of the newest 20,000 feeds, every one naming a member.
  They are shown as the client's own sentence, "이름님이 입장하셨습니다." —
  three names at most, then "외 N명" — and not as a bracketed tag.
- **A newcomer is looked for again.** `names()` is read once per reader and
  the interface keeps its reader, so somebody who joined after a room was
  opened stayed `User <id>`. `Reader._sender_name` re-reads the map at most
  every `NAME_RETRY_SECONDS`, then takes the room's join-feed nickname.

### What else a room records: 공감, edits, notices, files, SafeBot

Measured on this client on 2026-09-29 (#44), schema and counts only, against
what kanao-cli 0.22.0 found on Windows. Most of it is the same server data in
a different table; where it is not, this build says less rather than guessing.

- **공감 are `NTChatLogMeta` type 2, keyed `(logId, chatId, type)`.** 4,582
  rows, content `{"rx": [{"k", "c", "o", "a": {"ko"}}]}` exactly as on Windows,
  plus an `extra` of `{"myRx": [...]}` this build does not read. The key has the
  room in it — Windows keys on `logId` alone — so the correlated subquery inside
  `_message_query` matches `chatId` too, and a 공감 added later changes the
  row's version like any other change. Only 600 of the 4,582 point at a row this
  database still holds.
- **An edit raises `NTChatMessage.revision`, and nothing keeps the old text.**
  45 of 63,766 rows at revision 1; there is no `chatLogHistory` counterpart, and
  neither `supplement`, `extra` nor `NTChatLogMeta` held an earlier version for
  any of them. `edited` is shown, and `revisions` answers an empty list — the
  contract's "never recorded" — after the same `MESSAGE_NOT_FOUND` check for a
  row that is gone. The `feedType 25` edit feed (146, all `hidden`, with
  `targetRevision`) is hidden beside the deletion feed 14.
- **The notice is `NTChatMeta` type 1, and there is no board table.** 8 rooms;
  `content` equals `noticeText` in every one, plain text, `authorId` and
  `updatedAt` in seconds. It is reported as `source: "legacy"`, the meta-row
  value in the shared enum. Type 15 also carries a long `noticeText` beside
  `{hostId, revision, warehouse}`; what it is has not been read off the screen,
  so it is not treated as a notice.
- **A file is `type = 18` with the Windows keys** (`url`, `name`, `size`/`s`,
  `expire` ms, `cs`, `k`), 93 on `talk.kakaocdn.net` and 22 on
  `dn.talk.kakao.com`. **The saved copy is `localFilePath`, not a folder.** The
  client records the absolute path the user saved to (Downloads 16, Desktop
  15, elsewhere 5, over types 2, 3 and 18), and 35 of those 36 files were gone.
  `files.local_copy` therefore takes the path only when it is absolute, not a
  symlink, a regular file and exactly the recorded size, and opens it with
  `O_NOFOLLOW`; otherwise the whitelisted address is fetched. Live: a
  22,483-byte file streamed whole as `remote`, and an expired one was 410
  without a request.
- **SafeBot is `{"safeBot": true}` in the attachment**, 37 rows (36 text, one
  since deleted for everyone), never in `supplement` or `extra`; `type &
  0x2000` was 0 rows. `Reader.reveal_covered` is off by default and withholds
  the text from the message, a quote of it, its links and search.
- **In the interface the notice rides on the OPEN job.** Asking for it as a job
  of its own put a second job per opened room into a queue that is bounded on
  purpose, and the worker-saturation tests caught it filling. `/safebot` is a
  `REVEAL` job: the switch is made on the owner thread, because the watcher
  reads through the same reader, and a failed re-read puts it back.

### Sending

Sending into an **already open** room window costs 0.07 s of accessibility
work and never touches the desktop: no activation, no focus, no pointer, no
keystrokes. Everything below is what it took to get there.

- **Set the text with `AXValue`, and do not activate anything.** The write
  needs no keyboard and no focus, and it also enables the client's send
  button. Synthesised typing was tried and abandoned: `CGEventPost` unicode
  drops characters unpredictably, and it only arrives at all once the app has
  been activated and the field clicked — which is the entire cost this avoids.
- **Send by pressing the 전송 `AXButton`, not by posting Return.** A Return
  over AXValue-placed text is ignored, because the client does not count the
  write as typing. `AXUIElementPerformAction` reports failure even on a press
  that works, so success is judged by the composer going empty.
- **An unreadable composer is not an empty composer.** A failed AX read
  returns `None`; after a press that means the outcome is unknown, not
  `submitted: true`. A disabled/unreadable button is never pressed, and a
  missing button must not fall back to a process-wide Return.
- **Minimization is a CFBoolean, not the text `"true"`.** The client accepted
  the CFString setter with status 0 but emitted no event and did not minimize.
  Real booleans produced `AXWindowMiniaturized`/`AXWindowDeminiaturized`.
  Check the resulting state, not just the asynchronous setter's return.
- **A missing AX list entry is not a missing window.** Hidden windows can
  appear in `AXChildrenInNavigationOrder`; a chat restored while the app is
  hidden was absent from both lists but its controls remained addressable.
  WindowServer bounds plus `AXUIElementCopyElementAtPosition` scoped to the
  application recovered it without an event, activation or unhide. Merge by
  `CFEqual`, never by title, then do the usual exact-title resolution.
  A window on another Space stayed absent after both that hit-test and a
  Window-menu `AXPress`, but the press published the exact target through
  `AXFocusedWindow` and `AXMainWindow`. Reading those direct references
  recovered it while the terminal remained frontmost. The fallback presses
  only an exact requested title and still runs the ordinary ambiguity checks;
  it never activates KakaoTalk or switches the current Space. See
  `docs/background-sending.md` for live evidence.
- **The button is a direct child of the chat window.** Finding it by crawling
  measured **3.5 s** — the transcript holds a button per message and every
  node is an IPC round trip — against **0.002 s** for reading the window's own
  children. The composer is found the same way: two levels of direct children,
  never a subtree walk.
- **Wait for the button to enable; do not press on faith.** The write enables
  it, but not instantly. While the crawl was slow it accidentally supplied that
  delay, and removing the crawl exposed the race as a message left sitting in
  the box. Both that wait and the emptying check poll, so a quick client pays
  one read rather than a fixed sleep.
- **The implemented *closed* room opener still touches the screen.** The
  chat-list rows ignore `AXPress`, `AXSelected` and a posted Return — selecting
  one only highlights it. The verified opener uses a real double-click, and macOS does
  not infer one from two clicks: the second must carry a click state of 2.
  Because that is a click at screen coordinates, the client is brought forward
  first so it cannot land in another window, and the pointer is put back.
- **A client with every window closed is a state to recover from, not to
  refuse.** Closing them all leaves KakaoTalk running and signed in, but absent
  from `AXWindows`, the navigation children and hit-testing alike, so
  `main_state` is `missing` and there is nothing to unminimize. Only a reopen
  request produces a window, and the client draws it *after* answering the
  request — measured 0.828 s and 0.721 s, both longer than `activate`'s own
  settle, which is why `show_main_window` waits for the window rather than
  reading once the way it used to. The first read of the room list on a
  freshly drawn window then blocks while the client fills it: **41 s for 264
  rows**.
- **A client left with no windows is one macOS may kill, so the drawn window
  is parked and never closed back.** The client does not quit itself. With
  nothing open, AppKit turns on automatic termination for it — the unified log
  shows `_NSEnableAutomaticTerminationAndLog ... No windows open yet` and then
  `_kLSApplicationWouldBeTerminatedByTALKey=1` — which tells the system it may
  be taken away silently. It was: `runningboardd` terminated KakaoTalk with
  `CacheDeleteAppContainerCaches requesting termination assertion`, exit reason
  `0xBADDD15C OS_REASON_RUNNINGBOARD`, and no crash report was written. The
  machine measured on sat at **99% disk (3.6 GB free)** with the cache daemon
  failing to reclaim 11 GB, which is what kept sending it after windowless
  apps — and why the same client also sat windowless for 4 m 32 s untouched
  when nothing was hunting. Checking `Info.plist` proves nothing here: neither
  `NSSupportsAutomaticTermination` nor `NSSupportsSuddenTermination` is
  declared, because AppKit enables it at runtime. So closing a window that was
  found closed hands the client to the system, and no amount of waiting makes
  it safe. Restoration minimizes the window it had to draw instead, which
  leaves the screen just as clear while keeping one window open.
- **A sent message is confirmed by reading it back**, never by the submit call
  alone; `submitted` means the composer emptied, delivery is proven by a new
  from-me row in the log.

- **The list reports rows it has not scrolled to, at their place in the
  whole list.** Row 46 of 264 sat at y = 3,571 while the list showed
  y = 167 to 712, so its double-click landed below the list, nothing opened,
  and after 75 s the send failed while the screen said "보내는 중". A row is
  now scrolled into view first by writing its position to the list's vertical
  scroll bar `AXValue` (a number, 0 to 1 — `ax.Element.set_attribute` takes
  one now), and a row that still is not inside the scroll area is **not
  clicked at all**.
- **Nothing below a table is walked.** `descendants` over the main window
  visited 1,962 elements, nearly all of them cells inside rows, and took
  15.8 s; `_room_table` stops at a table. Choosing a row reads names outward
  from the catalog's recency hint (`outward`) and stops at the first match,
  where reading all 264 names first cost about 18 s. Measured on two closed
  one-to-one rooms, rows 46 and 70: 75 s and a failure became 3.8 s and 4.5 s.

### The demo

- **`demo/mockup.py` keeps its own copy of the schema, and that copy rotted
  once.** It builds the macOS single-database shape (`NTChatRoom`,
  `NTChatMessage`, `NTUser`) in memory so it can run standalone. When the
  runtime moved off the Windows catalog-plus-a-file-per-room layout the
  generator silently stopped matching it — nothing failed, because nothing ran
  it, and the committed screenshots were simply older than the code.
  `tests/test_demo.py` now drives it through the real `App`, so drift is a test
  failure rather than a stale picture.
- **A room's kind comes from its shape, so the fixtures must carry the shape.**
  `_kind_of` reads a `linkId` as an open chat, a `directChatMemberUserId` as a
  direct chat, and members that are only the account as the memo chat. There is
  no shape that yields `PlusChat`, which is why the demo has no channel room.
- **Screenshots are rendered, not photographed.** `demo/capture.py` turns a
  scene into a page that is itself the picture, frame and all; the Windows
  original drove a terminal and photographed the window, which is how Windows
  chrome and Windows fonts ended up in the shots. Alignment is not left to the
  font — a terminal gives a Korean syllable two cells and a browser gives it
  whatever the font says, so every character is placed in a box sized by
  `layout.char_width`, the same function the interface lays itself out with.
- Shots are committed as **lossless WebP**; the same nine frames as PNG weigh
  four times as much.

### The remote server (`kamado serve`)

Ported from the Windows branch, where it grew to 760 lines in one module before
being split. It arrived here already split, so the layout below is the shape to
keep rather than one to rediscover.

| Module | Responsibility |
| --- | --- |
| `digest.py` | The digest the command and the server both ask for |
| `files.py` | A file message's bytes: the client's saved copy, or its address |
| `serve/config.py` | Constants, token and interval validation, error-to-status map |
| `serve/handler.py` | Auth, routing, and every route that answers and closes |
| `serve/stream.py` | The SSE connection's own lifetime, and the watcher it owns |
| `serve/backends.py` | Reader, Sender and Watcher wiring for a live host |
| `serve/server.py` | Binding the socket and serving until interrupted |

- **Only the commands the daemon answers are forwarded to it.** `cli.main`
  used to forward everything not on an exclusion list, and the daemon answered
  `digest`, `search`, `names` and every new command with `unknown daemon
  command`, which was printed as the result. `cli.DAEMON_COMMANDS` names the
  seven `DaemonServer._dispatch` handles; `serve`, `tui`, `follow` and `send`
  stay local for the reasons they always did — a server holding a socket open
  would print one answer and exit. The daemon's reader outlives requests, so
  `_dispatch` sets `reveal_covered` from each request's `show_covered`.
- **SSE, not WebSockets.** This is a one-way push of small JSON payloads, so
  `curl -N` is a complete client and no dependency is needed for either side.
- **Sends are serialized under one lock and spaced by the server**, because the
  client's composer is a single shared resource and a remote caller may be
  several callers. The local CLI still only asks callers to wait two seconds;
  here it is enforced.
- **Sharing the snapshot cache across requests is one change with how its
  connections are opened.** `make_live_send` keeps a cache alive past the
  request, and every request arrives on its own thread — but a `sqlite3`
  connection may only be used from the thread that created it. `_connect`
  therefore opens the images with `check_same_thread=False`, which is safe
  because they are read-only bytes already in memory and sends are serialized
  under the handler's lock.

  This is written down because the half of it that is *not* obvious was left
  out of the work order sent to `kanao-cli`, and that build hit the wall the
  hard way: the second `POST /send` **died without an answer at all** —
  `ProgrammingError` out of the handler, `RemoteDisconnected` at the client —
  and no test caught it, because the suite had no send that crossed threads.
  Port the two together.

- **The HTTP layer is proven by stubs on any platform; the live wiring is not.**
  `tests/serve/` stands a stub sender, watcher and reader in front of the same
  handler, which is why those tests run on a machine with no KakaoTalk at all.
  What they do not touch is `backends.make_live_send` — the path that resolves
  a room, refuses a duplicate title and types into a real window. That one
  needs a Mac with the client running, and until it has had one, the route is
  unproven however green the suite is.
- **A remote caller is not a person typing, so the reads are bounded.** The
  commands cap nothing because whoever runs one is the cap. Over HTTP `limit`
  reaches SQLite, where a **negative limit means no limit at all**, so
  `limit` is 1–1000 and `highlights` 1–20, checked in `serve/config.py`
  **before the reader is opened**; a `roomId` outside the 64-bit range is a 400
  rather than an `OverflowError` out of the driver.
- **The stream's cursor names every room, because one id cannot stand for an
  account.** `/stream` follows every room, so the last message it sent says
  nothing about where the others had reached — standard SSE `Last-Event-ID`
  would silently resume the wrong ones. The server emits `event: cursor` with
  `{roomId: logId}` *after* the messages it covers, and a client hands it back
  as repeated `since=<roomId>:<logId>`. `Watcher.resume` seeds `_seen` and
  leaves the account fingerprint unset, so the first poll goes through and
  pages each named room forward. A room the cursor does not name starts from
  now; that is a gap, and it is documented rather than papered over. A watcher
  that cannot resume answers 500 rather than quietly starting over.
- **The connection has a deadline and the streams have a count.** A client may
  otherwise promise a body in `Content-Length` and hold a thread indefinitely;
  it now answers `408` after 30 s. Each SSE connection owns a reader and a
  watcher for its whole life, so eight is the most that may be open and the
  ninth is `503 BUSY`. `QuietDisconnects` swallows the four ordinary
  disconnect errors, because a reader closing `curl -N` is SSE's success case
  and `socketserver` would print a traceback for each one.
- **A body refused unread is read anyway, or the refusal is lost.** The
  handler answers before reading the body whenever the body is what is wrong
  — no `Content-Length`, a bad one, too large, chunked — and for a POST's 401
  and 404, and HTTP/1.0 closes the connection after every answer. Closing a
  socket the caller is still writing to resets the connection, and the reset
  destroys the 400 the caller has not read: measured here on macOS 15,
  `ConnectionResetError` on every try when the body landed after the close,
  whatever its size, and from 32 KB up when it had landed before. Windows
  showed the same as `WinError 10053`, which is how it was found — its
  `test_post_without_content_length_is_rejected` failed once in a full run.
  So `Handler.finish` half-closes and reads what is still coming until the
  caller closes, for at most `LINGER_SECONDS` and `LINGER_BYTES`, and only
  when a body may be left (`_body_left_unread`): a lingering read on every
  request would hold the single-threaded test server until each client hung
  up. Ported from kanao-cli `a1906b0` onto `e074d49` unchanged; the regression
  test reads the server's side of the socket at its close rather than waiting
  for a reset, and failed 20 of 20 here before the fix.
- **The status map carries the macOS-only codes, which the Windows table had
  no reason to.** `PERMISSION_REQUIRED`, `NO_USER_DATA` and `INPUT_NOT_FOUND`
  all left as a 500 when this arrived. The first is the one that mattered: it
  is the failure a user fixes in half a minute by granting Accessibility or
  Full Disk Access, and an internal-server-error tells a remote caller there is
  nothing to be done. It is 503 with the other two "the host cannot answer
  yet"; `INPUT_NOT_FOUND` is 404 beside `WINDOW_NOT_FOUND`.
- **The digest is `digest.py`, not `cli._digest`.** The server wanted what the
  command does, and reaching into `cli` for it meant assembling an
  `argparse.Namespace` and pointing `serve` at the layer above it.

- **The 0.15.0 reads are answered here since contract 0.16.0.** `/notice`,
  `/attachments`, `/revisions` and `/file` lost their `x-platform` marks with
  the message keys and the two codes; the pinned set in `test_openapi.py` is
  back to the two macOS-only members. kanao-cli has to take the same document
  for the two copies to be byte-identical again. `/file` answers with the
  shared contract's `X-Kanao-Source` header name.
- **`open` is in the shared contract, and does nothing here.** The Windows
  build needs `open=true` on `/context` and `/digest` to have its client open a
  room whose key is not in memory. Every room is readable on this side, so the
  flag is accepted and opens nothing — but a value that is neither true nor
  false is a 400 on both, or one typo would get two answers.

- **The `photo` key is part of the shared contract.** `Message.as_json` has
  carried `photo: {url, width, height, count}` on a message with a picture,
  and `openapi.json` now documents it as `Photo`. kanao-cli answers with the
  same shape from 0.19.0. It is the address the client stored, not the image.
- **Profile pictures are read with the names, and follow the same snapshot.**
  Measured 2026-09-30 (#45): `NTUser.profileImageUrl` is the small picture
  (`C110x110`) on 1,058 of 1,342 rows, beside `fullProfileImageUrl`. The
  account keeps one row per open-chat profile as well as its own, so the
  `linkId = 0` row wins. A room's own photo is `NTChatMeta` type 4
  `imageUrl` (3 groups); an open group's is `NTOpenLink.linkImageUrl`; type 8
  is a chat background and `NTChatRoom.imageUrl` held one plain-HTTP address
  off the CDN, so neither is read. Only HTTPS `*.kakaocdn.net` passes; the
  four hosts seen (`open.`, `p.`, `iopen.`, `chat.`) each answered an
  unauthenticated GET with `200 image/jpeg`. A group is composed from
  `displayMemberIds`, the display list, not a full roll. Contract 0.18.1
  dropped the two keys' `x-platform`; the board-notice full text stays
  Windows-only, since this client keeps no board and its one 100-character
  notice row matched no message.

## Conventions

- Every command returns `{"ok": true, ...}` or
  `{"ok": false, "error": {"code", "message"}}`. Exit 0, 1 for a handled
  failure, 2 for a malformed command. New commands join this contract.
- Every identifier crossing the boundary is a **string**; KakaoTalk ids exceed
  2^53.
- Messages are returned oldest-first everywhere.
- Failures carry an `ErrorCode`.
- Nothing expensive happens in a constructor, so `status` can explain a broken
  install instead of failing for the same reason.
- Ambiguity is refused with candidates attached, never resolved by picking the
  first match.

## Testing rules

- **No real data, ever.** Not in fixtures, tests, logs, or committed output.
  Fixtures are synthetic; `tests/fixtures.py` mirrors the real schema.
- No sleeps and no timing assumptions.
- Stubs must fail the way production fails.
- When patching a module attribute, bind the original **before** patching or
  the wrapper will call itself.
- **A test that needs macOS says so and is skipped elsewhere.** Most of this
  suite is platform-neutral and gets run on the Windows sibling's machine while
  shared code is written there; before `tests/platform.py` that run answered
  `FAILED (errors=62)`, which says nothing except "not a Mac" and hides the one
  real failure somebody just introduced. The three real reasons are named
  rather than counted: `require_posix_console` (the interface needs `fcntl`),
  `require_commoncrypto` (the fixtures encrypt through Apple's library, which
  was 44 of them), and `require_unix_sockets` (the daemon's transport). Off
  platform that run is now `OK (skipped=63)`; on a Mac nothing is skipped, and
  a skip count above zero there means a guard is wrong.

## Deliberately absent

Login automation, protocol reimplementation, history harvesting, attachment
sending, and Windows support. The supported write operation is text sent
through the running client.

**Calling an agent from inside the interface.** `/ask` (#34) shipped in 0.9.0
and was removed in 0.10.0, on both sides. The direction was inverted: the
agent drives this CLI — `context`, `digest`, `follow` are the interface built
for it — rather than this tool driving an agent. Which agent, what it is shown
and how it is asked are the agent's decisions, and a `/ask` inside the screen
made the tool make all three. It also blurred "no model in this process" by
proxy: the core made no network call, but the `omo` it spawned did. This tool
spawns no process.
