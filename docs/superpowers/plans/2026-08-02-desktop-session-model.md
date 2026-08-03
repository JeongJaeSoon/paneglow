# Claude Code Desktop 세션 모델 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codex Micro의 6개 Agent 키가 살아 있는 Claude Code Desktop 세션의 상태를 색으로 표시하고, 키를 누르면 그 세션이 앱에서 열리며, 테두리가 지금 6키가 누구 것인지와 화면 밖 알림을 보여준다.

**Architecture:** 순수 함수 `sessions`·`slots`·`render`가 (세션 목록 + 훅 상태) → (6색 + 테두리)를 계산하고, `daemon`이 두 게이트(레이어/소유권)와 테두리 되찾기를 관리하며 `pad`·`deeplink`를 엮는다. 상태는 훅이 원자적으로 쓰는 JSON 파일로 주고받는다. 하드웨어가 필요한 것은 `pad` 하나뿐이다.

**Tech Stack:** Python 3.10+ · IOKit(ctypes) · pytest. 외부 의존은 표준 라이브러리뿐이다.

**설계 문서:** [`2026-08-02-desktop-session-model-design.md`](../specs/2026-08-02-desktop-session-model-design.md)
**실측 근거:** [`hardware-notes.md`](../../hardware-notes.md) · [`deeplink.md`](../../verification/deeplink.md) · [`hook-events.md`](../../verification/hook-events.md)

## Global Constraints

- **macOS 전용.** IOKit `IOHIDDevice`를 ctypes로 직접 쓴다. `hidapi`는 이 기기에서 `open_path()`가 항상 실패한다.
- **하드웨어:** VID `0x303A` / PID `0x8360`, 벤더 컬렉션 usage page `0xFF00`, **Report ID 6**.
- **기기 매칭은 VID/PID 로만 한다.** `PrimaryUsagePage`로 거르면 못 찾는다(벤더 컬렉션은 하위 컬렉션). `Product` 이름도 전송에 따라 다르다(`Codex Micro` / `Codex Micro #1`).
- **`IOHIDManagerOpen`을 쓰지 말 것.** `kIOReturnNotPermitted`가 난다. `IOServiceGetMatchingServices` + `IOHIDDeviceCreate` + `IOHIDDeviceOpen`을 쓴다.
- **프레이밍:** USB `[0x02][len][json]` 63바이트 / BLE `[0x06][0x02][len][json]` 64바이트.
- **`Transport` 값은 `"USB"` / `"Bluetooth Low Energy"`.** `"BLE"`로는 오지 않는다.
- **`v.oai.*`는 notification** — `id`를 넣으면 `404 Method not found`. 그래서 우리 명령의 ACK는 `id`가 `null`이고 **벤더 것은 `id`가 있다.**
- **성공 리턴 코드는 무의미하다.** 유일한 검증은 `device.status` 왕복이다.
- **마지막 write 뒤 flush가 필요하다.** 쓰고 바로 종료하면 반영되지 않는다.
- **Layer 1에서만 동작한다.** `device.status.layer_index`는 1-indexed.
- **C1~C7은 건드리지 않는다.** 키맵도 백라이트도 사용자 영역이다.
- 설정 `~/.paneglow/config.json`, 상태 `~/.paneglow/state/`, 로그 `~/.paneglow/logs/`.
- 커밋 메시지는 Conventional Commits, **영문**.
- 모든 공개 함수에 타입 힌트를 단다.

---

## 실행 인수인계 보정 (2026-08-03)

PR #15를 만든 Claude Code 세션의 전체 대화와 실측 프로브를 다시 대조하면서, 아래 누락을
발견했다. **이 절은 뒤의 예시 코드와 충돌할 때 우선한다.** 뒤의 코드는 태스크 경계를 보여주는
초안이고, 각 PR의 테스트와 실제 동작이 최종 계약이다.

1. **"최근"은 시작 시각이 아니라 활동 시각이다.** 슬롯 입력값은 상태 레코드가 있으면
   `record.updated_at`, 아직 훅 상태가 없으면 `session.started_at`을 쓴다. 뒤 Task 10처럼 항상
   `started_at`만 넘기면 오래된 세션이 방금 `waiting`이 되어도 "가장 오래 조용한 세션"으로
   잘못 축출된다. 상태 변경 뒤 슬롯 유지·축출을 검증하는 테스트를 추가한다.
2. **상태 레코드에는 `pid`도 필요 없다.** 훅 payload에는 Claude 프로세스 PID가 없고 훅 프로세스의
   부모 PID를 기록해도 거짓 데이터다. 생존 판단은 `~/.claude/sessions/*.json`이 전담한다.
   `SessionRecord`는 `session_id`, `cwd`, `state`, `rev`, `updated_at`만 가진다. `paneglow hook`은
   payload를 받은 시점의 `time.time_ns()`를 `rev`로, `time.time()`을 `updated_at`으로 쓰며 어떤
   입력 오류에서도 0을 반환한다. `agent_type` 키가 존재하면 값의 truthiness와 무관하게 서브에이전트
   이벤트로 보고 버리고, `PermissionDenied`도 관측 문서대로 `ERROR`에 포함한다. `SessionEnd`는
   상태를 덮지 않고 authoritative 세션 스캔의 prune에 맡긴다.
3. **R2 키 입력 경로를 daemon에 반드시 구현한다.** `v.oai.hid`의 `AG00`~`AG05`, `act == 1`만
   슬롯 0~5에 매핑한다. 현재 소유권이 `claude`이고 마지막으로 왕복 검증한 layer가 1일 때만
   `deeplink.open_session()`을 호출한다. release·노브·C키·Codex 양보 중 입력은 버린다.
4. **레이어·연결 상태는 추정하지 않는다.** 기동과 재연결 때 `device.status` 왕복이 성공하기
   전에는 LED와 입력을 arm하지 않는다. `layer_index != 1`이면 6키를 쓰지 않고 입력도 버린다.
   disconnect, sleep/wake, callback 오류 뒤에는 기존 매핑을 폐기하고 HID를 다시 열어 status 왕복
   후 전체 재도색한다. 연결 중에도 기본 1초 cadence로 status를 갱신하고, timeout 동안은 이전
   layer를 계속 믿지 않고 fail-closed한다. Layer 1→2→1 전환과 timeout을 fake clock/pad 및
   실기 양쪽에서 검증한다.
5. **Task 11은 hook 두 테스트로 끝나지 않는다.** `run`(foreground), `start`, `stop`, `status`,
   `doctor`, `hook`, `install-hooks`를 구현한다. 단일 인스턴스 PID/lock, SIGTERM 종료, 종료 시
   `Pad.close()` flush, 원자적 runtime snapshot, 기존 Claude hooks를 보존하는 병합 설치와 백업을
   각각 테스트한다. `status`는 snapshot을 읽고, `doctor`는 패드 왕복·Transport·세션 디렉터리·
   딥링크 매핑·훅 설치를 실제로 점검한다. installer의 이벤트 목록은 `SessionStart`,
   `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`,
   `Notification`, `Stop`, `StopFailure`, `PreCompact`, `SessionEnd`이고, 각각 기존 hook을 보존한다.
6. **frontmost 판별은 실제 무권한 경로를 남긴다.** 표준 라이브러리만 유지하므로 AppKit Python
   패키지를 가정하지 않는다. Objective-C runtime을 `ctypes`로 호출하거나, 실기 검증한 macOS
   내장 폴백을 쓰고 Claude/Codex/그 외 전환을 통합 테스트한다.
7. **Task 9의 `...`는 완료 코드가 아니다.** callback과 report buffer의 수명, CF 객체 release,
   run-loop schedule/unschedule, `IOHIDDeviceSetReport` 오류, 요청 id 상관관계, idempotent close,
   마지막 write flush까지 구현·테스트한다. 유선과 BLE 각각 `device.status` 왕복이 통과해야 PR을
   머지한다. 실측 scratchpad의 `padprobe.py`는 VID/PID로 찾은 뒤 다시 잘못된
   `PrimaryUsagePage == 0xFF00` 필터를 적용하므로 **복사하지 않는다**. VID/PID-only인
   `border_demo.py`의 discovery를 출발점으로 삼되 Transport 누락 시 USB 폴백은 제거한다.
   CFUNCTYPE input/removal callback, report buffer, run loop와 mode를 `Pad`가 강하게 보관하고,
   open/schedule/poll/unschedule/close를 같은 thread/run loop에서 수행한다. close는 write flush의
   성공 여부와 무관하게 `finally`에서 callback 해제 → unschedule → `IOHIDDeviceClose` →
   `CFRelease` 순으로 끝낸다.
8. **세션 스캔은 결과와 신뢰도를 함께 돌려준다.** 디렉터리 부재·전체 파싱 실패와 "살아 있는
   세션이 실제로 0개"를 모두 `[]`로 합치면 daemon이 `live_ids=set()`을 넘겨 정상 상태 파일까지
   즉시 지운다. `sessions.scan()`은 `sessions`, `authoritative`, 진단을 담은 snapshot을 반환하고,
   신뢰할 수 없을 때만 `store.prune(..., live_ids=None)`의 TTL 폴백을 사용한다.
