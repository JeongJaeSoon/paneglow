# Paneglow

> Codex Micro로 병렬 Claude Code Desktop 세션의 상태를 보고 바로 연다.

6개 Agent 키가 최근 세션의 상태를 색으로 보여주고, 누르면 그 세션을 Claude Desktop에서
연다. 테두리는 지금 Agent 키를 Claude와 Codex 중 누가 소유하는지 보여주며, 화면 밖 세션이
응답을 기다리거나 오류 상태이면 효과로 알린다.

macOS 전용이며 Python 3.10 이상, Claude Desktop, Codex Micro(VID `0x303A` / PID
`0x8360`)가 필요하다. USB와 BLE를 모두 지원하고 별도 TCC 권한은 요구하지 않는다.

```text
        ◯ 노브      [A1] [A2]      ● 스틱          A1~A6  최근 세션 6개
        [A3] [A4]  [A5] [A6]                      누르면  그 세션이 앱에서 열린다
        [C1] [C2 ] [C3 ] [C4]                     C1~C7   건드리지 않는다
        ⋮LED ◉터치  [═ C5+C6 ═]  [C7]             테두리   소유권 + 알림
```

## 설치와 첫 실행

런타임 외부 의존성은 없다. 저장소를 계속 둘 위치에서 가상환경을 만들고 설치한다.

```bash
git clone https://github.com/JeongJaeSoon/paneglow.git
cd paneglow
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Claude Code 훅을 기존 설정에 병합하고 로그인 자동 시작을 설치한다.

```bash
.venv/bin/paneglow install-hooks
.venv/bin/paneglow autostart install
.venv/bin/paneglow autostart status
.venv/bin/paneglow status
.venv/bin/paneglow doctor
```

`install-hooks`는 `~/.claude/settings.json`의 기존 항목을 보존하고, 원본이 있으면
`settings.json.paneglow.bak`을 만든다. 같은 환경에서 다시 실행해도 중복 설치되지 않는다.
이전 Paneglow 버전이 남긴 잘못된 base-Python 훅도 현재 가상환경 경로 하나로 이관한다.
훅에는 가상환경 Python의 절대 경로가 기록되므로, 훅을 사용하는 동안 저장소와 `.venv`를
옮기거나 지우지 않는다.

설치되는 이벤트는 `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionDenied`, `Notification`, `Stop`, `StopFailure`,
`PreCompact`, `SessionEnd`의 정확히 11개다.

`doctor`가 실제 딥링크 매핑까지 확인하려면 Claude Desktop에서 Claude Code 세션 하나 이상이
살아 있어야 한다. 발견된 **모든** live 세션에 Desktop mapping이 있어야 통과하므로, mapping이
없는 CLI 세션이 함께 살아 있으면 실패로 보고한다. 세션이 없으면 매핑을 실행해 보지 못했다는
경고를 낸다. 패드가 없어도 데몬은 상태 수집을 계속하며 재연결을 시도하지만, `doctor`의 패드
검사는 실패한다.

`autostart install`은 현재 사용자의
`~/Library/LaunchAgents/io.github.jeongjaesoon.paneglow.plist`를 owner-only `0600`으로
설치하고 daemon을 시작한다. launchd는 foreground `run` 프로세스를 직접 소유하며, 정상적인
`stop` 뒤에는 다시 띄우지 않고 runtime 실패만 제한적으로 재시작한다. plist에도 현재 가상환경
Python의 절대 경로가 기록되므로 저장소나 `.venv`를 옮겼다면 새 위치에서 `install-hooks`와
`autostart install`을 다시 실행한다.

자동 시작을 설치하지 않고 이번 로그인에서만 쓰려면 기존 수동 시작도 가능하다.

```bash
.venv/bin/paneglow start
```

종료와 상태 확인은 하드웨어를 열지 않는 별도 명령이다. 자동 시작이 설치된 상태의 `stop`은
현재 daemon만 정상 종료하며 다음 로그인에는 다시 시작된다. 현재 로그인에서 `start`하면
detached process 대신 설치된 LaunchAgent를 다시 시작한다. 자동 시작 자체를 없애려면
`autostart uninstall`을 사용한다.

```bash
.venv/bin/paneglow status
.venv/bin/paneglow stop
.venv/bin/paneglow autostart uninstall
```

데몬 로그는 `~/.paneglow/logs/daemon.log`에 기록된다.

Paneglow가 받은 Agent-key 메시지의 중복·순서와 각 dispatch 시점의 owner 표본을 확인할 때만
일회성 trace를 실행한다. 데몬이 실행 중이어야 하며, 기본값은 5초·최대 32개 이벤트다.

```bash
.venv/bin/paneglow trace-input --seconds 5
```

trace는 평소에는 비활성화되어 있고 영구 입력 로그를 남기지 않는다. 공개 결과는 schema와
실제 수집 시간·잘림 여부, 그리고 각 이벤트의 순번, 상대 시각, `A1`~`A6` 또는 `other`로
정규화한 키, `0`·`1` 또는 `other`인 action, owner, Layer 1·slot 상태, 처리 결과, trace-local
연결 순번으로 제한한다. raw HID payload, 장치 식별자, 경로, 세션 ID, bundle ID와 wall-clock
시각은 내보내지 않는다. 한 번에 최대 10초·64개 이벤트까지만 허용한다. 이 결과만으로 물리
bounce의 원인이나 정확한 앱 focus 전환 시각을 판별할 수는 없다.

## 선택 설정

설정 파일은 `~/.paneglow/config.json`이다. 파일이 없으면 측정된 기본값을 사용하며, 잘못된
항목 하나는 그 항목만 기본값으로 되돌리고 데몬 기동을 막지 않는다.

```json
{
  "gate": {
    "mode": "frontmost",
    "own_when": ["com.anthropic.claudefordesktop"],
    "yield_to": ["com.openai.codex"]
  },
  "slots": {"order": "recent_sticky"},
  "colors": {
    "idle": "#FFFFFF",
    "working": "#304FFE",
    "waiting": "#FF6D00",
    "done": "#00FF4C",
    "error": "#FF0033"
  },
  "layer_gate": {"underglow": "keep"},
  "underglow": {
    "claude": "#FF6D00",
    "codex": "#304FFE",
    "effects": {"normal": "solid", "alert": "blink", "fault": "rainbow"},
    "scope": "outside",
    "reclaim_delay_ms": 200
  },
  "state": {
    "ttl_minutes": 30,
    "done_fade_seconds": 180,
    "working_max_seconds": 900
  },
  "timing": {"poll_ms": 250, "status_poll_ms": 1000}
}
```

`colors`는 6개 키에 표시되는 상태 색이다. 다섯 상태 중 바꾸고 싶은 것만 적으면 되고,
`"#RRGGBB"` 문자열과 24비트 정수를 모두 받는다. 잘못된 값은 그 상태만 위의 기본값으로
되돌리고 경고를 남긴다. 상태 판정 규칙(어떤 훅이 어떤 상태가 되는지)과 done 소등 시간은
`colors`가 아니라 `state` 항목이 정한다. 테두리(underglow)는 소유권 색이므로 `underglow`가
따로 관리한다.

모든 값과 결정 근거는 [현재 설계 문서](docs/superpowers/specs/2026-08-02-desktop-session-model-design.md)에
정리되어 있다.

## 현재 구현 상태

Claude Desktop 경로의 구현은 완료되어 있다.

| 영역 | 상태 |
|---|---|
| 상태 저장·렌더링·설정·프로토콜 | ✅ 순수 로직과 회귀 테스트 |
| live session 검색·sticky 6슬롯·딥링크 | ✅ Desktop 로컬 파일과 정확한 URL 매핑 |
| 11개 Claude 훅·원자적 설치 | ✅ 훅 경로는 항상 무출력·종료 코드 0 |
| macOS frontmost gate·USB/BLE IOKit | ✅ 별도 TCC 권한 없이 동작 |
| 상주 daemon·start/stop/status/doctor | ✅ private snapshot과 fail-closed PID 전환 |
| 로그인 시 자동 시작 | ✅ owner-only per-user LaunchAgent 설치·상태·제거 |

실기와 앱 번들로 확인한 근거는 [하드웨어 노트](docs/hardware-notes.md),
[딥링크 실측](docs/verification/deeplink.md), [훅 이벤트 검증](docs/verification/hook-events.md)에
남겨 두었다. `4f3dcbe` 당시 설치본의 USB 핵심 E2E와 남은 반복 범위는
[first-light 기록](docs/verification/first-light.md)에 분리했다.

| 확인 항목 | 결과 |
|---|---|
| 세션 딥링크 | `claude://claude.ai/epitaxy/<local_id>` |
| `device.status` 왕복 | USB · BLE 양쪽 |
| A1~A6 개별 색과 효과 | USB · BLE 양쪽 |
| LED 효과 | solid · spin · rainbow · blink · pulse |
| 벤더가 쓰는 존 | Agent 키와 테두리. C1~C7은 paneglow가 건드리지 않음 |
| 테두리 되찾기 | 벤더 ACK를 관찰한 이벤트 기반 reclaim |
| 권한 | Accessibility · Input Monitoring · Screen Recording 불필요 |

