# kakaoread

**Read conversations and friends, follow new messages, and send through KakaoTalk on Windows — for AI agents.**

**윈도우 카카오톡 대화·친구 목록 조회, 실시간 추적, 메시지 전송 CLI.**

## What it does / 무엇을 하는가

kakaoread decrypts the KakaoTalk desktop client's own local databases and hands
an AI agent enough context to summarise a conversation or draft a reply.
Friends also come from the local database. The explicit `send` command drives
the running client's message input; KakaoTalk itself sends the message.

카카오톡 데스크톱 클라이언트가 이 PC에 저장한 대화를 읽어, AI 에이전트가 요약하거나
답장 초안을 쓸 수 있게 합니다. 친구 목록도 로컬에서 읽습니다. 명시적인 `send` 명령은
실행 중인 카카오톡의 입력창을 조작해 실제 메시지를 보냅니다.

- **Terminal browser / 터미널 UI** — `kakaoread tui` reads conversations at a
  desk rather than through JSON
- **Conversation context / 대화 맥락** — one call returns the room, its recent
  messages in order, and how far those records actually reach
- **Follow / 실시간 추적** — stream new messages as NDJSON as they arrive
- **Search / 검색** — keyword search across the rooms that can be read
- **Friends / 친구** — local friend list, favorites, optional phone numbers
- **Send / 전송** — text through an already-open chat window, without moving
  the cursor, taking focus, or using the clipboard
- **Inspect / 구조 조회** — schema dump and read-only SQL

## Requirements / 요구 사항

- Windows 10 or 11
- 64-bit Python 3.10 or newer (3.11+ additionally keeps decryption off disk)
- KakaoTalk desktop running and signed in

No third-party packages. The runtime uses only the Python standard library and
Windows system APIs, so there is no `pip install` step and no virtual
environment to manage.

서드파티 패키지가 없습니다. 표준 라이브러리와 윈도우 시스템 API만 사용하므로
`pip install`도, 가상환경도 필요하지 않습니다.

## Install as an agent skill / AI 스킬로 설치

```powershell
python install.py
```

That copies the skill, its runtime, and the licence to
`~/.agents/skills/kakaoread`. The installed copy is self-contained: it keeps
working after the source checkout is moved or deleted. Use `--target` for
another agent's skill directory and `--force` to update an existing install.

```powershell
python install.py --target "$HOME/.claude/skills/kakaoread"
python install.py --force
```

Then ask your agent things like:

- "카톡에서 지수랑 최근 대화 요약해줘."
- "회의 시간 얘기한 카톡 찾아줘."
- "이 대화 읽고 내 말투로 답장 초안 써줘. 보내지는 마."

## Terminal browser / 터미널 UI

```powershell
python -m kakaoread tui
```

A room list, a transcript, and time digests, styled to match the OmO
interactive interface so the two sit side by side.

**Messages arrive while you read.** The open room is followed in the
background, so a conversation updates in place rather than needing to be
reopened. An idle check costs about 0.3 ms, because a room whose log has not
moved is never decrypted. If you have scrolled up into history the viewport
holds its place and the status line reports `↓ 새 메시지 N개` instead; press
`end` to drop back to the newest line. The room list refreshes the same way,
so unread counts stay current without a keypress.

| Key | Action |
| --- | --- |
| `↑` `↓` | 이동 / 스크롤 |
| `enter` | 대화방 열기 |
| `f` | 대화방 목록 ↔ 친구 목록; 친구에서 `enter`는 기존 1:1 대화 열기 |
| `pgup` `pgdn` | 페이지 단위 스크롤 |
| 글자 입력 | 메시지 작성. 대화방에서 글자는 단축키가 아니라 입력입니다 |
| `enter` | 전송. `/`로 시작하면 명령으로 실행 |
| `/older` | 이전 기록 400개 더 불러오기 |
| `end` | 맨 아래로 (새 메시지 표시 해제) |
| `/sum 30m`, `/sum 2h` | 구간별 다이제스트 (최대 12h) |
| `/export` | 다이제스트 원문을 홈 디렉터리로 내보내기 |
| `esc` | 초안 지우기 → 다이제스트 닫기 → 뒤로 |
| `q` | 목록에서 종료. 대화방에서는 `/quit` 또는 ctrl+c |

`/sum` is **extractive, not generative**: with no model in the process, a slice
reports how busy it was, who carried it, the words it was actually about, and
its most substantive lines. Nothing is invented. `e` writes the slice text out
so a model can summarise it properly when one is available.

Speakers are told apart by tint rather than by colour — one base grey with a
slight per-speaker shift at near-constant lightness, so a long transcript stays
readable instead of turning into a palette.

