# paneglow

> Codex Micro 매크로패드로 iTerm2 안의 병렬 Claude Code 세션을 보고 다룬다.

6개 Agent 키가 **현재 탭의 pane별 Claude Code 상태**를 색으로 보여주고, 누르면 그 pane으로 이동한다.
테두리 LED는 **지금 화면에 안 보이는 곳**에서 나를 기다리는 것이 있는지 알려준다.

macOS 전용. Codex Micro(VID `0x303A` / PID `0x8360`)가 필요하다.

```
        ◯ 노브      [A1] [A2]      ● 스틱          A1~A6  현재 탭의 pane
        [A3] [A4]  [A5] [A6]                      C7+A n  탭 n 으로 전환
        [C1] [C2✓] [C3✗] [C4]                     C2/C3   보고 있는 pane 승인·거절
        ⋮LED ◉터치  [═ C5+C6 ═]  [C7]             테두리   화면 밖의 대기 알림
```

## 상태

**Phase 1 진행 중.** 계산하는 부분은 끝났고 하드웨어에 연결하는 부분이 남았다.
관문 검증(Phase 0)은 통과했다.

| 모듈 | |
|---|---|
| ✅ `state` `store` `render` `protocol` `config` | 순수 로직. 테스트 102개 |
| ✅ `iterm` | pane 발견·순서·포커스 |
| ⬜ `hook` [#5](../../issues/5) | 훅 → 상태 분류 |
| ⬜ `pad` [#10](../../issues/10) | IOKit. 여는 코드는 검증됨 |
| ⬜ `cli` [#11](../../issues/11) | 첫 통합 |

실기로 확인한 것 — 근거는 [하드웨어 노트](docs/hardware-notes.md):

| | |
|---|---|
| `device.status` 왕복 | USB · BLE 양쪽 |
| A1~A6 점등 | USB · BLE 양쪽. 빈 슬롯은 소등 |
| 전송 자동 판별 | IOKit `Transport` 속성 |
| pane 순서 | 균등 분할 · 구분선을 드래그한 불균등 분할 |
| TCC 권한 | **필요 없음** (권한을 끈 상태로 확인) |

- [설계 문서](docs/design.html) — 결정과 근거
- [훅 이벤트 검증](docs/verification/hook-events.md) — 설계 전제 셋이 뒤집힌 기록

```bash
python -m pytest tests/ -m "not integration"   # 하드웨어·iTerm2 없이
python -m pytest tests/ -m integration         # 실기 필요
```

## 왜 만드나

기존 도구([FreeMicro](https://github.com/eliBenven/freemicro))는 세션을 **디렉터리 단위로 합친다.**
한 repo에서 pane을 4~6개 띄워 병렬로 돌리면 전부 키 하나에 뭉쳐서, 어느 pane이 나를
기다리는지 알 수 없다. paneglow는 **pane 하나에 키 하나**를 준다.

## 알아둘 것

- **Layer 1에서만 동작한다.** 터치 센서로 Layer 2+로 넘기면 조용해지고 평범한 매크로패드가 된다.
- **Codex 앱과 함께 쓸 수 있다.** Codex를 볼 때는 6키를 벤더에게 넘긴다. 다만 완전한 격리는
  불가능하다 — [설계 문서 §03](docs/design.html) 참조.
- **특별한 권한은 필요 없다.** 벤더 채널은 VID/PID로 이 기기만 열면 Input Monitoring 없이
  읽고 쓴다. iTerm2 Python API도 유닉스 소켓이라 TCC를 안 탄다. 상주 데몬은 필요하다.
- **유선·무선 둘 다 된다.** 프레이밍이 다르지만(63B/64B) `Transport` 속성으로 자동 판별하므로
  터치 센서로 전환해도 손댈 것이 없다.

> ⚠️ Work Louder Input 앱의 **Setup 탭에서 펌웨어 플래싱을 누르지 말 것.**
> 목록에 이 기기용이 없고, BLE 연결 중에는 앱이 "bootloader mode"로 오진한다.
> 자세한 내용은 [하드웨어 노트](docs/hardware-notes.md).

## 라이선스

MIT
