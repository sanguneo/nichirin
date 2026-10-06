# `serve` 패키지화 작업지시서

작성일: 2026-09-17  
대상: `kanao/serve.py`와 `tests/test_serve.py`  
목표: 비대해진 단일 모듈을 전용 패키지로 재구성하되 공개 API와 동작을 보존한다.

## 원칙

- 먼저 **동작 변경 없는 패키지화**를 완료하고 기존 테스트를 통과시킨다.
- 입력 제한, SSE 재접속 등 행동 변경은 패키지화 이후 별도 작업으로 수행한다.
- `kanao/serve.py`와 `kanao/serve/`를 최종 상태에 함께 두지 않는다.
- 기존 사용처인 `from kanao import serve`, `serve.build_handler(...)`,
  `from .serve import run`을 깨지 않는다.
- 불필요한 범용 계층이나 호환용 중복 구현을 만들지 않는다.

## 목표 구조

```text
kanao/
  serve/
    __init__.py       # 외부 공개 API 재노출
    config.py         # 상수와 입력 검증
    handler.py        # HTTP 라우팅과 일반 요청 처리
    stream.py         # SSE와 스트림 수명
    backends.py       # Reader/Sender/Watcher 연결
    server.py         # 서버 생성과 run()
  digest.py           # CLI, TUI, serve가 공유할 digest 계산

tests/
  serve/
    __init__.py
    helpers.py
    test_auth.py
    test_queries.py
    test_send.py
    test_stream.py
    test_server.py
```

`kanao/digest.py` 분리는 패키지화 완료 후 별도 행동 변경 작업으로 진행할 수 있다.
첫 패키지화 단계에서는 현재 digest 호출 계약을 그대로 유지해도 된다.

## 모듈별 책임

### `kanao/serve/__init__.py`

기존 공개 API만 재노출한다.

```python
from .backends import (
    make_live_context_provider,
    make_live_digest_provider,
    make_live_rooms_provider,
    make_live_send,
    make_live_watcher_factory,
)
from .handler import build_handler
from .server import run

__all__ = [
    "build_handler",
    "make_live_context_provider",
    "make_live_digest_provider",
    "make_live_rooms_provider",
    "make_live_send",
    "make_live_watcher_factory",
    "run",
]
```

필요한 기존 상수가 외부 테스트나 호출자에서 사용되고 있다면 해당 상수도 명시적으로
재노출한다. 추측으로 공개 범위를 늘리지는 않는다.

### `kanao/serve/config.py`

- 기본 host와 port
- 본문 최대 크기
- 기본 전송·폴링 간격
- 토큰 검증
- poll/min interval 검증
- 향후 `limit`, `highlights` 상한 검증
- 오류 코드에서 HTTP 상태로의 변환

HTTP handler나 Reader를 가져오지 않는다.

### `kanao/serve/handler.py`

- `build_handler()`
- bearer 인증
- JSON 요청 본문 읽기
- JSON envelope 응답
- `/health`
- `/rooms`
- `/context`
- `/digest`
- `/send`
- 404와 405 처리

SSE 구현을 직접 키우지 않고 `stream.py`의 진입점을 호출한다.

### `kanao/serve/stream.py`

- SSE 이벤트 직렬화
- heartbeat
- 연결 해제 감지
- Watcher와 Reader 정리
- 향후 재접속 커서와 단절 구간 catch-up
- 향후 스트림 연결 상한

일반 HTTP 라우팅과 live Reader 생성은 담당하지 않는다.

### `kanao/serve/backends.py`

- `make_live_send()`
- `make_live_rooms_provider()`
- `make_live_context_provider()`
- `make_live_digest_provider()`
- `make_live_watcher_factory()`

Windows 자동화 및 Reader/Watcher 연결을 HTTP 계층에서 격리한다.

### `kanao/serve/server.py`

- `ThreadingHTTPServer` 생성
- `run()`
- 서버 시작과 종료
- 소켓 정리
- 향후 종료 신호 및 연결 정리

## 테스트 구조

