# 기여 가이드

nichirin에는 Windows 빌드(`windows/`, `kanao`)와 macOS 빌드(`macos/`, `kamado`)가 함께 있습니다.
대부분은 한 플랫폼만 씁니다. Mac을 쓰면 Mac 쪽을, Windows를 쓰면 Windows 쪽을 작업하고 테스트하면
충분합니다.

## 작업 흐름

1. 내 플랫폼의 디렉터리에서 작업합니다.
2. 내 플랫폼의 테스트를 돌립니다.

   ```bash
   cd macos && python3 -m unittest discover -t . -s tests     # Mac
   cd windows && python -m unittest discover -t . -s tests    # Windows
   ```

3. 바꾼 것이 **다른 플랫폼에도 닿는데 그쪽에서 테스트할 수 없다면**, 짧은 이슈를 하나 남겨 주세요.
   그쪽 플랫폼을 쓰는 사람이 이어서 확인합니다.

## 다른 플랫폼에 닿는 변경

이런 것을 바꾸면 반대쪽도 영향을 받습니다.

- `kakaocommon/`: 두 빌드가 같이 쓰는 코드
- `openapi.json`: 두 `serve`가 답하는 계약
- 루트 진입점: `nichirin/`, `kanao/`, `kamado/`, `kakaoread/`, `install.py`
- 두 빌드가 똑같이 내야 하는 것: 명령과 옵션, JSON 봉투의 키, 오류 코드
- 한쪽에 넣은 기능을 다른 쪽에도 맞춰야 하는 경우

한 빌드의 디렉터리 안에서만 바뀌고 출력도 달라지지 않는다면 이슈는 필요 없습니다.

## 확인 요청 이슈

[새 이슈](https://github.com/sanguneo/nichirin/issues/new/choose)에서 **Windows 확인 요청**이나
**macOS 확인 요청**을 고르면 됩니다. 라벨(`check: windows`, `check: macos`)은 자동으로 붙습니다.
세 줄이면 충분합니다.

- 무엇을 바꿨는지: PR이나 커밋 링크
- 그쪽에서 확인할 것: 돌려 볼 명령, 기대하는 결과
- 내 쪽에서 확인한 것: 돌린 테스트와 결과

PR 본문에 "Windows는 확인하지 못했습니다, #번호"처럼 이슈를 적어 두면 리뷰하는 사람도 압니다.

## 강제는 아닙니다

이슈를 꼭 열어야 PR을 받는 것은 아닙니다. 잊었다면 PR 본문에 "macOS 미확인" 한 줄이면 됩니다. OmO 같은
에이전트와 함께 작업한다면, 에이전트가 [AGENTS.md](AGENTS.md)를 읽고 필요할 때 이슈 초안을 알아서
제안합니다.

## 더 자세한 규칙

각 빌드의 기여 규칙과 증거 기준은 플랫폼 문서에 있습니다.

- Windows: [windows/CONTRIBUTING.md](windows/CONTRIBUTING.md), [windows/AGENTS.md](windows/AGENTS.md)
- macOS: [macos/CONTRIBUTING.md](macos/CONTRIBUTING.md), [macos/AGENTS.md](macos/AGENTS.md)

어느 쪽이든 실제 대화, 이름, 전화번호, 계정 경로, 키와 토큰은 테스트 데이터, 로그, 스크린샷, PR에
넣지 않습니다. 재현은 합성 데이터로 합니다.
