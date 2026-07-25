# paneglow Phase 1 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codex Micro의 6개 Agent 키가 현재 iTerm2 탭의 pane별 Claude Code 상태를 색으로 표시하고, 키를 누르면 그 pane으로 이동한다.

**Architecture:** 순수 함수 `render`가 (pane 목록 + 상태 + 뷰) → 색 배열을 계산하고, `daemon`이 두 게이트(레이어/소유권)와 세대(generation)를 관리하며 `pad`·`iterm`·`hooks`를 엮는다. 상태는 훅이 원자적으로 쓰는 JSON 파일로 주고받는다. 로직 대부분이 하드웨어 없이 테스트된다.

**Tech Stack:** Python 3.10+ · `iterm2` 패키지 · IOKit(ctypes) · pytest

## Global Constraints

- **macOS 전용.** IOKit `IOHIDDevice`를 ctypes로 직접 쓴다. `hidapi`는 이 기기에서 `open_path()`가 항상 실패하므로 쓰지 않는다.
- **하드웨어:** VID `0x303A` / PID `0x8360`, 벤더 컬렉션 usage page `0xFF00`, **Report ID 6**.
- **프레이밍:** USB `[0x02][len][json]` 63바이트 / BLE `[0x06][0x02][len][json]` 64바이트.
- **`v.oai.*`는 notification** — `id`를 넣으면 `404 Method not found`가 온다.
- **성공 리턴 코드는 무의미하다.** 유일한 검증은 `device.status` 왕복이다.
- **노브는 `act=2`로 온다.** `act==1` 필터를 걸면 다이얼이 죽는다.
- **Layer 1에서만 동작한다.** `device.status.layer_index`는 1-indexed.
- 설정 파일 `~/.paneglow/config.json`, 상태 `~/.paneglow/state/`, 로그 `~/.paneglow/logs/`.
- 커밋 메시지는 Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- 모든 공개 함수에 타입 힌트를 단다. 외부 의존은 `iterm2`와 표준 라이브러리뿐.

**설계 문서:** [`docs/design.html`](../../design.html) · **하드웨어 사실:** [`docs/hardware-notes.md`](../../hardware-notes.md)

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `src/paneglow/state.py` | 상태 어휘(`AgentState`)와 우선순위. 순수 |
| `src/paneglow/store.py` | 세션 레코드 원자적 읽기/쓰기, `rev` 역행 방지, TTL |
| `src/paneglow/hook.py` | 훅 이벤트 → `AgentState` 분류. 순수 + `store` 호출 |
| `src/paneglow/render.py` | (pane + 상태 + 뷰) → 6키 색 배열. **완전 순수** |
| `src/paneglow/protocol.py` | JSON-RPC 메시지 조립·프레이밍. 순수 |
| `src/paneglow/pad.py` | IOKit 열기, 송수신, `device.status` 왕복 |
| `src/paneglow/iterm.py` | 탭·pane 트리, `tty`/`jobName`, pane 포커스 |
| `src/paneglow/config.py` | 설정 로드·기본값 폴백 |
| `src/paneglow/daemon.py` | 게이트·세대·루프 |
| `src/paneglow/cli.py` | `start`/`stop`/`status`/`doctor`/`hook` |

Phase 1은 `state` · `store` · `hook` · `render` · `protocol` · `pad` · `iterm` · `config`까지 만든다.
`daemon`·`cli` 완성과 테두리·MOD 탭 뷰·승인은 Phase 2다.

---

## Task 0: 관문 검증 — 훅 상태 매핑

설계 전체가 여기 걸려 있다. **실패하면 Task 3 이후가 전부 무효**이므로 가장 먼저 한다.
이 태스크만 유일하게 코드가 아니라 관찰을 산출한다.

**Files:**
- Create: `docs/verification/hook-events.md`

- [ ] **Step 1: 관찰용 훅을 설치한다**

`~/.claude/settings.json`에 임시로 추가한다. 기존 훅은 건드리지 않는다.

```json
{
  "hooks": {
    "SessionStart":     [{"hooks":[{"type":"command","command":"jq -c '{e:\"SessionStart\"} + .' >> /tmp/pg-hooks.jsonl"}]}],
    "UserPromptSubmit": [{"hooks":[{"type":"command","command":"jq -c '{e:\"UserPromptSubmit\"} + .' >> /tmp/pg-hooks.jsonl"}]}],
    "PreToolUse":       [{"hooks":[{"type":"command","command":"jq -c '{e:\"PreToolUse\"} + .' >> /tmp/pg-hooks.jsonl"}]}],
    "PostToolUse":      [{"hooks":[{"type":"command","command":"jq -c '{e:\"PostToolUse\"} + .' >> /tmp/pg-hooks.jsonl"}]}],
    "Notification":     [{"hooks":[{"type":"command","command":"jq -c '{e:\"Notification\"} + .' >> /tmp/pg-hooks.jsonl"}]}],
    "Stop":             [{"hooks":[{"type":"command","command":"jq -c '{e:\"Stop\"} + .' >> /tmp/pg-hooks.jsonl"}]}],
    "SessionEnd":       [{"hooks":[{"type":"command","command":"jq -c '{e:\"SessionEnd\"} + .' >> /tmp/pg-hooks.jsonl"}]}]
  }
}
```

- [ ] **Step 2: Claude Code를 재시작하고 시나리오를 실행한다**

새 pane에서 Claude Code를 띄우고 순서대로:
1. 아무 질문 (→ `UserPromptSubmit`, `PreToolUse`, `Stop`)
2. **권한 프롬프트가 뜨는 작업** — 예: 승인이 필요한 파일 쓰기 (→ `Notification` 기대)
3. 실패하는 명령 실행 (→ `Stop`에 에러 흔적 기대)
4. 세션 종료 (→ `SessionEnd`)

- [ ] **Step 3: 세 가지 질문에 답을 찾는다**

```bash
# 어떤 이벤트가 왔나
jq -r .e /tmp/pg-hooks.jsonl | sort | uniq -c

# Notification 페이로드에 "권한 프롬프트"를 식별할 필드가 있나  ← 승인 기능의 생사
jq 'select(.e=="Notification")' /tmp/pg-hooks.jsonl

# Stop 페이로드에 에러를 구분할 필드가 있나                      ← error 상태의 생사
jq 'select(.e=="Stop")' /tmp/pg-hooks.jsonl

# tty / cwd / session_id 를 어디서 얻나
jq 'select(.e=="SessionStart")' /tmp/pg-hooks.jsonl
```

- [ ] **Step 4: 결과를 문서로 남기고 분기를 확정한다**

`docs/verification/hook-events.md`에 실제 페이로드 예시와 함께 기록한다.

| 결과 | 계획에 미치는 영향 |
|---|---|
| `waiting` 판별 가능 | 계획대로 진행 |
| `waiting` 판별 **불가** | `waiting` 상태와 승인 기능(Phase 2)을 **제거**. 색 팔레트 4색으로 축소 |
| `error` 구분 **불가** | `error` 상태 제거. `Stop`은 항상 `done` |
| `tty`가 페이로드에 **없음** | `pid`에서 `ps -o tty= -p <pid>`로 유도. Task 2에 단계 추가 |

