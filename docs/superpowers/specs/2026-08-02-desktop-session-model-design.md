# paneglow — Claude Code Desktop 세션 모델 설계

> 2026-08-02 · [기존 설계](../../design.html)의 iTerm2 pane 모델을 대체한다.
> 관문 실측: [훅 이벤트](../../verification/hook-events.md) · [딥링크](../../verification/deeplink.md)

## 왜 바꾸나

기존 설계는 **iTerm2 한 탭 안의 pane들**을 6키에 올렸다. 실사용은 Claude Code Desktop으로 옮겨갔고,
데스크톱에는 pane도 탭도 분할 트리도 없다. 세션이 평평한 목록 하나로 존재한다.

동시에 Codex Micro의 순정 동작 — **열어둔 것이 6개를 넘어도 최근 것 위주로 보여준다** — 이
이 워크플로우에 잘 맞는다는 것이 사용 경험으로 확인됐다. 트리를 평평한 최근순 목록으로 바꾸면
설계에서 덜어낼 것이 많다.

## 요구사항

| # | |
|---|---|
| R1 | 살아 있는 Claude Code 세션의 상태를 키별 색으로 표시 |
| R2 | 키를 누르면 **그 세션이 Claude 앱에서 열린다** |
| R3 | 세션이 6개를 넘어도 최근 것 위주로 보여준다. 배치는 예측 가능할 것 |
| R4 | Codex 앱을 볼 때 6키를 벤더에게 양보 |
| R5 | **전용 LED 존으로 지금 6키가 누구 것인지 즉시 알 수 있다** |
| R6 | 6키에 안 보이는 대기·오류를 알린다 |
| R7 | 특별한 권한을 요구하지 않는다 |

**승인/거절(C2·C3)은 범위에서 제외한다.** 데스크톱 앱에는 세션에 응답을 보낼 수단이 없고,
남은 길은 Accessibility 권한으로 앱 UI를 찌르는 것뿐이라 R7과 정면으로 충돌한다.

## 조명 — C키 백라이트 하나가 색과 움직임을 따로 쓴다

기존 설계는 테두리에 "화면 밖 대기 알림"을 넣었다. 그런데 우리는 벤더와 **같은 팔레트**를
일부러 쓰므로(주황=대기, 초록=완료…) **6키만 봐서는 Codex 것인지 Claude 것인지 구분되지 않는다.**
소유권을 말해줄 존이 따로 필요하다.

**그 존은 테두리가 아니라 C1~C7 백라이트다.** 실측 결과 벤더는 Codex에서 A키를 누를 때마다
**테두리에 그 키의 색을 한 번 비춘다** — 선택 피드백이다. 우리가 2초마다 다시 쏘면 이길 수는
있지만(경쟁이 아니라 번갈아 쓰는 관계다) 누를 때마다 잔상이 남는다.
같은 조건에서 **C키 백라이트는 벤더가 전혀 건드리지 않았다.**
근거는 [하드웨어 노트 §3](../../hardware-notes.md).

| 존 | 명령 | 이 설계에서 |
|---|---|---|
| A1~A6 | `thstatus` | 세션별 상태. 양보 중에는 벤더 것 |
| **C1~C7 백라이트** | `rgbcfg` → `keys` | **소유권 + 알림** |
| 테두리 | `rgbcfg` → `ambient` | **쓰지 않는다** — 벤더에게 남긴다 |

**색 = 누구 것인가 · 움직임 = 무슨 일인가**

| 소유 | 색 | 왜 이 값인가 |
|---|---|---|
| Claude | `#FF6D00` | 공장 팔레트의 `waiting` — Claude Code가 승인을 기다릴 때 보는 그 주황 |
| Codex (양보 중) | `#304FFE` | 공장 팔레트의 `working` — Codex가 돌 때 보는 그 파랑 |
| 무소유 · 게이트 off · 레이어 ≠ 1 | 소등 | |

> 브랜드 아이콘 색(`#D87050` / `#6888F8`)도 실기에서 시험했으나 **LED에서 연하게 보였다.**
> 공장 팔레트 값이 더 잘 읽히고, "그 제품을 쓸 때 가장 자주 보는 색"이라는 점에서 의미도 맞는다.

