# paneglow — Claude Code Desktop 세션 모델 설계

> 2026-08-02 · [기존 설계](../../design.html)의 iTerm2 pane 모델을 대체한다.
> 관문 실측: [훅 이벤트](../../verification/hook-events.md) · [딥링크](../../verification/deeplink.md) ·
> [하드웨어 노트](../../hardware-notes.md)

## 왜 바꾸나

기존 설계는 **iTerm2 한 탭 안의 pane들**을 6키에 올렸다. 실사용은 Claude Code Desktop으로
옮겨갔고, 데스크톱에는 pane도 탭도 분할 트리도 없다. 세션이 평평한 목록 하나로 존재한다.

동시에 Codex Micro의 순정 동작 — **열어둔 것이 6개를 넘어도 최근 것 위주로 보여준다** — 이
이 워크플로우에 잘 맞는다는 것이 사용 경험으로 확인됐다.

---

## 1. 요구사항

| # | | |
|---|---|---|
| R1 | 살아 있는 Claude Code 세션의 상태를 키별 색으로 표시 | 실측으로 가능 |
| R2 | 키를 누르면 **그 세션이 Claude 앱에서 열린다** | 실측으로 가능 |
| R3 | 세션이 6개를 넘어도 최근 것 위주로. 배치는 예측 가능할 것 | |
| R4 | Codex 앱을 볼 때 6키를 벤더에게 양보 | |
| R5 | **테두리로 지금 6키의 상태가 Claude 것인지 Codex 것인지 안다** | 실측으로 가능 (§5) |
| R6 | 6키에 안 보이는 대기·오류를 알린다 | |
| R7 | **C1~C7 은 건드리지 않는다** — 사용자 커스텀 영역 | |
| R8 | 유선·무선 양쪽에서 동작 | 실측으로 가능 |
| R9 | 특별한 권한을 요구하지 않는다 | 실측으로 가능 |

### 요구대로 하지 못한 것

**승인·거절(C2/C3)을 뺀다.** 원래 설계에 있던 기능이다. 데스크톱 앱에는 세션에 응답을 보낼
수단이 없고, 남은 길은 Accessibility 권한으로 앱 UI를 찌르는 것뿐이라 R9와 정면으로
충돌한다. C키를 쓰므로 R7과도 충돌한다.

**소유권 색은 브랜드색이 아니라 공장 팔레트를 쓴다.** 앱 아이콘에서 뽑은 `#D87050`(Claude) /
`#6888F8`(Codex)를 실기에서 시험했으나 LED에서 연하게 보였다. `#FF6D00` / `#304FFE` 로
간다 — 더 잘 읽히고, "그 제품을 쓸 때 가장 자주 보는 색"이라는 점에서 의미도 맞는다.

**"지금 보고 있는 세션" 표시를 뺀다.** 앱 안에서 마우스로 세션을 바꾸면 우리는 알 수 없어
"마지막으로 패드에서 연 세션"까지만 가능한데, 값에 비해 어휘를 하나 더 늘린다.

---

## 2. 실측으로 확정된 것

설계가 여기 딛고 서 있다. 방법과 원본 데이터는 각 문서에.

