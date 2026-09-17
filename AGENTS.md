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
- **Opening a *closed* room is the one step that must touch the screen.** The
  chat-list rows ignore `AXPress`, `AXSelected` and a posted Return — selecting
  one only highlights it. Only a real double-click opens a room, and macOS does
  not infer one from two clicks: the second must carry a click state of 2.
  Because that is a click at screen coordinates, the client is brought forward
  first so it cannot land in another window, and the pointer is put back.
- **A sent message is confirmed by reading it back**, never by the submit call
  alone; `submitted` means the composer emptied, delivery is proven by a new
  from-me row in the log.

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
