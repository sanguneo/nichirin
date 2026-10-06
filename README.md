# kanao-cli · kamado-cli

로컬 카카오톡 데스크톱 클라이언트가 이미 저장한 대화를 읽고, 명시적으로 허락한 글만
클라이언트를 통해 보내는 도구입니다. 의존성 없는 Python이고, 두 플랫폼 빌드가 한
저장소에 있습니다.

| 플랫폼 | 도구 | 디렉터리 | 문서 |
| --- | --- | --- | --- |
| Windows | `kanao-cli` (`python -m kanao`) | [`windows/`](windows/) | [windows/README.md](windows/README.md) |
| macOS | `kamado-cli` (`python3 -m kamado`) | [`macos/`](macos/) | [macos/README.md](macos/README.md) |

두 빌드의 `serve`는 같은 HTTP + SSE 계약, 저장소 루트의
[`openapi.json`](openapi.json)에 답합니다.

## 설치와 실행

합치기 전과 같은 명령을 저장소 루트에서 그대로 씁니다. `install.py`는 이 컴퓨터의
빌드(맥이면 `macos/`, 그 밖에는 `windows/`)를 골라 설치합니다.

```powershell
# Windows
python install.py
python -m kanao status
```

```bash
# macOS
python3 install.py
python3 -m kamado status
```

`windows/`나 `macos/` 안에서 실행해도 같습니다. 설치되는 스킬 이름(`kanao-cli`,
`kamado-cli`)과 실행 스크립트 경로도 합치기 전과 같습니다. 이미 설치한 스킬은 그대로
동작하고, 업데이트할 때만 `install.py --force`를 다시 실행하면 됩니다.

## 저장소 이전

이 저장소는 `sanguneo/kanao-cli`와 `sanguneo/kamado-cli`를 합친 것입니다. 두 저장소의
커밋 기록은 각각 `windows/`, `macos/` 아래에 그대로 남아 있습니다
(`git log --follow windows/kanao/reader.py`).