An open room carries a message box at the bottom, and it keeps focus the whole
time: a letter belongs to the message being written, not to a shortcut. `enter`
hands the text to the same client input the `send` command drives, so the
sent line appears the way anyone else's does — once the client writes it to the
log — rather than being echoed optimistically. A refusal (no open chat window,
a draft already in the box, two rooms sharing a title) is reported on the status
line and the room stays open.

The browser needs a real interactive console; through a pipe it refuses with
`INVALID_REQUEST` rather than drawing garbage.

## Command line / 명령줄

From the repository root, `python -m kakaoread <command>`. From an installed
skill, `python "<skill-dir>/scripts/kakaoread.py" <command>`.

| Command | Purpose |
| --- | --- |
| `context --room "Name"` | 방을 찾아 시간순 대화와 커버리지를 한 번에 반환 |
| `context --room-id 123 --limit 100` | 아이디로 지정, 더 넓은 창 |
| `follow --interval 1` | 새 메시지를 NDJSON으로 스트리밍 |
| `rooms --readable-only` | 읽을 수 있는 대화방 목록 |
| `messages --room-id 123 --before-id 999` | 과거로 페이지 넘기기 |
| `search "keyword"` | 읽을 수 있는 방에서 키워드 검색 |
| `friends --favorites` | 즐겨찾기 친구 조회; 전체 목록은 `friends` |
| `friends --with-phone` | 명시적으로 전화번호 포함 (기본은 제외) |
| `send --room "Name" --dry-run "Text"` | 대상과 입력창 점검만; 입력·전송 없음 |
| `send --room-id 123 "Text"` | 열린 채팅창을 통해 실제 텍스트 전송 |
| `tui` | 터미널 브라우저 실행 |
| `status` / `verify` | 접근 상태 확인 / 복호화 검증 |
| `schema` / `query "SELECT ..."` | 스키마 조회 / 읽기 전용 SQL |

Every command answers with the same envelope — `{"ok": true, ...}` or
`{"ok": false, "error": {"code", "message"}}` — and exits 0, 1 for a handled
failure, or 2 for a malformed command. All identifiers are strings, because
KakaoTalk ids exceed 2^53 and ordinary JSON parsing would corrupt them.

### Sending / 메시지 전송

Open the conversation in its own KakaoTalk window first. The CLI resolves the
catalog, then requires an exact window-title match. Duplicate catalog titles
or duplicate open windows are refused with candidates; even a room id cannot
disambiguate identical window titles. Give the target a unique title first.

```powershell
python -m kakaoread send --room "Sample Room" --dry-run "Hello"
python -m kakaoread send --room "Sample Room" "Hello"
python -m kakaoread send --room "Sample Room" --open "Hello"   # open it first
```

`--open` lets the tool open the conversation itself when its window is closed.
It posts a double-click into KakaoTalk's room list without moving the cursor,
reads the title of whatever window appears, and closes anything that is not
the room asked for — rows can only be addressed by coordinate, so a miss
costs a window that flickers open and shut, never a message in the wrong
place — though a room opened while searching **is marked read**, which is the
real cost of a miss. Opening does take focus, exactly as clicking the room
yourself would;
sending afterwards does not. The self-chat is out of reach this way, because
its window is titled with the account's own name rather than `나와의 채팅`.

`--open` 은 대화방 창이 닫혀 있을 때 도구가 직접 엽니다. 커서를 움직이지 않고
목록에 더블클릭 메시지를 부친 뒤, 열린 창의 제목이 정확히 일치할 때만 보냅니다.
빗나가면 엉뚱한 창이 잠깐 열렸다 닫힐 뿐 메시지는 나가지 않습니다. 창을 여는
순간에는 포커스를 가져갑니다 — 직접 클릭할 때와 같습니다.

`send` supports text, including Hangul, emoji and newlines. It does not open
windows or compose messages in the TUI. Existing drafts are refused rather
than overwritten. The current Korean client's literal empty-input hint
`메시지 입력` is treated as an empty box, not as a draft; that exact text is
reserved and cannot itself be sent by this adapter.

The response contains `roomId` (string), `room`, `length`, `dryRun` and
`submitted`. **`submitted: true` means Enter was queued, not server delivery.**
Use `context` or `messages` to confirm a new matching record from your account;
do not automatically retry an uncertain result and risk duplicates.

Confirm the recipient and content before sending to other people. Keep at
least two seconds between sends; callers enforce this interval. Use self-chat
or an explicitly authorized test room for real tests. Do not type into the
target input while a send is running. If a previous WM_SETTEXT experiment left
the button disabled, close and reopen that chat window before retrying.

전송 전에 상대와 내용을 확인하세요. 전송 간격은 호출자가 최소 2초로 유지해야 합니다.
커서·포커스·클립보드를 사용하지 않지만 실제 메시지가 나가므로, 불확실한 결과를
자동 재전송하지 말고 먼저 대화 기록에서 확인하세요.

## How it works / 동작 원리