| | |
|---|---|
| **딥링크** | `claude://claude.ai/claude-code-desktop/<local_sessionId>` 로 세션이 열린다. host 자리에 라우트를 넣으면 **조용히 실패**한다 |
| **`claude://resume` 금지** | `importCliSession` 을 호출해 데스크톱 세션에는 **사본을 만든다** |
| **id 공간이 둘** | 훅의 `session_id` ≠ 앱의 `local_…`. `cliSessionId` 필드로 매핑 |
| **패드 왕복** | `device.status` 응답 수신. **유선·무선 양쪽** |
| **전송 판별** | IOKit `Transport` = `'USB'` / **`'Bluetooth Low Energy'`**. `'BLE'` 로는 오지 않는다 (§10) |
| **기기 이름** | 유선 `Codex Micro` · 무선 `Codex Micro #1`. **이름으로 찾으면 깨진다** |
| **기기 매칭** | `IOHIDManagerOpen` 은 `kIOReturnNotPermitted`. `IOServiceGetMatchingServices` + VID/PID 는 권한 없이 열린다. `PrimaryUsagePage` 로 거르면 **못 찾는다** |
| **`e`(효과)** | 1 켜짐 · 2 회전 · 3 무지개 회전 · 4 깜빡임 · 6 펄스. **기기가 스스로 돌린다.** 유선·무선 동일 |
| **효과 지속성** | 한 번 걸면 2분 후에도 계속 움직인다. 재전송 불필요 |
| **`thstatus` 는 키별 `e`** | 한 키만 따로 움직일 수 있다 |
| **존 독립성** | 우리 `thstatus` 는 다른 존을 안 건드린다 |
| **벤더가 쓰는 존** | A1~A6(스레드 상태 변화 시) · **테두리(A키 입력 시, 누른 키 색을 비춘다)** · **C키는 안 건드린다** |
| **벤더 트래픽이 보인다** | 채널이 공유다. **우리 명령은 notification 이라 ACK 의 `id` 가 `null`, 벤더 것은 `id` 가 있다** |
| **키 입력도 보인다** | 양보 중에도 `{"m":"v.oai.hid","p":{"k":"AG00","act":1}}` 가 온다 |
| **write 후 flush 필요** | 마지막 write 뒤 시간을 안 주면 반영되지 않는다 |
| **Codex bundle id** | `com.openai.codex`. 기존 기본값 `com.openai.chat` 은 **틀렸다** |

### 사람이 구분하지 못하는 차이에 의미를 싣지 않는다

`e4`(깜빡임)와 `e6`(펄스)는 **나란히 놓아야 겨우 구분된다.** 처음 설계는 펄스=확인 필요,
깜빡임=오류로 나눠놨는데 실기에서 구분되지 않았다.

그리고 애초에 나눌 필요가 없었다 — **대기인지 오류인지는 6키 색이 이미 말해준다**
(주황 / 빨강). 테두리가 할 일은 "화면 밖에서 뭔가 부른다" 하나면 충분하고, 가서 보면 안다.

**확실히 구분되는 축만 쓴다: 안 움직임 / 움직임 / 색이 없어짐.**

---

## 3. 동작 — 무엇이 어떻게 보이나

```
        ◯ 노브      [A1] [A2]      ● 스틱      A1~A6  세션 상태 (색)
        [A3] [A4]  [A5] [A6]                   누르면 그 세션이 앱에서 열린다
        [C1] [C2 ] [C3 ] [C4]                  C1~C7  건드리지 않는다 (R7)
        ⋮LED ◉터치  [═ C5+C6 ═]  [C7]          테두리  누구 것인가 + 알림
```

| 존 | 명령 | 이 설계에서 |
|---|---|---|
| A1~A6 | `thstatus` | **세션별 상태.** 양보 중에는 벤더 것 |
| 테두리 | `rgbcfg` → `ambient` | **색 = 누구 것 · 움직임 = 무슨 일** |
| C1~C7 백라이트 | `rgbcfg` → `keys` | **쓰지 않는다** |

### 테두리 — 색과 움직임이 서로 다른 것을 말한다

우리는 벤더와 **같은 팔레트**를 일부러 쓰므로(주황=대기, 초록=완료…) **6키만 봐서는
Codex 것인지 Claude 것인지 구분되지 않는다.** 테두리가 그 답을 준다.

| 색 | 뜻 |
|---|---|
| `#FF6D00` | 6키는 **Claude** 세션 상태 — 공장 팔레트의 `waiting` 주황 |
| `#304FFE` | 6키는 **Codex** 스레드 상태 (양보 중) — 공장 팔레트의 `working` 파랑 |
| 소등 | 무소유 · 게이트 off · 레이어 ≠ 1 |

