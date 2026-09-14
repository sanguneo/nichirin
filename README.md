# kakaoread

**Read, search, and follow your own KakaoTalk conversations on Windows — read-only, for AI agents.**

**윈도우 카카오톡 대화를 읽고, 검색하고, 실시간으로 따라가는 읽기 전용 CLI.**

## What it does / 무엇을 하는가

kakaoread decrypts the KakaoTalk desktop client's own local databases and hands
an AI agent enough context to summarise a conversation or draft a reply. It
never sends, never writes to KakaoTalk, and never contacts Kakao's servers.

카카오톡 데스크톱 클라이언트가 이 PC에 저장한 대화를 읽어, AI 에이전트가 요약하거나
답장 초안을 쓸 수 있게 합니다. 보내지 않고, 카카오톡을 수정하지 않으며, 카카오 서버에
접속하지 않습니다.

- **Terminal browser / 터미널 UI** — `kakaoread tui` reads conversations at a
  desk rather than through JSON
- **Conversation context / 대화 맥락** — one call returns the room, its recent
  messages in order, and how far those records actually reach
- **Follow / 실시간 추적** — stream new messages as NDJSON as they arrive
- **Search / 검색** — keyword search across the rooms that can be read
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
| `pgup` `pgdn` | 페이지 단위 스크롤 |
| `o` | 이전 기록 400개 더 불러오기 |
| `end` | 맨 아래로 (새 메시지 표시 해제) |
| `/sum 30m`, `/sum 2h` | 구간별 다이제스트 (최대 12h) |
| `e` | 다이제스트 원문을 홈 디렉터리로 내보내기 |
| `esc` | 뒤로 / 다이제스트 닫기 |
| `q` | 종료 |

`/sum` is **extractive, not generative**: with no model in the process, a slice
reports how busy it was, who carried it, the words it was actually about, and
its most substantive lines. Nothing is invented. `e` writes the slice text out
so a model can summarise it properly when one is available.

Speakers are told apart by tint rather than by colour — one base grey with a
slight per-speaker shift at near-constant lightness, so a long transcript stays
readable instead of turning into a palette.

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
| `tui` | 터미널 브라우저 실행 |
| `status` / `verify` | 접근 상태 확인 / 복호화 검증 |
| `schema` / `query "SELECT ..."` | 스키마 조회 / 읽기 전용 SQL |

Every command answers with the same envelope — `{"ok": true, ...}` or
`{"ok": false, "error": {"code", "message"}}` — and exits 0, 1 for a handled
failure, or 2 for a malformed command. All identifiers are strings, because
KakaoTalk ids exceed 2^53 and ordinary JSON parsing would corrupt them.

## How it works / 동작 원리

The client derives its SQLCipher keys at run time, so nothing on disk can
reproduce them. kakaoread finds the live key by scanning the running
`KakaoTalk.exe` for SQLCipher's `codec_ctx` structure using read-only
`ReadProcessMemory`, then pairs each key with the database carrying the same
salt. Pages are decrypted with Windows CNG, and committed write-ahead log
frames are applied so messages appear without waiting for a checkpoint.

The process is opened for query and read access only. It is never written to,
suspended, or debugged, and the databases are never modified.

실행 중인 클라이언트의 메모리에서 키를 읽기 전용으로 찾고, 파일 선두의 salt로 어떤
데이터베이스의 키인지 대조합니다. 커밋된 WAL 기록까지 반영하므로 체크포인트를
기다리지 않아도 최신 메시지가 보입니다. 카카오톡 프로세스와 원본 파일은 수정하지
않습니다.

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

- **Read-only.** No send, login, or history download. Replies are drafts.
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
already stored on your own computer; it does not call Kakao APIs, implement the
KakaoTalk protocol, or modify the application.

이 프로젝트는 카카오와 제휴하거나 보증받지 않은 독립적인 비공식 프로젝트입니다.
사용자 본인의 컴퓨터에 저장된 데이터만 읽습니다.

## Credits

The SQLCipher container layout, key-discovery technique, and database shard
layout come from [kakaocli](https://github.com/silver-flight-group/kakaocli) by
**[Brian ByungHyun Shin](https://github.com/brianshin22)** at
**[Silver Flight Group](https://github.com/silver-flight-group)**.

## License

MIT. See [LICENSE](LICENSE).
