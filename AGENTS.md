# AGENTS.md — working on kakaoread-mac

macOS-only KakaoTalk reader, friend browser and sender in zero-dependency
Python 3.10+. Reads stay local; the explicit send command drives the client.
This file is about *changing* the tool; `skill/SKILL.md` is about using it.

Forked from the Windows-only `kakaoread`. The port is in progress and this
file is being rewritten as each macOS fact is established rather than
inherited. Anything still describing Windows is a leftover, not a claim.

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
- **Accessibility** — required to drive the client for sending.

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
