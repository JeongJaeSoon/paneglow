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
