# kamado-cli 개선 검토와 수정 계획

검토일: 2026-09-26. 최종 대상: macOS `kamado-cli` 0.20.0, HEAD
`fb5e347cf9e988448a615dd04151214afd2c238a`.
비교 대상: `../kakaoread`, Windows `kanao-cli` 0.20.1, HEAD
`996441ff89e7f309af69f652b29420072c9c40c8`.

이 문서는 **수정 전 검토와 계획**이다. 제품 코드를 고치거나 테스트를 새로
추가한 결과가 아니다. 줄 번호는 위 커밋 기준이며, Windows 검토는
[`../kakaoread/improvement.md`](../kakaoread/improvement.md)에 있다.

초기 로컬 HEAD는 `2415080d8b8aba0e6e8ffcbc6339a5d046110422`였지만,
사용자의 최신화 요청에 따라 fetch 후 55커밋을 fast-forward하고 관련 소스와
테스트를 다시 대조했다. 초기 발견 중 이미 해결된 queue·fork 진입 조건·cache
소유권·적응형 예산·명세 파일 부재는 아래에서 해결됨으로 정정했다.
상류 코드를 동기화한 것이며 이 검토가 제품 수정 55건을 구현한 것은 아니다.

## 0. 적용 현황 (2026-09-27, 0.21.1)

아래 1–7절은 검토 당시 그대로 둔다. 적용 결과만 여기에 적는다. 모든 항목은 합성 fixture
회귀 테스트로 확인했고, 각 회귀는 수정 전 코드에서 실패하는 것을 확인했다. 전체 suite는
`Ran 1163 tests`, `OK`이다. 실제 클라이언트에서는 `status`·`verify`와 TUI 워커의 대조 경로만
실행했고, 실제 WAL 재시작 경합·FDA 거부·멈춘 자식은 합성 재현으로만 확인했다.

| 항목 | 상태 | 비고 |
| --- | --- | --- |
| M01 DB/WAL 복제 쌍 | 적용 | 복제 전 WAL 헤더(세대)를 읽고 복제된 WAL과 대조. 생김·사라짐도 불일치. 일반 복사는 원본과 재비교 |
| M02 캐시 공개 버전 | 적용 | full/overlay 모두 읽기 전 지문으로 저장. stat 충돌(ABA)까지 해결했다고 주장하지 않음 |
| M03 병렬 작업 수명 | 적용 | 전체 기한(120 s) join, 실패·시간 초과 시 terminate→kill 후 회수, 그 뒤 공유 메모리 해제와 직렬 fallback. `_forkable()` 정책 유지 |
| M04 데몬 framing | 적용 | 개행 framing, 요청 1 MiB·응답 64 MiB·10 s. 초과/불완전/잘못된 JSON을 다른 메시지로 구분. 클라이언트는 불완전 응답을 None으로 돌려 직접 실행 |
| M05 전송 limiter | 해결됨 | 검토 시점에 이미 해결 |
| M06 문자열 room ID | 적용 | 숫자 문자열은 id로 먼저, 없으면 제목. kanao-cli `28aa232`과 같은 규칙 |
| M07 metadata 수명 | 적용 | 이름 맵·컬럼은 캐시가 돌려준 연결(스냅샷)이 바뀌면 다시 읽음. 신원은 조회가 답한 뒤에만 확정 |
| M08 status 캐시 | 적용 | 계정·DB 경로·DB+WAL 지문을 키로, 세기 전 지문으로 저장, 깨진 캐시는 miss |
| M09 방별 순서·커서 | 적용 | 방 사이 시간순 병합, 방 안 ID순. 커서는 정수 비교로 전진만. 빈 방도 위치 0으로 prime |
| M10 외부 텍스트 | 적용 | `layout.clean`으로 C0/DEL/C1을 보이는 문자로. 링크 주소는 제어 문자에서 끝남. JSON은 그대로 |
| M11 권한 timeout | 적용 | plist 읽기도 `guarded`. `PermissionError`는 권한 오류로, 없음·깨짐은 빈 값으로. 멈춘 worker가 있으면 새로 만들지 않음 |
| M12 보유 행 갱신 | 적용 | `Watcher.moved` + `row_versions`/`messages_at`, 워커 POLL이 대조까지 해서 돌려줌. 요약 재계산, 스크롤 위치 고정 |
| D01 초기 커서 | 보류 | 공유 `openapi.json`의 `ready` 커서 의미 변경이라 kanao-cli와 함께 |
| D02 제한 배치 | 부분 적용 | 시작 시 최신 ID까지만 페이지. 누적 행 `Θ(M)`과 TUI 보유 상한은 정책 결정 필요 |