9. **시간 기반 설정을 실제 표시 경로에 적용한다.** `WORKING`이 `working_max_seconds`를 넘으면
   파생 표시 상태를 `IDLE`로 내리고, `DONE`은 `done_fade_seconds` 뒤 소등한다. 원본 store
   레코드는 바꾸지 않는다. `WAITING`·`ERROR`는 만료시키지 않는다. 경계값과 0초 설정을 테스트한다.
10. **repaint는 원인과 존별로 나눈다.** 세션/상태/슬롯/소유권/layer 변화는 즉시 원하는 상태를
    다시 그린다. 외부 `rgbcfg`/`lights.preview` ACK는 ambient만 되찾고, 외부 `thstatus` ACK는
    Claude 소유일 때만 A존을 되찾는다. Codex 소유 중 외부 `thstatus`에 반응해 6키를 소등하면
    벤더의 정상 표시를 깨므로 반드시 무시한다.
11. **입력 실패 피드백과 runtime 진단도 계약이다.** 매핑 없음·`open` 실패 시 ambient를 0.3초
    움직인 뒤 직전 표시로 복구한다. 모든 state diff와 pad 연결 epoch를 원자적 snapshot에 남겨
    `status`가 예시 문자열이 아니라 실제 실행 상태를 설명하게 한다. GUI hook은 셸 `PATH`를
    가정하지 않고 설치 시 해석한 실행 파일 절대 경로를 기록한다.

### 순차 PR 경계

