# Installed-product first-light

> **실기 측정 기준**: 2026-08-04 JST · `main` `4f3dcbe8ce1e25c99b5725e248e7ac837c235b24`
>
> 이 문서는 이번 실행에서 직접 확인한 installed-product 결과와 아직 반복하지 않은
> transport·edge-case 항목을 분리한다. 로컬 세션 ID, 작업 디렉터리, 장치 serial,
> 계정 식별자, 로그 원문은 기록하지 않는다.

## 결론

`4f3dcbe` 당시 설치본의 **USB 핵심 경로는 실제 사용자 환경에서 동작했다.** 안정된 프로젝트
`.venv`에 설치하고 Claude 설정에 훅을 병합했으며, 상주 daemon이 실제 Claude Code Desktop
세션과 훅 상태를 읽었다. Codex Micro를 연결하자 daemon이 자동으로 잡았고, fresh
`device.status`, Layer 1 gate, 물리 A1 입력, slot mapping, Desktop 딥링크를 거쳐 같은 세션을
열었다. 후속 입력 타이밍 변경이 포함된 최신 `main`에서 이 물리 E2E 전체를 반복했다는 뜻은
아니다.

Codex가 전면일 때 같은 물리 A1 입력에 대해 **Paneglow의 세션 dispatch**를 거부하는 소유권
gate도 확인했다. 다만 이 gate는 Codex가 동일한 raw report를 받는 것까지 막지는 않는다.
Claude 화면에서 A키를 빠르게 누를 때 Codex 창이 잠깐 전면으로 오는 현상이 별도로 관측됐으며,
아직 깨끗한 공존 해결책을 출시하지 않았다. BLE 반복, 다른 Agent 키와 fault/overflow feedback,
Layer 2, 명시적 unplug/replug 반복은 이번 installed-product 실행에서 완료했다고 기록하지
않는다.

## 최신 main 재확인

2026-08-04 JST에 후속 변경을 모두 병합한 `main`
`20b6a04c6bb2849e58cb99f17f27d630d79af8db`에서도 사용자 개입 없이 확인 가능한 범위를 다시
실행했다.

- editable install을 갱신한 뒤 package summary와 CLI help가 새 프로젝트 설명을 표시했다.
- 회귀 테스트는 `482 passed, 1 skipped`, 별도로 실제 장치를 여는 integration marker는
  `1 passed, 482 deselected`였다.
- `install-hooks`는 `hooks already installed`를 반환했고 Claude 설정과 백업은 계속 `0600`이다.
- daemon이 멈춘 상태의 `doctor`는 11개 훅, authoritative live scan, fresh USB
  `device.status`와 Layer 1을 통과했다.
- daemon을 시작한 뒤 `status`와 running `doctor`가 USB, firmware v0.4.1, Layer 1을 읽었다.
  실행 중 `start`를 다시 호출해도 기존 instance 하나만 유지했다.
- `trace-input --seconds 0.5 --max-events 32`의 무입력 왕복은
  `{"duration_ms":500,"events":[],"schema_version":1,"truncated":false}`를 반환했다. 완료 뒤
  request, active, result artifact는 남지 않았고 runtime 파일은 `0600`이다.

이 재확인 시점에는 live Claude Code Desktop 세션이 0개였다. 따라서 최신 `main`에서 Desktop
mapping·딥링크와 물리 A1→A2 입력을 다시 실행했다는 뜻은 아니다. 아래의 `4f3dcbe` 실기 근거와
최신 main 재확인 범위를 합쳐 과장해서는 안 된다.

## 측정 환경

| 항목 | 값 |
|---|---|
| macOS | 26.5.2 (25F84) |
| Python | 3.14.6 |
| Claude.app | 1.24012.9 |
| Claude Code | 2.1.220 |
| 설치 형태 | 저장소 `.venv`의 editable install |
| 런타임 의존성 | 없음 |
| Codex Micro | USB · firmware v0.4.1 · Layer 1 |

## 이번 실행에서 검증됨

### 설치와 훅

- `.venv/bin/python -m pip install -e .`로 설치하고 `.venv/bin/paneglow` console command를
  실제로 실행했다.
- `install-hooks`가 기존 Claude 설정과 사용자 훅을 구조적으로 보존했다.
- 원본 전체와 byte-identical한 `settings.json.paneglow.bak`을 만들었다.
- 설정과 백업 모두 owner-only `0600`이다.
- 정확히 11개 이벤트에 canonical `.venv` Python command가 한 번씩 있다.
- 두 번째 설치는 설정과 백업 bytes를 바꾸지 않았다.
- 별도 임시 runtime에서 hook command가 무출력·종료 코드 0 계약을 지켰다.

