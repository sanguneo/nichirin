# 남은 검증 및 리뷰 계획

사용자 요청에 따라 아래 작업은 실행하지 않고 남긴다. 후속 리뷰는 Opus로
진행하며 Astra/Fable에는 위임하지 않는다.

## 현재 확인된 결과

- 친구 조회 CLI, 즐겨찾기 필터, 전화번호 기본 제외, TUI 친구 목록 구현.
- 배경 전송 경로:
  `EM_SETSEL → EM_REPLACESEL → PostMessage Enter`.
- 지정된 테스트방에서 3건을 새 `from_me` 기록으로 되읽어 확인했다.
  한글·이모지와 여러 줄 메시지가 포함된다.
- 전송 코드는 포커스 변경, 커서 이동, SendInput, 클립보드를 사용하지 않는다.
  한글·이모지 CLI 실행에서 전후 활성 창과 커서 좌표 모두 동일했다.
  여러 줄 실행에서는 활성 창은 같았지만 커서 좌표는 달랐다. 해당 구간에
  커서가 고정돼 있었다고 주장하지 않는다.
- 전체 unittest: 325개, 7.001초, 단일 실행 통과.
- 운영 코드와 새 전송 테스트의 LSP 오류는 없었다.
- 커밋 대상만 내보낸 복사본에서 `__main__.py` 누락으로 실행 실패를 재현했다.
  `.gitignore`를 루트 임시 파일에만 적용하도록 고치고 패키지 진입점을 포함했다.
  이 수정 이후 깨끗한 복사본 재실행은 아직 하지 않았다.

## 후속 작업

### 1. 깨끗한 배포본 실행 확인

- 새 clone 또는 `git archive` 결과에서 아래 파일의 포함 여부를 확인한다.
  - `kakaoread/__init__.py`
  - `kakaoread/__main__.py`
  - `kakaoread/tui/__init__.py`
  - `tests/__init__.py`
- 그 복사본에서 실행한다.

```powershell
python -m kakaoread --help
python -m kakaoread send --help
python -m unittest discover -t . -s tests
```

- 임시 디렉터리에 `install.py --target <dir>`로 설치한 뒤, 설치된
  `scripts/kakaoread.py send --help`도 확인한다.
- 성공 기준: 원본 작업 디렉터리에 의존하지 않고 CLI와 테스트가 실행된다.

### 2. Opus 전송 코드 리뷰

- `kakaoread/winauto.py`, `kakaoread/cli.py`,
  `tests/test_send.py`, `tests/test_send_cli.py`를 리뷰한다.
- 중복된 DB 방 이름과 창 제목을 잘못된 대상으로 해석하지 않는지 확인한다.
- 이미 작성 중인 초안, 창 종료·재열기, 입력 중 사용자 개입을 검토한다.
- 한국어 입력 안내문 `메시지 입력`을 빈 상태로 취급하는 제약을 검토한다.
  이 문구 자체는 현재 어댑터에서 전송할 수 없다.
- `submitted`가 서버 전달 성공으로 오인되지 않는지 확인한다.
- Win32 포인터 크기, 메시지 타임아웃, Enter 실패 시 중복 재전송 위험을 검토한다.
- 추가 실전 전송은 새로 승인된 횟수와 문구로만 진행한다.

### 3. 기존 테스트 타입 오류 정리

실행 테스트는 통과하지만 기존 테스트에서 LSP 오류 23건이 확인됐다.
이번 전송 변경에서는 수정하지 않았다.

- `tests/fixtures.py`: `StubCache`와 `SnapshotCache` 인수 타입 불일치.
- `tests/test_reader.py`: 같은 캐시 타입 문제와 Optional 접근.
- `tests/test_tui_layout.py`, `tests/test_tui_live.py`: Optional 접근.
- 성공 기준: 오류 무시나 테스트 약화 없이 타입 계약을 맞추고 전체 테스트 통과.

### 4. 이전 실험의 시스템 상태 점검

- 앞선 실패 실험에서 포커스 잠금 설정과 클립보드를 변경한 코드가 실행됐다.
  원래 값이 기록돼 있지 않아 정확한 원상복구 여부는 확인하지 못했다.
- 복구 가능한 기록이 있는지 확인하고, 원래 값을 모르면 임의의 기본값으로
  덮어쓰지 않는다. 최종 전송 모듈은 이 설정들과 클립보드를 건드리지 않는다.