- [ ] **Step 5: 임시 훅을 제거하고 커밋**

```bash
# settings.json 에서 위 임시 훅 블록을 지운다 (기존 훅은 유지)
git add docs/verification/hook-events.md
git commit -m "docs: record hook event payloads and state mapping"
```

---

## Task 1: 상태 어휘

**Files:**
- Create: `src/paneglow/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: 없음
- Produces: `AgentState` (StrEnum), `PRIORITY: dict[AgentState, int]`, `highest(states: Iterable[AgentState]) -> AgentState | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
import pytest
from paneglow.state import AgentState, highest


def test_priority_order_waiting_beats_error():
    assert highest([AgentState.ERROR, AgentState.WAITING]) is AgentState.WAITING


def test_priority_order_full_chain():
    every = [AgentState.IDLE, AgentState.WORKING, AgentState.DONE,
             AgentState.ERROR, AgentState.WAITING]
    assert highest(every) is AgentState.WAITING
    assert highest([AgentState.IDLE, AgentState.WORKING]) is AgentState.WORKING
    assert highest([AgentState.IDLE, AgentState.DONE]) is AgentState.DONE


def test_empty_is_none():
    assert highest([]) is None


def test_single():
    assert highest([AgentState.IDLE]) is AgentState.IDLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.state'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/state.py
"""상태 어휘와 우선순위. 하드웨어도 파일도 모른다."""
from __future__ import annotations

from enum import Enum
from typing import Iterable


class AgentState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"
    WAITING = "waiting"


#: 높을수록 먼저 보여준다. waiting 이 최상위인 이유는 그것만이 사람을 기다리기 때문이다.
PRIORITY: dict[AgentState, int] = {
    AgentState.IDLE: 0,
    AgentState.WORKING: 1,
    AgentState.DONE: 2,
    AgentState.ERROR: 3,
    AgentState.WAITING: 4,
}


def highest(states: Iterable[AgentState]) -> AgentState | None:
    """가장 먼저 보여줘야 할 상태. 비어 있으면 None."""
    ranked = sorted(states, key=lambda s: PRIORITY[s], reverse=True)
    return ranked[0] if ranked else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_state.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/state.py tests/test_state.py
git commit -m "feat: add agent state vocabulary and priority"
```

---

## Task 2: 세션 스토어 — 원자적 쓰기와 rev 역행 방지

**Files:**
- Create: `src/paneglow/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `AgentState` (Task 1)
- Produces:
  - `SessionRecord` dataclass — 필드 `session_id: str`, `tty: str`, `cwd: str`, `state: AgentState`, `rev: int`, `updated_at: float`, `pid: int`
  - `write(record: SessionRecord, root: Path) -> bool` — `rev`가 기존보다 낮으면 쓰지 않고 `False`
  - `read_all(root: Path) -> list[SessionRecord]` — 깨진 파일은 조용히 건너뛴다
  - `by_tty(records: list[SessionRecord]) -> dict[str, SessionRecord]` — 같은 tty면 `updated_at` 최신만
  - `prune(root: Path, live_ttys: set[str], ttl_seconds: float, now: float) -> int` — 삭제한 개수

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import json
from pathlib import Path

from paneglow.state import AgentState
from paneglow.store import SessionRecord, write, read_all, by_tty, prune


def rec(sid="s1", tty="/dev/ttys002", state=AgentState.WORKING, rev=1, at=100.0):
    return SessionRecord(session_id=sid, tty=tty, cwd="/tmp",
                         state=state, rev=rev, updated_at=at, pid=1)


def test_write_then_read(tmp_path: Path):
    assert write(rec(), tmp_path) is True
    got = read_all(tmp_path)
    assert len(got) == 1
    assert got[0].state is AgentState.WORKING


def test_lower_rev_is_rejected(tmp_path: Path):
    write(rec(rev=5, state=AgentState.WAITING), tmp_path)
    assert write(rec(rev=4, state=AgentState.IDLE), tmp_path) is False
    assert read_all(tmp_path)[0].state is AgentState.WAITING


def test_same_rev_is_rejected(tmp_path: Path):
    write(rec(rev=5), tmp_path)
    assert write(rec(rev=5, state=AgentState.IDLE), tmp_path) is False


