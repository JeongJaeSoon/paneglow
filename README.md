# paneglow

> Codex Micro 매크로패드로 병렬 Claude Code Desktop 세션을 보고 다룬다.

6개 Agent 키가 **최근 세션들의 상태**를 색으로 보여주고, 누르면 그 세션이 앱에서 열린다.
테두리는 **지금 6키가 Claude 것인지 Codex 것인지**를 말하고, 화면 밖에서 나를 기다리는
것이 있으면 깜빡인다.

macOS 전용. Codex Micro(VID `0x303A` / PID `0x8360`)가 필요하다. 유선·무선 둘 다 된다.

```
        ◯ 노브      [A1] [A2]      ● 스틱          A1~A6  최근 세션 6개
        [A3] [A4]  [A5] [A6]                      누르면  그 세션이 앱에서 열린다
        [C1] [C2 ] [C3 ] [C4]                     C1~C7   건드리지 않는다
        ⋮LED ◉터치  [═ C5+C6 ═]  [C7]             테두리   누구 것인가 + 알림
```

## 상태

**설계 재확정 완료, 하드웨어 연결 착수 전.** 관문 검증 셋이 전부 닫혔다.

| 모듈 | |
|---|---|
| ✅ `state` `store` `render` `protocol` `config` | 순수 로직. 테스트 101개 |
| ⬜ `sessions` | 살아 있는 세션 목록 |
| ⬜ `slots` | 세션 → 6슬롯 배정. 순수 |
| ⬜ `deeplink` | 세션을 앱에서 연다 |
| ⬜ `hook` [#5](../../issues/5) | 훅 → 상태 분류 |
| ⬜ `pad` [#10](../../issues/10) | IOKit |
| ⬜ `cli` [#11](../../issues/11) | 첫 통합 |
| ❌ `iterm` | 삭제 예정 — pane 도 탭도 안 쓴다 |

실기와 앱 번들로 확인한 것 — 근거는 [하드웨어 노트](docs/hardware-notes.md)와
[딥링크 실측](docs/verification/deeplink.md):

| | |
|---|---|
| 세션 딥링크 | `claude://claude.ai/claude-code-desktop/<id>`. 다른 형태 6개는 실패, 그중 둘은 **조용히** |
| `device.status` 왕복 | USB · BLE 양쪽 |
| A1~A6 개별 색 · 키별 효과 | USB · BLE 양쪽 |
| LED 효과 5종 | 켜짐 · 회전 · 무지개 · 깜빡임 · 펄스. **기기가 스스로 돌린다** |
| 벤더가 쓰는 존 | 6키와 테두리는 쓴다. **C1~C7 은 안 건드린다** |
| 테두리 되찾기 | 벤더 명령의 ACK 를 보고 이벤트 기반으로 |
| TCC 권한 | **필요 없음** |

- [설계 문서](docs/superpowers/specs/2026-08-02-desktop-session-model-design.md) — 결정과 근거
- [훅 이벤트 검증](docs/verification/hook-events.md) — 설계 전제 셋이 뒤집힌 기록
- [기존 설계](docs/design.html) — iTerm2 pane 모델. **대체됨**

```bash
python -m pytest tests/ -m "not integration"   # 하드웨어 없이
python -m pytest tests/ -m integration         # 실기 필요
```

## 왜 만드나

기존 도구([FreeMicro](https://github.com/eliBenven/freemicro))는 세션을 **디렉터리 단위로 합친다.**
한 repo의 워크트리 여러 개에서 병렬로 돌리면 전부 키 하나에 뭉쳐서, 어느 세션이 나를
기다리는지 알 수 없다. paneglow는 **세션 하나에 키 하나**를 준다.

## 알아둘 것

- **Layer 1에서만 동작한다.** 터치 센서로 Layer 2+로 넘기면 조용해지고 평범한 매크로패드가 된다.
- **Codex 앱과 함께 쓸 수 있다.** Codex를 볼 때는 6키를 벤더에게 넘기고, 테두리가 지금 어느
  쪽 상태를 보고 있는지 알려준다. 벤더가 테두리를 덮으면 되찾는다.
- **C1~C7 은 사용자 것이다.** paneglow는 이 키들의 백라이트도 키맵도 건드리지 않는다.
- **승인·거절은 없다.** 데스크톱 앱에는 세션에 응답을 보낼 수단이 없고, 남은 길은
  Accessibility 권한뿐이라 "권한 없이 동작한다"는 원칙과 충돌한다.
- **특별한 권한은 필요 없다.** 벤더 채널은 VID/PID로 이 기기만 열면 Input Monitoring 없이
  읽고 쓴다. 상주 데몬은 필요하다.
- **유선·무선 둘 다 된다.** 프레이밍이 다르지만(63B/64B) `Transport` 속성으로 자동 판별한다.
  단, 그 값은 `"BLE"`가 아니라 `"Bluetooth Low Energy"` 다.

> ⚠️ Work Louder Input 앱의 **Setup 탭에서 펌웨어 플래싱을 누르지 말 것.**
> 목록에 이 기기용이 없고, BLE 연결 중에는 앱이 "bootloader mode"로 오진한다.
> 자세한 내용은 [하드웨어 노트](docs/hardware-notes.md).

## 라이선스

MIT
