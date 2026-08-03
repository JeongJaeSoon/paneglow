# Phase 0 — Claude Code 훅 이벤트 실측

> 이슈 #1. 설계 전체가 여기 걸려 있었다.
> **측정**: 2026-07-26 · Claude Code 2.1.220 · 8개 세션 · 107건 수집
>
> **현재 모델 주의:** 페이로드 실측은 근거로 유지하지만 pane/`ITERM_SESSION_ID` 조인과
> 승인 키 결론은 폐기되었다. 현재 구현은 hook `session_id`를 live Claude 세션 목록과
> Claude Desktop 딥링크 매핑에 조인한다.

수집 방법: `~/.claude/settings.json`에 관찰용 훅을 임시로 걸어 페이로드를 그대로 append.

---

## 결론 요약

| 질문 | 답 | 영향 |
|---|---|---|
| `waiting`(승인 대기)를 판별할 수 있는가 | **✅ 가능** | 상태 표시에 사용. 승인 키 기능은 폐기 |
| `error`를 구분할 수 있는가 | **✅ 가능** | `StopFailure`·`PostToolUseFailure` 별도 이벤트 |
| `tty`를 얻을 수 있는가 | **❌ 불가** | 현재 모델은 `session_id`로 live 세션과 조인 |
| `session_id` · `cwd` | ✅ 모든 이벤트에 있음 | — |

**추가 발견 둘:**

- 서브에이전트가 **별도 `session_id`로 같은 상위 세션에서** 이벤트를 낸다.
  거르지 않으면 부모의 `waiting`을 덮어쓴다 (§4).
- **`Stop`이 안 오는 경우가 있다** — 사용자가 ESC로 턴을 중단하면 아무 이벤트도 안 온다.
  그러면 세션이 `working`에 갇힌다 (§7).

> **1차 결론 중 둘이 틀렸다.** 로컬 관측만으로 판단했기 때문이다.
> `error`는 `Stop` 페이로드에 필드가 없다는 것만 보고 불가로 판정했으나,
> 실제로는 **별도 이벤트**로 온다. `tty`는 "대화형 claude에 tty가 있으니
> 자식도 상속한다"고 추론했으나 문서가 이를 명시적으로 부정한다.
> 공식 문서와 이슈 트래커를 확인하고 나서야 바로잡혔다.

---

## 1. 실제로 오는 이벤트

```text
19  PreToolUse
16  PostToolUse
 3  UserPromptSubmit
 2  Notification
 2  Stop
 2  SessionEnd
 1  SessionStart
 1  SubagentStop
```

`PreCompact`은 훅을 걸었으나 관측 기간에 발생하지 않았다(미확인, 부재 증명 아님).

### 공통 필드

| 필드 | 있는 이벤트 |
|---|---|
| `session_id` `cwd` `transcript_path` `hook_event_name` | **전부** |
| `prompt_id` | 거의 전부 (`SessionStart` 제외) |
| `permission_mode` | 대부분 (`auto` 관측) |
| `agent_type` | 서브에이전트 관련 이벤트 |
| `tool_name` `tool_input` `tool_use_id` | `PreToolUse` · `PostToolUse` |
| `tool_response` `duration_ms` | `PostToolUse` |

**`tty`도 `pid`도 없다.**

---

## 2. `waiting` 판별 — 가능 ✅ (단, 화이트리스트로)

`Notification`에 **`notification_type`** 이 실려 온다. 이것이 판별 키다.
**모든 값이 `waiting`은 아니다** — §9의 `idle_prompt` 참조.

```json
{"hook_event_name":"Notification","notification_type":"permission_prompt",
 "message":"Claude needs your permission","session_id":"45805b14-…","cwd":"…"}

{"hook_event_name":"Notification","notification_type":"agent_needs_input",
 "message":"… needs your input: …","session_id":"8ede5e9e-…","cwd":"…"}
```

기대보다 낫다 — **두 종류의 대기가 구분된다.**