### daemon lifecycle

- `start`가 detached daemon을 준비 완료 상태까지 올렸다.
- 실행 중 다시 `start`해도 같은 instance를 보고하고 두 번째 daemon을 만들지 않았다.
- PID hint, snapshot, log가 모두 `0600`이며 PID와 snapshot identity가 일치했다.
- `status`는 authoritative session count와 6개 sticky slot을 읽었다.
- 장치가 없을 때 시작한 daemon은 USB 연결 뒤 자동으로 Codex Micro를 잡고 firmware v0.4.1과
  Layer 1을 검증했다.
- daemon을 멈춘 상태의 `doctor`가 독립적인 fresh USB `device.status` 왕복을 통과했다.
- 그 뒤 `start`로 다시 올리고 running `doctor`와 `status`가 같은 USB·Layer 1 상태를 읽었다.

### 실제 Claude Desktop 경로

- 기존 Claude Code Desktop 세션을 재개해 새 hook 이벤트와 응답을 만들었다.
- live session scan은 authoritative 1개를 반환했다.
- 실제 hook 상태가 해당 세션 slot에 반영됐다.
- live session의 Desktop mapping이 1/1로 해석됐다.
- mapping 자체를 분리 검증한 `open_session()`이 성공했고 Claude 앱 URL이 exact Desktop
  local-session route로 이동했다.
- 딥링크 직후 `NSWorkspace` 표본 10/10에서 frontmost bundle이 Claude Desktop이었다.
- Claude가 Agent 키를 소유한 뒤 Finder를 전면에 둔 상태에서 물리 A1을 눌렀다. daemon의 실제
  input dispatch가 slot 1의 mapping을 열었고 Claude Desktop이 다시 전면으로 이동했다.
- Codex가 전면일 때 누른 물리 A1은 `ignored_owner`로 기록되어 Paneglow가 세션을 열지 않았다.
  이 결과는 Codex 자체가 같은 입력을 처리하지 않았다는 뜻은 아니다.
- running `doctor`에서 hooks, live scan, Desktop mapping, daemon USB pad snapshot이 모두
  `PASS`였고 종료 코드도 0이었다.

## Codex 공존 조사

Claude 화면에서 A키를 빠르게 누르면 Codex가 잠깐 전면으로 오는 현상을 확인했다. 현재
Paneglow의 owner gate는 자신의 세션 dispatch만 거부할 수 있고, Codex가 같은 raw HID report를
동시에 받는 것까지 막을 수 없다.

검토하거나 시험한 대안은 다음과 같다.

- Codex가 전면으로 온 뒤 Claude로 focus를 되돌리는 prototype은 동작했지만, 먼저 보이는
  Codex flash 자체가 나쁜 사용자 경험이므로 출시하지 않았다.
- IOHID exclusive open은 Agent report 하나가 아니라 키보드 기능을 포함한 장치 전체를
  seize한다. 이미 Codex가 잡은 handle을 Paneglow가 선점하는 시험도 실패해 이 접근은 폐기했다.
- Codex에는 Paneglow가 호출할 수 있는 supported external pause API가 없다.
- Codex 내부 상태를 바꿔 A키를 사실상 비활성화하는 우회도 조사했지만, Codex의 native A키
  기능을 잃고 창이 없는 상태·startup race·Codex update에 취약하다. 자동 적용하지 않는다.

따라서 이 문서는 owner gate 성공을 "Codex 입력 차단"으로 확대 해석하지 않는다. Paneglow가
받은 메시지의 순서·중복과 dispatch 시점 owner 표본을 짧고 민감정보 없이 수집하는 opt-in
trace를 후속 구현했다. trace의 mailbox, 경계값, 정규화 출력과 실패 닫힘 동작은 자동화
테스트와 독립 보안 재검토를 통과했지만, 아래 순서의 물리 A1→A2 표본은 아직 수집하지 않았다.

## 이번 실행에서 남긴 범위

`4f3dcbe` 당시 설치본의 USB A1 핵심 E2E는 완료했다. 아래 항목은 과거 protocol·실기 근거 또는
자동화된 회귀 테스트가 있더라도, 그 설치본과 당시 장치 조합으로 다시 관측하지 않은 범위다.

- A2~A6 물리 입력과 각 상태 색의 육안 확인
- 빈 slot, overflow alert, mapping fault feedback의 물리 효과
- Layer 2 이상에서 입력·표시를 포기하고 Layer 1 복귀 시 다시 그리는 물리 반복
- 명시적인 USB unplug/replug 반복과 종료 시 LED flush의 육안 확인
- `4f3dcbe` 설치본의 BLE 반복
- 상태→LED 500ms, press→세션 열림 150ms 목표의 실제 관측값