GitHub 이슈 #42(삭제된 메시지 처리)의 남은 질문 — `0x4000` 없는 삭제가 있는가 — 는 실제 DB에서
feed 14가 가리키는 266개가 모두 비트를 달고 있어 없다고 확인했다. 방을 연 뒤의 삭제 반영은 M12가
채운다.

## 1. 결론과 검토 한계

Mac의 우선 과제는 **DB/WAL 복제 쌍의 동일 세대 보장, 이미지와 캐시 버전의
결합, fork 작업의 수명, 데몬 프레이밍, HTTP 계약 차이**다.
단일 계정 DB와 로컬 파생 키라는 구조는 유지한다. 이미 들어온 TUI worker와
멀티스레드 fork 방지 장치도 유지한다. Windows의 방별 DB·메모리 스캔을
가져올 이유는 없다.

사용자 요청대로 코드·기존 테스트·호출 경로를 주로 읽었다. macOS 네이티브
실행, 전체 테스트, 빌드, 광범위한 정적 진단, 실제 카톡·AX·APFS 조작,
성능 측정은 하지 않았다. Windows 쪽에서만 두 가지 공통 패턴을 인메모리
합성 호출로 확인했다. 따라서 이 문서에서 **macOS 실행으로 재현했다는
주장은 없다**.

근거는 **S**(소스와 구체적 반례), **H**(실환경 조건이 필요한 가설),
**D**(현재 계약을 확장하는 개선)로 구분한다. Windows 문서의 **R**은 그쪽의
작은 합성 재현을 뜻한다. P1은 데이터·상태 정확성 또는 입력 경계,
P2는 복구·자원·성능·계약 유지 문제다. 확률·발생 빈도·속도 향상률은
측정하지 않았으므로 임의로 숫자를 붙이지 않았다.

## 2. 모델: 무엇이 같아야 하고 무엇은 달라도 되는가

`B`는 계정 DB 바이트, `W`는 WAL 바이트, `p`는 페이지 크기,
`P = B/p`는 페이지 수, `U`는 WAL이 덮어쓰는 고유 페이지 수,
`R`은 방 수, `M`은 따라잡을 메시지 수, `L`은 페이지 제한,
`K`는 복호화 worker 수다.

| 공통 불변식 | macOS에서의 의미 |
| --- | --- |
| `image = apply(base_g, committed_WAL_g)` | 각각 복제한 DB와 WAL이 서로 조합 가능한 세대여야 한다. |
| 캐시 버전은 읽은 내용보다 앞서지 않음 | 복제 이후 live 파일의 지문을 오래된 clone에 붙이지 않는다. |
| 방별 ID순과 커서 단조성 | 단일 DB여도 room별 재개 위치는 별개다. |
| 실패는 성공 상태를 확정하지 않음 | 신원 조회, 폴링, 전송 간격, cache 갱신에 모두 적용한다. |
| 외부 문자열은 터미널 명령이 아님 | ANSI 폭 계산과 데이터 정규화는 별개다. |
| 자원에는 소유자와 종료 조건이 있음 | worker, 공유 메모리, socket, cache, AX 객체에 적용한다. |

`clone(DB)`와 `clone(WAL)`을 각각 할 수 있다는 사실은
`clone(DB, WAL)`의 원자성을 증명하지 않는다.
`quick_check`는 SQLite 구조가 성립하는지 검사하며 두 파일이 같은 시점의
업무 데이터를 담았는지는 검사하지 않는다. 또한 `stat` 일치는 바이트 일치의
충분조건이 아니다.

## 3. 우선 수정할 문제

### M01. 개별 APFS clone은 DB/WAL 쌍의 원자적 snapshot이 아님 — P1 / S

**근거:** `kamado/snapshot.py:323-356,441-481,765-781`.
`clone_snapshot()`은 DB를 먼저, WAL을 다음에 별도로 복제한다.
이후 `build_image()`의 바이트 비교 대상은 이미 고정된 복제본이다.
live 파일 쌍의 중간 checkpoint/restart를 확인하지 않는다.