| 움직임 | `e` | 뜻 |
|---|---|---|
| 계속 켜짐 | 1 | 평소 — 색만 읽으면 된다 |
| **부드러운 펄스** | 6 | **확인 필요** (기본) |
| 회전 | 2 | 확인 필요 (대안, 설정으로 선택) |
| 깜빡임 | 4 | 오류 |
| 무지개 회전 | 3 | **paneglow 자체 장애** — 색을 못 믿는 상태이므로 색을 버린다 |

**속도(`s`)로 의미를 나누지 않는다.** 같은 효과의 속도 차이는 눈으로 구분되지 않는다(실측).
축은 언제나 `e` 다.

효과는 **기기가 스스로 돌린다.** 메시지 한 번이면 2분이 지나도 계속 움직인다(실측).
호스트 트래픽도, 데몬이 멈춰 애니메이션이 굳는 위험도, 주기적 재전송도 필요 없다.

집계 대상은 슬롯에 못 오른 세션 전부이고, Codex를 볼 때는 **모든 Claude 세션**이다
(전부 화면 밖이므로). **움직임은 언제나 Claude 얘기다** — Codex 세션의 상태는 얻을 경로가
없다(범위 밖). 파란 C키가 펄스하면 "6키는 Codex 것, 그런데 Claude가 너를 기다린다"는 뜻이다.

### 눌림에 대한 응답

키를 눌렀는데 열 수 없으면(매핑이 아직 없는 갓 생긴 세션 등) C키 백라이트를 **0.3초 깜빡이고
평소로 돌아온다.** 상태가 아니라 눌림에 대한 대답이므로 위 넷과 경쟁하지 않는다.
없으면 눌린 것인지 키가 죽은 것인지 구분할 수단이 없다.

## 아키텍처

```
~/.claude/sessions/<pid>.json ──┐
  sessionId·cwd·name·kind·pid   │  sessions.py   살아있는 대화형 세션
                                │       │
~/.paneglow/state/<sid>.json ───┘       │  store.py   훅이 원자적으로 쓴 상태
  state·rev·updated_at                  │
                                        ▼
                          slots.assign()      최근순 고정 슬롯 6개
                                        │
                          render()  ──────────► pad.thstatus / rgbcfg
                                        │
  A키 press ────────────────────────────┴──► deeplink.open(session_id)
```

되돌릴 수 없는 동작이 하나도 없으므로 **기존 설계 §07의 세대(generation)·입력 잠금 기계를 뺀다.**
그 기계는 "엉뚱한 pane에 승인이 나가는 것"을 막으려고 있었다. 승인이 없으면 최악의 사고가
"엉뚱한 세션이 열린다"이고, 다시 누르면 끝난다.

### 모듈

| 파일 | 책임 | 변경 |
|---|---|---|
| `state.py` | 상태 어휘·우선순위 | 그대로 |
| `protocol.py` | 벤더 메시지·HID 프레이밍 | 그대로 |
| `store.py` | 상태 레코드 읽기/쓰기 | `tty` 필드·`by_tty()`·tty 재사용 분기 **삭제** |
| `render.py` | (세션+상태) → 6색 | `Pane` → `Session`, `is_claude` 삭제 |
| `config.py` | 설정 로드·폴백 | 키 교체 |
| `sessions.py` | 살아있는 세션 목록 | **신규** |
| `slots.py` | 세션 → 6슬롯 배정 | **신규**, 순수 |
| `deeplink.py` | 세션을 앱에서 연다 | **신규** |
| `hook.py` | 훅 이벤트 → 상태 | 계획대로 |
| `pad.py` | IOKit 송수신 | 계획대로 |
| `daemon.py` `cli.py` | 게이트·루프·CLI | 계획대로 |

`iterm.py`는 삭제한다. `iterm2` 패키지 의존과 iTerm2 Python API 활성화 안내도 함께 사라진다.

## 데이터 소스 셋

### ① 세션 목록 — `sessions.py`

`~/.claude/sessions/*.json` 을 glob 한다. 파일 하나가 세션 하나이고 수백 바이트다.