| 상태 | 기본 효과 | 뜻 |
|---|---|---|
| `normal` | `solid` (`e1`) | 평소 — 색만 읽으면 된다 |
| `alert` | `blink` (`e4`) | **화면 밖에서 뭔가 너를 기다린다** |
| `fault` | `rainbow` (`e3`) | **paneglow 자체 장애** — 색을 못 믿으니 색을 버린다 |

세 상태 모두 **효과를 설정으로 바꿀 수 있다**(§10). 기본값만 위와 같다.
`fault` 를 `rainbow` 로 둔 이유는, 고장 났을 때는 소유권 색 자체를 믿을 수 없으므로 색을
버리는 표시가 정직하고 다른 무엇과도 헷갈리지 않아서다.

**속도(`s`)로 의미를 나누지 않는다.** 같은 효과의 속도 차이는 눈으로 구분되지 않는다.

**움직임은 언제나 Claude 얘기다.** Codex 세션의 상태는 얻을 경로가 없다(범위 밖).
파란 테두리가 깜빡이면 "6키는 Codex 것, 그런데 Claude가 너를 기다린다"는 뜻이다 —
Codex를 보는 동안 Claude 상태를 볼 방법이 이것뿐이라 이 조합이 가장 값지다.

`alert` 집계 대상은 슬롯에 못 오른 세션 전부이고, Codex를 볼 때는 **모든 Claude 세션**이다.

### 눌림에 대한 응답

키를 눌렀는데 열 수 없으면(매핑이 아직 없는 갓 생긴 세션 등) 테두리를 **0.3초 깜빡이고
평소로 돌아온다.** 상태가 아니라 눌림에 대한 대답이다. 없으면 눌린 것인지 키가 죽은 것인지
구분할 수단이 없다.

---

## 4. 구조

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
  벤더 ACK  ───────────────────────────────► 테두리 되찾기 (§5)
```

되돌릴 수 없는 동작이 하나도 없으므로 **기존 설계 §07의 세대(generation)·입력 잠금 기계를
뺀다.** 그 기계는 "엉뚱한 pane에 승인이 나가는 것"을 막으려고 있었다. 승인이 없으면 최악의
사고가 "엉뚱한 세션이 열린다"이고, 다시 누르면 끝난다.

| 파일 | 책임 | 변경 |
|---|---|---|
| `state.py` | 상태 어휘·우선순위 | 그대로 |
| `protocol.py` | 벤더 메시지·HID 프레이밍 | **`Transport` 정규화**(§10) · `e` 노출 |
| `store.py` | 상태 레코드 읽기/쓰기 | `tty` 필드·`by_tty()`·tty 재사용 분기 **삭제** |
| `render.py` | (세션+상태) → 6색 + 테두리 효과 | `Pane` → `Session`, `is_claude` 삭제 |
| `config.py` | 설정 로드·폴백 | 키 교체 |
| `sessions.py` | 살아있는 세션 목록 | **신규** |
| `slots.py` | 세션 → 6슬롯 배정 | **신규**, 순수 |
| `deeplink.py` | 세션을 앱에서 연다 | **신규** |
| `hook.py` | 훅 이벤트 → 상태 | 계획대로 |
| `pad.py` | IOKit 송수신 | 계획대로 |
| `daemon.py` `cli.py` | 게이트·루프·CLI | 계획대로 |

`iterm.py` 는 삭제한다. `iterm2` 패키지 의존과 iTerm2 Python API 활성화 안내도 함께 사라진다.

---

## 5. 테두리를 벤더에게서 되찾기

**벤더는 A키를 누를 때마다 테두리에 그 키의 색을 한 번 비춘다.** 선택 피드백이다.
디바이스가 non-exclusive라 벤더의 수신을 막을 수는 없다.

다행히 **경쟁이 아니라 번갈아 쓰는 관계다** — 벤더는 이벤트마다 한 번 쏘고 붙들지 않는다.
그리고 우리는 벤더가 언제 썼는지 **볼 수 있다.**

```
{"m":"v.oai.hid","p":{"k":"AG00","act":1}}                  ← 키 입력 (우리도 받는다)
{"result":{"ok":1},"id":679,"method":"v.oai.rgbcfg"}        ← 벤더가 테두리를 칠했다
{"result":{"ok":1},"id":177,"method":"v.oai.thstatus"}      ← 벤더가 6키를 칠했다
{"result":{"ok":1},"id":null,"method":"v.oai.rgbcfg"}       ← 이건 우리 것
```

**우리 명령은 notification이라 ACK의 `id` 가 `null` 이고, 벤더 것은 `id` 가 있다.**
이걸 안 거르면 우리 재도색이 자기 ACK를 보고 다시 재도색하는 되먹임 고리가 생긴다
(실측: 60초에 406회).

```
id 있는 v.oai.rgbcfg / v.oai.thstatus ACK 를 보면
  → 0.2초 뒤 테두리를 다시 칠한다 (연속 이벤트는 하나로 합친다)