| `notification_type` | 뜻 | 사람이 할 일 |
|---|---|---|
| `permission_prompt` | 도구 실행 승인 대기 | C2/C3로 바로 승인·거절 가능 |
| `agent_needs_input` | 에이전트가 질문함 | pane으로 가서 타이핑해야 한다 |

Phase 1은 둘 다 `waiting`으로 합친다. Phase 2에서 승인 기능을 붙일 때
`permission_prompt`에만 C2/C3를 열면 오발을 막을 수 있다.

> `message`는 사람이 읽는 문자열이다. **파싱하지 말 것.**
> 안정적인 판별 키는 `notification_type`이다.

---

## 3. `error` 판별 — 가능 ✅ (1차 결론 정정)

**에러는 필드가 아니라 별도 이벤트로 온다.** `Stop`만 뒤져서는 안 보인다.

| 이벤트 | 언제 | 출처 |
|---|---|---|
| `StopFailure` | "When the turn ends due to an API error" — 429·인증 실패·크레딧 소진 | 공식 문서 |
| `PostToolUseFailure` | "After a tool call fails" | 공식 문서 |
| `PermissionDenied` | "When a tool call is denied by the auto mode classifier" | 공식 문서 |

`StopFailure`는 v2.1.78에 추가됐고 **문서 이벤트 표에서 누락돼 있다**
([anthropics/claude-code#35620](https://github.com/anthropics/claude-code/issues/35620)).
그래서 1차에서는 존재 자체를 몰랐다.

> `StopFailure`는 "Output and exit code are ignored" — 알림 전용이라 턴을 막을 수 없다.
> 우리 용도(상태 기록)에는 문제없다.

**1차에서 틀린 판정을 한 경위** (같은 실수를 반복하지 않기 위해 남긴다):

`Stop` 페이로드에 에러 필드가 없는 것은 사실이다.

```text
stop_hook_active  last_assistant_message  background_tasks  session_crons
permission_mode   effort  prompt_id  session_id  cwd  transcript_path
```

`PostToolUse`의 `tool_response`도 쓸 수 없다 — 도구마다 모양이 다르다.

```text
Bash    -> stdout, stderr, interrupted, isImage, noOutputExpected, gitOperation
Edit    -> filePath, oldString, newString, structuredPatch, userModified
Skill   -> commandName, success        ← success 는 Skill 에만 있다
```

여기까지 보고 "불가"로 결론지었다. **관측 기간에 API 오류가 나지 않았으므로
관측만으로는 영원히 알 수 없었다.** 부재는 증명이 아니다.

관측 데이터에 `429` 문자열이 13건 있었으나 전부 파일 내용·프롬프트에 우연히 섞인
것이었고, 사용자가 실제로 겪은 429는 훅에 **아무 흔적도 남기지 않았다** —
당시 `StopFailure`를 걸어두지 않았기 때문이다.

---

## 4. 서브에이전트 필터 — 실측 결론은 현재도 유효 ⚠️

**계획서에 없던 문제.**

당시 iTerm2 관측에서 서브에이전트는 **부모와 같은 pane에서 돌지만 `session_id`가 달랐다.**

```text
46e739ff  PreToolUse   agent_type=claude  cwd=phone-link-data-split   31건
46e739ff  PostToolUse  agent_type=claude  cwd=phone-link-data-split   32건
46e739ff  PreToolUse   agent_type=claude  cwd=UsageLink                1건  ← cwd 가 바뀐다
```

폐기된 `tty` 조인에서는 같은 tty에 레코드가 둘 생기고, `by_tty()`가 `updated_at` 최신을
골랐다. 서브에이전트가 부모보다 훨씬 자주 이벤트를 내므로 화면에는 늘 서브에이전트 상태가
뜨는 문제가 있었다.

치명적인 경우: **부모가 `waiting`인데 서브에이전트가 `working`이면 주황이 파랑에 덮인다.**
사용자가 승인을 기다리는 pane을 놓친다 — 이 프로젝트가 막으려는 바로 그 증상이다.

`cwd`도 서브에이전트를 따라 바뀌므로 디렉터리 기반 식별도 안전하지 않다.

**현재 대응**: Desktop 세션 모델에서도 `agent_type`이 있는 이벤트는 상태로 쓰지 않는다.
`SubagentStop`은 이미 무시 대상이고, `agent_type` 필드로 나머지도 거른다.

---

## 5. 폐기된 pane 식별 결론 — 당시 `tty` 대신 `ITERM_SESSION_ID`

> **현재 구현에는 적용하지 않는다.** 아래 내용은 iTerm pane 모델 안에서만 유효했던 중간
> 결론이다. Desktop 모델은 hook `session_id`를 live 세션 목록과 딥링크 mapping에 조인한다.

**`tty`는 얻을 수 없다.** 공식 문서:

> "On macOS and Linux, command hooks run in their own session **without a
> controlling terminal** as of v2.1.139. The hook process and any child
> processes can't open `/dev/tty` …"

이 환경은 2.1.220이므로 해당된다. `ps -o tty= -p $$`는 `??`를 낸다.
**설계 전체가 tty를 pane 키로 쓰고 있었으므로 대체가 필요하다.**

### 당시 대체 수단

iTerm2는 각 pane의 셸에 `ITERM_SESSION_ID`를 심고, Claude Code가 이를 상속하며,
훅도 "Claude Code's environment"로 실행되므로 그대로 받는다.

```text
환경변수   ITERM_SESSION_ID = w0t1p0:57437D3D-ED91-4B7D-BB91-844F603E6994
                                     └───────────────┬────────────────┘
iTerm2 API  Session.session_id =      57437D3D-ED91-4B7D-BB91-844F603E6994
```

`:` 앞은 `w<window>t<tab>p<pane>` 위치이고 **바뀔 수 있다.** 뒤의 GUID만 쓴다.
실측으로 iTerm2 API의 `Session.session_id`와 정확히 일치함을 확인했다.

### 당시 tty보다 나았던 점

| | `tty` | `ITERM_SESSION_ID` |
|---|---|---|
| 훅에서 접근 | ❌ 제어 터미널 없음 | ✅ 환경변수 |
| 재사용 | pty가 재활용된다 | GUID, 재사용 없음 |
| iTerm2 매칭 | `async_get_variable("tty")` | `session.session_id` 직접 |

pty 재활용이 없어지므로 `store.prune()`의 "재사용된 tty의 고아 정리" 로직이
**불필요해진다.** `SessionRecord.tty`를 GUID로 바꾸면 그 분기를 덜어낼 수 있다.

### 당시 한계

- **iTerm2 전용이다.** 다른 터미널에서는 비어 있다. 이 프로젝트는 iTerm2 전용이므로 수용한다.
- 백그라운드 잡(`claude bg-spare`)에는 없다. 어느 pane에도 없으므로 매핑하지 않는 것이 맞다.

---

## 6. 그 밖에 쓸 만한 필드

| 필드 | 용도 |
|---|---|
| `prompt_id` | 한 턴의 이벤트를 묶는다. 턴 단위 집계에 쓸 수 있다 |
| `permission_mode` | `auto` 등. 승인이 아예 안 뜨는 모드를 감지할 수 있다 |
| `SessionStart.source` | `startup` — 재개인지 신규인지 구분 가능 |
| `SessionEnd.reason` | `other` 관측. 값의 종류는 미확인 |
| `Stop.last_assistant_message` | 마지막 응답 전문. Phase 1에는 불필요 |

---

## 7. `working` 갇힘 — 알려진 이슈 ⚠️

**사용자가 ESC로 턴을 중단하면 `Stop`도 `StopFailure`도 오지 않는다.**
공식 문서의 `Stop` 정의는 "When Claude finishes responding"뿐이고,
`StopFailure`는 API 오류 전용이다. 중단을 알리는 이벤트는 **없다.**

커뮤니티가 같은 증상을 보고했다:

> "any hook-driven state that was set on UserPromptSubmit (e.g. a **'working'
> indicator**) is permanently stuck when the user interrupts"
> — [anthropics/claude-code#45289](https://github.com/anthropics/claude-code/issues/45289)

관련 이슈: [#9516](https://github.com/anthropics/claude-code/issues/9516) (User Interrupt Hook 요청),
[#29881](https://github.com/anthropics/claude-code/issues/29881) (턴 중간 정지 시 `Stop` 미발화).

관측에서도 불균형이 나타났다 — 8개 세션 중 3개에서 `UserPromptSubmit`이
`Stop`보다 많았다(`55a0f363`은 2:0). 진행 중인 턴으로는 설명되지 않는다.

**왜 중요한가**: `error`를 못 잡는 것은 색 하나가 없는 것이지만, `working` 갇힘은
**거짓 정보**다. 실제로는 멈춰서 사람을 기다리는 pane이 "작업 중"으로 보인다.
아무것도 안 보여주는 것보다 나쁘다.

**대응 후보**: `working`에 시간 상한을 두고 넘으면 `idle`로 떨어뜨린다.
"모르겠다"가 "작업 중"이라는 거짓말보다 낫다. 상한값은 설정으로 연다
(오래 걸리는 작업이 흰색이 되는 부작용이 있으므로).

---

## 8. 계획에 반영할 것

1. **`classify()`에 화이트리스트** — `Notification`은 `notification_type`이
   `permission_prompt`·`agent_needs_input`일 때만 `WAITING`. 나머지는 `None`.
2. **`error` 유지** — `StopFailure`·`PostToolUseFailure`를 `ERROR`로 매핑. 팔레트 5색 유지.
3. **`record_from()`에 서브에이전트 필터** — `agent_type`이 있으면 `None`.
4. ~~pane 키를 `tty`에서 `ITERM_SESSION_ID`의 GUID로 교체~~ — **폐기됨.** 현재 구현은
   hook `session_id`를 live Desktop 세션과 조인하며 iTerm 환경변수를 읽지 않는다.
5. **`working` 시간 상한** 도입 (§7).
6. `pid`가 페이로드에 없으므로 `SessionRecord.pid`는 훅 프로세스의 pid다. 진단용.

### 이 검증이 막은 것

계획서대로 짰다면 만났을 문제들이다.

| 계획 | 실제 | 증상이었을 것 |
|---|---|---|
| `tty`를 `pid`로 유도 | 훅에 제어 터미널 없음 | 항상 매핑 실패, 키가 전부 꺼짐 |
| `error` 제거 | 별도 이벤트로 존재 | 쓸 수 있는 신호를 버림 |
| `Notification` → 무조건 `WAITING` | `idle_prompt`가 섞임 | 노는 pane이 주황으로 거짓말 |
| 서브에이전트 미고려 | 같은 pane, 다른 `session_id` | 부모의 `waiting`이 덮임 |

> **관측**: 훅이 **재시작 없이 적용됐다.** 설치 전에 시작된 세션(`1d12fd41`)에서도
> 이벤트가 잡혔다. FreeMicro 시절 "세션 재시작 필요" 진단과 다르다.
> 다만 모든 이벤트 종류에 대해 확인한 것은 아니므로 안전하게는 재시작을 권한다.

---

## 9. 2차 수집 결과 (812건)

1차는 `StopFailure`·`PostToolUseFailure`를 걸지 않아 놓쳤다. 2차에 추가하고
모든 이벤트에 `_iterm`·`_tty`·`_pid`를 덧붙여 기록했다.

```text
368  PreToolUse          12  PostToolUseFailure    8  SessionEnd
356  PostToolUse          9  UserPromptSubmit      7  SubagentStart
 22  SubagentStop         8  SessionStart          7  Notification
 12  Stop                                          2  PermissionRequest
```

### `_tty`는 예외 없이 비었다

811건 전부 `_tty=??`. 문서의 "hooks run without a controlling terminal"이 맞다.
**tty 기반 pane 매핑은 불가능하다.**

### 당시 `_iterm`은 pane 세션에서만 채워졌다

빈 경우가 대부분이었으나 이는 결함이 아니라 정확한 동작이다.
tty를 가진 claude 프로세스는 4개뿐이고 **전부** `ITERM_SESSION_ID`를 갖는다:

```text
pid=42502 ttys001  ITERM_SESSION_ID=w0t1p0:57437D3D-…
pid=61943 ttys004  ITERM_SESSION_ID=w0t0p0:04E8E4A6-…   ← 훅 기록의 _iterm 과 일치
```

나머지는 `bg-pty-host`·`bg-spare`·데스크톱 앱 헬퍼로 **애초에 pane이 아니다.**

| 당시 세션 종류 | `_iterm` | 당시 결론 |
|---|---|---|
| iTerm2 pane의 대화형 세션 | 있음 | 키에 배정 |
| 백그라운드 잡 · 데스크톱 앱 | 없음 | 배정하지 않음 |

당시에는 **"비어 있으면 무시"가 곧 fail-closed**라는 결론이었다. 현재 구현의 fail-closed
기준은 live Desktop 세션과 딥링크 mapping의 검증 성공 여부다.

### `PostToolUseFailure` 실측

```text
_iterm _pid _ppid _tty  agent_id  agent_type  cwd  duration_ms  effort
error  hook_event_name  is_interrupt  permission_mode  prompt_id
session_id  tool_input  tool_name  tool_use_id  transcript_path
```

- **`error`** — `"Exit code 1"`, `"Output does not match required schema: …"` 등
- **`is_interrupt`** — 관측된 12건 모두 `false`. 도구 중단을 구분하는 필드로 보이나
  참인 사례를 못 봤다(미확인)

### `Notification` — 세 번째 종류가 나왔다 ⚠️

2차 관측은 7건 **전부 `idle_prompt`** 였다. 1차의 `permission_prompt`·
`agent_needs_input`과 다른 종류다.

`idle_prompt`는 "사용자가 오래 응답하지 않음" 알림이므로 **`waiting`이 아니다.**
이것까지 `waiting`으로 잡으면 그냥 놀고 있는 pane이 주황으로 "나를 기다린다"고
거짓말한다.

**따라서 `notification_type`은 화이트리스트로 다룬다:**

```python
_WAITING_NOTIFICATIONS = {"permission_prompt", "agent_needs_input"}
```

문서가 나열한 전체 값: `permission_prompt`, `idle_prompt`, `auth_success`,
`elicitation_dialog`, `elicitation_complete`, `elicitation_response`,
`agent_needs_input`, `agent_completed`.
블랙리스트로 하면 새 종류가 추가될 때마다 오작동한다.

### `PermissionRequest` — 별개 이벤트

```json
{"hook_event_name":"PermissionRequest","tool_name":"AskUserQuestion", …}
{"hook_event_name":"PermissionRequest","tool_name":"ExitPlanMode", …}
```

`notification_type`이 없고 `tool_name`을 준다. 승인 대기의 더 직접적인 신호일 수
있다. Phase 2에서 승인 기능을 붙일 때 재검토한다.

### 여전히 미확인

| 항목 | 왜 |
|---|---|
| `StopFailure` 페이로드 | 관측 기간에 API 오류가 나지 않았다 |
| ESC 중단 시 무이벤트 | `Stop`이 없는 세션이 3개 있었으나 진행 중인 턴과 구분되지 않는다 |
| `is_interrupt=true` 사례 | 미관측 |

`StopFailure`는 훅을 걸어두었으므로 다음 API 오류 때 자동으로 잡힌다.
ESC 중단은 확증되지 않았으나 **대응은 같다** — 중단이든 진행 중이든 `Stop`이
오지 않는 상태가 존재하고, `working` 시간 상한이 그 둘을 모두 덮는다.
