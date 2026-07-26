import json
import time
from pathlib import Path

import pytest

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
    assert list(tmp_path.glob("*.tmp")) == []
    assert [p.name for p in tmp_path.glob("*.json")] == ["s1.json"]


@pytest.mark.parametrize("session_id", [
    "../escaped", "a/b", "..", ".", "", "x\x00y", "sub/../../out",
])
def test_session_id_cannot_escape_the_store(tmp_path: Path, session_id):
    """session_id comes from a hook's stdin JSON and lands in os.replace() and
    unlink(). '../escaped' used to write a file outside the store."""
    root = tmp_path / "state"
    root.mkdir()
    with pytest.raises(ValueError):
        write(rec(sid=session_id), root)
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize("claimed", ["../escaped", "someone-else", 123, None, ""])
def test_a_record_that_renames_itself_is_ignored(tmp_path: Path, claimed):
    """The filename is the authority. A record that declares its own id turns
    that field into a pointer at another file."""
    write(rec(sid="ok"), tmp_path)
    payload = json.loads((tmp_path / "ok.json").read_text())
    payload["session_id"] = claimed
    (tmp_path / "ok.json").write_text(json.dumps(payload))
    assert read_all(tmp_path) == []


def test_prune_cannot_delete_another_sessions_file(tmp_path: Path):
    """A dead record claiming a live session's id used to make prune() unlink
    the live file and leave the dead one behind."""
    write(rec(sid="important", tty="/dev/ttys002", at=500.0), tmp_path)
    write(rec(sid="junk", tty="/dev/ttys099", at=100.0), tmp_path)

    payload = json.loads((tmp_path / "junk.json").read_text())
    payload["session_id"] = "important"
    (tmp_path / "junk.json").write_text(json.dumps(payload))

    prune(tmp_path, live_ttys={"/dev/ttys002"}, ttl_seconds=999, now=600.0)
    assert (tmp_path / "important.json").exists(), "the live session was deleted"


def test_prune_survives_a_non_string_id(tmp_path: Path):
    """`"/" in 123` raises TypeError and took prune() down with it."""
    write(rec(sid="x", tty="/dev/ttys002"), tmp_path)
    payload = json.loads((tmp_path / "x.json").read_text())
    payload["session_id"] = 123
    (tmp_path / "x.json").write_text(json.dumps(payload))

    assert prune(tmp_path, live_ttys=set(), ttl_seconds=1, now=99.0) == 0


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
    """Hooks only fire on change. A quiet pane iTerm2 still has is alive."""
    write(rec(sid="quiet", tty="/dev/ttys002", at=0.0), tmp_path)
    removed = prune(tmp_path, live_ttys={"/dev/ttys002"}, ttl_seconds=10, now=99999.0)
    assert removed == 0


def test_prune_ttl_is_only_a_fallback(tmp_path: Path):
    """TTL only kicks in when iTerm2 could not be reached and live_ttys is unknown."""
    write(rec(sid="stale", tty="/dev/ttys002", at=0.0), tmp_path)
    removed = prune(tmp_path, live_ttys=None, ttl_seconds=10, now=99999.0)
    assert removed == 1


def test_prune_reclaims_a_record_whose_tty_was_reused(tmp_path: Path):
    """A pty gets recycled for the next pane, so the tty stays live while the
    session behind it is gone. Liveness of the tty string alone leaks forever."""
    write(rec(sid="closed", tty="/dev/ttys005", at=100.0), tmp_path)
    write(rec(sid="reopened", tty="/dev/ttys005", at=200.0), tmp_path)

    removed = prune(tmp_path, live_ttys={"/dev/ttys005"}, ttl_seconds=999, now=201.0)
    assert removed == 1
    assert {r.session_id for r in read_all(tmp_path)} == {"reopened"}


def test_concurrent_writers_cannot_lose_a_newer_state(tmp_path: Path, monkeypatch):
    """Two hooks both pass the rev check before either renames, and completion
    order decides. The loss that matters is Stop(done) being overwritten by a
    stale PostToolUse(working): Stop is the last event, so nothing corrects it.
    """
    import threading
    from paneglow import store

    write(rec(sid="s1", rev=4, state=AgentState.IDLE), tmp_path)

    real_load = store._load

    def slow_load(path):
        """Hold the check-and-write window wide open."""
        got = real_load(path)
        time.sleep(0.2)
        return got

    monkeypatch.setattr(store, "_load", slow_load)

    def run(revision, state):
        store.write(rec(sid="s1", rev=revision, state=state), tmp_path)

    done = threading.Thread(target=run, args=(6, AgentState.DONE))
    done.start()
    time.sleep(0.05)          # start the second writer inside the first's window
    working = threading.Thread(target=run, args=(5, AgentState.WORKING))
    working.start()
    done.join(); working.join()

    final = read_all(tmp_path)[0]
    assert final.rev == 6
    assert final.state is AgentState.DONE