가능한 순서:

```text
1. DB 본체의 두 데이터 페이지 값은 (0, 0).
2. WAL g의 이전 커밋은 두 값을 (10, 10)으로 만든 상태.
3. DB만 먼저 clone -> (0, 0).
4. writer가 checkpoint -> live DB (10, 10), WAL을 h로 restart.
5. h의 새 커밋이 첫 페이지만 11로 갱신.
6. WAL h를 clone; 오래된 DB clone과 overlay -> (11, 0).
```

실제 커밋 상태는 `(10, 10)` 또는 `(11, 10)`인데 결과는 둘 다 아니다.
페이지 구조가 정상인 단순 값 변경이면 SQLite 구조 검사를 통과할 수 있다.
이는 APFS 동작을 새로 실험한 결과가 아니라 **파일 두 개를 별도 취득하는
순서에서 도출한 반례**다.

**수정 계획:** 복제 전 live WAL generation을 확보하고, DB/WAL 복제 후
복제 WAL 및 live WAL의 generation과 대조한다. checkpoint는 같은 세대의
WAL이 덮어주는 페이지를 DB에 복사하므로 허용할 수 있지만 restart는 거절한다.
WAL 없음/생김/사라짐도 상태로 구분한다. 세대가 없는 경로와 일반 copy fallback에는
원본 DB 안정성을 별도로 검증하며 stat만으로 원자성을 선언하지 않는다.
이 증거를 M02의 snapshot 버전과 함께 전달한다.

**합격 기준:** clone 두 번 사이에 checkpoint/restart를 주입한 합성 경우를
거절하거나 재시도한다. 같은 세대의 checkpoint는 정확한 결과를 낸다.
`tests/test_snapshot.py`의 직접 읽기 일관성 테스트와 별도로
**live -> clone 경계**를 검사한다.

### M02. 오래된 clone/image에 새 live 지문을 붙임 — P1 / S

**근거:** `kamado/snapshot.py:668-691,714-742`.
처음 읽은 `current`가 아니라 복호화·SQLite 검사 후의 `Fingerprint.of(database)`로
캐시를 저장한다. overlay도 같다.

`clone A -> live에 B 추가 -> 이미지 A 완성 -> (B,A) 캐시`이면 원본이 B 그대로인
다음 읽기에도 A가 나온다. 이 문제는 M01과 독립적이다.
clone이 완벽히 일관되어도 캐시의 공개 지문은 잘못될 수 있다.
Windows의 동일 패턴에서는 합성 지문 A/B/B로 오래된 연결이 재사용됨을 확인했다.

**수정 계획:** 우선 읽기 시작 시점의 지문을 보수적으로 전달하고 후행 지문으로
대체하지 않는다. M01에서 확보한 세대/취득 증거를 image와 묶어 저장한다.
복제 후 새로운 live 커밋이 있으면 다음 호출은 다시 읽어야 한다.
같은 크기·mtime 충돌은 별도 한계이며 `tests/test_snapshot.py`도 이를 설명한다.

**합격 기준:** full/overlay 각각에서 clone 완료 후 새 WAL 커밋을 주입한다.
다음 조회와 Watcher가 그 메시지를 회수한다. 기존
`tests/test_snapshot.py`의 호출 사이 변경 테스트만으로 완료를 판정하지 않는다.

### M03. 허용된 병렬 경로의 무기한 join과 부분 실패 정리 — P2 / S

**근거:** `kamado/snapshot.py:130-218`.
최신 `_forkable()`는 main thread이면서 Python thread 수가 1인 경우만 허용한다.
TUI worker와 HTTP 요청 스레드는 serial 복호화로 간다.
초기 버전의 **스레드 서버에서 무조건 fork한다는 발견은 해결되었으므로 철회**한다.

남은 문제는 단일 스레드 CLI 등에서 병렬 경로를 선택한 뒤의 수명이다.
`proc.join()`에는 기한이 없고, 중간 `proc.start()` 실패 시 이미 시작한
worker를 종료·회수하지 않은 채 serial fallback과 공유 메모리 정리로 간다.
자식이 멈추면 join은 끝나지 않으며 exception fallback도 실행되지 않는다.
실제 자식 정지를 관측한 것은 아니다.