1. 이 구현 계획 인수인계
2. `protocol` 전송 정규화·LED 효과 (#16)
3. `store`·`render`·`config` 세션 어휘 전환 (#17, #14). 이 단계에서는 전체 테스트를 위해
   구 `Pane` API를 deprecated 호환 shim으로 유지한다
4. `sessions`·`slots`와 실제 활동 시각 (#18)
5. `deeplink` (#19)
6. `hook` (#5)
7. `pad`와 유선·무선 왕복 (#10)
8. `daemon`의 게이트·키 입력·재연결·테두리 되찾기 (#20)
9. `cli` lifecycle·doctor·hook 설치 (#21)
10. 호환 shim과 iTerm2 제거, 패키징 (#22)
11. 설치·첫 빛·실사용 검증 (#11)

각 PR은 해당 테스트와 전체 비통합 테스트를 통과시키고, 열린 review thread와 실패한 check가
없음을 확인한 뒤 squash merge한다. 다음 PR은 갱신된 `main`에서 시작한다.

---

## 파일 구조

| 파일 | 책임 | 상태 |
|---|---|---|
| `src/paneglow/state.py` | 상태 어휘와 우선순위 | 그대로 |
| `src/paneglow/protocol.py` | 메시지 조립·프레이밍·전송 정규화 | Task 1 |
| `src/paneglow/store.py` | 상태 레코드 원자적 읽기/쓰기 | Task 2 |
| `src/paneglow/sessions.py` | 살아 있는 세션 목록 | Task 3 **신규** |
| `src/paneglow/slots.py` | 세션 → 6슬롯 배정 | Task 4 **신규** |
| `src/paneglow/deeplink.py` | 세션을 앱에서 연다 | Task 5 **신규** |
| `src/paneglow/render.py` | (세션+상태) → 6색·알림 등급 | Task 6 |
| `src/paneglow/config.py` | 설정 로드·폴백·경고 | Task 7 |
| `src/paneglow/hook.py` | 훅 이벤트 → 상태 | Task 8 **신규** |
| `src/paneglow/pad.py` | IOKit 송수신 | Task 9 **신규** |
| `src/paneglow/daemon.py` | 게이트·루프·되찾기 | Task 10 **신규** |
| `src/paneglow/cli.py` | `start`/`stop`/`status`/`doctor`/`hook` | Task 11 **신규** |
| `src/paneglow/iterm.py` | — | Task 12 **삭제** |

Task 1~8은 하드웨어 없이 전부 테스트된다. Task 9부터 실기가 필요하다.

---

## Task 1: `protocol` — 전송 정규화와 효과

지금 코드는 **무선에서 예외가 난다.** `BLE = "BLE"`인데 IOKit은 `'Bluetooth Low Energy'`를 준다. 그리고 LED 효과(`e`)를 solid/off 두 값으로만 쓰고 있다.

**Files:**
- Modify: `src/paneglow/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces:
  - `normalize_transport(value: str) -> str` — `protocol.USB` 또는 `protocol.BLE`. 모르는 값은 `ValueError`
  - `EFFECTS: dict[str, int]` — `{"off":0, "solid":1, "spin":2, "rainbow":3, "blink":4, "pulse":6}`
  - `rgbcfg(keys=..., ambient=...)` — 각 값은 `None`(소등) · `int`(색, solid) · `tuple[int, str]`(색, 효과 이름)
  - `thstatus(colors: list[int | None]) -> dict` — 시그니처 유지

- [ ] **Step 1: 전송 정규화 테스트를 쓴다**

`tests/test_protocol.py` 끝에 추가한다.

```python
import pytest
from paneglow import protocol


def test_normalize_transport_accepts_iokit_strings():
    # 실측: IOKit 은 'BLE' 가 아니라 전체 이름을 준다
    assert protocol.normalize_transport("Bluetooth Low Energy") == protocol.BLE
    assert protocol.normalize_transport("USB") == protocol.USB


def test_normalize_transport_is_case_insensitive():
    assert protocol.normalize_transport("bluetooth low energy") == protocol.BLE
    assert protocol.normalize_transport("usb") == protocol.USB


def test_normalize_transport_refuses_unknown():
    # 추측해서 보내면 잘못된 프레이밍이 조용히 버려진다. 거부가 옳다.
    for bad in ("", None, "Serial", "SPI"):
        with pytest.raises(ValueError):
            protocol.normalize_transport(bad)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_protocol.py -k normalize -v`
Expected: FAIL — `AttributeError: module 'paneglow.protocol' has no attribute 'normalize_transport'`

- [ ] **Step 3: 정규화를 구현한다**

`src/paneglow/protocol.py`의 `USB`/`BLE` 상수 바로 아래에 넣는다.

```python
def normalize_transport(value: str | None) -> str:
    """IOKit 의 Transport 속성을 우리 상수로 바꾼다.

    실측값은 'USB' 와 'Bluetooth Low Energy' 다. 'BLE' 로는 오지 않는다.
    모르는 값을 USB 로 폴백하면 63바이트 프레이밍이 BLE 로 나가 조용히 버려진다 --
    이 파일이 존재하는 이유가 그 실패 모드이므로, 모르면 거부한다.
    """
    text = (value or "").lower()
    if "bluetooth" in text or text == "ble":
        return BLE
    if "usb" in text:
        return USB
    raise ValueError(f"unknown transport: {value!r}")
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_protocol.py -k normalize -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 효과 테스트를 쓴다**

```python
def test_effects_map_to_measured_values():
    # 실측 (하드웨어 노트 §3). 5 는 1 과 구분되지 않아 뺐다.
    assert protocol.EFFECTS == {
        "off": 0, "solid": 1, "spin": 2, "rainbow": 3, "blink": 4, "pulse": 6}


def test_rgbcfg_plain_colour_stays_solid():
    msg = protocol.rgbcfg(ambient=0xFF6D00)
    assert msg["p"]["ambient"] == {"e": 1, "b": 1, "s": 1, "c": 0xFF6D00}


def test_rgbcfg_accepts_colour_and_effect():
    msg = protocol.rgbcfg(ambient=(0x304FFE, "blink"))
    assert msg["p"]["ambient"] == {"e": 4, "b": 1, "s": 1, "c": 0x304FFE}


def test_rgbcfg_none_turns_the_zone_off():
    assert protocol.rgbcfg(ambient=None)["p"]["ambient"]["e"] == 0


def test_rgbcfg_refuses_unknown_effect():
    with pytest.raises(ValueError):
        protocol.rgbcfg(ambient=(0xFF6D00, "sparkle"))
```

- [ ] **Step 6: 실패를 확인한다**

Run: `python -m pytest tests/test_protocol.py -k "effects or rgbcfg" -v`
Expected: FAIL — `EFFECTS` 없음, 튜플 처리 없음

- [ ] **Step 7: 효과를 구현한다**

`_EFFECT_OFF` / `_EFFECT_SOLID` 상수를 지우고 `EFFECTS`로 대체한다. `_side()`를 아래로 교체한다.

```python
#: 실측한 효과 값 (하드웨어 노트 §3). 5 는 1 과 구분되지 않아 노출하지 않는다.
EFFECTS: dict[str, int] = {
    "off": 0, "solid": 1, "spin": 2, "rainbow": 3, "blink": 4, "pulse": 6,
}


def _side(value: int | None | tuple[int, str]) -> dict:
    """존 하나의 설정. 색만 주면 solid, (색, 효과) 튜플이면 그 효과."""
    if value is None:
        return {"e": EFFECTS["off"], "b": 0, "s": 0, "c": 0}
    color, effect = value if isinstance(value, tuple) else (value, "solid")
    if effect not in EFFECTS:
        raise ValueError(f"unknown effect: {effect!r}")
    return {"e": EFFECTS[effect], "b": 1, "s": 1, "c": color}
```

`_entry()`의 `_EFFECT_OFF`/`_EFFECT_SOLID`도 `EFFECTS["off"]`/`EFFECTS["solid"]`로 바꾼다.

- [ ] **Step 8: 전체 테스트를 돌린다**

Run: `python -m pytest tests/test_protocol.py -v`
Expected: 기존 테스트 포함 전부 PASS. `_side`의 `s`가 0→1로 바뀌었으므로 기존 기대값을 쓰는 테스트가 있으면 함께 고친다.

- [ ] **Step 9: 커밋**

```bash
git add src/paneglow/protocol.py tests/test_protocol.py
git commit -m "fix: normalize the IOKit transport string and expose LED effects"
```

---

## Task 2: `store` — tty 를 걷어낸다

pane이 사라져 pty 재활용도 사라졌다. `tty` 필드와 그에 딸린 분기가 통째로 불필요해진다.

**Files:**
- Modify: `src/paneglow/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `SessionRecord(session_id: str, cwd: str, state: AgentState, rev: int, updated_at: float, pid: int)` — **`tty` 없음**
  - `prune(root: Path, live_ids: set[str] | None, ttl_seconds: float, now: float) -> int`
  - `by_tty()` 는 **삭제**

- [ ] **Step 1: 새 계약을 테스트로 쓴다**

`tests/test_store.py`에서 `tty=` 를 넘기는 모든 호출을 지우고, 아래를 추가한다.

```python
def test_prune_keeps_records_whose_session_is_live(tmp_path):
    rec = SessionRecord(session_id="alive", cwd="/x", state=AgentState.WAITING,
                        rev=1, updated_at=100.0, pid=1)
    store.write(rec, tmp_path)
    # 30분째 조용해도 살아 있으면 남는다 -- TTL 은 목록을 못 읽을 때만 쓴다
    removed = store.prune(tmp_path, live_ids={"alive"}, ttl_seconds=1.0, now=1e9)
    assert removed == 0
    assert [r.session_id for r in store.read_all(tmp_path)] == ["alive"]


def test_prune_drops_records_whose_session_is_gone(tmp_path):
    store.write(SessionRecord("dead", "/x", AgentState.IDLE, 1, 100.0, 1), tmp_path)
    assert store.prune(tmp_path, live_ids=set(), ttl_seconds=1e9, now=200.0) == 1
    assert store.read_all(tmp_path) == []


def test_prune_falls_back_to_ttl_when_the_session_list_is_unknown(tmp_path):
    store.write(SessionRecord("stale", "/x", AgentState.IDLE, 1, 100.0, 1), tmp_path)
    assert store.prune(tmp_path, live_ids=None, ttl_seconds=10.0, now=200.0) == 1
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `SessionRecord.__init__() missing 1 required positional argument: 'tty'`

- [ ] **Step 3: `store.py` 를 고친다**

```python
@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    cwd: str
    state: AgentState
    rev: int
    updated_at: float
    pid: int
```

`_load()`에서 `tty=raw["tty"],` 를 지운다. `by_tty()` 함수를 통째로 지운다. `prune()`을 아래로 교체한다.

```python
def prune(root: Path, live_ids: set[str] | None,
          ttl_seconds: float, now: float) -> int:
    """죽은 세션의 레코드를 지운다.

    ``live_ids`` 를 알 때는 그것이 결정한다 -- 조용한 세션도 살아 있으면 남긴다.
    TTL 은 세션 목록을 통째로 못 읽을 때의 폴백이다.

    쓰기 락 아래에서 돈다. 아니면 읽기와 unlink 사이에 훅이 레코드를 갈아치워
    갓 쓴 상태가 지워진다.
    """
    if not root.exists():
        return 0

    with _write_lock(root):
        removed = 0
        for rec in read_all(root):
            dead = (rec.session_id not in live_ids) if live_ids is not None \
                else (now - rec.updated_at > ttl_seconds)
            if dead:
                _path(root, rec.session_id).unlink(missing_ok=True)
                removed += 1
    return removed
```

모듈 docstring의 tty 관련 문단도 지운다.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_store.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/store.py tests/test_store.py
git commit -m "refactor: drop tty from the session record"
```

---

## Task 3: `sessions` — 살아 있는 세션 목록

`iterm.py`를 대체한다. 트리 대신 평평한 목록이고, 파일 glob 하나가 전부다.

**Files:**
- Create: `src/paneglow/sessions.py`
- Test: `tests/test_sessions.py`

**Interfaces:**
- Produces:
  - `Session(session_id: str, cwd: str, name: str, entrypoint: str, pid: int, started_at: float)`
  - `SESSIONS_DIR: Path` — `~/.claude/sessions`
  - `live(root: Path | None = None, alive=os.kill) -> list[Session]` — 최근 시작순 정렬

- [ ] **Step 1: 테스트를 쓴다**

```python
import json
import pytest
from paneglow import sessions


def _write(root, pid, **overrides):
    payload = {"pid": pid, "sessionId": f"sid-{pid}", "cwd": "/w",
               "name": f"n{pid}", "kind": "interactive",
               "entrypoint": "claude-desktop", "startedAt": 1000 + pid}
    payload.update(overrides)
    (root / f"{pid}.json").write_text(json.dumps(payload))


def _alive_always(pid, sig):
    return None


def test_live_returns_interactive_sessions(tmp_path):
    _write(tmp_path, 1)
    got = sessions.live(tmp_path, alive=_alive_always)
    assert [s.session_id for s in got] == ["sid-1"]
    assert got[0].entrypoint == "claude-desktop"


def test_live_skips_background_jobs(tmp_path):
    _write(tmp_path, 1, kind="bg")
    assert sessions.live(tmp_path, alive=_alive_always) == []


def test_live_keeps_cli_sessions(tmp_path):
    # entrypoint 로는 거르지 않는다. 표시용 메타일 뿐이다.
    _write(tmp_path, 1, entrypoint="cli")
    assert len(sessions.live(tmp_path, alive=_alive_always)) == 1


def test_live_skips_dead_pids(tmp_path):
    _write(tmp_path, 1)
    _write(tmp_path, 2)

    def alive(pid, sig):
        if pid == 2:
            raise ProcessLookupError
        return None

    assert [s.pid for s in sessions.live(tmp_path, alive=alive)] == [1]


def test_live_survives_a_broken_file(tmp_path):
    # 비공개 스키마다. 한 파일이 깨져도 나머지는 살아야 한다.
    _write(tmp_path, 1)
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "partial.json").write_text('{"pid": 3}')
    assert [s.pid for s in sessions.live(tmp_path, alive=_alive_always)] == [1]


def test_live_returns_empty_when_the_directory_is_missing(tmp_path):
    assert sessions.live(tmp_path / "nope", alive=_alive_always) == []


def test_live_is_sorted_most_recent_first(tmp_path):
    _write(tmp_path, 1, startedAt=100)
    _write(tmp_path, 2, startedAt=300)
    _write(tmp_path, 3, startedAt=200)
    assert [s.pid for s in sessions.live(tmp_path, alive=_alive_always)] == [2, 3, 1]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_sessions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.sessions'`

- [ ] **Step 3: 구현한다**

```python
"""살아 있는 대화형 Claude Code 세션 목록.

Claude Code 가 세션마다 ~/.claude/sessions/<pid>.json 을 쓴다. 수백 바이트짜리
파일이라 폴링 루프에서 읽어도 싸다. 이 스키마는 **비공개**이므로 읽을 수 없는
파일은 조용히 건너뛴다 -- 업데이트로 모양이 바뀌어도 데몬이 죽어서는 안 된다.
"""
from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SESSIONS_DIR = Path.home() / ".claude" / "sessions"


@dataclass(frozen=True)
class Session:
    session_id: str
    cwd: str
    name: str
    entrypoint: str      # 'claude-desktop' | 'cli'. 필터가 아니라 표시용 메타다.
    pid: int
    started_at: float


def _parse(path: Path) -> Session | None:
    try:
        raw = json.loads(path.read_text())
        if raw["kind"] != "interactive":     # bg 잡은 열 창이 없다
            return None
        return Session(
            session_id=str(raw["sessionId"]), cwd=str(raw["cwd"]),
            name=str(raw.get("name") or ""),
            entrypoint=str(raw.get("entrypoint") or ""),
            pid=int(raw["pid"]), started_at=float(raw.get("startedAt") or 0),
        )
    except Exception:
        return None


def live(root: Path | None = None,
         alive: Callable[[int, int], None] = os.kill) -> list[Session]:
    """살아 있는 대화형 세션. 최근에 시작한 것부터.

    ``alive`` 는 테스트에서 갈아끼우기 위한 구멍이다. os.kill(pid, 0) 은 신호를
    보내지 않고 프로세스 존재만 확인한다.
    """
    root = SESSIONS_DIR if root is None else root
    if not root.exists():
        return []

    out: list[Session] = []
    for path in root.glob("*.json"):
        session = _parse(path)
        if session is None:
            continue
        try:
            alive(session.pid, 0)
        except Exception:
            continue          # 파일이 남아 있어도 죽은 세션은 뺀다
        out.append(session)
    return sorted(out, key=lambda s: s.started_at, reverse=True)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_sessions.py -v`
Expected: 7 passed

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/sessions.py tests/test_sessions.py
git commit -m "feat: list live interactive Claude Code sessions"
```

---

## Task 4: `slots` — 최근순 고정 슬롯

키를 눌렀을 때 어디로 갈지 예측 가능해야 한다. 슬롯이 매번 재정렬되면 주황이 떠서 손이 가는 순간에 대상이 바뀐다.

**Files:**
- Create: `src/paneglow/slots.py`
- Test: `tests/test_slots.py`

**Interfaces:**
- Consumes: `state.AgentState`, `state.PRIORITY`
- Produces:
  - `COUNT: int = 6`
  - `assign(prev: list[str | None], live: dict[str, float], policy: str = "recent_sticky", states: dict[str, AgentState] | None = None) -> list[str | None]`

- [ ] **Step 1: 테스트를 쓴다**

```python
from paneglow import slots
from paneglow.state import AgentState

EMPTY = [None] * 6


def test_new_sessions_take_the_earliest_free_slot():
    got = slots.assign(EMPTY, {"a": 1.0})
    assert got == ["a", None, None, None, None, None]


def test_several_new_sessions_fill_in_recency_order():
    got = slots.assign(EMPTY, {"old": 1.0, "new": 9.0})
    assert got[:2] == ["new", "old"]


def test_an_existing_session_never_moves():
    prev = [None, "b", None, None, None, None]
    got = slots.assign(prev, {"b": 5.0, "c": 9.0})
    assert got[1] == "b"          # c 가 더 최근이어도 b 는 제자리
    assert got[0] == "c"


def test_a_dead_session_frees_its_slot():
    prev = ["a", "b", None, None, None, None]
    got = slots.assign(prev, {"b": 1.0})
    assert got == [None, "b", None, None, None, None]


def test_a_freed_slot_is_reused():
    prev = ["a", "b", None, None, None, None]
    got = slots.assign(prev, {"b": 1.0, "new": 2.0})
    assert got == ["new", "b", None, None, None, None]


def test_a_full_board_evicts_the_quietest():
    prev = [f"s{i}" for i in range(6)]
    live = {f"s{i}": float(i) for i in range(6)}      # s0 가 가장 오래 조용하다
    live["fresh"] = 99.0
    got = slots.assign(prev, live)
    assert "s0" not in got
    assert got[0] == "fresh"
    assert got[1:] == [f"s{i}" for i in range(1, 6)]


def test_recent_policy_reorders_every_tick():
    prev = ["a", "b", None, None, None, None]
    got = slots.assign(prev, {"a": 1.0, "b": 9.0}, policy="recent")
    assert got[:2] == ["b", "a"]


def test_priority_policy_puts_waiting_first():
    live = {"working": 9.0, "waiting": 1.0}
    states = {"working": AgentState.WORKING, "waiting": AgentState.WAITING}
    got = slots.assign(EMPTY, live, policy="priority", states=states)
    assert got[0] == "waiting"


def test_an_unknown_policy_falls_back_to_recent_sticky():
    got = slots.assign(EMPTY, {"a": 1.0}, policy="nonsense")
    assert got[0] == "a"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_slots.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현한다**

```python
"""세션을 6개 슬롯에 배정한다. 순수 -- 파일도 하드웨어도 모른다.

기본 정책이 배치를 고정하는 이유는 근육 기억 때문이다. 매 틱 재정렬하면 알림이
떠서 손이 가는 순간에 그 키가 가리키는 세션이 바뀌어 있을 수 있다.
"""
from __future__ import annotations

from paneglow.state import PRIORITY, AgentState

COUNT = 6


def _sticky(prev: list[str | None], live: dict[str, float]) -> list[str | None]:
    out: list[str | None] = [s if s in live else None for s in prev]
    held = {s for s in out if s is not None}
    incoming = sorted((s for s in live if s not in held),
                      key=lambda s: live[s], reverse=True)

    for session_id in incoming:
        try:
            out[out.index(None)] = session_id
            continue
        except ValueError:
            pass
        # 자리가 없다. 가장 오래 조용한 세션을 밀어낸다.
        oldest = min(range(COUNT), key=lambda i: live[out[i]])   # type: ignore[index]
        if live[session_id] > live[out[oldest]]:                 # type: ignore[index]
            out[oldest] = session_id
    return out


def _ordered(live: dict[str, float], key) -> list[str | None]:
    ranked = sorted(live, key=key, reverse=True)[:COUNT]
    return list(ranked) + [None] * (COUNT - len(ranked))


def assign(prev: list[str | None], live: dict[str, float],
           policy: str = "recent_sticky",
           states: dict[str, AgentState] | None = None) -> list[str | None]:
    """직전 배정과 살아 있는 세션으로 새 배정을 만든다.

    ``live`` 는 session_id -> 마지막 활동 시각. ``prev`` 는 길이 6.
    """
    if len(prev) != COUNT:
        prev = ([*prev] + [None] * COUNT)[:COUNT]

    if policy == "recent":
        return _ordered(live, key=lambda s: live[s])
    if policy == "priority":
        ranks = states or {}
        return _ordered(live, key=lambda s: (
            PRIORITY.get(ranks.get(s, AgentState.IDLE), 0), live[s]))
    return _sticky(prev, live)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_slots.py -v`
Expected: 9 passed

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/slots.py tests/test_slots.py
git commit -m "feat: assign sessions to six sticky slots"
```

---

## Task 5: `deeplink` — 세션을 앱에서 연다

**URL 형태를 틀리면 조용히 실패한다.** host 자리에 라우트를 넣으면 앱이 경로 앞 두 조각을 버려 id가 먹힌다. 근거는 [딥링크 실측](../../verification/deeplink.md).

**Files:**
- Create: `src/paneglow/deeplink.py`
- Test: `tests/test_deeplink.py`

**Interfaces:**
- Produces:
  - `MAPPING_GLOB: str`
  - `url_for(local_id: str) -> str`
  - `local_id_for(session_id: str, roots: list[Path] | None = None) -> str | None`
  - `open_session(session_id: str, roots=None, runner=subprocess.run) -> bool`

- [ ] **Step 1: 테스트를 쓴다**

```python
import json
from pathlib import Path

from paneglow import deeplink


def _mapping(tmp_path: Path, local_id: str, cli_id: str) -> Path:
    root = tmp_path / "org" / "account"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{local_id}.json").write_text(
        json.dumps({"sessionId": local_id, "cliSessionId": cli_id, "title": "t"}))
    return tmp_path


def test_url_puts_the_route_in_the_path_not_the_host():
    # host 자리에 라우트를 넣으면 앱이 id 를 먹고 조용히 실패한다.
    assert deeplink.url_for("local_abc") == \
        "claude://claude.ai/claude-code-desktop/local_abc"


def test_local_id_is_found_through_the_cli_session_id(tmp_path):
    _mapping(tmp_path, "local_aaa", "cli-111")
    assert deeplink.local_id_for("cli-111", [tmp_path]) == "local_aaa"


def test_local_id_is_none_when_unmapped(tmp_path):
    _mapping(tmp_path, "local_aaa", "cli-111")
    assert deeplink.local_id_for("cli-999", [tmp_path]) is None


def test_local_id_survives_a_broken_mapping_file(tmp_path):
    _mapping(tmp_path, "local_aaa", "cli-111")
    (tmp_path / "org" / "account" / "local_bad.json").write_text("{oops")
    assert deeplink.local_id_for("cli-111", [tmp_path]) == "local_aaa"


def test_open_session_runs_open_with_the_exact_url(tmp_path):
    _mapping(tmp_path, "local_aaa", "cli-111")
    calls = []
    assert deeplink.open_session("cli-111", [tmp_path], runner=calls.append) is True
    assert calls == [["open", "claude://claude.ai/claude-code-desktop/local_aaa"]]


def test_open_session_does_nothing_when_unmapped(tmp_path):
    _mapping(tmp_path, "local_aaa", "cli-111")
    calls = []
    assert deeplink.open_session("cli-999", [tmp_path], runner=calls.append) is False
    assert calls == []


def test_open_session_reports_failure_instead_of_raising(tmp_path):
    _mapping(tmp_path, "local_aaa", "cli-111")

    def boom(cmd):
        raise OSError("no open(1)")

    assert deeplink.open_session("cli-111", [tmp_path], runner=boom) is False
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_deeplink.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현한다**

```python
"""세션을 Claude 데스크톱 앱에서 연다.

URL 형태를 틀리면 **조용히 실패한다.** 앱의 네비게이터가 pathname 의 앞 두 조각을
버리므로, 라우트를 host 자리에 넣으면 세션 id 가 통째로 먹히고 빈 라우트가 열린다.
에러도 로그도 남지 않는다. 실패한 형태 전체는 docs/verification/deeplink.md.

claude://resume 은 쓰지 않는다 -- 훅의 id 를 그대로 받아 편해 보이지만
importCliSession 을 호출해 데스크톱 세션에는 사본을 만든다.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

#: 앱이 쓰는 세션 메타. <org>/<account> 는 계정별 uuid 라 glob 이어야 한다.
MAPPING_ROOT = (Path.home() / "Library" / "Application Support" / "Claude"
                / "claude-code-sessions")
MAPPING_GLOB = "*/*/local_*.json"

_ROUTE = "claude://claude.ai/claude-code-desktop"


def url_for(local_id: str) -> str:
    return f"{_ROUTE}/{local_id}"


def local_id_for(session_id: str, roots: list[Path] | None = None) -> str | None:
    """훅의 session_id 로 앱의 local_ id 를 찾는다.

    파일 하나가 200KB 안팎(트랜스크립트 포함)이라 폴링 루프에서 부르면 안 된다.
    키를 누른 그 순간에만 부른다.
    """
    for root in (roots if roots is not None else [MAPPING_ROOT]):
        for path in sorted(root.glob(MAPPING_GLOB)):
            try:
                raw = json.loads(path.read_text())
            except Exception:
                continue
            if raw.get("cliSessionId") == session_id:
                found = raw.get("sessionId")
                if isinstance(found, str) and found:
                    return found
    return None


def open_session(session_id: str, roots: list[Path] | None = None,
                 runner: Callable[[list[str]], object] = subprocess.run) -> bool:
    """앱에서 그 세션을 연다. 못 열면 False -- 아무 폴백도 하지 않는다.

    앱만 앞으로 내는 폴백을 두지 않는 이유는, 엉뚱한 세션이 열린 것처럼 보이는
    편이 아무 일도 안 일어나는 것보다 나쁘기 때문이다.
    """
    local_id = local_id_for(session_id, roots)
    if local_id is None:
        return False
    try:
        runner(["open", url_for(local_id)])
    except Exception:
        return False
    return True
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_deeplink.py -v`
Expected: 7 passed

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/deeplink.py tests/test_deeplink.py
git commit -m "feat: open a desktop session by deep link"
```

---

## Task 6: `render` — Pane 을 Session 으로

**Files:**
- Modify: `src/paneglow/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Produces:
  - `Session(session_id: str, state: AgentState | None)`
  - `render_keys(sessions: list[Session]) -> list[int | None]`
  - `overflow(sessions: list[Session]) -> list[Session]`
  - `alert_level(states) -> str` — `"alert"` 또는 `"normal"`
  - `Pane` · `render_pane_view` · `underglow_for` 는 **삭제**

- [ ] **Step 1: 테스트를 고쳐 쓴다**

`tests/test_render.py`의 `Pane`·`render_pane_view`·`underglow_for` 사용처를 아래로 바꾼다.

```python
from paneglow import render
from paneglow.render import Session
from paneglow.state import AgentState


def test_keys_take_the_palette_colour():
    got = render.render_keys([Session("a", AgentState.WAITING)])
    assert got[0] == render.PALETTE[AgentState.WAITING]
    assert got[1:] == [None] * 5


def test_a_session_without_state_is_dark():
    # 훅이 아직 아무것도 안 썼다. 슬롯은 잡았지만 색은 없다.
    assert render.render_keys([Session("a", None)])[0] is None


def test_only_six_sessions_fit():
    many = [Session(f"s{i}", AgentState.IDLE) for i in range(8)]
    assert len(render.render_keys(many)) == 6
    assert [s.session_id for s in render.overflow(many)] == ["s6", "s7"]


def test_alert_level_fires_on_waiting_and_error():
    assert render.alert_level([AgentState.WAITING]) == "alert"
    assert render.alert_level([AgentState.ERROR]) == "alert"


def test_alert_level_is_quiet_otherwise():
    # done/working/idle 로도 켜면 알림이 늘 켜져 있어 신호가 죽는다.
    assert render.alert_level([AgentState.DONE, AgentState.WORKING]) == "normal"
    assert render.alert_level([]) == "normal"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_render.py -v`
Expected: FAIL — `ImportError: cannot import name 'Session'`

- [ ] **Step 3: `render.py` 를 고친다**

`Pane`·`render_pane_view`·`underglow_for`를 지우고 아래로 대체한다. `PALETTE`와 `KEY_COUNT`는 그대로 둔다.

```python
@dataclass(frozen=True)
class Session:
    session_id: str
    state: AgentState | None      # None = 훅이 아직 아무것도 안 썼다


def render_keys(sessions: list[Session]) -> list[int | None]:
    """여섯 키의 색. None 은 소등."""
    out: list[int | None] = [None] * KEY_COUNT
    for i, session in enumerate(sessions[:KEY_COUNT]):
        if session.state is not None:
            out[i] = PALETTE[session.state]
    return out


def overflow(sessions: list[Session]) -> list[Session]:
    """여섯 키에 못 오른 세션. 테두리 집계에 넣지 않으면 완전히 사라진다."""
    return list(sessions[KEY_COUNT:])


def alert_level(states: Iterable[AgentState]) -> str:
    """테두리가 움직여야 하는가. 'alert' 아니면 'normal'.

    done/working/idle 로도 켜면 테두리가 늘 켜져 있어 신호가 죽는다.
    대기인지 오류인지는 6키 색이 이미 말해주므로 여기서 나누지 않는다.
    """
    return "alert" if any(s in _NOTABLE for s in states) else "normal"
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_render.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/render.py tests/test_render.py
git commit -m "refactor: render sessions instead of panes"
```

---

## Task 7: `config` — 새 스키마와 경고

**Files:**
- Modify: `src/paneglow/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` 의 새 필드
  - `gate_mode: str`, `yield_to: tuple[str, ...]`, `own_when: tuple[str, ...]`
  - `slots_order: str`, `underglow_claude: int`, `underglow_codex: int`
  - `effect_normal: str`, `effect_alert: str`, `effect_fault: str`
  - `underglow_scope: str`, `reclaim_delay_ms: int`
  - `ttl_minutes: int`, `done_fade_seconds: int`, `working_max_seconds: int`, `poll_ms: int`
  - 삭제: `mod_key`, `knob_tab_switch`, `mod_direct_tab`, `underglow_iterm`, `mod_release_timeout_ms`

- [ ] **Step 1: 테스트를 쓴다**

`tests/test_config.py`에서 `mod_key`·`tab_switch` 관련 테스트를 지우고 추가한다.

```python
def test_bundle_ids_default_to_the_desktop_apps():
    cfg, warnings = config.load(None)
    assert cfg.own_when == ("com.anthropic.claudefordesktop",)
    assert cfg.yield_to == ("com.openai.codex",)      # com.openai.chat 이 아니다
    assert warnings == []


def test_ownership_colours_default_to_the_factory_palette(tmp_path):
    cfg, _ = config.load(None)
    assert cfg.underglow_claude == 0xFF6D00
    assert cfg.underglow_codex == 0x304FFE


def test_colours_accept_hex_strings(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"underglow": {"claude": "#123456"}}')
    cfg, warnings = config.load(p)
    assert cfg.underglow_claude == 0x123456
    assert warnings == []


def test_a_bad_colour_falls_back_and_warns(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"underglow": {"claude": "not a colour"}}')
    cfg, warnings = config.load(p)
    assert cfg.underglow_claude == 0xFF6D00
    assert any("claude" in w for w in warnings)


def test_effects_default_to_the_measured_choices():
    cfg, _ = config.load(None)
    assert (cfg.effect_normal, cfg.effect_alert, cfg.effect_fault) == \
        ("solid", "blink", "rainbow")


def test_an_unknown_effect_falls_back_and_warns(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"underglow": {"effects": {"alert": "sparkle"}}}')
    cfg, warnings = config.load(p)
    assert cfg.effect_alert == "blink"
    assert any("alert" in w for w in warnings)


def test_duplicate_effects_warn_but_are_allowed(tmp_path):
    # 사용자의 선택이므로 막지 않는다. 다만 두 상태가 구분되지 않게 된다.
    p = tmp_path / "c.json"
    p.write_text('{"underglow": {"effects": {"normal": "blink", "alert": "blink"}}}')
    cfg, warnings = config.load(p)
    assert cfg.effect_normal == "blink" and cfg.effect_alert == "blink"
    assert any("같은 효과" in w or "same effect" in w for w in warnings)


def test_rainbow_on_a_colour_bearing_state_warns(tmp_path):
    # rainbow 는 색을 무시하므로 소유권 색이 사라진다.
    p = tmp_path / "c.json"
    p.write_text('{"underglow": {"effects": {"normal": "rainbow"}}}')
    _, warnings = config.load(p)
    assert any("rainbow" in w for w in warnings)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — 필드 없음

- [ ] **Step 3: `config.py` 를 고친다**

상단 상수를 교체한다.

```python
from paneglow.protocol import EFFECTS

_GATE_MODES = {"frontmost", "always", "off"}
_SCOPES = {"outside", "all_sessions", "off"}
_ORDERS = {"recent_sticky", "recent", "priority"}
_COLOUR_STATES = ("normal", "alert")     # rainbow 를 쓰면 소유권 색이 사라지는 자리
```

`Config` 를 교체한다.

```python
@dataclass(frozen=True)
class Config:
    gate_mode: str = "frontmost"
    yield_to: tuple[str, ...] = ("com.openai.codex",)
    own_when: tuple[str, ...] = ("com.anthropic.claudefordesktop",)
    slots_order: str = "recent_sticky"
    underglow_claude: int = 0xFF6D00
    underglow_codex: int = 0x304FFE
    effect_normal: str = "solid"
    effect_alert: str = "blink"
    effect_fault: str = "rainbow"
    underglow_scope: str = "outside"
    reclaim_delay_ms: int = 200
    ttl_minutes: int = 30
    done_fade_seconds: int = 180
    working_max_seconds: int = 900
    poll_ms: int = 250
```

색 파서를 추가한다.

```python
def _colour(value, default: int, label: str, warnings: list[str]) -> int:
    """'#RRGGBB' 또는 정수. 다른 것은 기본값으로 떨어진다."""
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.lstrip("#"), 16)
        except ValueError:
            return _reject(value, default, label, warnings)
        if 0 <= parsed <= 0xFFFFFF:
            return parsed
    return _reject(value, default, label, warnings)
```

`load()` 안에서 `glow` 섹션을 읽고 경고를 낸다.

```python
    glow = _section(raw, "underglow", "underglow", warnings)
    fx = _section(glow, "effects", "underglow.effects", warnings)
    effects = {
        name: _pick(fx.get(name), set(EFFECTS), default,
                    f"underglow.effects.{name}", warnings)
        for name, default in (("normal", "solid"), ("alert", "blink"),
                              ("fault", "rainbow"))
    }
    for name in _COLOUR_STATES:
        if effects[name] == "rainbow":
            warnings.append(
                f"underglow.effects.{name}: rainbow 는 색을 무시하므로 "
                "소유권 색이 보이지 않는다")
    if len(set(effects.values())) < len(effects):
        warnings.append("underglow.effects: 두 상태에 같은 효과가 걸려 구분되지 않는다")
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_config.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/config.py tests/test_config.py
git commit -m "feat: config for the desktop session model"
```

---

## Task 8: `hook` — 훅 이벤트를 상태로

훅 실측([hook-events.md](../../verification/hook-events.md))이 밝힌 함정 셋을 그대로 반영한다: `Notification`은 **화이트리스트**로, 서브에이전트 이벤트는 **버리고**, `error`는 **별도 이벤트**로 온다.

**Files:**
- Create: `src/paneglow/hook.py`
- Test: `tests/test_hook.py`

**Interfaces:**
- Consumes: `store.SessionRecord`, `state.AgentState`
- Produces:
  - `classify(event: dict) -> AgentState | None`
  - `record_from(event: dict, rev: int, now: float, pid: int) -> SessionRecord | None`

- [ ] **Step 1: 테스트를 쓴다**

```python
from paneglow import hook
from paneglow.state import AgentState


def ev(name, **extra):
    return {"hook_event_name": name, "session_id": "s1", "cwd": "/w", **extra}


def test_session_start_is_idle():
    assert hook.classify(ev("SessionStart")) is AgentState.IDLE


def test_tool_use_is_working():
    for name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"):
        assert hook.classify(ev(name)) is AgentState.WORKING


def test_stop_is_done():
    assert hook.classify(ev("Stop")) is AgentState.DONE


def test_failures_are_errors():
    # 실측 정정: error 는 Stop 의 필드가 아니라 별도 이벤트로 온다
    assert hook.classify(ev("StopFailure")) is AgentState.ERROR
    assert hook.classify(ev("PostToolUseFailure")) is AgentState.ERROR


def test_only_whitelisted_notifications_are_waiting():
    assert hook.classify(
        ev("Notification", notification_type="permission_prompt")) is AgentState.WAITING
    assert hook.classify(
        ev("Notification", notification_type="agent_needs_input")) is AgentState.WAITING


def test_idle_prompt_is_not_waiting():
    # 실측: idle_prompt 를 waiting 으로 잡으면 노는 세션이 주황으로 거짓말한다
    assert hook.classify(ev("Notification", notification_type="idle_prompt")) is None
    assert hook.classify(ev("Notification", notification_type="auth_success")) is None
    assert hook.classify(ev("Notification")) is None


def test_unknown_events_are_ignored():
    assert hook.classify(ev("SomethingNew")) is None


def test_subagent_events_produce_no_record():
    # 실측: 서브에이전트는 같은 pane 에서 다른 session_id 로 이벤트를 낸다.
    # 거르지 않으면 부모의 waiting 을 덮어쓴다.
    assert hook.record_from(ev("PreToolUse", agent_type="claude"), 1, 1.0, 9) is None
    assert hook.record_from(ev("SubagentStop"), 1, 1.0, 9) is None


def test_record_carries_the_session_id_and_cwd():
    rec = hook.record_from(ev("Stop"), rev=7, now=12.5, pid=99)
    assert (rec.session_id, rec.cwd, rec.state) == ("s1", "/w", AgentState.DONE)
    assert (rec.rev, rec.updated_at, rec.pid) == (7, 12.5, 99)


def test_no_record_without_a_session_id():
    assert hook.record_from({"hook_event_name": "Stop"}, 1, 1.0, 9) is None
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_hook.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현한다**

```python
"""훅 이벤트를 상태로 옮긴다. 순수 + store 호출뿐이다.

세 가지가 실측으로 뒤집힌 전제다 (docs/verification/hook-events.md):
  - error 는 Stop 의 필드가 아니라 StopFailure / PostToolUseFailure 로 온다
  - Notification 은 종류가 여럿이라 화이트리스트여야 한다. idle_prompt 를
    waiting 으로 잡으면 노는 세션이 주황으로 거짓말한다
  - 서브에이전트가 같은 화면에서 다른 session_id 로 이벤트를 낸다. 거르지 않으면
    부모의 waiting 을 덮어쓴다 -- 이 프로젝트가 막으려는 바로 그 증상이다
"""
from __future__ import annotations

from paneglow.state import AgentState
from paneglow.store import SessionRecord

_WORKING = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}
_ERROR = {"StopFailure", "PostToolUseFailure"}
#: 블랙리스트로 하면 새 종류가 추가될 때마다 오작동한다.
_WAITING_NOTIFICATIONS = {"permission_prompt", "agent_needs_input"}


def classify(event: dict) -> AgentState | None:
    """이 이벤트가 뜻하는 상태. 상태를 안 바꾸는 이벤트면 None."""
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return AgentState.IDLE
    if name in _WORKING:
        return AgentState.WORKING
    if name == "Stop":
        return AgentState.DONE
    if name in _ERROR:
        return AgentState.ERROR
    if name == "Notification":
        if event.get("notification_type") in _WAITING_NOTIFICATIONS:
            return AgentState.WAITING
    return None


def record_from(event: dict, rev: int, now: float, pid: int) -> SessionRecord | None:
    """이벤트를 레코드로. 써서는 안 되는 이벤트면 None."""
    if event.get("agent_type") or event.get("hook_event_name") == "SubagentStop":
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    state = classify(event)
    if state is None:
        return None
    return SessionRecord(session_id=session_id, cwd=str(event.get("cwd") or ""),
                         state=state, rev=rev, updated_at=now, pid=pid)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_hook.py -v`
Expected: 10 passed

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/hook.py tests/test_hook.py
git commit -m "feat: classify hook events into agent states"
```

---

## Task 9: `pad` — IOKit 어댑터 (실기 필요)

여기부터 하드웨어가 필요하다. **성공 리턴 코드는 아무것도 증명하지 않는다** — `device.status` 왕복만이 검증이다.

**Files:**
- Create: `src/paneglow/pad.py`
- Test: `tests/test_pad.py`

**Interfaces:**
- Consumes: `protocol.normalize_transport`, `protocol.frame`, `protocol.FrameDecoder`, `protocol.status_request`
- Produces:
  - `VID: int`, `PID: int`, `REPORT_ID: int`
  - `Pad.open() -> Pad | None` (classmethod)
  - `Pad.send(message: dict) -> None`
  - `Pad.poll(seconds: float) -> list[dict]`
  - `Pad.status(timeout: float = 3.0) -> dict | None`
  - `Pad.close(flush_seconds: float = 1.0) -> None`
  - `is_vendor_write(message: dict) -> bool`

- [ ] **Step 1: 벤더 판별 테스트를 쓴다** (하드웨어 불필요)

```python
from paneglow import pad


def test_our_own_acks_are_not_vendor_writes():
    # 우리 명령은 notification 이라 ACK 의 id 가 null 이다.
    # 이걸 안 거르면 재도색이 자기 ACK 를 보고 다시 재도색한다 (실측: 60초에 406회)
    assert pad.is_vendor_write(
        {"result": {"ok": 1}, "id": None, "method": "v.oai.rgbcfg"}) is False


def test_id_bearing_acks_are_vendor_writes():
    assert pad.is_vendor_write(
        {"result": {"ok": 1}, "id": 679, "method": "v.oai.rgbcfg"}) is True
    assert pad.is_vendor_write(
        {"result": {"ok": 1}, "id": 177, "method": "v.oai.thstatus"}) is True


def test_key_events_are_not_vendor_writes():
    assert pad.is_vendor_write({"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}) is False


def test_our_own_status_reply_is_not_a_vendor_write():
    # device.status 는 우리가 id 를 넣어 보내므로 응답에도 id 가 있다.
    assert pad.is_vendor_write(
        {"result": {"version": "v0.4.1"}, "id": 1, "method": "device.status"}) is False
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_pad.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 판별 함수와 IOKit 래퍼를 구현한다**

`src/paneglow/pad.py`. ctypes 선언은 길지만 기계적이다. 검증된 프로브가
`docs/verification/` 의 실측 기록과 같은 순서를 따른다.

```python
"""벤더 HID 채널. IOKit 을 ctypes 로 직접 쓴다.

기기 매칭은 VID/PID 로만 한다:
  - PrimaryUsagePage 로 거르면 못 찾는다. 0xFF00 은 하위 컬렉션이라 주 usage 가
    아니고, 주 usage 는 키보드(0x0001/0x0006)다
  - Product 이름은 전송에 따라 다르다 ('Codex Micro' / 'Codex Micro #1')
  - IOHIDManagerOpen 은 매칭된 기기를 한꺼번에 열려 해서 kIOReturnNotPermitted 다
"""
from __future__ import annotations

import ctypes
import ctypes.util
import time
from ctypes import POINTER, byref, c_int32, c_uint8, c_uint32, c_void_p

from paneglow import protocol

VID, PID, REPORT_ID = 0x303A, 0x8360, 6
_OUTPUT_REPORT = 1
_UTF8, _SINT32 = 0x08000100, 3

_VENDOR_METHODS = {"v.oai.rgbcfg", "v.oai.thstatus", "lights.preview"}


def is_vendor_write(message: dict) -> bool:
    """벤더가 LED 를 건드렸다는 신호인가.

    우리 v.oai.* 는 notification 이라 ACK 의 id 가 null 이다. id 가 붙어 있으면
    우리가 안 보낸 명령의 응답, 즉 벤더 것이다.
    """
    return (message.get("id") is not None
            and message.get("method") in _VENDOR_METHODS)
```

이어서 IOKit 바인딩과 `Pad` 클래스를 넣는다. 핵심 규칙 셋을 지킨다:
`IOServiceGetMatchingServices` 로 열 것, `Transport` 를 `normalize_transport` 로 거를 것,
`close()` 에서 flush 할 것.

```python
class Pad:
    """열린 벤더 채널 하나."""

    def __init__(self, device: c_void_p, transport: str) -> None:
        self._device = device
        self.transport = transport
        self._decoder = protocol.FrameDecoder()
        self._inbox: list[bytes] = []

    @classmethod
    def open(cls) -> "Pad | None":
        """기기를 찾아 연다. 없거나 못 열면 None."""
        ...   # IOServiceMatching("IOHIDDevice") + VendorID/ProductID,
              # IOIteratorNext -> IOHIDDeviceCreate -> IOHIDDeviceOpen,
              # Transport 를 읽어 protocol.normalize_transport 로 거른다,
              # IOHIDDeviceRegisterInputReportCallback + ScheduleWithRunLoop

    def send(self, message: dict) -> None:
        for packet in protocol.frame(message, self.transport):
            buf = (c_uint8 * len(packet)).from_buffer_copy(packet)
            _iokit.IOHIDDeviceSetReport(self._device, _OUTPUT_REPORT, REPORT_ID,
                                        buf, len(packet))

    def poll(self, seconds: float) -> list[dict]:
        """런루프를 돌리며 들어온 메시지를 모은다."""
        ...

    def status(self, timeout: float = 3.0) -> dict | None:
        """device.status 왕복. 프레이밍이 맞는지 증명하는 유일한 수단이다."""
        ...

    def close(self, flush_seconds: float = 1.0) -> None:
        """LED 를 끄고 닫는다.

        write 직후 종료하면 그 색이 반영되지 않고 직전 색이 남는다 (실측).
        flush 없이 나가면 '종료 시 소등' 이 동작하지 않는다.
        """
        self.send(protocol.thstatus([None] * 6))
        self.send(protocol.rgbcfg(ambient=None))
        self.poll(flush_seconds)
```

> 구현자 주: 검증된 ctypes 호출 순서와 argtypes 는
> `docs/verification/deeplink.md` 가 아니라 **하드웨어 노트 §4** 에 있다.
> `IOHIDManagerOpen` 을 쓰면 `0xe00002e2` 가 난다.

- [ ] **Step 4: 판별 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_pad.py -v`
Expected: 4 passed

- [ ] **Step 5: 실기 통합 테스트를 쓴다**

```python
import pytest
from paneglow import pad


@pytest.mark.integration
def test_round_trip_on_real_hardware():
    device = pad.Pad.open()
    if device is None:
        pytest.skip("Codex Micro not connected")
    try:
        reply = device.status()
        assert reply is not None, "device.status 무응답 -- 프레이밍이 틀렸다"
        assert reply["result"]["layer_index"] == 1, "Layer 1 이 아니다"
        assert device.transport in (protocol.USB, protocol.BLE)
    finally:
        device.close()
```

- [ ] **Step 6: 실기로 확인한다**

패드를 **유선으로** 연결하고 Layer 1에서:
Run: `python -m pytest tests/test_pad.py -m integration -v`
Expected: PASS. `v0.4.1` 응답과 `transport == 'USB'`

**무선으로도 반복한다.** 케이블을 뽑고 BLE 로 연결한 뒤 같은 명령.
Expected: PASS, `transport == 'BLE'`. 실패하면 `normalize_transport` 를 의심한다.

- [ ] **Step 7: 커밋**

```bash
git add src/paneglow/pad.py tests/test_pad.py
git commit -m "feat: vendor HID channel over IOKit"
```

---

## Task 10: `daemon` — 게이트·루프·테두리 되찾기

**Files:**
- Create: `src/paneglow/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: 위 전부
- Produces:
  - `Owner` — `"claude"` | `"codex"` | `"none"`
  - `owner_for(bundle_id: str | None, previous: str, cfg: Config) -> str`
  - `Daemon(cfg, pad, ...)` with `tick(now: float) -> None`

- [ ] **Step 1: 게이트 테스트를 쓴다**

```python
from paneglow import config, daemon

CFG = config.Config()


def test_claude_app_gives_us_the_keys():
    assert daemon.owner_for("com.anthropic.claudefordesktop", "none", CFG) == "claude"


def test_codex_app_takes_them():
    assert daemon.owner_for("com.openai.codex", "claude", CFG) == "codex"


def test_any_other_app_keeps_the_previous_owner():
    # 브라우저를 볼 때마다 패드가 꺼졌다 켜지면 거슬린다.
    assert daemon.owner_for("com.google.Chrome", "claude", CFG) == "claude"
    assert daemon.owner_for("com.google.Chrome", "codex", CFG) == "codex"


def test_nothing_is_owned_before_a_known_app_appears():
    assert daemon.owner_for("com.google.Chrome", "none", CFG) == "none"


def test_gate_off_owns_nothing():
    cfg = config.Config(gate_mode="off")
    assert daemon.owner_for("com.anthropic.claudefordesktop", "claude", cfg) == "none"


def test_gate_always_owns_regardless_of_the_frontmost_app():
    cfg = config.Config(gate_mode="always")
    assert daemon.owner_for("com.openai.codex", "none", cfg) == "claude"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_daemon.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 게이트를 구현한다**

```python
def owner_for(bundle_id: str | None, previous: str, cfg: Config) -> str:
    """지금 6키가 누구 것인가.

    '그 외' 가 직전 유지인 것이 중요하다. 잠깐 검색하러 나갔다 돌아오는 동작이
    가장 흔한데, 그때마다 패드가 꺼졌다 켜지면 거슬린다. 기동 직후만 예외로
    무소유인데, 참조할 직전이 없어서다.
    """
    if cfg.gate_mode == "off":
        return "none"
    if cfg.gate_mode == "always":
        return "claude"
    if bundle_id in cfg.own_when:
        return "claude"
    if bundle_id in cfg.yield_to:
        return "codex"
    return previous
```

- [ ] **Step 4: 되찾기 테스트를 쓴다**

```python
def test_a_vendor_write_schedules_a_repaint():
    d = daemon.Daemon(CFG, pad=None)
    d.note_messages([{"result": {"ok": 1}, "id": 5, "method": "v.oai.rgbcfg"}], now=10.0)
    assert d.repaint_due == 10.2                # reclaim_delay_ms 기본 200


def test_our_own_ack_schedules_nothing():
    d = daemon.Daemon(CFG, pad=None)
    d.note_messages([{"result": {"ok": 1}, "id": None, "method": "v.oai.rgbcfg"}], 10.0)
    assert d.repaint_due is None


def test_consecutive_vendor_writes_coalesce():
    d = daemon.Daemon(CFG, pad=None)
    ack = {"result": {"ok": 1}, "id": 5, "method": "v.oai.rgbcfg"}
    d.note_messages([ack], now=10.0)
    d.note_messages([ack], now=10.1)
    assert d.repaint_due == 10.2                # 첫 예약이 유지된다
```

- [ ] **Step 5: 실패를 확인한 뒤 구현한다**

Run: `python -m pytest tests/test_daemon.py -k repaint -v` → FAIL

```python
    def note_messages(self, messages: list[dict], now: float) -> None:
        """패드에서 온 메시지를 본다. 벤더가 LED 를 건드렸으면 되찾기를 예약한다.

        주기적 재도색이 아니다. 평소 트래픽은 0 이고 벤더가 쓸 때만 한 번 쏜다.
        연속 이벤트는 하나로 합친다 -- 벤더는 한 번 누를 때 rgbcfg 와 thstatus 를
        연달아 보낸다.
        """
        if self.repaint_due is not None:
            return
        if any(pad.is_vendor_write(m) for m in messages):
            self.repaint_due = now + self.cfg.reclaim_delay_ms / 1000
```

- [ ] **Step 6: 통과를 확인한다**

Run: `python -m pytest tests/test_daemon.py -v`
Expected: 9 passed

- [ ] **Step 7: 루프를 구현한다**

`tick()` 이 하는 일을 순서대로:

```python
    def tick(self, now: float) -> None:
        live = sessions.live()
        ids = {s.session_id for s in live}
        store.prune(self.state_dir, ids, self.cfg.ttl_minutes * 60, now)
        records = {r.session_id: r for r in store.read_all(self.state_dir)}

        self.slots = slots.assign(
            self.slots, {s.session_id: s.started_at for s in live},
            self.cfg.slots_order,
            {sid: r.state for sid, r in records.items()})

        owner = owner_for(self.frontmost(), self.owner, self.cfg)
        if owner != self.owner:
            self.owner = owner
            self.repaint_due = now          # 소유권이 바뀌면 즉시 다시 칠한다

        self.note_messages(self.pad.poll(self.cfg.poll_ms / 1000), now)
        if self.repaint_due is not None and now >= self.repaint_due:
            self.repaint_due = None
            self.paint(records, live)
```

`paint()` 는 소유권에 따라 6키를 칠하거나 소등하고, 테두리 색과 효과를 정한다.

```python
    def paint(self, records, live) -> None:
        if self.owner == "none":
            self.pad.send(protocol.thstatus([None] * 6))
            self.pad.send(protocol.rgbcfg(ambient=None))
            return

        if self.owner == "claude":
            shown = [render.Session(sid, records[sid].state if sid in records else None)
                     if sid else render.Session("", None) for sid in self.slots]
            self.pad.send(protocol.thstatus(render.render_keys(shown)))
            colour = self.cfg.underglow_claude
            outside = [r.state for sid, r in records.items() if sid not in self.slots]
        else:
            # 양보한다. 우리 색이 남으면 Codex 스레드 상태로 오독된다.
            self.pad.send(protocol.thstatus([None] * 6))
            colour = self.cfg.underglow_codex
            outside = [r.state for r in records.values()]

        level = render.alert_level(outside)
        effect = self.cfg.effect_alert if level == "alert" else self.cfg.effect_normal
        self.pad.send(protocol.rgbcfg(ambient=(colour, effect)))
```

- [ ] **Step 8: 커밋**

```bash
git add src/paneglow/daemon.py tests/test_daemon.py
git commit -m "feat: gate, loop and event-driven border reclaim"
```

---

## Task 11: `cli` — start / stop / status / doctor / hook

키가 어두운 이유는 여럿인데 눈에는 전부 똑같이 보인다. **로그를 열어보게 하는 것은 답이 아니다.**

**Files:**
- Create: `src/paneglow/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: `hook` 서브커맨드 테스트를 쓴다**

```python
import json
from paneglow import cli, store


def test_hook_writes_a_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PANEGLOW_HOME", str(tmp_path))
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w"})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
    assert cli.main(["hook"]) == 0
    records = store.read_all(tmp_path / "state")
    assert [r.session_id for r in records] == ["s1"]


def test_hook_never_fails_the_turn(tmp_path, monkeypatch):
    # 훅이 0 이 아닌 값을 내면 Claude Code 의 턴을 방해할 수 있다.
    monkeypatch.setenv("PANEGLOW_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{not json"))
    assert cli.main(["hook"]) == 0
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현한다**

`hook` 은 stdin JSON을 읽어 `hook.record_from()` → `store.write()`. **무슨 일이 있어도 0을 낸다.**

`status` 는 각 키의 배정과 **왜 어두운지**를 말한다:

```
paneglow status
  데몬        실행 중 (pid 4211)
  패드        연결됨 · USB · v0.4.1 · layer 1
  소유권      claude (frontmost: com.anthropic.claudefordesktop)
  A1  waiting   dev-item-review    (desktop)
  A2  working   timeline-grid      (desktop)
  A3  —         빈 슬롯
  A4  어두움    상태 미상 -- 훅이 아직 이 세션에 안 붙었다
  테두리      #FF6D00 blink -- 화면 밖에 대기 2건
```

`doctor` 는 설치를 점검한다: 패드 왕복, 훅 설치 여부, 설정 경고, `Transport` 인식,
매핑 디렉터리 존재.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 2 passed

- [ ] **Step 5: 커밋**

```bash
git add src/paneglow/cli.py tests/test_cli.py
git commit -m "feat: cli with status and doctor"
```

---

## Task 12: `iterm` 을 걷어낸다

**Files:**
- Delete: `src/paneglow/iterm.py`, `tests/test_iterm.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: 참조가 없는지 확인한다**

Run: `grep -rn "iterm" src/ tests/ --include="*.py" | grep -v "^src/paneglow/iterm.py\|^tests/test_iterm.py"`
Expected: 출력 없음

- [ ] **Step 2: 지운다**

```bash
git rm src/paneglow/iterm.py tests/test_iterm.py
```

- [ ] **Step 3: `pyproject.toml` 을 고친다**

```toml
description = "Codex Micro macropad as a live dashboard for parallel Claude Code Desktop sessions"
dependencies = []
```

`markers` 의 설명도 고친다: `"integration: needs the Codex Micro"`.

- [ ] **Step 4: 전체 테스트를 돌린다**

Run: `python -m pytest tests/ -m "not integration" -v`
Expected: 전부 PASS. `iterm2` import 실패가 없어야 한다.

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "refactor: remove the iTerm2 adapter and its dependency"
```

---

## Task 13: 첫 통합 — 실제로 빛나게 한다

여기서 처음으로 전체가 엮인다. [#11](https://github.com/JeongJaeSoon/paneglow/issues/11).

**Files:**
- Create: `docs/verification/first-light.md`

- [ ] **Step 1: 훅을 설치한다**

`~/.claude/settings.json` 에 추가한다. 모든 이벤트가 같은 커맨드로 간다.

```json
{
  "hooks": {
    "SessionStart":        [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "UserPromptSubmit":    [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "PreToolUse":          [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "PostToolUse":         [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "PostToolUseFailure":  [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "Notification":        [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "Stop":                [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "StopFailure":         [{"hooks":[{"type":"command","command":"paneglow hook"}]}],
    "SessionEnd":          [{"hooks":[{"type":"command","command":"paneglow hook"}]}]
  }
}
```

- [ ] **Step 2: 데몬을 띄우고 시나리오를 돌린다**

```bash
paneglow doctor      # 전부 초록인지
paneglow start
```

순서대로 확인하고 각 결과를 `first-light.md` 에 적는다.

| # | 시나리오 | 기대 |
|---|---|---|
| 1 | 데스크톱 세션 두 개를 연다 | A1·A2 에 색이 붙는다 |
| 2 | 한쪽에 작업을 시킨다 | 그 키가 파랑 |
| 3 | 승인 프롬프트를 띄운다 | 그 키가 주황 |
| 4 | 그 키를 누른다 | **그 세션이 앱에서 열린다** |
| 5 | 세션을 7개까지 늘린다 | 앞 6개는 자리 유지, 7번째는 테두리 깜빡임 |
| 6 | 세션 하나를 닫는다 | 그 슬롯이 비고 다음 세션이 들어온다 |
| 7 | Codex 앱으로 전환한다 | 6키가 벤더 것으로, 테두리가 파랑 |
| 8 | Codex 에서 A키를 누른다 | 테두리가 잠깐 벤더 색이었다가 **0.2초 안에 돌아온다** |
| 9 | Codex 를 보는 중에 Claude 가 승인을 기다린다 | 테두리가 **파란색으로 깜빡인다** |
| 10 | Chrome 으로 전환한다 | 직전 소유권이 유지된다 |
| 11 | 터치 센서로 Layer 2 로 간다 | 6키가 조용해진다 |
| 12 | `paneglow stop` | **6키와 테두리가 꺼진 채로 남는다** (flush 확인) |
| 13 | 무선으로 바꾸고 1~12 를 반복한다 | 동일 |

- [ ] **Step 3: 목표 지연을 잰다**

| 경로 | 목표 |
|---|---|
| 상태 변화 → LED | 500ms 이내 |
| A키 press → 세션 열림 | 150ms 이내 |

못 지키면 `poll_ms` 를 늘리고 LED 갱신을 합친다. 결과를 기록한다.

- [ ] **Step 4: 결과를 커밋한다**

```bash
git add docs/verification/first-light.md
git commit -m "docs: record the first-light run"
```

---

## 자기 점검

**스펙 커버리지**

| 스펙 | 태스크 |
|---|---|
| R1 세션 상태를 색으로 | 3 · 6 · 10 |
| R2 키를 누르면 세션이 열린다 | 5 · 10 · 13 |
| R3 최근 것 위주, 예측 가능한 배치 | 4 |
| R4 Codex 에 양보 | 10 |
| R5 테두리로 누구 것인지 | 10 · 13 |
| R6 화면 밖 대기·오류 알림 | 6 · 10 |
| R7 C1~C7 안 건드림 | 전 태스크 — `rgbcfg(keys=…)` 를 아무 데서도 부르지 않는다 |
| R8 유선·무선 | 1 · 9 · 13 |
| R9 권한 불필요 | 9 |
| 테두리 되찾기 | 9 · 10 |
| `working` 갇힘 상한 | 7 (설정) · 10 (적용) |
| flush | 9 · 13 |

**남은 미확인** (스펙 §15) 은 태스크로 만들지 않았다 — auto-dim 조사 결과와 실사용
관측이 필요하고, 그 전에 코드를 쓰면 없는 문제에 대비하는 것이 된다.