### `tests/serve/helpers.py`

- `StubSender`
- `StubWatcher`
- 합성 Room과 Message
- 서버 시작·종료 helper
- HTTP 요청 helper
- SSE 읽기 helper

### 테스트 파일별 범위

- `test_auth.py`: 토큰, Authorization, 보호 경로
- `test_queries.py`: rooms, context, digest, 쿼리 검증
- `test_send.py`: 본문 검증, 전송 직렬화, rate limit
- `test_stream.py`: SSE 이벤트, 오류, 연결 해제와 자원 정리
- `test_server.py`: 실제 `ThreadingHTTPServer` 동시성 및 종료

테스트 패키지에는 `__init__.py`를 두어 아래 공식 발견 명령으로 모두 실행되게 한다.

```powershell
python -m unittest discover -t . -s tests
```

## 구현 순서

1. 현재 작업 트리와 `serve` 관련 공개 심벌 및 import 사용처를 조사한다.
2. 기존 `tests.test_serve`를 실행해 패키지화 전 기준선을 기록한다.
3. `kanao/serve/`를 만들고 책임별로 코드를 이동한다.
4. `serve/__init__.py`에서 기존 공개 API를 재노출한다.
5. 기존 `kanao/serve.py`를 삭제한다.
6. 기존 `tests/test_serve.py`를 그대로 실행해 동작 보존을 확인한다.
7. 테스트를 `tests/serve/` 아래 책임별 파일로 나눈다.
8. 전체 테스트 발견 명령을 실행한다.
9. CLI 도움말과 `python -m kanao status`를 실행해 import 회귀를 확인한다.
10. 패키지화가 녹색인 상태에서 입력 제한과 SSE 재접속 개선을 별도 작업으로 시작한다.

## 패키지화 완료 기준

- `kanao/serve.py`가 삭제되고 `kanao/serve/`만 존재한다.
- 기존 공개 import와 호출 방식이 그대로 동작한다.
- 패키지화 전후 HTTP 응답 envelope와 상태 코드가 바뀌지 않는다.
- `tests.test_serve`의 기존 테스트가 모두 통과한다.
- 분할된 테스트가 공식 unittest discovery로 모두 발견되고 통과한다.
- `python -m kanao --help`와 `python -m kanao status`가 import 오류 없이 실행된다.
- 패키지화 단계에서 SSE 재접속, 입력 상한, heartbeat 정책 등 행동을 함께 변경하지 않는다.

## 패키지화 이후 작업 순서

1. `/context`와 `/digest`의 `limit`, `highlights` 양수·상한 검증
2. 멀티룸 SSE 재접속 커서와 단절 구간 복구
3. 요청 타임아웃, 동시 요청 및 SSE 연결 상한
4. 운영과 같은 `ThreadingHTTPServer` 통합 테스트
5. digest 계산을 `kanao/digest.py`로 옮겨 CLI 역참조 제거
6. README와 `skill/SKILL.md`의 공개 계약 정정

## 병렬 작업 지침

초기에는 메인 에이전트 한 명이 패키지화와 통합을 소유한다. 서브에이전트는 다음 내용을
읽기 전용으로 병렬 조사할 수 있다.

- HTTP 입력 검증과 자원 제한
- SSE 재접속 및 종료 테스트 설계
- README와 `skill/SKILL.md`의 계약 불일치
- 현재 `serve.py`의 의존 방향과 공개 심벌

여러 에이전트가 동시에 `serve.py`, `cli.py`, `tests/test_serve.py`를 수정하지 않는다.
구현을 병렬화할 때는 패키지화가 완료된 뒤 파일 소유권을 명확히 분리한다.

## 금지 사항

- 패키지화와 SSE 프로토콜 변경을 한 변경에 섞지 않는다.
- 전송 실패 시 자동 재시도를 추가하지 않는다.
- `submitted`를 네트워크 전달 완료로 해석하지 않는다.
- 고정 sleep이나 타이밍 운에 의존하는 테스트를 추가하지 않는다.
- 테스트를 통과시키기 위해 기존 검증을 삭제하거나 약화하지 않는다.
- 사용자 승인 없이 실제 카카오톡 메시지를 보내지 않는다.