```json
{"pid":34508,"sessionId":"998574ae-…","cwd":"…","name":"…",
 "kind":"interactive","entrypoint":"claude-desktop","startedAt":1785658815386}
```

**받아들이는 조건**은 둘뿐이다.

| 조건 | 이유 |
|---|---|
| `kind == "interactive"` | `bg`는 백그라운드 잡이라 열 창이 없다 |
| `os.kill(pid, 0)` 가 성공 | 파일이 남아 있어도 죽은 세션은 뺀다 |

`entrypoint`(`claude-desktop` / `cli`)로는 **거르지 않는다.** 필터가 아니라 `paneglow status`가
보여줄 메타로만 들고 있는다. CLI 세션은 딥링크 대상이 아닐 수 있으나(§미확인), 그때는
매핑 조회가 빈손으로 끝나 무동작이 되므로 별도 분기가 필요 없다.

**비공개 스키마다.** 읽을 수 없는 파일은 조용히 건너뛰고, 필드가 없으면 그 파일만 버린다.
Claude Code 업데이트로 모양이 바뀌어도 데몬이 죽어서는 안 된다.

### ② 상태 — `store.py` (훅이 쓴다)

`~/.paneglow/state/<session_id>.json`. 파일명이 곧 조인 키다.
원자적 쓰기(temp → fsync → rename)와 `rev` 역행 방지는 기존 구현을 그대로 쓴다.

`SessionRecord`에서 `tty` 필드를 뺀다. 그러면 `by_tty()`와 `prune()`의 "재사용된 tty의 고아 정리"
분기가 함께 사라진다 — pty 재활용이 없어졌으므로 존재 이유가 없다.

`prune()`의 생존 판정은 ①의 목록이 결정한다. 목록에 없는 `session_id`의 레코드는 지운다.
TTL은 ①을 통째로 못 읽을 때의 안전망으로만 남긴다.

### ③ 딥링크 매핑 — `deeplink.py` (누를 때만)

`~/Library/Application Support/Claude/claude-code-sessions/<org>/<account>/local_*.json` 에
`cliSessionId` ↔ `sessionId` 매핑이 있다. 파일이 200KB 안팎이라 **폴링 루프에서 읽지 않는다.**

## 슬롯 배정 — `slots.py`

순수 함수 하나다. 데몬이 직전 배정을 들고 있는다.

```python
def assign(prev: list[str | None],          # 직전 배정 (길이 6)
           live: dict[str, float],          # session_id -> last_activity
           policy: str) -> list[str | None]
```

**`recent_sticky`(기본)** — 근육 기억을 지키는 정책이다.

1. `prev`에서 `live`에 없는 세션의 슬롯을 비운다.
2. 새 세션은 **빈 슬롯 중 가장 앞**에 넣는다. 여러 개면 최근 활동순.
3. 슬롯이 가득 찬 상태에서 새 세션이 오면 **가장 오래 조용한 세션**을 밀어낸다.
4. 이미 슬롯을 가진 세션은 **절대 움직이지 않는다.**

밀려난 세션과 7번째 이후 세션은 **C키 움직임 집계**로 넘어간다 — 6키에도 C키에도
없어 완전히 사라지는 세션이 생기면 안 된다.

**`recent`** — 매 틱 최근 활동순으로 재정렬. Codex 순정과 같은 동작.
**`priority`** — `waiting` > `error` > `done` > `working` > `idle` 순.

## 렌더 — `render.py`

기존 구현을 그대로 쓴다. `Pane`이 `Session`이 되고 `is_claude`가 빠진다 —
목록에 오른 것은 정의상 전부 Claude 세션이다.

```python
@dataclass(frozen=True)
class Session:
    session_id: str
    state: AgentState | None      # None = 훅이 아직 아무것도 안 썼다 → 소등
```

팔레트와 우선순위는 변경 없다. 기존 `underglow_for()` 는 **색이 아니라 효과를 돌려준다** —
집계가 `waiting`이면 펄스(또는 회전), `error`면 깜빡임, 그 외에는 계속 켜짐. 이름은 `alert_effect()`.
C키 **색**은 상태를 보지 않으므로 `render` 를 거치지 않고 게이트가 정한다.

