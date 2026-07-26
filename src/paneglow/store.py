"""Session record store.

Hooks fire overlapping and short-lived, so writes must be atomic. Writing to a
temp file, fsyncing it, then renaming within the same directory means a reader
either sees a whole file or does not see it at all -- never a half-written one.

Rename alone is not enough for the writers, though. "Is my rev newer" and the
rename are two steps, and two hooks can both pass the check before either lands
-- then completion order decides, not rev order. The loss that matters is Stop
(done) being overwritten by a PostToolUse (working) that read stale: Stop is the
last event of a turn, so nothing corrects it and the pane stays blue. Hence the
lock around check-and-write.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from paneglow.state import AgentState

#: ponytail: one lock for the whole store, not one per session. Writes are a few
#: hundred bytes and only happen on state changes, so contention is not a concern
#: at this scale. Split it per session_id if that ever stops being true.
_LOCK_NAME = ".write.lock"


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    tty: str
    cwd: str
    state: AgentState
    rev: int
    updated_at: float
    pid: int


def _checked_id(session_id: str) -> str:
    """Refuse anything that would not stay a single file inside the store.

    session_id arrives in a hook's stdin JSON and ends up in os.replace() and
    unlink(). Claude Code sends a uuid, but a field read off stdin becoming a
    filesystem path is a trust boundary: '../escaped' writes outside the store.
    """
    if not session_id or session_id in (".", "..") or "/" in session_id \
            or "\\" in session_id or "\x00" in session_id:
        raise ValueError(f"unusable session_id: {session_id!r}")
    return session_id


def _path(root: Path, session_id: str) -> Path:
    return root / f"{_checked_id(session_id)}.json"


def _load(path: Path) -> SessionRecord | None:
    try:
        raw = json.loads(path.read_text())
        # The filename is the authority. Letting the contents declare their own
        # id turns that field into a pointer at another file: a record claiming
        # "important" makes prune() unlink important.json instead of this one.
        if raw["session_id"] != path.stem:
            return None
        _checked_id(path.stem)
        return SessionRecord(
            session_id=path.stem, tty=raw["tty"], cwd=raw["cwd"],
            state=AgentState(raw["state"]), rev=int(raw["rev"]),
            updated_at=float(raw["updated_at"]), pid=int(raw["pid"]),
        )
    except Exception:
        # A broken file is normal -- it may be mid-write. Read again next tick.
        return None


@contextmanager
def _write_lock(root: Path):
    """Serialise check-and-write across hook processes. flock is released when
    the fd closes, so a hook that dies mid-write cannot wedge the store."""
    with open(root / _LOCK_NAME, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write(record: SessionRecord, root: Path) -> bool:
    """Write atomically if rev is newer than what is stored. Otherwise False."""
    root.mkdir(parents=True, exist_ok=True)
    target = _path(root, record.session_id)

    with _write_lock(root):
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
            os.replace(tmp, target)   # atomic: same directory
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
    """One record per tty. A restarted pane leaves two, so keep the newest."""
    picked: dict[str, SessionRecord] = {}
    for r in records:
        cur = picked.get(r.tty)
        if cur is None or r.updated_at > cur.updated_at:
            picked[r.tty] = r
    return picked


def prune(root: Path, live_ttys: set[str] | None,
          ttl_seconds: float, now: float) -> int:
    """Delete records for dead sessions.

    When ``live_ttys`` is known it decides -- a quiet pane is still alive as long
    as iTerm2 has it. TTL is the fallback for when iTerm2 cannot be reached.

    A record also dies when a newer one has taken over its tty. Liveness of the
    tty string alone would keep the old one forever: the pty gets recycled for
    the next pane, so the tty stays live while the session behind it is gone.

    Runs under the write lock: otherwise a hook can replace a record between the
    read and the unlink, and the brand new state gets deleted.
    """
    if not root.exists():
        return 0

    with _write_lock(root):
        records = read_all(root)
        current = {r.session_id for r in by_tty(records).values()}

        removed = 0
        for rec in records:
            superseded = rec.session_id not in current
            dead = superseded or ((rec.tty not in live_ttys) if live_ttys is not None
                                  else (now - rec.updated_at > ttl_seconds))
            if dead:
                _path(root, rec.session_id).unlink(missing_ok=True)
                removed += 1
    return removed
