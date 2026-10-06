# demo — 소개용 목업

실제 대화로 스크린샷을 찍으면 남의 대화를 공개하게 됩니다. 그래서 이 폴더는
**전부 지어낸 프로필**을 메모리에 만들어 인터페이스만 보여줍니다. 여기 있는 이름,
메시지, 방은 어느 것도 실존하지 않고, 서명된 계정이나 실제 데이터베이스는 열지
않습니다.

## 장면 띄우기

```powershell
python demo/mockup.py rooms     # 대화방 목록
python demo/mockup.py chat      # 대화 (답장 인용, 멘션 표시, 작성 중인 초안)
python demo/mockup.py digest    # /sum 30m 다이제스트
python demo/mockup.py friends   # 친구 목록 (★ 즐겨찾기, 상태 메시지)
python demo/mockup.py real      # /real — 같은 닉네임을 쓰는 계정 두 개
python demo/mockup.py filter    # / 로 목록 걸러보기
python demo/mockup.py sent      # 클라이언트가 되돌려준 전송 확인 ✓
python demo/mockup.py unseen    # 위로 스크롤한 채 새 메시지가 쌓일 때
python demo/mockup.py cli       # 에이전트가 받는 JSON (TUI 아님, 그냥 출력)
```

창 크기는 **112x32**가 의도한 모양입니다. 끄는 법은 실제와 같습니다 — 목록에서는
`esc`나 `q`, 대화 안에서는 `ctrl+c` 또는 `/quit`.

터미널 없이 프레임만 보고 싶다면:

```powershell
python demo/mockup.py --print chat
```

## 직접 캡처

가장 안전한 방법은 장면을 띄워놓고 **Win+Shift+S**(또는 `Alt+PrintScreen`)로 창을
찍는 것입니다.

자동으로 찍고 싶다면:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File demo/capture.ps1 -Scene chat
```

`demo/shots/<scene>.png`로 저장됩니다. 이 스크립트는 **어떤 프로세스도 종료하지
않습니다.** Windows Terminal은 모든 창을 한 프로세스에서 돌리기 때문에, "데모 창만
닫겠다"고 프로세스를 종료하면 작업 중이던 다른 창까지 전부 닫힙니다 — 실제로 한 번
그렇게 닫혔습니다. 데모 창은 찍은 뒤에도 열어두니 직접 닫으세요.

## 소개 문구

`demo/INTRO.md`에 한 줄·세 줄·캡션·SNS용 문구가 있습니다.