## 동작 원칙

- **Layer 1에서만 동작한다.** Layer 2 이상에서는 Agent 키 입력과 표시를 포기한다.
- **소유권 gate는 Paneglow의 dispatch만 제어한다.** Codex가 앞에 있으면 세션 열기를 거부하고
  테두리만 소유권을 보여주지만, Codex가 같은 raw Agent-key report를 받는 것까지 차단하지는
  않는다. 따라서 Claude에서 A키를 빠르게 누를 때 Codex 창이 잠깐 전면으로 오는 공존 문제가
  현재 남아 있다.
- **C1~C7은 사용자 영역이다.** 백라이트와 키맵을 변경하지 않는다.
- **승인·거절 기능은 없다.** 세션에 응답하려면 Accessibility가 필요해 권한 없는 동작 원칙과
  충돌한다.
- **잘못된 상태를 추측하지 않는다.** 세션 스캔, 패드 status, 레이어, PID 신원을 검증하지 못하면
  해당 동작을 거부하고 `status` 또는 `doctor`에 이유를 남긴다.

> ⚠️ Work Louder Input 앱의 **Setup 탭에서 펌웨어 플래싱을 누르지 말 것.** 목록에 Codex
> Micro용 이미지가 없고 BLE 연결을 bootloader mode로 오진할 수 있다. 자세한 내용은
> [하드웨어 노트](docs/hardware-notes.md)에 있다.

## 개발·검증

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -m 'not integration' -q
.venv/bin/python -m pytest -m integration -q  # 실제 Codex Micro 필요
```

이전 iTerm pane 설계는 현재 구현이 아니며, 의사결정 기록으로만
[보존](docs/design.html)한다.

## 라이선스

MIT