## 딥링크 — `deeplink.py`

```
A키 press → slots[i] → session_id → local_sessionId → open(url)
```

```
claude://claude.ai/claude-code-desktop/<local_sessionId>
```

**host 자리에 라우트를 넣으면 조용히 실패한다.** 앱 내부 네비게이터가 pathname의 앞 두 조각을
버리기 때문이다. 근거와 실패한 형태 전체는 [딥링크 실측](../../verification/deeplink.md).

`claude://resume?session=<cliSessionId>` 는 **쓰지 않는다.** 훅의 id를 그대로 넘길 수 있어
유혹적이지만 데스크톱 세션에는 사본을 만든다.

매핑을 못 찾으면 **아무것도 하지 않는다.** 앱만 앞으로 내는 폴백도 두지 않는다 —
엉뚱한 세션이 열린 것처럼 보이는 편이 더 나쁘다.

## 게이트

**레이어 게이트**는 그대로다. `layer_index != 1`이면 6키와 입력을 포기하고, C키 백라이트는 설정에 따라 유지한다.
하드웨어 사실이라 협상 불가.

**frontmost 게이트**는 bundle id만 갈아끼운다. `NSWorkspace` 활성 앱 변경 알림(폴백 1초),
TCC 권한 불필요.

| 보고 있는 앱 | 6키 | C키 색 | C키 움직임 | 입력 |
|---|---|---|---|---|
| `com.anthropic.claudefordesktop` | 우리 | `#FF6D00` | 슬롯 밖 `waiting`·`error` 에 따라 | 처리 |
| `com.openai.codex` | **소등 후 양보** | `#304FFE` | 모든 Claude 세션 기준 | 처리 안 함 |
| 그 외 | 직전 유지 | 직전 유지 | 직전 유지 | 직전 기준 |
| 기동 직후 | 무소유(소등) | 소등 | — | 처리 안 함 |

테두리(`ambient`)는 어느 경우에도 쓰지 않는다. 벤더가 A키 입력 피드백으로 쓰는 존이다.

> 기존 `config.py` 기본값 `com.openai.chat`은 **틀렸다.** Codex 데스크톱 앱의 실제 bundle id는
> `com.openai.codex` 다. 지금 게이트를 켜면 Codex 앞에서 양보하지 않는다.

양보할 때 6키를 명시적으로 소등하고 넘긴다. 그냥 손을 떼면 우리 색이 남아
Codex 스레드 상태로 오독된다.

## 설정 — `~/.paneglow/config.json`

```jsonc
{
  "gate": {
    "mode": "frontmost",                          // frontmost | always | off
    "own_when": ["com.anthropic.claudefordesktop"],
    "yield_to": ["com.openai.codex"]
  },
  "layer_gate": { "agent_keys": "off", "ckeys": "keep" },
  "slots":  { "order": "recent_sticky" },         // recent_sticky | recent | priority
  "ckeys": {                                      // C키 백라이트 = 소유권 + 알림
    "claude": "#FF6D00",                          // 소유권 색
    "codex":  "#304FFE",
    "alert":  "pulse",                            // pulse | spin | off  -- 확인 필요 표시
    "scope":  "outside"                           // outside | all_sessions | off
  },
  "state":  { "ttl_minutes": 30, "done_fade_seconds": 180,
              "working_max_seconds": 900 },
  "timing": { "poll_ms": 250 }
}
```

`working_max_seconds`는 [훅 실측 §7](../../verification/hook-events.md)의 `working` 갇힘 대응이다.
ESC로 턴을 중단하면 `Stop`이 오지 않아 세션이 영원히 파랑에 갇힌다. 상한을 넘으면 `idle`로
떨어뜨린다 — "모르겠다"가 "작업 중"이라는 거짓말보다 낫다.

**삭제**: `mod_key` · `tab_switch` · `double_tap` · `approve` · `experimental.codex_status` ·
구 `underglow.when_iterm` · `underglow.*.current_tab` 모드.

설정 오류는 해당 키만 기본값으로 폴백하고 경고를 모은다. 기동을 거부하지 않는다 — 기존 동작 유지.