The client derives its SQLCipher keys at run time, so nothing on disk can
reproduce them. kakaoread finds the live key by scanning the running
`KakaoTalk.exe` for SQLCipher's `codec_ctx` structure using read-only
`ReadProcessMemory`, then pairs each key with the database carrying the same
salt. Pages are decrypted with Windows CNG, and committed write-ahead log
frames are applied so messages appear without waiting for a checkpoint.

Discovery is remembered between runs. The **addresses** of the contexts found
in the client are written to `%LOCALAPPDATA%\kakaoread\contexts.json`, so the
next command re-reads them in under a millisecond instead of searching the
heap again — `status` goes from 2.43 s to 0.30 s. No key and no salt is ever
written there, every cached address is validated exactly like a fresh
candidate, and one that no longer holds a context sends the tool back to a
full scan.

키 탐색 결과는 **주소만** `%LOCALAPPDATA%\kakaoread\contexts.json` 에 기억해
다음 호출에서 힙을 다시 뒤지지 않습니다. 키나 salt 는 저장하지 않으며, 주소가
더는 유효하지 않으면 전체 스캔으로 되돌아갑니다.

The process handle is opened for query and read access only. No process memory
or database is directly modified. Sending separately uses Win32 control
messages (`EM_SETSEL`, `EM_REPLACESEL`, then posted Enter), not protocol
reimplementation or global keyboard/mouse injection.

실행 중인 클라이언트의 메모리에서 키를 읽기 전용으로 찾고, 파일 선두의 salt로 어떤
데이터베이스의 키인지 대조합니다. 커밋된 WAL 기록까지 반영하므로 체크포인트를
기다리지 않아도 최신 메시지가 보입니다. 메모리나 원본 DB를 직접 수정하지 않습니다.
전송은 별도로 카카오톡 입력 컨트롤에 윈도우 메시지를 보내 처리합니다.

### Performance

Reading is whole-file decryption, so the work is avoided rather than optimised:
a snapshot is cached and reused until the file actually changes. Change
detection uses `os.stat`, not `ReadDirectoryChangesW` — across a 30-second
observation of the live client, directory notifications produced **zero**
events while stat polling caught every change, because NTFS defers
directory-entry updates while SQLite holds the log handle open. The same
applies to C#'s `FileSystemWatcher`, which wraps that API.

Measured on a 76.5 MB conversation with 445,714 messages:

| Operation | Time |
| --- | --- |
| Reading 20 messages, cold | ~1.0 s |
| Reading the same messages again | ~0.6 ms |
| One idle `follow` poll across 6 rooms | ~0.7 ms |
| One idle refresh while reading in the UI | ~0.3 ms |

## Limits / 제한 사항

- **Client-driven sending.** Requires an open, uniquely titled chat window and
  the current Korean RichEdit input. No attachment sending, login automation,
  background window opening, or history download.
- **Local only.** Only what this computer has cached, and only for rooms
  KakaoTalk has opened — a key exists only for a database the client loaded.
  A missing result is not proof a conversation never happened.
- **Attachments.** Photos, videos, files, and stickers are not rendered.
- **Live writes.** If the client rewrites a database mid-read, the read is
  refused and retried rather than returning a torn result.
- **Plaintext.** On Python 3.11+ the decrypted image stays in memory. On 3.10 a
  temporary file is used and deleted; deletion is not secure erasure.
- **Integrity, not authenticity.** SQLite integrity and log checksums are
  verified; SQLCipher's per-page HMAC is not.
- **Compatibility.** Client updates or process-access restrictions can stop it
  from reading at all.

## Development

```powershell
python -m unittest discover -t . -s tests
```

The suite is synthetic throughout: AES is checked against the NIST SP 800-38A
vector, page and log handling against fixtures built from real SQLite and real
BCrypt round-trips. No real conversation, contact, or key belongs in this
repository, its tests, or its logs.

## Disclaimer / 면책 조항

An independent, unofficial project, not affiliated with or endorsed by Kakao
Corp. "KakaoTalk" and "카카오톡" are trademarks of Kakao Corp. It reads data
already stored on your own computer and drives the existing client for sends.
It does not call Kakao APIs directly, implement the KakaoTalk protocol, or
patch the application.

이 프로젝트는 카카오와 제휴하거나 보증받지 않은 독립적인 비공식 프로젝트입니다.
사용자 본인의 로컬 데이터를 읽고, 전송 시에는 실행 중인 카카오톡을 조작합니다.

## Credits

The SQLCipher container layout, key-discovery technique, and database shard
layout come from [kakaocli](https://github.com/silver-flight-group/kakaocli) by
**[Brian ByungHyun Shin](https://github.com/brianshin22)** at
**[Silver Flight Group](https://github.com/silver-flight-group)**.

## License

MIT. See [LICENSE](LICENSE).
