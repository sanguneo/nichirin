<div align="center">

<img src="assets/og-kamado-cli.png" alt="kamado-cli — 맥의 카카오톡을 터미널에서 읽고, 따라가고, 보낸다" width="900">

# kamado-cli

**내 Mac의 카카오톡을 읽고, 따라가고, 보내는 macOS용 CLI**

의존성 0개 · 읽기는 로컬에서 · 파이썬 표준 라이브러리와 macOS API만

![macOS](https://img.shields.io/badge/macOS-15.7%20tested-000000?logo=apple&logoColor=white)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![의존성 0개](https://img.shields.io/badge/dependencies-0-2ea44f)
![로컬 읽기](https://img.shields.io/badge/reads-local-2ea44f)
![MIT](https://img.shields.io/badge/license-MIT-blue)

**윈도우라면 → [kanao-cli](https://github.com/sanguneo/kanao-cli)** · 같은 명령과 같은 JSON 봉투를 쓰는 윈도우용 자매 프로젝트

<img src="demo/shots/chat.webp" alt="합성 대화 화면 — 답장 인용과 멘션 표시" width="900">

<br><br>

[5분 안에](#5분-안에) · [무엇을 하나](#무엇을-하나) · [터미널에서 직접](#터미널에서-직접) ·
[이름 이력](#이름은-지금-이름일-뿐입니다) · [보내기](#보내기) ·
[에이전트](#ai-에이전트에-붙이기) · [못 하는 것](#못-하는-것)

<br>

*A macOS CLI that reads, follows and sends your own KakaoTalk, with no third-party
runtime dependencies. Korean-first documentation; structured commands answer in JSON.*

<br>

</div>

> [!NOTE]
> 이 문서의 스크린샷은 전부 `demo/mockup.py`가 메모리에 만든 **합성 데이터**입니다.
> 실제 대화도, 실존하는 사람도 한 줄 들어 있지 않습니다. macOS에서 지금 런타임으로
> 찍은 화면이며, 실제 계정을 촬영한 결과가 아닙니다.

---

## 5분 안에

macOS와 **Python 3.9 이상**, 이 Mac에 로그인한 카카오톡 계정의 로컬 데이터가 필요합니다.
macOS 15.7에서 확인했습니다. 저장소 디렉터리에서 실행하세요. `pip install`도 가상환경도
필수는 아닙니다. 서드파티 런타임 패키지를 쓰지 않습니다.

> [!TIP]
> **빠르게 하는 것은 파이썬 3.11+ 이고, `uv` 는 그것을 구하는 방법입니다.**
> 3.11 부터 `sqlite3` 가 메모리 이미지를 그대로 받는 `deserialize` 를 지원해서, 복호화
> 결과가 디스크를 아예 거치지 않습니다 — macOS 가 들고 오는 기본 Python 3.9 에서는
> 임시 파일을 거치므로 실행이 **3배 이상** 걸립니다(약 1.6s). uv 는 맥에 최신 파이썬을
> 얹는 가장 쉬운 수단일 뿐이고, 3.11+ 를 이미 쓰고 있다면 그대로 실행하는 편이 빠릅니다
> (uv 런처 자체는 명령마다 30 ms 남짓을 더합니다):
> ```bash
> uv run --python 3.12 -m kamado status
> uv run --python 3.12 -m kamado tui
> ```

먼저 **시스템 설정 → 개인정보 보호 및 보안**에서 도구를 실행하는 **터미널 앱**
(iTerm 또는 Terminal)에 권한을 주세요. 파이썬 파일에 주는 권한이 아닙니다.

| 권한 | 필요한 때 |
| --- | --- |
| **전체 디스크 접근 권한 (Full Disk Access)** | 카카오톡 앱 컨테이너의 데이터 읽기 |
| **손쉬운 사용 (Accessibility)** | 클라이언트를 조작해 메시지 보내기 |

권한이 없으면 macOS가 오류 대신 접근을 **멈춰 세울 수 있습니다.** 도구는 접근 시간 제한과
권한 검사로 무한 대기를 막고 `PERMISSION_REQUIRED`를 반환합니다. 응답의 `permission`은
`fullDiskAccess` 또는 `accessibility`입니다. `status`는 진단용 예외로, 실패해도
`ok: true`, `ready: false`, `detail`로 현재 상태를 설명할 수 있습니다.

```bash
python3 -m kamado status                      # 지금 무엇이 되는지
python3 -m kamado context --room "이름"        # 방 하나를 통째로 이해하기
python3 -m kamado follow --interval 1          # 새 메시지를 NDJSON으로
python3 -m kamado names --room-id 123 "닉"     # 그 이름을 쓴 계정 전부
python3 -m kamado send --room "이름" --dry-run "안녕"  # 보내기 전 확인
python3 -m kamado tui                         # 터미널에서 직접 읽기
```

**읽을 때는 카카오톡을 꺼도 됩니다.** 새 메시지가 로컬에 동기화되거나 실제로 전송되려면
클라이언트가 실행 중이어야 합니다.

`context` 한 번이면 방을 찾고, 최근 대화를 오래된 순으로 주고, **그 기록이 어디까지
닿는지**까지 말해줍니다. 방을 이미 안다면 `status` → `rooms` → `messages`로 세 번
물을 이유가 없습니다.

구조화된 명령의 성공은 `{"ok": true, ...}`, 실패는
`{"ok": false, "error": {"code": "...", "message": "..."}}`입니다.
처리된 실패는 종료 코드 1, 성공은 0, 잘못된 명령·인자는 2입니다. 인자 오류와 도움말은
일반 CLI 텍스트이고, `follow`는 한 줄씩 JSON, `tui`는 대화형 화면입니다.
**방·메시지·사용자 식별자는 문자열입니다** — 카카오톡 아이디는 2^53을 넘을 수 있습니다.
원시 SQL인 `query`는 SQLite 값을 그대로 돌려주므로 식별자는 `CAST(... AS TEXT)`로 읽으세요.

## 무엇을 하나

| | |
| --- | --- |
| **읽기** | 이 Mac과 계정 정보에서 열쇠를 유도해 로컬 DB를 복호화합니다. 읽기 전용, 서버 조회 없음 |
| **따라가기** | 계정 전체 DB와 로그의 변경을 확인해 새 메시지를 NDJSON으로 흘려보냅니다 |
| **읽는 화면** | 터미널 안의 대화방·대화·친구 목록·다이제스트, 답장과 멘션 표시 |
| **이름 이력** | 같은 닉네임을 쓰는 계정이 둘일 때, 이름이 아니라 `userId`로 구분 |
| **보내기** | 로그인된 클라이언트의 입력창에 글자를 넣고 전송 버튼 한 번. 포커스·커서·클립보드는 그대로 |
| **에이전트** | 한 번의 호출에 대화·커버리지·나를 부른 줄이 함께 |

## 이 도구가 푸는 문제

카카오톡은 대화를 이 Mac에 저장하지만 SQLCipher로 잠급니다. macOS의 열쇠는 실행 중인
프로세스에서 찾는 것이 아닙니다. **기기의 `IOPlatformUUID`와 로그인한 계정의 `userId`**로
PBKDF2-HMAC-SHA256을 100,000회 적용해 유도합니다. 힙 스캔도, 실행 중인 클라이언트도
읽기에는 필요 없습니다.

계정의 방·메시지·사용자는 다음 디렉터리의 **DB 하나**에 들어 있습니다.
파일 이름은 78자리 16진수입니다. 방마다 파일과 열쇠가 따로 있는 구조가 아닙니다.

```text
~/Library/Containers/com.kakao.KakaoTalkMac/Data/Library/Application Support/com.kakao.KakaoTalkMac/
```

주요 테이블은 `NTChatRoom`, `NTChatMessage`, `NTUser`, `NTOpenLink`, `NTChatContext`입니다.
확인한 클라이언트는 SQLCipher v3(페이지 1024바이트, 예약 영역 48바이트)를 쓰며,
도구는 v3/v4 형식을 자동 판별합니다. AES는 `ctypes`로 macOS **CommonCrypto**를 호출합니다.
별도 암호화 패키지를 설치하지 않습니다.

클라이언트가 큰 DB를 계속 쓰는 동안 원본을 끝까지 읽으려 하면 시작과 끝이 달라집니다.
그래서 먼저 APFS의 **copy-on-write 복제(`cp -c`)**로 고정된 사본을 만들고 복호화합니다.
복제는 파일 크기만큼 다시 쓰지 않습니다. 복제를 지원하지 않는 파일시스템에서는 일반
복사로 돌아갑니다. DB가 바뀌지 않으면 같은 프로세스 안에서 복호화된 스냅샷을 재사용합니다.
복제가 빠른 것이지, 큰 DB의 복호화 비용까지 없다는 뜻은 아닙니다.

## 명령 전체

`--room`은 방 제목, `--room-id`는 방 아이디입니다. 둘 중 하나만 지정합니다.
제목은 정확히 일치하는 방을 먼저 찾고, 없으면 부분 일치를 찾습니다. 후보가 여러 개면
`AMBIGUOUS_CHAT`과 `candidates`로 거절합니다. 첫 번째 방을 임의로 고르지 않습니다.

| 명령 | 인자·옵션 |
| --- | --- |
| `status` | 읽기 준비 상태: `ready`, `account`, `database`, `rooms`, `detail` |
| `verify` | 실제 복호화 확인: `verified`, `rooms`, `readableRooms` |
| `rooms` | `[--limit N] [--readable-only]` — 최근 활동 순 |
| `context` | `(--room TITLE \| --room-id ID) [--limit N]` — 기본 40개와 커버리지 |
| `messages` | `(--room TITLE \| --room-id ID) [--limit N] [--before-id ID]` — 기본 40개, 오래된 순 |
| `friends` | `[--favorites] [--with-phone]` — 전화번호는 요청할 때만 |
| `names` | `(--room TITLE \| --room-id ID) [NAME] [--user-id ID]` — 닉네임은 한 개 |
| `search` | `KEYWORD [--limit N]` — 기본 20개, 최신 일치부터 |
| `send` | `(--room TITLE \| --room-id ID) TEXT [--dry-run] [--open]` |
| `follow` | `[--room-id ID ...] [--interval SECONDS]` — 방마다 `--room-id` 반복, 기본 1초 |
| `query` | `SQL [--room-id ID]` — 읽기 전용 SQL |
| `schema` | `[--room-id ID]` — 테이블과 열 |
| `daemon` | `(start [--foreground] \| stop \| status)` — 백그라운드 상주 데몬 (쿼리 즉각 응답) |
| `tui` | 대화형 POSIX 터미널에서 실행 |

macOS에서는 한 열쇠로 모든 저장된 방을 엽니다. `readable`은 모두 참이고,
`--readable-only`로 방이 더 생기거나 열쇠가 새로 로드되지는 않습니다.
`query`와 `schema`의 `--room-id`는 호환용이며 같은 계정 DB를 봅니다.
**SQL을 그 방으로 제한하지 않습니다.** 직접 `WHERE chatId = ...`를 쓰세요.

```bash
python3 -m kamado messages --room-id 123 --before-id 9876 --limit 100
python3 -m kamado friends --favorites
python3 -m kamado search "회의 시간" --limit 20
python3 -m kamado schema
python3 -m kamado query 'SELECT COUNT(*) AS count FROM NTChatMessage'
python3 -m kamado follow --room-id 123 --room-id 456
```

`follow`는 시작 시 `event: "watching"`, 이후 `event: "message"`를 한 줄씩 내보냅니다.
기존 기록을 재생하지 않고 시작 이후 새 기록을 따라갑니다. 로컬 저장분을 보는 것이므로
검색 결과가 비었다고 그런 대화가 없었다는 증거는 아닙니다.

## 터미널에서 직접

```bash
python3 -m kamado tui
```

`termios`를 쓰는 POSIX 터미널 UI입니다. 파이프로 연결하지 말고 Terminal이나 iTerm에서
직접 실행하세요. 읽기는 클라이언트가 닫혀 있어도 되고, 입력창에서 보내려면 실행 중인
카카오톡과 손쉬운 사용 권한이 필요합니다.

![합성 대화방 목록](demo/shots/rooms.webp)

대화방 목록, 대화, 친구 목록, 그리고 하단의 입력창. 글자를 치면 메시지가 되고 `/`로
시작하면 명령이 됩니다. 답장은 원문 인용과 함께, 나를 `@`로 부른 줄은 별도 표시로
보여줍니다. 과거를 읽는 중이면 새 메시지가 와도 화면을 빼앗지 않습니다. 화자마다 이름
색이 달라서, 여러 사람이 오가는 방에서도 이름을 읽기 전에 누가 말했는지 구분됩니다.

밝은 터미널과 어두운 터미널은 알아서 가립니다. `COLORFGBG`와 macOS의
`AppleInterfaceStyle`을 차례로 보고, 밝은 화면에는 명암비 3.8:1 이상을 지키는 별도
팔레트를 씁니다. 추측이 틀렸다면 못박으면 됩니다.

```bash
KAMADO_THEME=light python3 -m kamado tui    # dark 도 됩니다
```

| 환경변수 | 하는 일 |
| --- | --- |
| `KAMADO_THEME` | `light`/`latte`/`white` 또는 `dark`/`mocha`/`black`. 없으면 자동 판별 |
| `KAMADO_TUI_MARGIN` | 오른쪽 여백 칸 수(0–10, 기본 0). 스크롤바가 마지막 칸을 덮을 때 |
| `KAMADO_TUI_SIZE` | `110x30` 처럼 화면 크기를 고정. 화면을 그대로 찍어 볼 때 씁니다 |
| `KAMADO_FORCE_COLOR` | 파이프로 연결해도 색을 켭니다. `NO_COLOR`가 있으면 그쪽이 이깁니다 |

<table>
<tr>
<td width="50%"><img src="demo/shots/unseen.webp" alt="합성 새 메시지 알림 화면"></td>
<td width="50%"><img src="demo/shots/sent.webp" alt="합성 전송 확인 표시"></td>
</tr>
<tr>
<td align="center"><sub>과거를 읽는 중이면 상태줄로만 알립니다</sub></td>
<td align="center"><sub>*는 제출, ✓는 로컬 기록에서 확인, ?는 아직 확인하지 못한 전송</sub></td>
</tr>
</table>

![합성 다이제스트](demo/shots/digest.webp)

`/sum 30m`이나 `/sum 2h`는 **요약하는 척하지 않습니다.** 이 프로세스 안에는 모델이
없으므로 구간의 활동량·화자·단어·대표 문장을 뽑을 뿐입니다. `/export`는 다이제스트의
원문을 내보냅니다. `/older`는 이전 기록, `/real 닉네임`은 이름 이력, `/back`은 뒤로,
`/quit`은 종료입니다. 목록에서 `f`는 친구, `/`는 필터입니다.

<table>
<tr>
<td width="50%"><img src="demo/shots/friends.webp" alt="합성 친구 목록"></td>
<td width="50%"><img src="demo/shots/filter.webp" alt="합성 목록 필터"></td>
</tr>
</table>

## 이름은 지금 이름일 뿐입니다

`NTUser`의 이름은 현재 값입니다. 옛 닉네임으로 쓴 메시지도 현재 이름으로 보일 수
있습니다. 두 계정이 같은 닉네임을 쓸 수는 있어도 **`userId`를 나눠 갖지는 못합니다.**
화자를 구분할 때는 이름이 아니라 아이디를 보세요.

```bash
python3 -m kamado names --room-id 123             # 관측한 개명 이력이 있는 계정
python3 -m kamado names --room-id 123 "닉네임"     # 그 이름을 쓴 계정 전부
python3 -m kamado names --room-id 123 --user-id 456
```

이력은 입장·퇴장·초대 피드와 도구가 읽으며 관측한 이름을 합칩니다. 클라이언트의 모든
개명을 복원하는 기능은 아닙니다. 그냥 바꾼 이름은 도구가 그 뒤 처음 읽었을 때부터
알 수 있습니다. 닉네임 검색에 계정이 둘 이상 나오는 것은 오류가 아니라 답입니다.

![합성 데이터의 같은 이름을 쓰는 계정 두 개](demo/shots/real.webp)

> [!IMPORTANT]
> 이름 이력은 `~/Library/Application Support/kamado-cli/names.jsonl`에 남습니다.
> **실제 닉네임이 들어가는 파일입니다.** DB 원본은 읽기 전용이어도 이 관측 이력은
> 도구가 직접 저장합니다. 방 목록 위치 캐시는 같은 디렉터리의 `rows.json`이며 제목의
> 해시와 행 번호를 저장합니다. 열쇠나 프로세스 메모리 주소를 저장하는 캐시는 없습니다.

## 보내기

보내기는 **실행 중이며 로그인된 카카오톡**을 사용합니다. macOS Accessibility의
`AXUIElement`로 대상 방의 입력창을 찾고 `AXValue`에 글자를 넣은 뒤, 그 창의
**전송 버튼을 누릅니다.** 앱을 활성화하거나 포커스를 가져오지 않고, 마우스 커서와
클립보드도 건드리지 않습니다.

Return을 보내지 않는 이유가 있습니다. 클라이언트는 `AXValue`로 넣은 글자를 타이핑으로
치지 않아서, 그 위에 올린 Return은 무시됩니다. 버튼을 누르는 것이 실제로 보내는
동작입니다. 전송 버튼이 없는 클라이언트를 위해 그 프로세스에만 Return을 보내는
대비책이 남아 있습니다.

```bash
python3 -m kamado send --room "이름" --dry-run "확인만"
python3 -m kamado send --room "이름" "보낼 말"
python3 -m kamado send --room "이름" --open "창이 닫혀 있어도"
```

기본값은 이미 열린 채팅 창을 찾습니다. `--open`은 닫힌 방을 목록에서 열도록 허용합니다.
목록의 접근성 제목을 먼저 맞추고, 열린 창의 제목도 확인합니다. 목록 탐색 중 방이 열리면
읽음 처리될 수 있습니다. `--dry-run --open`도 **방은 열 수 있고**, 글자를 넣고 전송하는
것만 하지 않습니다. 새로 연 채팅 창은 최소화하고 메인 창의 최소화 상태는 복구합니다.
동일한 제목의 방이 여러 개면 `--room-id`만으로도 안전하게 구별할 수 없어 거절합니다.

> [!TIP]
> **가상 데스크탑(Spaces)을 쓸 때의 팁**  
> macOS Accessibility API는 현재 보고 있는 데스크탑(Space)에 없는 윈도우를 감지하지
> 못합니다. 터미널을 전체화면으로 쓰거나 다른 Space에서 작업할 때 `WINDOW_NOT_FOUND`가
> 발생하면, Dock의 카카오톡 아이콘을 **우클릭 → 옵션 → 대상 지정: '모든 데스크탑'**으로
> 설정해 두세요. 어떤 Space에서도 화면 전환 없이 백그라운드로 안전하게 전송됩니다.

> [!IMPORTANT]
> `submitted: true`는 **입력창이 비워졌다**는 뜻이지 서버 전달 확인이 아닙니다.
> `context`나 `messages`로 내 계정의 새 기록을 되읽으세요. 불확실한 전송을 자동으로
> 재시도하면 같은 말을 두 번 보낼 수 있습니다.

쓰다 만 초안은 **지우고 보냅니다.** 조용히 지우지는 않습니다. 실제 전송에는
`clearedDraft`, 미리보기에는 `wouldClearDraft`가 붙습니다. 초안이 있으면 먼저 확인하세요.

- 다른 사람에게는 **받는 방과 내용에 대한 명시적 허락**을 받은 뒤 보내세요.
- 호출하는 쪽에서 전송 간격을 **최소 2초**로 유지하세요. 도구가 강제하는 제한은 아닙니다.
- 실제 전송 테스트는 **나와의 채팅**이나 테스트를 명시적으로 허락받은 방에서만 하세요.

## AI 에이전트에 붙이기

```bash
python3 install.py
python3 install.py --target "$HOME/.claude/skills/kamado-cli" --force
```

기본 위치는 `~/.agents/skills/kamado-cli`입니다. `SKILL.md`, 런처, 파이썬 런타임 소스,
라이선스를 복사합니다. 설치본은 자립형이라 원본 체크아웃을 옮기거나 지워도 됩니다.
기존 디렉터리는 `--force`가 있어야 갱신하며, 배포 파일 외의 사용자 파일은 남깁니다.

> "카톡에서 지수랑 최근 대화 요약해줘."
> "회의 시간 얘기한 카톡 찾아줘."
> "이 대화 읽고 내 말투로 답장 초안 써줘. 보내지는 마."

`context`에는 메시지뿐 아니라 **`coverage`**(로컬 한정인지, 최신 알려진 기록까지 닿았는지,
더 오래된 기록이 있는지)와 **`attention`**(나를 부른 메시지, 내 말에 달린 답장,
`@전체`의 아이디 목록)이 함께 옵니다. `reachesLatestKnown: null`은 최신 여부를
판단할 근거가 없다는 뜻입니다. 메시지의 답장·멘션 정보는 해당할 때만 키가 붙습니다.

![합성 데이터로 만든 에이전트 JSON](demo/shots/cli.webp)

## 못 하는 것

- **로컬에 있는 것만.** 서버의 전체 이력을 내려받거나 빈 기록을 복원하지 않습니다.
- **첨부는 안 보입니다.** 사진·동영상·파일·이모티콘은 종류만 알고 내용을 보지는 못합니다.
- **글자만 보냅니다.** 첨부 전송, 로그인 자동화, 프로토콜 재구현, 기록 수집은 없습니다.
- **macOS 전용입니다.** 다른 운영체제는 지원하지 않습니다.
- **일관성이 없으면 거절합니다.** 읽기가 충돌하면 `BUSY` 등으로 실패하며 완전한 기록인 척하지 않습니다.
- **무결성은 확인하지만 진위는 아닙니다.** SQLite 무결성과 로그 체크섬은 검증하지만 SQLCipher 페이지별 HMAC은 검증하지 않습니다.
- **평문 임시 파일이 생길 수 있습니다.** Python 3.9/3.10 등 SQLite `deserialize`가 없는 환경에서는 복호화 결과가 임시 파일을 거칩니다. 삭제는 안전 소거가 아닙니다. 해당 API가 있으면 평문 DB는 메모리에서 엽니다.
- **클라이언트가 바뀌면** 스키마나 접근성 구조가 달라져 읽기·보내기가 실패할 수 있습니다.

## 개발

```bash
python3 -m unittest discover -t . -s tests
```

테스트는 합성 데이터를 씁니다. **실제 대화·연락처·열쇠는 저장소에도, 테스트에도,
로그에도 넣지 않습니다.** 이 문서의 화면은 `demo/mockup.py`가 지어낸 프로필로 만든 것입니다.
다시 찍는 방법은 [demo/README.md](demo/README.md)를 보세요.
코드를 고칠 생각이라면 `AGENTS.md`를 먼저 읽으세요.

## 출처와 라이선스

macOS 키 유도와 접근성 전송은
**[Brian ByungHyun Shin](https://github.com/brianshin22)** 및
**[Silver Flight Group](https://github.com/silver-flight-group)**의
[kakaocli](https://github.com/silver-flight-group/kakaocli)를 참고했습니다.
이 도구는 포커스·커서·클립보드를 건드리지 않는 전송 원칙을 유지합니다.

MIT. [LICENSE](LICENSE) 참조.

## 면책

카카오와 제휴하거나 보증받지 않은 독립적인 비공식 프로젝트입니다. "카카오톡"과
"KakaoTalk"은 카카오의 상표입니다. 사용자의 컴퓨터에 이미 저장된 데이터를 읽고,
전송 시에는 사용자가 로그인해 둔 클라이언트를 조작합니다. 도구가 카카오 API를 직접
호출하거나 프로토콜을 구현하거나 애플리케이션을 변조하지 않습니다. 실제 전송에 필요한
네트워크 통신은 카카오톡 클라이언트가 합니다.

*Not affiliated with or endorsed by Kakao Corp. Reads data already on your own
Mac and drives your own signed-in client; no direct Kakao API calls, no protocol
reimplementation, no patching of the application.*
