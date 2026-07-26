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