**수정 계획:** 기존 `_forkable()` 정책은 유지한다.
병렬 경로에 전체 작업 deadline, 시작한 worker의 추적,
실패·timeout 시 자식 회수, 공유 메모리 정리 순서를 둔다.
종료가 확인되기 전에 serial fallback을 시작하지 않는다.
`threading.active_count()`가 네이티브 라이브러리의 모든 OS thread까지
증명하는지는 별도 플랫폼 검토 사항이며, 이번에 새 교착 결함으로 확정하지 않는다.

**합격 기준:** 주입한 process adapter로 시작 중 실패, 자식 오류, 종료되지 않는
자식의 회수 순서를 결정적으로 확인한다. Mac의 실제 병렬 결과는 serial과
바이트 단위로 비교한다. 최신 `tests/test_fork_safety.py`에는 threshold를 낮추고
serial 바이트를 oracle로 삼는 별도 검증이 이미 있으므로 이를 재사용한다.
초기 단일 길이 assertion만 보고 “병렬 회귀가 없다”고 말해서는 안 된다.

### M04. 데몬의 한 번 recv 가정과 무제한 응답 축적 — P1 / S

**근거:** `kamado/daemon.py:46-71,203-225,284-286`.
client는 JSON+개행을 보내지만 server는 `recv(65536)` 한 번만 한다.
SOCK_STREAM에서 write 경계와 read 경계는 같지 않다.

유효한 `request = a || b`가 두 read로 나뉘면 `json.loads(a)`가 실패한다.
65,536바이트를 넘는 request는 한 번의 read로 전부 받을 수 없다.
client 응답 버퍼는 개행까지 누적하며, query 결과 크기에도 제한이 없다.
메모리는 입력·출력 바이트 `Q`, `A`에 대해 `Theta(Q + A)`이고
현재 `A`의 상한을 코드에서 얻을 수 없다.

**수정 계획:** 개행까지 읽는 framing을 양방향에서 정의한다.
요청에는 명시적 바이트 상한과 총 deadline을 두고, 초과·불완전·잘못된 JSON을
구분한다. 응답은 단순히 중간에서 잘라 성공으로 반환하지 않는다.
큰 로컬 query는 페이지/stream 계약 또는 명시적 초과 오류 중 하나를 정하고
CLI direct 경로와의 의미 차이를 문서화한다.

**합격 기준:** 두 조각 request, 정확히 경계 길이, 초과, 개행 없는 EOF,
응답 중간 종료를 합성 socket으로 검사한다. 분할 순서는 event/barrier로 제어한다.
고정 sleep으로 “조각이 나뉘었을 것”이라고 가정하지 않는다.

### M05. 초기 전송 limiter 문제 — 최신 버전에서 해결됨 / 재작업 제외

초기 HEAD의 선행 quota 기록과 조기 429는 최신
`kamado/serve/handler.py:51-143,530-571`의 queue와 성공 후 `commit()`으로
대체되었다. 새 이식 작업으로 잡지 않는다.
전송 시간이 `d`, 최소 간격이 `delta`이면 연속 성공의 시작 간격은
최소 `max(d, delta)`다. 실패한 시도는 다음 성공의 시간을 예약하지 않는다.
이 불변식과 대기 중 shutdown 응답을 유지한다.
queue가 strict FIFO를 보장한다고 광고하지 않는다.

`tests/serve/test_send.py:182,207,320,362,380,393`에 queue·실패·간격 사례가
이미 있다. 이번에는 실행하지 않고 읽어 확인했다.

### M06. /rooms의 문자열 ID를 /send가 제목으로만 해석 — P1 / S

**근거:** `kamado/serve/backends.py:79-97`, `kamado/context.py:95-116`.
HTTP는 room을 string으로 받고 backend는 그대로 `_resolve`에 넘긴다.
그 함수의 ID 분기는 int만 허용하므로 `"123"`은 ID가 아니라 제목 검색이다.
Windows 계약 `openapi.json:1138`과도 다르다.

**수정 계획:** Windows backend의 방식처럼 숫자 string을 ID로 먼저 해석하고,
그 ID가 없을 때 숫자 제목 해석으로 이어간다. 범위 검증, 모호성 거절,
중복 window title 거절을 유지한다.