USB/BLE protocol과 과거 실기 왕복 근거는 [하드웨어 실측 노트](../hardware-notes.md)에 있다.
그 근거는 이번 installed-product 하드웨어 E2E를 대신하지 않는다.

## 2026-08-04 로그인 자동 시작 실측

`323b0b8` 설치본으로 실제 per-user LaunchAgent 수명주기를 확인했다. 시작 전에는 자동 시작이
설치되지 않았고, 수동 daemon PID 2960이 USB v0.4.1 Layer 1 패드를 정상 보고하고 있었다.

- `autostart install --timeout 15`가 수동 daemon을 종료하고 launchd 소유 PID 60546으로
  전환했다. `launchctl print`와 설치 plist에서 현재 저장소의
  `.venv/bin/python -m paneglow.cli run`, `KeepAlive/SuccessfulExit=false`, umask 077을 확인했다.
- plist, 계정 전역 installer lock, daemon log는 현재 계정 소유·mode 0600·link count 1이었고,
  `~/.paneglow`는 0700으로 강화됐다. 설치 plist는 `plutil -lint`를 통과했다.
- 같은 install을 반복하면 PID 60546을 유지한 채 already-installed no-op으로 끝났다.
- `paneglow stop` 뒤 daemon은 stopped, plist는 current/not-loaded가 됐다. 12초 뒤에도 daemon과
  launchd job은 다시 생기지 않았다.
- `paneglow start --timeout 15`가 plist를 다시 bootstrap해 PID 62049를 올렸고, USB v0.4.1
  Layer 1 상태가 복구됐다.
- uninstall은 daemon과 plist/job을 제거했고, 두 번째 uninstall은 already-uninstalled
  no-op이었다. 그 뒤 다시 install해 PID 63096이 current/loaded 상태가 됐다.
- 최종 `status`, `doctor`, `pip check`가 통과했다. 0.5초 `trace-input`은 빈 이벤트 배열을
  반환했고 request/active/result 임시 파일을 남기지 않았다.
- 최종 PID 63096을 SIGKILL한 뒤 12초를 기다렸다. launchd의 runs가 1에서 2로 늘고
  `last terminating signal = Killed: 9`가 기록됐으며, 새 PID 68752가 USB v0.4.1 Layer 1을
  다시 보고했다. 따라서 정상 stop의 무재실행과 비정상 종료의 자동 복구를 각각 실측했다.

이 실측에는 logout/login 또는 재부팅이 포함되지 않았다. 따라서 다음 로그인에서 별도
`paneglow start` 없이 올라오는지는 여전히 사용자 입회 검증으로 남는다.

## 남은 사용자 확인

daemon을 시작한 뒤 Claude Desktop을 전면에 두고 다음 명령을 실행한다.

```bash
.venv/bin/paneglow trace-input --seconds 5
```

5초 안에 **A1을 한 번, 이어서 A2를 한 번만** 누른다. 표시된 결과를 그대로 공유하면 된다.
출력은 민감정보를 포함하지 않는 정규화된 진단 정보로 제한된다. 이 한 번의 표본으로 Paneglow가
받은 메시지의 중복·순서와 각 dispatch 시점의 owner 표본을 비교할 수 있다. 물리 bounce나
transport 중복의 원인, 정확한 앱 focus 전환 시각 자체를 이 결과만으로 판별할 수는 없다.

그 밖에 현재 장치로 직접 반복할 항목은 다음과 같다.

- A2~A6의 물리 입력과 상태 색
- Layer 2 진입·복귀, USB unplug/replug, BLE 연결
- 빈 slot과 fault/overflow 표시

## 다시 사용할 때

기록 종료 시 per-user LaunchAgent는 current/loaded였고 PID 68752가 USB Layer 1 패드를
보고했다. 이 시점 상태를 이후 실행 상태로 단정하지 말고 먼저 다음 명령으로 확인한다.

```bash
cd /path/to/paneglow
.venv/bin/paneglow autostart status
.venv/bin/paneglow status
.venv/bin/paneglow doctor
```

자동 시작이 아직 설치되지 않았으면 `.venv/bin/paneglow autostart install`을 실행한다.
logout/login 또는 재부팅 뒤 별도 `start` 없이 올라오는지는 아직 확인하지 않았다. Claude
Desktop이 Agent 키를 소유할 때 A1은 현재 live 세션을 연다. Codex가 전면이면 Paneglow는
의도적으로 자신의 dispatch를 양보한다. 이는 Codex의 동일 raw 입력 처리를 중단시키지는 않는다.