```

- **주기적 재도색이 아니다.** 평소 트래픽은 0이고 벤더가 쓸 때만 한 번 쏜다.
- 잔상은 0.2초 남짓. 2초 폴링 방식은 최대 2초였다.
- **양보 중이 아닐 때도 돌려야 한다.** Claude를 보는 중에 A키를 눌러도 벤더는 백그라운드에서
  자기 스레드를 바꾸고 테두리를 칠한다.

> `ponytail:` 0.2초 지연과 이벤트 합치기는 실측 한 번으로 고른 값이다. 잔상이 거슬리면
> 줄이고 벤더가 늦게 쓰면 늘린다. `reclaim_delay_ms` 로 열어둔다.

---

## 6. 데이터 소스

### ① 세션 목록 — `sessions.py`

`~/.claude/sessions/*.json` 을 glob 한다. 파일 하나가 세션 하나이고 수백 바이트다.

```json
{"pid":34508,"sessionId":"998574ae-…","cwd":"…","name":"…",
 "kind":"interactive","entrypoint":"claude-desktop","startedAt":1785658815386}
```

**받아들이는 조건은 둘뿐이다.**

| 조건 | 이유 |
|---|---|
| `kind == "interactive"` | `bg` 는 백그라운드 잡이라 열 창이 없다 |
| `os.kill(pid, 0)` 가 성공 | 파일이 남아 있어도 죽은 세션은 뺀다 |

`entrypoint`(`claude-desktop` / `cli`)로는 **거르지 않는다.** 필터가 아니라 `paneglow status`
가 보여줄 메타로만 들고 있는다. CLI 세션이 딥링크로 안 열리면 매핑 조회가 빈손으로 끝나
무동작이 되므로 별도 분기가 필요 없다.

**비공개 스키마다.** 읽을 수 없는 파일은 조용히 건너뛰고, 필드가 없으면 그 파일만 버린다.
Claude Code 업데이트로 모양이 바뀌어도 데몬이 죽어서는 안 된다.

### ② 상태 — `store.py` (훅이 쓴다)

`~/.paneglow/state/<session_id>.json`. 파일명이 곧 조인 키다.
원자적 쓰기(temp → fsync → rename)와 `rev` 역행 방지는 기존 구현을 그대로 쓴다.

`SessionRecord` 에서 `tty` 필드를 뺀다. 그러면 `by_tty()` 와 `prune()` 의 "재사용된 tty의
고아 정리" 분기가 함께 사라진다 — pty 재활용이 없어졌으므로 존재 이유가 없다.

`prune()` 의 생존 판정은 ①의 목록이 결정한다. 목록에 없는 `session_id` 의 레코드는 지운다.
TTL은 ①을 통째로 못 읽을 때의 안전망으로만 남긴다.

### ③ 딥링크 매핑 — `deeplink.py` (누를 때만)

`~/Library/Application Support/Claude/claude-code-sessions/<org>/<account>/local_*.json` 에
`cliSessionId` ↔ `sessionId` 매핑이 있다. 파일이 200KB 안팎이라 **폴링 루프에서 읽지 않는다.**
`<org>` · `<account>` 는 계정별 uuid이므로 glob으로 찾는다 — 하드코딩 불가.

---

## 7. 슬롯 배정 — `slots.py`

순수 함수 하나다. 데몬이 직전 배정을 들고 있는다.

```python
def assign(prev: list[str | None],          # 직전 배정 (길이 6)
           live: dict[str, float],          # session_id -> last_activity
           policy: str) -> list[str | None]
```

**`recent_sticky`** — 근육 기억을 지키는 정책이다.

1. `prev` 에서 `live` 에 없는 세션의 슬롯을 비운다.
2. 새 세션은 **빈 슬롯 중 가장 앞**에 넣는다. 여러 개면 최근 활동순.
3. 슬롯이 가득 찬 상태에서 새 세션이 오면 **가장 오래 조용한 세션**을 밀어낸다.
4. 이미 슬롯을 가진 세션은 **절대 움직이지 않는다.**

밀려난 세션과 7번째 이후 세션은 **테두리 `alert` 집계**로 넘어간다 — 6키에도 테두리에도
없어 완전히 사라지는 세션이 생기면 안 된다.

**`recent`(기본)** — 매 틱 최근 활동순으로 재정렬. Codex 순정과 같은 동작이고,
Desktop 사이드바의 Sort by Recency와 키 순서를 맞춘다(#45). 대가는 키를 누르려는 순간
대상이 바뀔 수 있다는 것이고, 그때는 `recent_sticky`로 되돌린다.
**`priority`** — `waiting` > `error` > `done` > `working` > `idle` 순.

---

## 8. 딥링크 — `deeplink.py`

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

매핑을 못 찾으면 **테두리를 0.3초 깜빡이고** 아무것도 하지 않는다. 앱만 앞으로 내는 폴백은
두지 않는다 — 엉뚱한 세션이 열린 것처럼 보이는 편이 더 나쁘다.

---

## 9. 게이트

**레이어 게이트**는 그대로다. `layer_index != 1` 이면 6키와 입력을 포기하고, 테두리는 설정에
따라 유지한다. 하드웨어 사실이라 협상 불가.

**frontmost 게이트**는 bundle id만 갈아끼운다. `NSWorkspace` 활성 앱 변경 알림(폴백 1초),
TCC 권한 불필요.

| 보고 있는 앱 | 6키 | 테두리 색 | 테두리 효과 | 입력 |
|---|---|---|---|---|
| `com.anthropic.claudefordesktop` | 우리 | `#FF6D00` | 슬롯 밖 `waiting`·`error` 기준 | 처리 |
| `com.openai.codex` | **소등 후 양보** | `#304FFE` | 모든 Claude 세션 기준 | 처리 안 함 |
| 그 외 | 직전 유지 | 직전 유지 | 직전 유지 | 직전 기준 |
| 기동 직후 | 무소유(소등) | 소등 | — | 처리 안 함 |

"그 외"가 **직전 유지**인 것이 중요하다. 브라우저를 볼 때마다 패드가 꺼졌다 켜졌다 하면
거슬리고, "잠깐 검색하러 나갔다 돌아오는" 동작이 가장 흔하다. 기동 직후만 예외로 무소유인데
참조할 직전이 없어서다.

양보할 때 6키를 명시적으로 소등하고 넘긴다. 그냥 손을 떼면 우리 색이 남아 Codex 스레드
상태로 오독된다.

---

## 10. `Transport` 정규화 — 지금 코드의 버그

[`protocol.py`](../../../src/paneglow/protocol.py) 는 `BLE = "BLE"` 를 기대하는데
**IOKit은 `'Bluetooth Low Energy'` 를 준다.** 지금 코드로 무선에서 돌리면 예외가 난다.

```python
ValueError: unsupported transport: 'Bluetooth Low Energy'
```

**예외가 나는 편이 다행이다.** 모르는 값을 USB로 폴백하게 짰다면 63바이트 프레이밍이 BLE로
나가 **조용히 버려졌을 것**이고, 그건 이 파일이 존재하는 이유인 바로 그 실패 모드다.

`pad.py` 가 IOKit 문자열을 상수로 정규화한다. 모르는 값은 **거부한다** — 추측해서 보내면
증상 없이 실패한다.

```python
def normalize_transport(value: str) -> str:
    v = (value or "").lower()
    if "bluetooth" in v or v == "ble":
        return protocol.BLE
    if "usb" in v:
        return protocol.USB
    raise ValueError(f"모르는 전송: {value!r}")
```

기기 이름도 전송에 따라 다르다 — 유선 `Codex Micro`, 무선 `Codex Micro #1`.
**이름으로 매칭하지 말 것.** VID/PID 로만 찾는다.

---

## 11. 설정 — `~/.paneglow/config.json`

```jsonc
{
  "gate": {
    "mode": "frontmost",                          // frontmost | always | off
    "own_when": ["com.anthropic.claudefordesktop"],
    "yield_to": ["com.openai.codex"]
  },
  "layer_gate": { "agent_keys": "off", "underglow": "keep" },
  "slots":  { "order": "recent" },                // recent | recent_sticky | priority
  "underglow": {                                  // 테두리
    "claude": "#FF6D00",
    "codex":  "#304FFE",
    "effects": {                                  // solid | blink | pulse | spin | rainbow | off
      "normal": "solid",
      "alert":  "blink",
      "fault":  "rainbow"
    },
    "scope":  "outside",                          // outside | all_sessions | off
    "reclaim_delay_ms": 200                       // 벤더가 쓴 뒤 되찾기까지 (§5)
  },
  "state":  { "ttl_minutes": 30, "done_fade_seconds": 180,
              "working_max_seconds": 900 },
  "timing": { "poll_ms": 250 }
}
```

효과 이름은 `e` 값을 가린다: `solid`=1 · `spin`=2 · `rainbow`=3 · `blink`=4 · `pulse`=6.

**두 상태에 같은 효과를 넣으면 경고를 남긴다.** 막지는 않는다 — 사용자의 선택이다.
`rainbow` 는 색을 무시하므로 `normal` 이나 `alert` 에 쓰면 소유권 색이 사라진다는 경고도 낸다.
`config.py` 가 이미 경고를 모으는 구조라 검사 하나를 더하면 된다.

`working_max_seconds` 는 [훅 실측 §7](../../verification/hook-events.md)의 `working` 갇힘
대응이다. ESC로 턴을 중단하면 `Stop` 이 오지 않아 세션이 영원히 파랑에 갇힌다. 상한을 넘으면
`idle` 로 떨어뜨린다 — "모르겠다"가 "작업 중"이라는 거짓말보다 낫다.

**삭제**: `mod_key` · `tab_switch` · `double_tap` · `approve` · `experimental.codex_status` ·
구 `underglow.when_iterm` · `underglow.*.current_tab` 모드.

설정 오류는 해당 키만 기본값으로 폴백하고 경고를 모은다. 기동을 거부하지 않는다.

---

## 12. 에러 처리 — fail-quiet

| 상황 | 동작 |
|---|---|
| 세션 파일 하나를 못 읽음 | 그 파일만 건너뛴다 |
| 세션 목록이 통째로 빔 | 6키 소등. `doctor` 가 이유를 말한다 |
| 상태 파일이 깨짐 | 다음 틱에 다시 읽는다 |
| 딥링크 매핑을 못 찾음 | 테두리 0.3초 깜빡. 로그 |
| `open` 실패 | 무동작. LED는 건드리지 않는다 |
| **모르는 `Transport` 값** | 거부하고 `doctor` 에 남긴다. 추측해서 보내지 않는다 (§10) |
| 패드 미연결 | 재시도. 상태 수집은 계속. 재연결 시 전체 재도색 |
| 레이어가 1이 아님 | 6키·입력 포기. 테두리는 설정대로 |
| macOS 절전 → 복귀 | HID·타이머 재수립, 전체 재도색 |
| **데몬 종료** | 6키·테두리를 끄고 나간다. **write 후 flush 필수** — 그냥 쓰고 나가면 반영되지 않는다 |

LED는 읽을 수 없으므로 덮어쓰기를 감지할 수 없다. 다만 **벤더 트래픽은 보이므로**(§5)
테두리에 한해서는 되찾을 수 있다. 6키는 양보 중이라 되찾을 필요가 없다.

---

## 13. 테스트

실기가 필요한 것은 `pad` 하나다.

| 대상 | 방법 |
|---|---|
| `sessions.py` | tmp 디렉터리에 가짜 json — 정상 · 깨진 파일 · 필드 누락 · 죽은 pid · `kind:"bg"` |
| `slots.py` | 순수 함수. 등장·소멸·7개 초과·밀어내기·정책 3종·슬롯 불변 |
| `deeplink.py` | 매핑 파일 픽스처 + `subprocess` 목킹. URL 문자열을 정확히 검증 |
| 되찾기 로직 | 가짜 ACK 스트림. **`id` 가 `null` 인 우리 ACK에는 반응하지 않을 것** (되먹임 회귀) |
| `normalize_transport` | `'Bluetooth Low Energy'` · `'USB'` · 모르는 값은 거부 |
| `config.py` | 효과 이름 검증 · 중복 효과 경고 · `rainbow` 경고 |
| `store.py` | 기존 테스트에서 tty 관련만 정리 |
| `render` `state` `protocol` | 기존 테스트 유지 |
| `daemon` | 넷 다 목킹. 게이트 전환·양보 시 소등 |

---

## 14. 삭제 목록

| | 이유 |
|---|---|
| `iterm.py` (135줄) · `tests/test_iterm.py` | 트리가 없다 |
| `iterm2` 패키지 의존 · Python API 활성화 안내 | 안 쓴다 |
| 탭 뷰 · MOD 키 · MOD 고착 방지 | 전환할 탭이 없다 |
| 노브 탭 순차 전환 (구 설계 R7) | 위와 같음 |
| 세대(generation) · 입력 잠금 (구 §07) | 되돌릴 수 없는 동작이 없다 |
| 승인·거절 C2/C3 (구 설계 §04) | Accessibility 권한이 필요해 R7·R9와 충돌 |
| 더블탭 = 창 맨 앞으로 | 딥링크가 이미 앱을 앞으로 낸다 |
| `store` 의 tty 재사용 분기 | pty 재활용이 없다 |
| 주기적 재도색 | 이벤트 기반으로 대체 (§5) |
| C키 백라이트 사용 | 사용자 커스텀 영역 (R7) |

---

## 15. 미확인

| 항목 | 대응 |
|---|---|
| auto-dim 이 우리 색을 어떻게 바꾸는가 | 조사 중. LED를 읽을 수 없으므로 어두워져도 감지 못 한다 |
| 살아 있는 CLI 세션에 딥링크를 걸면 | 매핑이 없으면 무동작. 확인 후 필요하면 필터 추가 |
| 갓 생긴 세션의 `local_*.json` 기록 시점 | 잠시 못 누를 수 있다. `status` 가 사유를 표시 |
| 계정이 여럿일 때 `<org>/<account>` 다중 | glob으로 전부 훑는다 |
| 매핑 파일이 쌓였을 때 조회 지연 | 누를 때만 스캔. 느려지면 캐시 도입 |
| 세션 15개 병렬에서 목표 지연 | 폴링 간격·LED 갱신 병합으로 대응 |
| `reclaim_delay_ms` 의 적정값 | 실측 한 번으로 0.2초를 골랐다. 사용하며 조정 |
| BLE 에서의 패킷 유실 | 6회 연속 write 가 전부 ACK 됐다. 유실 대비는 아직 없다 |

## 16. 범위 밖

- Codex 세션 상태 표시 — 상태 소스가 없다
- 승인·거절 — §1 참조
- iTerm2 · 다중 창 · 탭
- C1~C7 키맵과 백라이트 — 사용자 커스텀 영역 (R7)
- Layer 2+ 키맵 관리 — Work Louder Input의 영역
- 메뉴바 UI — CLI가 안정된 뒤
