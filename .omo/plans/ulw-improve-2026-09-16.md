# Ultrawork Notepad — kakaoread 전체 개선 루프 + kakaoread-mac 이슈화

Started: 2026-09-16T21:24:46+09:00
Review deadline: 2026-09-16T22:24:46+09:00 (이후 검토 중단, 개발·이슈화는 끝까지)
Tier: HEAVY — 캐시 무효화/스냅샷 수명 코드, 사용자 care 신호("전체적으로", "정성스럽게"),
실제 클라이언트로 전송하는 부작용 검증 포함.

Skills surveyed: programming(.py 작업 — 읽음), memory-discipline(사용자 사실 기록 — 읽음),
git-master(증분 커밋·푸시 — 규약은 git log 모방으로 대체), ast-grep(구조 검색 — 이번엔 grep/LSP로 충분),
review-work(plan-gated 리뷰 — 이 런은 ulw-plan 산출물이 없으므로 self-review),
visual-qa(TUI 시각 검증 — 이번 변경은 렌더 기하에 손대지 않으므로 미적용),
kakaoread(도구 사용법 — 변경이 아니라 사용 문서라 미적용).

## Plan (exhaustively detailed)

P0. 작업 트리의 0.5.1 포트를 커밋·푸시한다 (C1).
P1. A1 메시지 유실: reader.messages 의 after_id 페이지 방향을 고치고 watch 가 끝까지 따라잡게 한다.
P2. A2 실패한 방이 다시 시도되지 않는 문제: 지문을 성공 후에만 기록한다.
P3. A6 newest_log_id 가 삭제된 행을 센다.
P4. A3 /back 이 사용자가 지운 초안을 되살린다.
P5. A5 베이스 페이지 보관이 LRU 예산을 두 배로 먹어 살아있는 스냅샷을 일찍 축출한다.
P6. A4 SendResult TypedDict 가 실제 반환 키를 선언하지 않는다 (LSP 5건).
P7. A8 app.py 모듈 독스트링이 캐시된 트랜스크립트를 부정하는 서술.
P8. 실전 검증 C4 (읽기 + 근카스방 전송 1회).
P9. 릴리스 C6, kakaoread-mac 이슈화 C5.

## Success criteria + QA scenarios
goal 에 등록된 C1~C6. 각 항목별 RED→GREEN 캡처 + 실면 증거.

## Now
P0 커밋·푸시

## Todo
P1..P9 (todo 툴이 라이브 커서)

## Findings

### 검증 완료 — 실제 결함 (구현 대상)

A1. **follow 가 폴 사이에 500개 넘게 도착하면 메시지를 영구히 건너뛴다.**
  - `kakaoread/reader.py:422` — `sql += " ORDER BY logId DESC LIMIT ?"`. `after_id` 가 주어져도
    **가장 최근 limit 개**를 가져온다.
  - `kakaoread/watch.py:101-103` — `arrived = messages(room, limit=500, after_id=since)` 후
    `self._seen[room_id] = max(message.id for message in arrived)`.
  - 600개가 도착하면 최신 500개만 반환되고 `_seen` 이 600번째로 점프 → 아래 100개는 영원히
    전달되지 않는다. 카카오톡이 재접속하며 히스토리를 한꺼번에 쓰는 상황이 정확히 이 경우.
  - 최소 수정: `after_id` 만 주어진 경우(즉 따라잡기)는 ASC 로 오래된 것부터 가져오고,
    watch 는 짧은 페이지가 나올 때까지 커서를 밀며 반복한다.
  - PORTABLE: YES — watch.py / reader.py 는 mac 갈래와 공유.

A2. **읽기가 실패한 방은 다시 시도되지 않는다.**
  - `kakaoread/watch.py:95` — `self._prints[room_id] = current` 가 try 블록 **앞**.
  - 읽기가 `_ROOM_FAILURES` 로 실패하면 continue 하는데 지문은 이미 갱신돼 있어,
    다음 폴은 "변한 게 없다"고 보고 그 방을 건너뛴다. 그 변화분은 로그가 또 바뀔 때까지 유실.
  - 최소 수정: 성공한 뒤에만 지문을 기록한다.
  - PORTABLE: YES.

A6. **newest_log_id 만 삭제된 행을 센다.**
  - `kakaoread/reader.py:435` — `SELECT MAX(logId) FROM chatLogs` (deleted 필터 없음).
    `messages`(:412) 와 `count_messages`(:430) 는 `COALESCE(deleted, 0) = 0` 을 건다.
  - PORTABLE: YES.