**합격 기준:** 직렬화된 실제 room ID, ID가 없는 숫자 제목, ID와 같은 숫자 제목이
함께 있는 경우, 범위 초과, 중복 제목을 stub reader/sender 경계에서 확인한다.
별도 문서 없이 `_resolve` 전체의 제목 의미를 바꾸지 않는다.

### M07. snapshot보다 오래 사는 이름·스키마·신원 상태 — P2 / S

**근거:** `kamado/reader.py:142-158,186-207,321-381,737-769`,
`kamado/daemon.py:189-221`.
snapshot은 바뀌어도 `_names`, `_columns`, `_own_id_resolved`는 유지된다.
지속 Reader의 새 메시지가 옛 이름을 사용하거나 새 schema를 놓칠 수 있다.
최신 버전에는 미확인 ID를 만났을 때 이름을 다시 읽는 기능이 있으므로
신규 사용자를 항상 놓친다는 주장은 아니다. 이미 아는 ID의 rename과
snapshot/schema 교체가 핵심이다.
또한 own ID는 실제 조회 전에 resolved를 True로 설정하여 일시 실패가
이후 `None`으로 고착되는 별도 문제가 있다.

**수정 계획:** metadata를 유효한 source/schema 세대와 결합하고, 성공한 조회에만
신원 완료 플래그를 기록한다. 단일 DB에서는 어느 방의 새 메시지도 DB를 바꾸므로
매번 모든 이름을 무조건 다시 읽는 방식도 비싸다. 우선 snapshot 교체 시
명시적으로 무효화하되, 이후 측정에 따라 사용자/schema 변경 토큰으로 좁힌다.
변경 토큰 없는 영구 캐시나 불명확한 TTL로 정확성 계약을 대신하지 않는다.

**합격 기준:** 이미 알려진 ID의 rename, 새 사용자, schema 교체,
첫 조회 실패 후 성공, 같은 snapshot 반복 사용을 묶어 확인한다.
이름 `V`개의 처리 비용은 세대당 `O(V)`이며 메시지별 `O(MV)`가 되지 않아야 한다.

### M08. status 방 수 캐시는 WAL과 계정 식별자를 무시 — P2 / S

**근거:** `kamado/reader.py:249-306`, `tests/test_reader.py`의 status cache 사례.
`status.json`은 DB 본체의 mtime·크기와 방 수만 저장한다.
WAL만 바뀌는 방 추가/삭제는 감지하지 못한다.
다른 계정 DB가 같은 stat을 가지면 그 방 수도 재사용할 수 있다.

**수정 계획:** DB/계정 식별자와 DB+WAL 취득 버전을 키에 포함한다.
방 수를 센 snapshot보다 나중의 지문으로 다시 저장하지 않도록 M02와 함께 고친다.
캐시 내용을 검증하지 못하면 `ready`인 정확한 현재 값으로 가장하지 않는다.

**합격 기준:** DB stat 동일/WAL 변경, 동일 stat의 다른 DB, count 도중 갱신,
손상된 status 캐시를 확인한다. 정상 재사용은 복호화를 추가하지 않아야 한다.

### M09. 방별 순서 역전과 SSE 커서 후퇴 — P1 / S

**근거:** `kamado/watch.py:166-204`,
`kamado/serve/stream.py:135-149`, `kamado/tui/app.py:694-706`.
`(id,time)=(10,12:01),(11,12:00)`을 Watcher가 `11,10` 순으로 바꾸면,
SSE의 마지막 대입 커서는 `10`이다. TUI도 역순을 덧붙인다.

**수정 계획:** Windows W02와 같은 계약으로 방별 ID순을 보존하고,
성공적으로 배출한 ID의 정수 최댓값만 커서에 기록한다.
방 사이의 시각순은 방 안의 순서를 깨지 않는 범위에서만 적용한다.

**합격 기준:** 역전 시각·누락 시각·동일 시각·여러 방과
재연결 후 중복 여부를 합성 메시지로 확인한다.

### M10. 외부 텍스트가 터미널 제어열로 출력됨 — P1 / S