## 에러 처리 — fail-quiet

| 상황 | 동작 |
|---|---|
| 세션 파일 하나를 못 읽음 | 그 파일만 건너뛴다 |
| 세션 목록이 통째로 빔 | 6키 소등. `doctor`가 이유를 말한다 |
| 상태 파일이 깨짐 | 다음 틱에 다시 읽는다 |
| 딥링크 매핑을 못 찾음 | C키를 0.3초 깜빡여 눌림을 알리고 평소로 복귀. 로그 |
| `open` 실패 | 무동작. LED는 건드리지 않는다 |
| 패드 미연결 | 재시도. 상태 수집은 계속. 재연결 시 전체 재도색 |
| 레이어가 1이 아님 | 6키·입력 포기. C키는 설정대로 |
| macOS 절전 → 복귀 | HID·타이머 재수립, 전체 재도색 |
| 데몬 종료 | 6키·C키를 끄고 나간다. **write 후 flush 필수** — 그냥 쓰고 나가면 반영되지 않는다(실측) |

LED는 읽을 수 없으므로 덮어쓰기를 감지할 수 없다. 주기적 재도색은 하지 않고
**이름 붙일 수 있는 사건**에서만 다시 칠한다 — 게이트 전환, 레이어 변화, 패드 재연결,
절전 복귀, 세션 구성 변경.

## 테스트

실기가 필요한 것은 `pad` 하나다.

| 대상 | 방법 |
|---|---|
| `sessions.py` | tmp 디렉터리에 가짜 json — 정상 · 깨진 파일 · 필드 누락 · 죽은 pid · `kind:"bg"` |
| `slots.py` | 순수 함수. 등장·소멸·7개 초과·밀어내기·정책 3종·슬롯 불변 |
| `deeplink.py` | 매핑 파일 픽스처 + `subprocess` 목킹. URL 문자열을 정확히 검증 |
| `store.py` | 기존 테스트에서 tty 관련만 정리 |
| `render` `state` `protocol` `config` | 기존 테스트 유지. `underglow_for` → `alert_effect` 반영 |
| `daemon` | 넷 다 목킹. 게이트 전환·양보 시 소등 |

## 삭제 목록

| | 이유 |
|---|---|
| `iterm.py` (135줄) · `tests/test_iterm.py` | 트리가 없다 |
| `iterm2` 패키지 의존 · Python API 활성화 안내 | 안 쓴다 |
| 탭 뷰 · MOD 키 · MOD 고착 방지 | 전환할 탭이 없다 |
| 노브 탭 순차 전환 (구 설계 R7) | 위와 같음 |
| 세대(generation) · 입력 잠금 (구 §07) | 되돌릴 수 없는 동작이 없다 |
| 승인·거절 C2/C3 (구 설계 §04) | 유일한 수단이 Accessibility 권한이라 R7과 충돌 |
| 더블탭 = 창 맨 앞으로 | 딥링크가 이미 앱을 앞으로 낸다 |
| `store` 의 tty 재사용 분기 | pty 재활용이 없다 |

## 미확인

| 항목 | 대응 |
|---|---|
| 살아 있는 CLI 세션에 딥링크를 걸면 | 매핑이 없으면 무동작. 확인 후 필요하면 필터 추가 |
| 갓 생긴 세션의 `local_*.json` 기록 시점 | 잠시 못 누를 수 있다. `status`가 사유를 표시 |
| 계정이 여럿일 때 `<org>/<account>` 다중 | glob으로 전부 훑는다 |
| 매핑 파일이 쌓였을 때 조회 지연 | 누를 때만 스캔. 느려지면 캐시 도입 |
| 세션 15개 병렬에서 목표 지연 | 폴링 간격·LED 갱신 병합으로 대응 |
| 벤더가 6키를 다시 그리는 시점 | 양보 후 잔상. 기존 설계와 동일한 미해결 |

## 범위 밖

- Codex 세션 상태 표시 — 상태 소스가 없다
- 승인·거절 — 위 참조
- iTerm2 · 다중 창 · 탭
- Layer 2+ 키맵 관리 — Work Louder Input의 영역
- 메뉴바 UI