A3. **/back 이 사용자가 지운 초안을 되살린다.**
  - `kakaoread/tui/app.py:1051` — `elif name == "back": self.chat = None`.
    Escape 는 `_leave_room`(:914) 을 거쳐 빈 초안이면 `self.drafts.pop` 한다.
  - `_submit`(:921) 이 명령 실행 전에 `chat.draft = ""` 하므로, `/back` 경로는 drafts 에 남은
    **예전 초안을 지우지 않는다** → 방을 다시 열면 사용자가 Ctrl+U 로 지운 초안이 되살아난다.
  - 탐색 에이전트는 "초안이 사라진다"고 반대로 진단했다. 실제 증상은 되살아나는 것.
  - PORTABLE: YES.

A5. **(내가 만든 회귀) 베이스 페이지 보관이 LRU 예산을 두 배로 먹는다.**
  - `kakaoread/snapshot.py` `_store` 가 `held = size + len(base)` 로 청구한다(정직하지만),
    그 결과 256MB 예산에 들어가는 스냅샷 수가 절반이 되어 살아있는 연결이 더 일찍 닫힌다.
  - 베이스 페이지는 순수 캐시(다시 만들 수 있음)이고 연결은 아니다. 예산 초과 시
    **베이스부터 버리고, 그래도 넘치면 그때 엔트리를 축출**해야 한다.
  - PORTABLE: YES — mac 판은 베이스를 예산에 **아예 청구하지 않아서** 더 나쁘다(조용히 초과).

A4. **SendResult TypedDict 가 실제 반환 키를 선언하지 않는다.**
  - `kakaoread/winauto.py:80` 의 TypedDict 에 `clearedDraft`(:430 반환) 와
    `wouldClearDraft`(:414 반환) 가 없다. basedpyright 오류 5건(winauto 2, test_send 3).
  - PORTABLE: NO (Win32 전용 모듈) — 다만 mac 의 대응 타입도 같은 점검이 필요.

A8. **app.py 모듈 독스트링이 성능 불변식을 부정한다.**
  - `kakaoread/tui/app.py:1-8` 이 "매 키 입력마다 상태에서 전부 다시 만든다"고 적었는데,
    `_transcript_lines`(:536) 는 `(width, len(messages))` 로 캐시한다(AGENTS.md 가 71~142ms
    측정치와 함께 그 이유를 기록). 산문 변경이므로 테스트 없이 리뷰로 출고.

### 검증 완료 — 기각 (이유 기록)

D1. "SnapshotCache 가 호출자가 든 연결을 닫는다 → lease/refcount 재설계".
  기각: 모든 호출 경로가 질의마다 `connect()` 를 다시 부르고 `reader.py:423` 처럼 즉시
  `fetchall()` 한다. 어떤 경로도 연결을 넘어서 들고 있지 않아 이 위험이 실현되지 않는다.
  큰 재설계를 정당화하지 못한다. 내가 실제로 악화시킨 부분만 A5 로 좁혀 고친다.

D2. "방 열기·슬래시 명령을 키 루프 밖으로 빼라(비동기화)".
  기각: 이 프로그램은 의도적으로 단일 스레드이고, AGENTS.md 가 `_submit` 을 정확히 한 프레임
  미루는 이유를 이미 기록해 두었다. 전면 비동기화는 이번 범위를 넘고 회귀 위험이 크다.
  값싼 부분(막히기 전에 프레임 한 장 그리기)만 A7 후보로 남긴다.

D3. "Reader.names() 가 세션 내내 무효화되지 않는다".
  기각: AGENTS.md 가 "이 도구가 보여주는 모든 이름은 그 사용자가 **지금** 쓰는 이름"이라고
  이미 계약으로 못박았다. 무효화 트리거를 새로 만드는 건 계약 변경이지 결함 수정이 아니다.
  mac 이슈 후보로만 남긴다.

D4. "names.from_feeds 가 호출마다 피드 전체를 스캔·JSON 파싱한다".
  기각: 명시적 `/real`·`name_history` 에서만 도는 경로라 뜨겁지 않다. 캐시를 붙이면
  무효화 시점이라는 새 결정이 생긴다. 이득 대비 위험이 맞지 않는다.

D6. "winauto.close_window 가 PostMessage 반환을 확인하지 않는다 / Enter 절반 실패".
  기각: mac 저장소 FOLLOWUP.md 에 이미 알려진 항목으로 기록돼 있고, Win32 전용이라
  이식 대상도 아니다. 이번 런의 범위 밖.

## Learnings
- 빠른 탐색 모델은 증상의 **방향**을 반대로 말할 수 있다(A3: "사라진다" vs 실제 "되살아난다").
  file:line 은 신뢰하되 결론은 직접 읽어 다시 세울 것.
- 성능 최적화가 캐시 예산 같은 **다른 자원 회계**를 조용히 바꾸는지 항상 확인할 것(A5).
</content>