**근거:** `kamado/tui/console.py:343-370`,
`kamado/tui/layout.py`, `kamado/tui/app.py`의 메시지·닉네임·링크 조립.
콘솔은 CR/LF/TAB만 치환하고 스타일 포함 문자열을 출력한다.
폭 계산에서 ANSI를 제외해도 외부 ESC/OSC를 무력화한 것은 아니다.
화면 지우기·커서 이동 등으로 렌더 캐시와 실제 화면을 다르게 만들 수 있다.
실제 터미널 영향이나 카카오톡의 제어문자 보존은 이번에 실행하지 않았다.

**수정 계획:** Windows W03과 같은 외부 텍스트 정규화 계약을 먼저 정한다.
본문·인용·이름·제목·상태와 링크 대상을 스타일 생성 전에 처리하고,
렌더러 자체의 ANSI는 유지한다. JSON 원문을 일괄 변경하지 않는다.

**합격 기준:** 양쪽 renderer에 같은 합성 CSI/OSC/BEL 사례를 적용한다.
외부 제어열은 남지 않고 정상 색상·폭·클릭 가능한 링크는 유지된다.

### M11. 권한 timeout이 실제 plist 읽기를 감싸지 않음 — P2 / H

**근거:** `kamado/paths.py:49-81,106-120`,
`kamado/keyring.py:53-60`, `kamado/derive.py:181-189`.
디렉터리 열거는 `guarded()` 안이지만 실제 plist open/read는 밖이다.
프로젝트가 설명하는 FDA 접근 정지가 파일 open/read에도 적용된다면
discovery가 기한 없이 멈출 수 있다. **해당 OS 조건은 이번에 확인하지 않았다.**

**수정 계획:** 권한 경계의 실제 파일 취득을 감싸고, permission/timeout과
선택적인 파일 부재·잘못된 plist를 구별한다.
이미 `guarded()`가 포기한 daemon thread는 취소되지 않으므로 새 wrapper를
호출마다 무제한 만드는 방식은 피한다. 실패 상태 재사용 또는 동시 취득 수를 제한한다.

**합격 기준:** 주입한 차단 취득 동작에서 timeout과 worker 수 상한,
정상 plist·없는 plist·깨진 plist의 서로 다른 처리를 확인한다.
실제 FDA 시험은 Mac에서 필요할 때 한정한다.

## 4. 자원·성능 개선의 정확한 대상

### 적응형 예산은 이미 구현됨: 유지 비용과 피크 비용을 구별

초기 256 MiB 고정 예산은 최신 `kamado/snapshot.py:262-275,630-648`에서
Windows와 같은 `min(max(256 MiB, 3B_max), RAM/4)`로 개선되었다.
따라서 이식을 새 작업으로 제안하지 않는다.
다만 명시한 budget, 마지막 snapshot 하나가 예산보다 클 수 있는 eviction,
임시 이미지·SQLite 객체·worker 메모리의 별도 비용은 여전히 구별해야 한다.
이것은 RSS 상한이 아니다.

### send 전용 cache/keyring 재사용도 이미 구현됨

`kamado/serve/backends.py:38-97`는 cache와 account를 요청 사이에 공유하고
Reader만 새로 만든다. `kamado/reader.py:143-147,174-175`의 `_owns_cache`와
`kamado/snapshot.py:531-551`의 thread-compatible 연결도 함께 있다.
초기 버전에서 발견한 소유권 누락과 반복 전체 복호화는 **해결된 항목**이다.

이 설정이 동시 접근을 안전하게 만드는 것은 아니므로 read route까지
같은 cache를 확대 공유하지 않는다. 남은 정확성 문제 M02/M07은 따로 수정한다.

### 병렬화와 overlay의 비용식

전체 복호화 시간의 모델은
`T_full ~= T_acquire(B,W) + T_copy(B,W) + T_decrypt(P)/K + T_spawn_join + T_check(B)`다.
`K`를 늘려도 직렬 취득·복사·검사 시간은 줄지 않는다.
병렬화가 이기는 조건은 절약한 복호화 시간이 추가 생성·복사·정리 비용보다 큰 경우다.
실측 없이 `K`배 빨라진다고 말할 수 없다.

overlay도 WAL 전체를 parse하고 결과 이미지 전체를 복사한다.
페이지 복호화는 `O(U)`지만 전체 바이트 처리는 `Theta(B+W)`다.
단일 큰 DB라는 특성상 캐시 보유량, 동시 요청의 각 Reader, worker 공유 메모리,
임시 이미지까지 합산해야 한다. HTTP 본문 1 MiB, stream 8개 제한이
일반 read 요청의 전체 메모리/worker 수를 제한하는 것은 아니다.
우선 직렬 경로의 단계별 비용과 동시 읽기 수를 확인한 뒤 admission limit을 정한다.

