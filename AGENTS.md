# AGENTS.md — working on kakaoread-mac

macOS-only KakaoTalk reader, friend browser and sender in zero-dependency
Python 3.9+. Reads stay local; the explicit send command drives the client.
This file is about *changing* the tool; `skill/SKILL.md` is about using it.

Forked from the Windows-only `kakaoread`. Every macOS fact below was
established against a live client rather than inherited; the Windows platform
layer (CNG, the heap scan, `winauto`) is gone, and reading and sending are
both verified working on this machine.

## Commands

```bash
python3 -m unittest discover -t . -s tests    # the whole suite
python3 -m kakaoread status                   # does anything work right now
python3 install.py --target <dir> --force     # install the skill
```

Discovery needs `-t .` so `tests` stays an importable package.

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