## 실행 결과 (2026-09-17)

패키지화 단계는 끝났다. 행동 변경은 섞지 않았다.

| 완료 기준 | 결과 | 증거 |
| --- | --- | --- |
| `kanao/serve.py` 삭제, `kanao/serve/`만 존재 | PASS | `__init__` `config` `handler` `stream` `backends` `server` |
| 기존 공개 import·호출 방식 유지 | PASS | `serve.__all__` 16개, `cli.run`의 `from .serve import …` 그대로 |
| 레거시 별칭 유지 | PASS | `kakaoread.serve is kanao.serve` → True |
| 봉투·상태 코드 불변 | PASS | 기존 37개 serve 테스트 전부 통과(수정 없음) |
| 전체 discovery 통과 | PASS | `Ran 730 tests … OK` |
| CLI import 회귀 없음 | PASS | `--help`, `status`(readableRooms 6) |
| 실기 확인(계획에 없던 추가) | PASS | 실제 `kanao serve` 기동 → `/health` `/rooms`(273) `/context` `/digest` `/stream`(ready·ping) |

옮긴 방식: `build_handler` 본문과 `make_live_*`는 원본 텍스트를 그대로 잘라 옮겼고,
손으로 다시 쓴 것은 SSE 두 메서드뿐이다(`_sse_emit`/`_handle_stream` →
`stream.emit`/`stream.serve_stream`, handler를 인자로 받는 모듈 함수). 헤더의 죽은
`import argparse` 하나가 함께 사라졌다.

계획과 다르게 한 것:

- `tests/serve/test_server.py`는 만들지 않았다. 지금 옮길 테스트가 없고, 운영과 같은
  `ThreadingHTTPServer` 통합 테스트는 이 문서의 후속 4번이다. 빈 파일을 먼저 두지
  않는다.
- `kanao/digest.py` 분리는 후속 5번 그대로 남겼다. 패키지화에 섞지 않는다.
- README의 명백한 오기 네 개는 지금 고쳤다(후속 6번의 일부). `401 INVALID_TOKEN`
  → `UNAUTHORIZED`, ping 30초 → 폴링 간격, `/send`의 `roomId` 지원 주장 삭제,
  그리고 **"재접속하면 끊긴 사이 메시지가 따라온다"는 거짓 보증을 실제 동작으로
  정정**했다. 남은 문서 작업(후속 1이 끝난 뒤 다시 쓸 부분)은 그대로 있다.

## 맥 갈래(`kamado-cli`) 이식 (2026-09-17)

Windows에서 할 수 있는 데까지 했다. 맥 저장소에는 `serve`가 아예 없었으므로
패키지화가 아니라 기능 이식이다.

- `kamado/serve/` 여섯 모듈(같은 구조), `kamado/errors.py`에 `UNAUTHORIZED`·
  `RATE_LIMITED` 추가, `kamado/cli.py`에 `serve` 서브커맨드.
- `serve`는 데몬 전달 목록에서 제외했다. 데몬은 질의 하나에 답하고 끝나므로
  서버가 그 답만 찍고 종료해 버린다.
- `tests/serve/` 46개 통과. 전체 499개 중 실패 1·오류 62는 **이식 전과 동일**하며
  전부 Windows에서 맥 코드를 돌려 생기는 것(`fcntl`, Unix 소켓)이다.
- 문서: README 원격 절과 명령 표, `skill/SKILL.md` 원격 절과 오류 두 줄,
  `AGENTS.md` 모듈 표와 결정 기록, CHANGELOG.
- 맥 실기에서 남은 검증은 `kakaoread-mac/FOLLOWUP.md` 5번에 적었다.
  특히 `POST /send`(스텁으로 덮이지 않는 유일한 경로)와 맥 전용
  `PERMISSION_REQUIRED`가 현재 500으로 나가는 문제.