### 새 TUI worker의 강점과 한계

`kamado/tui/worker.py:326-377`은 계산 완료 후에도 결과를 소비할 때까지
job key를 busy로 유지하므로 pending/running/done을 합친 수가 16 이하라는
불변식이 있다. unbounded Queue라는 이유만으로 `_done`이 무제한이라고
결론내리면 틀린다. 그러나 job 결과 한 개의 메시지 수는 별도이므로
이 불변식은 바이트 메모리 상한이 아니다.
`229-260`행의 watch-before-window와 pane token도 유지할 가치가 있다.

## 5. 공통 계약 개선과 양방향 학습

| 방향 | 배울 내용 | 그대로 복사하지 않을 내용 |
| --- | --- | --- |
| 양쪽에 이미 적용 | 성공 후 pacing, bounded send queue, shutdown 응답 | Win32 대상 창·메모리 키 탐색 |
| 양쪽에 이미 적용 | 적응형 cache budget, send 전용 cache 소유권 | 방별 DB 수만큼 watcher를 두는 구조 |
| Windows -> Mac | 아직 남은 문자열 room ID 처리 | AX 대신 Win32 창 조작을 가져오기 |
| Mac -> Windows | `LogRestarted` 즉시 full fallback | APFS clone과 fork 복호화 |
| Mac -> Windows | 단일 DB owner, pane token, 소비 완료까지 job 수 제한 | 비용 측정 없이 worker 구조 전체를 복제하기 |
| 공동 | image/source 버전, 커서 단조성, 외부 텍스트 경계, 실패 상태 미확정 | 같은 기능명이 같다는 이유만으로 플랫폼 동작을 같다고 가정하기 |

**최신화 후 확인:** `openapi.json`, 사진·sixel, `_SendQueue`, 적응형 예산,
`include_erased`, TUI worker가 모두 있다. 양쪽 HEAD의 `openapi.json` Git blob은
`895f73ef63abd4f46f38afeb022766d95247ae1b`로 동일하다.
작업 트리 바이트 해시는 LF/CRLF 차이로 다르지만 줄 끝 차이를 제외한 diff는 없다.
명세 파일 부재는 더 이상 발견 사항이 아니다.

같은 명세가 있다고 M06의 실제 backend 차이까지 사라지는 것은 아니다.
Windows의 `open=true`는 키 확보를 위해 창을 열지만, 최신 Mac은 값을 검증하고
창을 열지 않는 계약을 이미 구현했다. test envelope·에러 코드·ID string·생략 필드·재개 cursor를
같은 합성 사례로 검증하는 쪽이 두 구현을 성급히 공용 패키지로 묶는 것보다 낫다.

### D01. 조용한 방의 초기 커서

`kamado/watch.py:122-132`는 prime 위치를 알지만
`kamado/serve/stream.py:100-118`은 빈 ready cursor를 보낸다.
연결 중 조용했던 방은 끊긴 사이 도착한 메시지의 재개 기준을 받지 못한다.
현재 설명된 한계의 개선이지 은폐된 버그로 분류하지 않는다.
성공적으로 prime한 모든 방의 위치와 빈 방의 시작점 `0`을 전달하고,
실패한 방은 구별한다. 양쪽 stream 계약·테스트를 함께 갱신한다.

### D02. 따라잡기·TUI 보유량을 제한하되 메시지를 버리지 않기

`kamado/watch.py:181-205`는 페이지를 모두 모아 반환한다.
대상 집합이 읽는 동안 고정되어 있고 오류가 없다면
`floor(M/L) + 1`번의 조회와 `Theta(M)` 행 메모리가 필요하고,
마지막 정렬은 `O(M log M)`이다. 새 행이 계속 추가되면 full page가 계속 나와
한 번의 poll을 오래 붙잡을 수도 있다.
시작 시 상한 ID를 고정하고 제한 배치/페이지별 배출·방별 공정성·실패 재개를
함께 설계한다. 최종 최대 ID를 저장한 뒤 중간 메시지를 자르는 방식은 금지한다.
TUI의 append-only 목록도 세션 길이만큼 자라므로 보유 창과 이전 기록 재로딩,
스크롤 앵커를 함께 정한다.