def test_corrupt_file_is_skipped(tmp_path: Path):
    write(rec(), tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    assert len(read_all(tmp_path)) == 1


def test_no_partial_file_left_behind(tmp_path: Path):
    write(rec(), tmp_path)
    # 임시 파일이 남지 않아야 한다
    assert [p.name for p in tmp_path.iterdir()] == ["s1.json"]


def test_by_tty_keeps_newest(tmp_path: Path):
    old = rec(sid="old", rev=1, at=100.0, state=AgentState.WAITING)
    new = rec(sid="new", rev=1, at=200.0, state=AgentState.WORKING)
    picked = by_tty([old, new])
    assert picked["/dev/ttys002"].session_id == "new"


def test_prune_removes_dead_tty(tmp_path: Path):
    write(rec(sid="alive", tty="/dev/ttys002", at=1000.0), tmp_path)
    write(rec(sid="dead", tty="/dev/ttys009", at=1000.0), tmp_path)
    removed = prune(tmp_path, live_ttys={"/dev/ttys002"}, ttl_seconds=999, now=1001.0)
    assert removed == 1
    assert {r.session_id for r in read_all(tmp_path)} == {"alive"}


def test_prune_keeps_quiet_but_live_session(tmp_path: Path):
    """훅은 상태가 바뀔 때만 온다 — 조용해도 iTerm2 에 있으면 살아 있다."""
    write(rec(sid="quiet", tty="/dev/ttys002", at=0.0), tmp_path)
    removed = prune(tmp_path, live_ttys={"/dev/ttys002"}, ttl_seconds=10, now=99999.0)
    assert removed == 0


def test_prune_ttl_is_only_a_fallback(tmp_path: Path):
    """iTerm2 조회가 실패해 live_ttys 를 모를 때만 TTL 이 작동한다."""
    write(rec(sid="stale", tty="/dev/ttys002", at=0.0), tmp_path)
    removed = prune(tmp_path, live_ttys=None, ttl_seconds=10, now=99999.0)
    assert removed == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.store'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/store.py
"""세션 레코드 저장소.

훅은 짧게 여러 개가 겹쳐 실행되므로 쓰기는 반드시 원자적이어야 한다.
임시 파일에 쓰고 fsync 한 뒤 같은 디렉터리로 rename 하면, 읽는 쪽은
언제 읽어도 온전한 파일을 보거나 아예 못 본다 — 반쪽짜리는 없다.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from paneglow.state import AgentState


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    tty: str
    cwd: str
    state: AgentState
    rev: int
    updated_at: float
    pid: int


def _path(root: Path, session_id: str) -> Path:
    return root / f"{session_id}.json"


def _load(path: Path) -> SessionRecord | None:
    try:
        raw = json.loads(path.read_text())
        return SessionRecord(
            session_id=raw["session_id"], tty=raw["tty"], cwd=raw["cwd"],
            state=AgentState(raw["state"]), rev=int(raw["rev"]),
            updated_at=float(raw["updated_at"]), pid=int(raw["pid"]),
        )
    except Exception:
        # 깨진 파일은 정상적인 일이다 — 쓰는 도중일 수도 있다. 다음 틱에 다시 읽는다.
        return None


def write(record: SessionRecord, root: Path) -> bool:
    """rev 가 기존보다 크면 원자적으로 쓴다. 아니면 False."""
    root.mkdir(parents=True, exist_ok=True)
    target = _path(root, record.session_id)

    existing = _load(target)
    if existing is not None and record.rev <= existing.rev:
        return False

    payload = asdict(record) | {"state": record.state.value}
    fd, tmp = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)   # 같은 디렉터리라 원자적이다
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def read_all(root: Path) -> list[SessionRecord]:
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("*.json")):
        rec = _load(p)
        if rec is not None:
            out.append(rec)
    return out


def by_tty(records: list[SessionRecord]) -> dict[str, SessionRecord]:
    """tty 하나당 하나. 껐다 켠 pane 에는 레코드가 둘 남으므로 최신만 쓴다."""
    picked: dict[str, SessionRecord] = {}
    for r in records:
        cur = picked.get(r.tty)
        if cur is None or r.updated_at > cur.updated_at:
            picked[r.tty] = r
    return picked


def prune(root: Path, live_ttys: set[str] | None,
          ttl_seconds: float, now: float) -> int:
    """죽은 세션 파일을 지운다.

    ``live_ttys`` 를 알면 그게 기준이다 — 조용해도 iTerm2 에 있으면 살아 있다.
    모를 때(iTerm2 조회 실패)만 TTL 로 떨어진다.
    """
    removed = 0
    for rec in read_all(root):
        dead = (rec.tty not in live_ttys) if live_ttys is not None \
            else (now - rec.updated_at > ttl_seconds)
        if dead:
            _path(root, rec.session_id).unlink(missing_ok=True)
            removed += 1
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/store.py tests/test_store.py
git commit -m "feat: add atomic session store with rev guard and liveness prune"
```

---

## Task 3: 훅 분류기

Task 0의 결과를 반영한다. `waiting`/`error` 판별이 불가로 나왔다면 해당 테스트와 분기를 제거하고 진행한다.

**Files:**
- Create: `src/paneglow/hook.py`
- Test: `tests/test_hook.py`

**Interfaces:**
- Consumes: `AgentState` (Task 1), `SessionRecord`·`write` (Task 2)
- Produces: `classify(event: dict) -> AgentState | None`, `record_from(event: dict, rev: int, now: float) -> SessionRecord | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hook.py
from paneglow.hook import classify, record_from
from paneglow.state import AgentState


def test_session_start_is_idle():
    assert classify({"hook_event_name": "SessionStart"}) is AgentState.IDLE


def test_prompt_and_tools_are_working():
    for name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"):
        assert classify({"hook_event_name": name}) is AgentState.WORKING


def test_notification_is_waiting():
    assert classify({"hook_event_name": "Notification"}) is AgentState.WAITING


def test_stop_is_done():
    assert classify({"hook_event_name": "Stop"}) is AgentState.DONE


def test_session_end_is_none():
    """레코드를 지우라는 뜻이므로 상태가 아니다."""
    assert classify({"hook_event_name": "SessionEnd"}) is None


def test_unknown_event_is_none():
    assert classify({"hook_event_name": "SubagentStop"}) is None
    assert classify({}) is None


def test_record_from_event():
    ev = {"hook_event_name": "PreToolUse", "session_id": "abc",
          "cwd": "/repo", "transcript_path": "/x"}
    rec = record_from(ev, rev=7, now=123.0)
    assert rec is not None
    assert rec.session_id == "abc"
    assert rec.cwd == "/repo"
    assert rec.state is AgentState.WORKING
    assert rec.rev == 7
    assert rec.updated_at == 123.0


def test_record_from_unclassifiable_event_is_none():
    assert record_from({"hook_event_name": "SubagentStop"}, rev=1, now=1.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.hook'`

- [ ] **Step 3: Write minimal implementation**

`tty`는 훅 페이로드에 없을 수 있다. Task 0에서 확인한 결과에 따라 페이로드에서 읽거나 `pid`로 유도한다. 아래는 페이로드에 없을 때의 유도 경로를 포함한 형태다.

```python
# src/paneglow/hook.py
"""Claude Code 훅 이벤트를 AgentState 로 분류한다.

이 파일이 유일하게 에이전트 종류를 안다. 나머지 모듈은 AgentState 만 본다 —
나중에 다른 에이전트를 붙이면 이 파일만 늘어난다.
"""
from __future__ import annotations

import os
import subprocess

from paneglow.state import AgentState
from paneglow.store import SessionRecord

#: 에이전트가 무언가 하고 있다는 뜻의 이벤트들
_WORKING = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}


def classify(event: dict) -> AgentState | None:
    """이 이벤트가 뜻하는 상태. 상태 변화가 아니면 None."""
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return AgentState.IDLE
    if name in _WORKING:
        return AgentState.WORKING
    if name == "Notification":
        return AgentState.WAITING
    if name == "Stop":
        return AgentState.DONE
    # SessionEnd 는 삭제 신호, SubagentStop 등은 잡음이다.
    return None


def _tty_of(pid: int | None) -> str:
    """훅 페이로드에 tty 가 없을 때 pid 에서 유도한다."""
    if not pid:
        return ""
    try:
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ""
    return f"/dev/{out}" if out and out != "??" else ""


def record_from(event: dict, rev: int, now: float) -> SessionRecord | None:
    """이벤트를 저장 가능한 레코드로. 분류 불가면 None."""
    state = classify(event)
    if state is None:
        return None
    pid = event.get("pid") or os.getppid()
    return SessionRecord(
        session_id=str(event.get("session_id", "")),
        tty=str(event.get("tty") or _tty_of(pid)),
        cwd=str(event.get("cwd", "")),
        state=state,
        rev=rev,
        updated_at=now,
        pid=int(pid),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hook.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/hook.py tests/test_hook.py
git commit -m "feat: classify Claude Code hook events into agent states"
```

---

## Task 4: 렌더러 — 순수 함수

로직의 심장이며 하드웨어 없이 전부 테스트된다.

**Files:**
- Create: `src/paneglow/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `AgentState`·`highest` (Task 1)
- Produces:
  - `Pane` dataclass — `tty: str`, `is_claude: bool`, `state: AgentState | None`
  - `KEY_COUNT = 6`
  - `PALETTE: dict[AgentState, int]` — 공장 정품 값
  - `render_pane_view(panes: list[Pane]) -> list[int | None]` — 길이 6, `None`은 소등
  - `overflow(panes: list[Pane]) -> list[Pane]` — 6키에 못 올라간 pane
  - `underglow_for(states: Iterable[AgentState]) -> int | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py
from paneglow.render import (
    Pane, KEY_COUNT, PALETTE, render_pane_view, overflow, underglow_for,
)
from paneglow.state import AgentState


def p(state=None, claude=True, tty="/dev/ttys0"):
    return Pane(tty=tty, is_claude=claude, state=state)


def test_always_six_slots():
    assert len(render_pane_view([])) == KEY_COUNT
    assert len(render_pane_view([p(AgentState.WORKING)])) == KEY_COUNT


def test_empty_slots_are_dark():
    out = render_pane_view([p(AgentState.WORKING)])
    assert out[0] == PALETTE[AgentState.WORKING]
    assert out[1:] == [None] * 5


def test_non_claude_pane_is_dark_but_occupies_a_slot():
    """키를 누르면 이동은 되어야 하므로 자리는 차지한다."""
    out = render_pane_view([p(claude=False), p(AgentState.WAITING)])
    assert out[0] is None
    assert out[1] == PALETTE[AgentState.WAITING]


def test_claude_without_state_is_dark():
    """jobName 으로 발견했지만 훅이 아직 안 붙은 pane."""
    assert render_pane_view([p(state=None, claude=True)])[0] is None


def test_screen_order_is_preserved():
    out = render_pane_view([p(AgentState.IDLE), p(AgentState.ERROR), p(AgentState.DONE)])
    assert out[:3] == [PALETTE[AgentState.IDLE],
                       PALETTE[AgentState.ERROR],
                       PALETTE[AgentState.DONE]]


def test_seventh_pane_does_not_appear():
    panes = [p(AgentState.WORKING) for _ in range(7)]
    assert len(render_pane_view(panes)) == KEY_COUNT


def test_overflow_returns_panes_beyond_six():
    panes = [p(AgentState.WORKING) for _ in range(6)] + [p(AgentState.WAITING)]
    extra = overflow(panes)
    assert len(extra) == 1
    assert extra[0].state is AgentState.WAITING


def test_overflow_is_empty_when_six_or_fewer():
    assert overflow([p() for _ in range(6)]) == []


def test_underglow_lights_on_waiting():
    assert underglow_for([AgentState.WORKING, AgentState.WAITING]) == PALETTE[AgentState.WAITING]


def test_underglow_lights_on_error():
    assert underglow_for([AgentState.IDLE, AgentState.ERROR]) == PALETTE[AgentState.ERROR]


def test_underglow_prefers_waiting_over_error():
    assert underglow_for([AgentState.ERROR, AgentState.WAITING]) == PALETTE[AgentState.WAITING]


def test_underglow_is_off_when_quiet():
    """done/working/idle 은 알릴 가치가 없다 — 켜두면 신호가 죽는다."""
    assert underglow_for([AgentState.WORKING, AgentState.DONE, AgentState.IDLE]) is None
    assert underglow_for([]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.render'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/render.py
"""무엇을 어떤 색으로 보여줄지 계산한다. 하드웨어도 파일도 모르는 순수 함수."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from paneglow.state import AgentState, highest

KEY_COUNT = 6

#: 공장 정품 값. 벤더가 Codex 에 쓰는 색과 같아야 눈이 헷갈리지 않는다.
PALETTE: dict[AgentState, int] = {
    AgentState.IDLE: 0xFFFFFF,
    AgentState.WORKING: 0x304FFE,
    AgentState.WAITING: 0xFF6D00,
    AgentState.DONE: 0x00FF4C,
    AgentState.ERROR: 0xFF0033,
}

#: 테두리에 띄울 가치가 있는 상태. 나머지는 켜두면 신호가 죽는다.
_NOTABLE = (AgentState.WAITING, AgentState.ERROR)


@dataclass(frozen=True)
class Pane:
    tty: str
    is_claude: bool
    state: AgentState | None


def render_pane_view(panes: list[Pane]) -> list[int | None]:
    """화면 배치 순서대로 6키 색. None 은 소등."""
    out: list[int | None] = [None] * KEY_COUNT
    for i, pane in enumerate(panes[:KEY_COUNT]):
        if pane.is_claude and pane.state is not None:
            out[i] = PALETTE[pane.state]
    return out


def overflow(panes: list[Pane]) -> list[Pane]:
    """6키에 못 올라간 pane. 테두리 집계에 넣어야 사라지지 않는다."""
    return list(panes[KEY_COUNT:])


def underglow_for(states: Iterable[AgentState]) -> int | None:
    """화면 밖에서 나를 기다리는 것이 있으면 그 색, 없으면 None."""
    top = highest(states)
    return PALETTE[top] if top in _NOTABLE else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_render.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/render.py tests/test_render.py
git commit -m "feat: add pure renderer for key colours and underglow"
```

---

## Task 5: 프로토콜 — 메시지 조립과 프레이밍

**Files:**
- Create: `src/paneglow/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `USB = "USB"`, `BLE = "BLE"`
  - `thstatus(colors: list[int | None]) -> dict`
  - `rgbcfg(keys: int | None = ..., ambient: int | None = ...) -> dict`
  - `status_request(req_id: int = 1) -> dict`
  - `frame(message: dict, transport: str) -> list[bytes]`
  - `FrameDecoder` — `feed(chunk: bytes) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_protocol.py
import json
import pytest

from paneglow.protocol import (
    USB, BLE, thstatus, rgbcfg, status_request, frame, FrameDecoder,
)


def test_thstatus_is_a_notification_without_id():
    """v.oai.* 에 id 를 넣으면 404 Method not found 가 온다."""
    msg = thstatus([0xFF0000] + [None] * 5)
    assert msg["m"] == "v.oai.thstatus"
    assert "id" not in msg


def test_thstatus_has_six_entries_with_ids():
    msg = thstatus([0xFF0000] + [None] * 5)
    assert [e["id"] for e in msg["p"]] == [0, 1, 2, 3, 4, 5]


def test_thstatus_none_is_dark_not_dim():
    """빈 슬롯은 꺼야 한다 — 어둡게가 아니라 꺼짐이어야 개수가 읽힌다."""
    entry = thstatus([None] * 6)["p"][0]
    assert entry["c"] == 0 and entry["b"] == 0 and entry["e"] == 0


def test_thstatus_colour_is_solid_full_brightness():
    entry = thstatus([0x304FFE] + [None] * 5)["p"][0]
    assert entry["c"] == 0x304FFE and entry["e"] == 1 and entry["b"] == 1


def test_thstatus_rejects_wrong_length():
    with pytest.raises(ValueError):
        thstatus([0xFF0000])


def test_status_request_has_an_id():
    """device.status 는 요청이므로 id 가 필요하다."""
    assert status_request(9)["id"] == 9


def test_usb_framing_prefix_and_length():
    packets = frame({"m": "x"}, USB)
    assert len(packets) == 1
    assert packets[0][0] == 0x02
    assert len(packets[0]) == 63


def test_ble_framing_has_report_id_prefix_and_64_bytes():
    packets = frame({"m": "x"}, BLE)
    assert packets[0][0] == 0x06 and packets[0][1] == 0x02
    assert len(packets[0]) == 64


def test_framing_carries_the_json():
    packets = frame({"m": "x"}, USB)
    length = packets[0][1]
    assert json.loads(packets[0][2:2 + length].decode().rstrip("\r\n")) == {"m": "x"}


def test_long_message_spans_several_packets():
    big = {"m": "v.oai.thstatus", "p": [{"id": i, "c": 0xFFFFFF, "b": 1, "e": 1, "s": 0}
                                        for i in range(6)]}
    assert len(frame(big, USB)) >= 2


def test_decoder_reassembles_a_message():
    dec = FrameDecoder()
    out = []
    for packet in frame({"m": "hello", "p": {"a": 1}}, USB):
        out += dec.feed(packet)
    assert out == [{"m": "hello", "p": {"a": 1}}]


def test_decoder_ignores_garbage():
    assert FrameDecoder().feed(b"\x00" * 63) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.protocol'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/protocol.py
"""벤더 JSON-RPC 메시지와 HID 프레이밍.

프레이밍이 전송별로 다른 것이 이 기기의 가장 큰 함정이다. 잘못 프레이밍한
write 도 성공을 반환하고 조용히 버려지므로, 여기서 틀리면 증상이 "아무 일도
안 일어남"으로만 나타난다.
"""
from __future__ import annotations

import json

USB = "USB"
BLE = "BLE"

_METHOD_THSTATUS = "v.oai.thstatus"
_METHOD_RGBCFG = "v.oai.rgbcfg"

_EFFECT_OFF = 0
_EFFECT_SOLID = 1

#: 페이로드가 들어갈 자리. USB 는 [0x02][len], BLE 는 앞에 리포트 id 가 하나 더 붙는다.
_USB_SIZE, _BLE_SIZE = 63, 64
_KEY_COUNT = 6


def _entry(index: int, color: int | None) -> dict:
    if color is None:
        return {"id": index, "c": 0, "b": 0, "e": _EFFECT_OFF, "s": 0}
    return {"id": index, "c": color, "b": 1, "e": _EFFECT_SOLID, "s": 0}


def thstatus(colors: list[int | None]) -> dict:
    """Agent 키 6개를 각각 칠한다. notification 이므로 id 를 넣지 않는다."""
    if len(colors) != _KEY_COUNT:
        raise ValueError(f"colors must have {_KEY_COUNT} entries, got {len(colors)}")
    return {"m": _METHOD_THSTATUS,
            "p": [_entry(i, c) for i, c in enumerate(colors)]}


def _side(color: int | None) -> dict:
    if color is None:
        return {"e": _EFFECT_OFF, "b": 0, "s": 0, "c": 0}
    return {"e": _EFFECT_SOLID, "b": 1, "s": 0, "c": color}


_UNSET = object()


def rgbcfg(keys: int | None | object = _UNSET,
           ambient: int | None | object = _UNSET) -> dict:
    """C키 백라이트(keys)와 테두리(ambient). 생략한 존은 건드리지 않는다."""
    params: dict = {}
    if keys is not _UNSET:
        params["keys"] = _side(keys)          # type: ignore[arg-type]
    if ambient is not _UNSET:
        params["ambient"] = _side(ambient)    # type: ignore[arg-type]
    if not params:
        raise ValueError("rgbcfg needs at least one of keys / ambient")
    return {"m": _METHOD_RGBCFG, "p": params}


def status_request(req_id: int = 1) -> dict:
    """유일하게 믿을 수 있는 건강 확인. 응답이 와야 프레이밍이 맞는 것이다."""
    return {"m": "device.status", "id": req_id}


def frame(message: dict, transport: str) -> list[bytes]:
    """메시지를 리포트 크기로 자른다. 메시지는 \\r\\n 으로 끝난다."""
    body = (json.dumps(message, separators=(",", ":")) + "\r\n").encode()
    prefix = b"" if transport == USB else b"\x06"
    size = _USB_SIZE if transport == USB else _BLE_SIZE
    room = size - len(prefix) - 2          # 0x02 와 길이 바이트를 뺀 나머지

    packets = []
    for i in range(0, len(body), room):
        chunk = body[i:i + room]
        packet = prefix + bytes([0x02, len(chunk)]) + chunk
        packets.append(packet.ljust(size, b"\x00"))
    return packets


class FrameDecoder:
    """조각난 리포트를 다시 메시지로 잇는다."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[dict]:
        if len(chunk) < 2:
            return []
        # 입력 리포트도 [0x02][len] 로 시작한다. 아니면 우리 것이 아니다.
        start = 1 if chunk[0] == 0x06 else 0
        if chunk[start] != 0x02:
            return []
        length = chunk[start + 1]
        self._buf += chunk[start + 2:start + 2 + length]

        out = []
        while b"\r\n" in self._buf:
            line, _, rest = bytes(self._buf).partition(b"\r\n")
            self._buf = bytearray(rest)
            try:
                out.append(json.loads(line.decode()))
            except Exception:
                pass          # 못 읽는 줄은 버린다
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_protocol.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/protocol.py tests/test_protocol.py
git commit -m "feat: add vendor JSON-RPC messages and HID framing"
```

---

## Task 6: 설정

**Files:**
- Create: `src/paneglow/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 없음
- Produces: `Config` dataclass, `DEFAULTS: dict`, `load(path: Path | None) -> tuple[Config, list[str]]` — 두 번째는 경고 목록

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json
from pathlib import Path

from paneglow.config import Config, load


def test_missing_file_gives_defaults(tmp_path: Path):
    cfg, warnings = load(tmp_path / "nope.json")
    assert cfg.mod_key == "C7"
    assert cfg.gate_mode == "frontmost"
    assert warnings == []


def test_user_values_override(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mod_key": "C4", "timing": {"poll_ms": 500}}))
    cfg, _ = load(p)
    assert cfg.mod_key == "C4"
    assert cfg.poll_ms == 500
    assert cfg.gate_mode == "frontmost"      # 손대지 않은 값은 기본값


def test_shared_keycap_is_rejected_with_warning(tmp_path: Path):
    """C5·C6 은 넓은 캡 하나를 공유해 두 id 가 함께 온다."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mod_key": "C5"}))
    cfg, warnings = load(p)
    assert cfg.mod_key == "C7"
    assert any("C5" in w for w in warnings)


def test_bad_value_falls_back_and_warns(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"mode": "sideways"}}))
    cfg, warnings = load(p)
    assert cfg.gate_mode == "frontmost"
    assert any("mode" in w for w in warnings)


def test_broken_json_does_not_raise(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    cfg, warnings = load(p)
    assert cfg.mod_key == "C7"
    assert warnings != []


def test_keep_is_only_allowed_for_other(tmp_path: Path):
    """keep 은 직전을 참조하므로 when_other 에서만 뜻이 선다."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"underglow": {"when_iterm": {"mode": "keep"}}}))
    cfg, warnings = load(p)
    assert cfg.underglow_iterm == "outside"
    assert any("keep" in w for w in warnings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/config.py
"""설정 로드. 틀린 값은 기본값으로 떨어뜨리고 경고를 모은다 — 기동을 막지 않는다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_GATE_MODES = {"frontmost", "always", "off"}
_UNDERGLOW_MODES = {"outside", "all_claude", "current_tab", "off"}
#: C5·C6 은 넓은 캡 하나를 공유해 누르면 두 id 가 함께 온다.
_MOD_KEYS = {"C1", "C2", "C3", "C4", "C7", "KNOB_PRESS"}


@dataclass(frozen=True)
class Config:
    gate_mode: str = "frontmost"
    yield_to: tuple[str, ...] = ("com.openai.chat",)
    own_when: tuple[str, ...] = ("com.googlecode.iterm2",)
    mod_key: str = "C7"
    knob_tab_switch: bool = True
    mod_direct_tab: bool = True
    underglow_iterm: str = "outside"
    underglow_codex: str = "all_claude"
    ttl_minutes: int = 30
    done_fade_seconds: int = 180
    poll_ms: int = 250
    mod_release_timeout_ms: int = 5000


def _pick(value, allowed: set[str], default: str, label: str,
          warnings: list[str]) -> str:
    if value is None:
        return default
    if value not in allowed:
        warnings.append(f"{label}: {value!r} 은 쓸 수 없어 {default!r} 로 대체했습니다")
        return default
    return value


def load(path: Path | None) -> tuple[Config, list[str]]:
    warnings: list[str] = []
    raw: dict = {}

    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:
            warnings.append(f"설정을 읽지 못해 전부 기본값을 씁니다: {exc}")
            raw = {}

    gate = raw.get("gate") or {}
    glow = raw.get("underglow") or {}
    timing = raw.get("timing") or {}
    state = raw.get("state") or {}
    tabs = raw.get("tab_switch") or {}

    return Config(
        gate_mode=_pick(gate.get("mode"), _GATE_MODES, "frontmost", "gate.mode", warnings),
        yield_to=tuple(gate.get("yield_to") or ("com.openai.chat",)),
        own_when=tuple(gate.get("own_when") or ("com.googlecode.iterm2",)),
        mod_key=_pick(raw.get("mod_key"), _MOD_KEYS, "C7", "mod_key", warnings),
        knob_tab_switch=bool(tabs.get("knob", True)),
        mod_direct_tab=bool(tabs.get("mod_direct", True)),
        underglow_iterm=_pick((glow.get("when_iterm") or {}).get("mode"),
                              _UNDERGLOW_MODES, "outside",
                              "underglow.when_iterm.mode", warnings),
        underglow_codex=_pick((glow.get("when_codex") or {}).get("mode"),
                              _UNDERGLOW_MODES, "all_claude",
                              "underglow.when_codex.mode", warnings),
        ttl_minutes=int(state.get("ttl_minutes", 30)),
        done_fade_seconds=int(state.get("done_fade_seconds", 180)),
        poll_ms=int(timing.get("poll_ms", 250)),
        mod_release_timeout_ms=int(timing.get("mod_release_timeout_ms", 5000)),
    ), warnings
```

`keep`이 `_UNDERGLOW_MODES`에 없으므로 `when_iterm`에 넣으면 자동으로 경고 후 기본값이 된다.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/config.py tests/test_config.py
git commit -m "feat: add config loading with warn-and-fallback"
```

---

## Task 7: iTerm2 어댑터

실제 iTerm2가 필요하므로 테스트는 `pytest.mark.integration`으로 분리한다.

**Files:**
- Create: `src/paneglow/iterm.py`
- Test: `tests/test_iterm.py`
- Modify: `pyproject.toml` (marker 등록)

**Interfaces:**
- Consumes: `Pane` (Task 4)
- Produces:
  - `async current_tab_panes(app) -> list[Pane]` — 화면 배치 순서, `state`는 `None`으로 채워 반환
  - `async tab_count(app) -> int`
  - `async focus_pane(app, tty: str, bring_to_front: bool) -> bool`
  - `async live_ttys(app) -> set[str]`
  - `flatten(root) -> list` — 분할 트리를 읽기 순서로. **순수 함수라 단위 테스트 가능**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_iterm.py
import pytest

from paneglow.iterm import flatten, is_claude_job


class FakeSession:
    def __init__(self, name): self.name = name


class FakeSplitter:
    def __init__(self, *children, vertical=True):
        self.children = list(children)
        self.vertical = vertical


def test_flatten_returns_leaves_in_order():
    a, b, c = FakeSession("a"), FakeSession("b"), FakeSession("c")
    assert flatten(FakeSplitter(a, b, c), leaf=FakeSession) == [a, b, c]


def test_flatten_handles_nesting():
    a, b, c = FakeSession("a"), FakeSession("b"), FakeSession("c")
    tree = FakeSplitter(a, FakeSplitter(b, c))
    assert flatten(tree, leaf=FakeSession) == [a, b, c]


def test_flatten_single_leaf():
    a = FakeSession("a")
    assert flatten(a, leaf=FakeSession) == [a]


def test_claude_job_is_recognised_by_version_string():
    assert is_claude_job("2.1.220") is True
    assert is_claude_job("0.9.1-beta") is True


def test_non_claude_jobs():
    for job in ("zsh", "bash", "vim", "", None):
        assert is_claude_job(job) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reads_real_panes():
    import iterm2
    async def main(connection):
        from paneglow.iterm import current_tab_panes
        app = await iterm2.async_get_app(connection)
        panes = await current_tab_panes(app)
        assert all(p.tty.startswith("/dev/tty") for p in panes)
    iterm2.run_until_complete(main)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_iterm.py -v -m "not integration"`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.iterm'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/iterm.py
"""iTerm2 어댑터.

pane 발견은 iTerm2 가 한다 — jobName 으로 Claude Code 가 떠 있는지 알 수 있어서
훅이 아직 안 붙은 세션도 자리를 차지한다. 훅은 '상태' 에만 필요하다.
"""
from __future__ import annotations

import re
from typing import Any

from paneglow.render import Pane

#: Claude Code 는 jobName 이 버전 문자열로 나온다. 이 표기가 바뀌면 판별이 깨진다.
_VERSION = re.compile(r"^\d+\.\d+\.\d+")


def is_claude_job(job_name: str | None) -> bool:
    return bool(job_name) and bool(_VERSION.match(job_name))


def flatten(node: Any, leaf: type) -> list:
    """분할 트리를 읽기 순서(좌→우, 위→아래)로 편다.

    iTerm2 는 children 을 배치 순서대로 준다. 그래서 깊이 우선으로 훑으면
    화면 순서가 그대로 나온다.
    """
    if isinstance(node, leaf):
        return [node]
    out = []
    for child in getattr(node, "children", []):
        out += flatten(child, leaf)
    return out


async def _pane_of(session) -> Pane:
    tty = await session.async_get_variable("tty")
    job = await session.async_get_variable("jobName")
    return Pane(tty=tty or "", is_claude=is_claude_job(job), state=None)


def _current_window(app):
    """가장 최근 활성 창 하나만 다룬다. 다중 창은 범위 밖이다."""
    return app.current_terminal_window


async def current_tab_panes(app) -> list[Pane]:
    import iterm2
    window = _current_window(app)
    if window is None:
        return []
    sessions = flatten(window.current_tab.root, leaf=iterm2.Session)
    return [await _pane_of(s) for s in sessions]


async def tab_count(app) -> int:
    window = _current_window(app)
    return len(window.tabs) if window else 0


async def live_ttys(app) -> set[str]:
    """지금 살아 있는 pane 의 tty 전부. 세션 생존 판정의 기준이다."""
    import iterm2
    out: set[str] = set()
    for window in app.windows:
        for tab in window.tabs:
            for session in flatten(tab.root, leaf=iterm2.Session):
                tty = await session.async_get_variable("tty")
                if tty:
                    out.add(tty)
    return out


async def focus_pane(app, tty: str, bring_to_front: bool = False) -> bool:
    """그 tty 의 pane 을 고른다. 못 찾으면 아무것도 하지 않는다."""
    import iterm2
    for window in app.windows:
        for tab in window.tabs:
            for session in flatten(tab.root, leaf=iterm2.Session):
                if await session.async_get_variable("tty") == tty:
                    await session.async_activate(
                        select_tab=True, order_window_front=bring_to_front)
                    return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_iterm.py -v -m "not integration"`
Expected: PASS (5 passed, 1 deselected)

`pyproject.toml`에 marker를 등록한다:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: needs real iTerm2 or hardware"]
```

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/iterm.py tests/test_iterm.py pyproject.toml
git commit -m "feat: add iTerm2 adapter with pane discovery and focus"
```

---

## Task 8: 패드 어댑터 — 왕복 검증

하드웨어가 필요하므로 실기 테스트는 `integration` 마커로 분리한다.
Phase 1의 마지막이자 **처음으로 하드웨어가 실제로 빛나는 지점**이다.

**Files:**
- Create: `src/paneglow/pad.py`
- Test: `tests/test_pad.py`

**Interfaces:**
- Consumes: `protocol` (Task 5)
- Produces:
  - `open_pad() -> Pad | None`
  - `Pad.transport: str`, `Pad.send(message: dict) -> None`
  - `Pad.request(message: dict, timeout: float) -> dict | None`
  - `Pad.status() -> dict | None` — `{version, layer_index, battery, ...}`
  - `Pad.close() -> None`, 컨텍스트 매니저 지원

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pad.py
import pytest

from paneglow.pad import VENDOR_ID, PRODUCT_ID, USAGE_PAGE, open_pad


def test_device_constants_match_the_hardware():
    assert (VENDOR_ID, PRODUCT_ID) == (0x303A, 0x8360)
    assert USAGE_PAGE == 0xFF00


@pytest.mark.integration
def test_status_round_trip_proves_framing():
    """성공 리턴 코드는 아무것도 증명하지 않는다 — 왕복만이 유일한 검증이다."""
    pad = open_pad()
    assert pad is not None, "패드를 열 수 없습니다 (연결·Input Monitoring 확인)"
    with pad:
        status = pad.status()
        assert status is not None
        assert "version" in status
        assert isinstance(status.get("layer_index"), int)


@pytest.mark.integration
def test_layer_index_is_one_based():
    pad = open_pad()
    assert pad is not None
    with pad:
        assert pad.status()["layer_index"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pad.py -v -m "not integration"`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.pad'`

- [ ] **Step 3: Write minimal implementation**

IOKit 호출은 ctypes로 감싼다. 참고 구현이 필요하면 검증에 쓴 프로브(`freemicro/src/freemicro/device/codex_micro.py`, MIT)의 IOKit 계층 구조를 따르되 우리 인터페이스로 다시 쓴다.

```python
# src/paneglow/pad.py
"""벤더 HID 채널.

hidapi 의 open_path() 는 이 기기에서 항상 실패한다 — hidapi 는 컬렉션마다
경로를 만드는데 macOS 는 0xFF00 을 품은 IOHIDDevice 하나만 내주기 때문이다.
IOKit 을 직접 쓴다. Input Monitoring 권한이 필요하다.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from paneglow import protocol

VENDOR_ID = 0x303A
PRODUCT_ID = 0x8360
USAGE_PAGE = 0xFF00
REPORT_ID = 6


class Pad:
    """열린 패드 핸들. 한 핸들로 읽고 쓴다."""

    def __init__(self, ref: Any, transport: str) -> None:
        self._ref = ref
        self.transport = transport
        self._decoder = protocol.FrameDecoder()
        self._inbox: list[dict] = []
        self._lock = threading.Lock()
        self._closed = False

    # -- 쓰기 -----------------------------------------------------------
    def send(self, message: dict) -> None:
        """notification 을 보낸다. 성공 반환은 아무것도 보장하지 않는다."""
        for packet in protocol.frame(message, self.transport):
            self._set_report(packet)

    # -- 왕복 -----------------------------------------------------------
    def request(self, message: dict, timeout: float = 3.0) -> dict | None:
        """응답을 기다린다. 프레이밍이 맞는지 확인하는 유일한 방법."""
        want = message.get("id")
        with self._lock:
            self._inbox.clear()
        self.send(message)

        deadline = time.time() + timeout
        while time.time() < deadline:
            self._pump(0.05)
            with self._lock:
                for msg in self._inbox:
                    if want is None or msg.get("id") == want:
                        return msg
        return None

    def status(self) -> dict | None:
        reply = self.request(protocol.status_request())
        return (reply or {}).get("result")

    # -- 수명 -----------------------------------------------------------
    def close(self) -> None:
        if not self._closed:
            self._close_device()
            self._closed = True

    def __enter__(self) -> "Pad":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- IOKit 경계 (아래 넷만 ctypes 를 안다) ---------------------------
    def _set_report(self, packet: bytes) -> None:
        raise NotImplementedError  # IOHIDDeviceSetReport(kIOHIDReportTypeOutput, REPORT_ID)

    def _pump(self, seconds: float) -> None:
        raise NotImplementedError  # CFRunLoopRunInMode 로 입력 리포트를 받아 _decoder 에 먹인다

    def _close_device(self) -> None:
        raise NotImplementedError  # IOHIDDeviceClose


def open_pad() -> Pad | None:
    """VID/PID 로 기기를 찾아 연다. 없거나 권한이 없으면 None."""
    raise NotImplementedError  # IOServiceGetMatchingServices → IOHIDDeviceCreate → IOHIDDeviceOpen
```

`NotImplementedError` 넷을 ctypes 구현으로 채운다. 각각 채울 때마다 `test_status_round_trip_proves_framing`을 돌려 확인한다 — **왕복이 성공하는 순간이 이 태스크의 완료 신호다.**

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_pad.py -v -m "not integration"    # 상수 확인
python -m pytest tests/test_pad.py -v -m integration          # 패드 연결 후
```
Expected: 둘 다 PASS

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/pad.py tests/test_pad.py
git commit -m "feat: add vendor HID channel with status round-trip"
```

---

## Task 9: 첫 통합 — 실제로 빛나게 한다

Phase 1의 결승선. 사람이 눈으로 확인하는 유일한 태스크다.

**Files:**
- Create: `src/paneglow/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: 전부
- Produces: `main(argv: list[str] | None = None) -> int`, 서브커맨드 `once` / `doctor` / `hook`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import json
import sys
from pathlib import Path

from paneglow.cli import main


def test_no_args_shows_help_and_succeeds(capsys):
    assert main([]) == 0
    assert "paneglow" in capsys.readouterr().out


def test_hook_reads_stdin_and_writes_a_record(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PANEGLOW_HOME", str(tmp_path))
    payload = json.dumps({"hook_event_name": "PreToolUse",
                          "session_id": "s1", "cwd": "/repo", "pid": 999})
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(payload))

    assert main(["hook"]) == 0
    files = list((tmp_path / "state").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["state"] == "working"


def test_hook_never_fails_the_agent(tmp_path: Path, monkeypatch):
    """훅이 0 이 아닌 값을 내면 에이전트가 방해받는다. 무슨 일이 있어도 0."""
    monkeypatch.setenv("PANEGLOW_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("not json"))
    assert main(["hook"]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paneglow.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/paneglow/cli.py
"""명령줄 진입점. Phase 1 은 once / doctor / hook 만 있다."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from paneglow import hook as hookmod
from paneglow import protocol, render, store
from paneglow.pad import open_pad


def _home() -> Path:
    return Path(os.environ.get("PANEGLOW_HOME", Path.home() / ".paneglow"))


def _cmd_hook() -> int:
    """훅에서 호출된다. 어떤 일이 있어도 0 을 반환해 에이전트를 막지 않는다."""
    try:
        event = json.load(sys.stdin)
        rev = int(time.time() * 1000)
        record = hookmod.record_from(event, rev=rev, now=time.time())
        if record is not None:
            store.write(record, _home() / "state")
    except Exception:
        pass
    return 0


def _cmd_doctor() -> int:
    pad = open_pad()
    if pad is None:
        print("[FAIL] 패드를 열 수 없습니다 — 연결과 Input Monitoring 권한을 확인하세요")
        return 1
    with pad:
        status = pad.status()
        if status is None:
            print("[FAIL] device.status 왕복 실패 — 프레이밍이 틀렸습니다")
            return 1
        print(f"[PASS] 패드 연결  transport={pad.transport}")
        print(f"       firmware={status.get('version')} "
              f"layer={status.get('layer_index')} battery={status.get('battery')}%")
        if status.get("layer_index") != 1:
            print("[warn] Layer 1 이 아닙니다 — 6키는 Layer 1 에서만 동작합니다")
    return 0


def _cmd_once() -> int:
    """지금 상태를 읽어 6키를 한 번 칠한다. Phase 1 의 결승선."""
    import iterm2
    from paneglow.iterm import current_tab_panes

    records = store.by_tty(store.read_all(_home() / "state"))
    result: list[render.Pane] = []

    async def collect(connection):
        app = await iterm2.async_get_app(connection)
        for pane in await current_tab_panes(app):
            rec = records.get(pane.tty)
            result.append(render.Pane(tty=pane.tty, is_claude=pane.is_claude,
                                      state=rec.state if rec else None))

    iterm2.run_until_complete(collect)

    colors = render.render_pane_view(result)
    pad = open_pad()
    if pad is None:
        print("패드를 열 수 없습니다")
        return 1
    with pad:
        if pad.status() is None:
            print("device.status 왕복 실패 — 칠하지 않습니다")
            return 1
        pad.send(protocol.thstatus(colors))

    for i, (pane, color) in enumerate(zip(result, colors), start=1):
        shown = f"#{color:06X}" if color else "off"
        print(f"  A{i}  {pane.tty:16} claude={pane.is_claude!s:5} {shown}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paneglow")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("once", help="현재 상태로 6키를 한 번 칠한다")
    sub.add_parser("doctor", help="패드 연결과 왕복을 확인한다")
    sub.add_parser("hook", help="내부용 — 훅 이벤트를 stdin 으로 받는다")

    args = parser.parse_args(argv)
    if args.cmd == "hook":
        return _cmd_hook()
    if args.cmd == "doctor":
        return _cmd_doctor()
    if args.cmd == "once":
        return _cmd_once()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests and verify by eye**

```bash
python -m pytest tests/ -v -m "not integration"     # 전체 단위 테스트
pip install -e .
paneglow doctor                                      # 왕복 확인
```

훅을 설치하고 (`~/.claude/settings.json`에 `paneglow hook` 등록) Claude Code를 재시작한 뒤:

```bash
paneglow once
```

**패드의 A1~A6이 현재 탭의 pane 상태대로 켜지면 Phase 1 완료다.**

- [ ] **Step 5: Commit**

```bash
git add src/paneglow/cli.py tests/test_cli.py
git commit -m "feat: add cli with once/doctor/hook and first end-to-end paint"
```

---

## Phase 2 (별도 계획)

Phase 1이 눈으로 확인되면 다음을 계획한다. **Task 0의 결과에 따라 일부가 빠질 수 있다.**

| 항목 | 선행 조건 |
|---|---|
| 데몬 루프 · launchd | Phase 1 완료 |
| 두 게이트(레이어·소유권) · 세대 | 데몬 |
| MOD 코드 · 탭 뷰 | — |
| 테두리 | — |
| 승인 · 거절 | **Task 0에서 `waiting` 판별 확인** + pane 키 전송 검증 |
| `status` 상세 출력 · 장애 표시 | 데몬 |
| 절전 복귀 · 재연결 복구 | 데몬 |

---

## Self-Review

**Spec coverage:** R1(Task 4·9) · R2(Task 7·9) · R3(Task 7 `current_tab_panes`) · R5 계산부(Task 4 `underglow_for`) · R6 근거인 원자적 쓰기(Task 2)와 `jobName` 발견(Task 7)을 덮는다.
R4(게이트) · R7(탭 전환) · 테두리 출력 · 승인은 **Phase 2로 명시적으로 미뤘다.**

**Placeholder scan:** Task 8의 `NotImplementedError` 넷은 placeholder가 아니라 **IOKit 경계를 좁히려는 의도된 구조**다 — 어디를 ctypes로 채워야 하는지가 명시돼 있고 완료 판정(왕복 성공)도 있다. Task 0은 코드가 아니라 관찰을 산출하므로 예외다.

**Type consistency:** `AgentState`(1) → `SessionRecord.state`(2) → `classify` 반환(3) → `Pane.state`(4) 일관. `Pane`은 Task 4에서 정의하고 Task 7이 재사용한다. `protocol.frame`의 `transport`는 Task 5의 `USB`/`BLE` 상수를 Task 8의 `Pad.transport`가 그대로 쓴다.