### M12. 이미 보유한 ID의 삭제·수정 반영 누락 — P1 / S

`kamado/tui/app.py:694-706`은 기존 ID를 무시하고 `1074-1096`의 렌더 키는
메시지 수·폭·열린 사진뿐이다. 최신 Reader/worker는 삭제 placeholder를
읽지만 Watcher는 높은 ID만 요청하므로 이미 그린 원문을 다시 읽지 않는다.
새 삭제 feed가 와도 원래 ID의 내용을 조정하지 않는다.
Windows W04와 같은 보유 행 재확인 및 render revision을 설계한다.
합격 기준은 새 메시지 없이 ID 8을 삭제했을 때 원문이 화면·인용·요약·내보내기에서
사라지는 것이다. worker 소유권을 깨고 UI thread에서 직접 DB를 읽으면 안 된다.

## 6. 단계별 수정 계획

| 순서 | 수정 단위 | 의존성 | 수정 후 최소 확인 |
| --- | --- | --- | --- |
| 1 | M01 복제 세대 + M02 cache 공개 버전 | 없음 | checkpoint/restart, clone 후 append, full/overlay |
| 2 | M03 병렬 작업 수명 | 기존 fork guard 유지 | 허용된 경로의 시작 실패·timeout 회수 |
| 3 | M04 데몬 framing | 없음 | 분할·경계·EOF, 명시적 크기 오류 |
| 4 | M06 문자열 ID | 현 공통 계약 유지 | 합성 backend, 숫자 ID/제목 충돌 |
| 5 | M07 metadata, M08 status | M02 | rename·schema·실패 복구·WAL-only 변경 |
| 6 | M09 cursor, M10 표시 경계 | 없음 | 역전 시각, 외부 제어열, 정상 링크 |
| 7 | M12 보유 행 갱신 | M02/M07 | 삭제·동일 ID 변경, worker 경계 유지 |
| 8 | M11 권한 경계, D01/D02 | 필요 OS 조건/계약 확인 | 기한·worker 수, 초기 cursor, 제한 배치 |

M01/M02는 데이터 정확성 때문에 먼저 하고, 성능 최적화는 그 뒤에 한다.
각 수정은 같은 동작을 검증하는 기존 테스트 옆에 작은 회귀를 추가한다.
전체 테스트와 실제 Mac 확인은 실제 코드 변경의 범위에 맞춰 수행하며,
검토 문서만 작성하는 이번 작업에는 소급하여 요구하지 않는다.

## 7. 유지할 설계와 이번에 확정하지 않은 사항

- 단일 계정 DB·파생 키, read-only 조회, 올바른 WAL commit 경계, base 세대 확인,
  실패한 poll의 지문 미확정, `LogRestarted`의 즉시 fallback은 유지한다.
- SQLCipher page HMAC 미검증은 현재의 명시적 위협 모델 한계다.
  WAL checksum을 위변조 방지로 설명하면 안 되지만, 새 인증 구현을
  이번 범위에 임의로 추가하지 않는다.
- Python 3.9/3.10의 plaintext 임시 파일, 닫힌 방을 여는 AX 클릭·활성화는
  이미 문서화된 플랫폼 동작이다. 삭제가 secure erase라는 보장은 하지 않는다.
- AX element registry의 지속 증가 후보는 호출자가 backend를 얼마나 오래
  보유하는지에 달렸다. HTTP `make_live_send()`는 현재 새 sender를 만들므로,
  정상 전송마다 영구 누수가 생긴다고 확정하지 않는다.
- 기존 멀티스레드 fork 방지, 전송 queue, cache 소유권, 적응형 예산,
  신규 사용자 이름 재조회, thread 인용과 삭제 placeholder는 유지할 구현이다.
- 네이티브 자식 정지와 FDA 파일 읽기 정지는 실제 Mac에서 확인하지 않았다.
  조건부 위험을 관측된 장애로 바꾸어 적지 않았다.
- 이번에는 실제 대화·계정 DB를 읽지 않았고, 테스트·빌드·클라이언트 전송을
  실행하지 않았다. 제품 코드·버전·계약 파일은 이 검토에서 수정하지 않는다.
