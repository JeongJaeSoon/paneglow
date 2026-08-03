from __future__ import annotations

import io
import json
import os
import shlex
import signal
import stat
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from paneglow import cli, store


def paths_for(tmp_path: Path) -> cli.RuntimePaths:
    return cli.RuntimePaths.from_env(
        {
            "HOME": str(tmp_path / "user"),
            "PANEGLOW_HOME": str(tmp_path / "paneglow"),
            "PANEGLOW_CLAUDE_SETTINGS": str(tmp_path / "claude" / "settings.json"),
            "PANEGLOW_MAPPING_DIR": str(tmp_path / "mappings"),
        }
    )


def identity(pid: int = 4321) -> cli.InstanceIdentity:
    return cli.InstanceIdentity(
        pid=pid,
        instance_id=uuid.uuid4().hex,
        started_at=time.time(),
    )


def snapshot_for(
    item: cli.InstanceIdentity | None = None,
    *,
    running: bool = True,
    written_at: float | None = None,
) -> dict:
    return cli._empty_snapshot(
        item or identity(),
        running=running,
        written_at=time.time() if written_at is None else written_at,
    )


def installable_settings(command: str) -> dict:
    return {
        "hooks": {
            event: [cli._hook_entry(command)]
            for event in cli._HOOK_EVENTS
        }
    }


def test_runtime_paths_are_absolute_and_scoped(tmp_path: Path):
    paths = paths_for(tmp_path)
    assert all(
        value.is_absolute()
        for value in (
            paths.home,
            paths.state_dir,
            paths.config_path,
            paths.runtime_dir,
            paths.transition_lock_path,
            paths.lock_path,
            paths.pid_path,
            paths.snapshot_path,
            paths.log_path,
            paths.claude_settings_path,
            paths.claude_sessions_dir,
            paths.mapping_dir,
        )
    )
    assert paths.state_dir == paths.home / "state"
    assert paths.snapshot_path == paths.home / "runtime" / "snapshot.json"


def test_runtime_paths_keep_symlink_evidence_for_trust_boundaries(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    paths = cli.RuntimePaths.from_env(
        {
            "HOME": str(tmp_path / "user"),
            "PANEGLOW_MAPPING_DIR": str(linked),
        }
    )
    assert paths.mapping_dir == linked.absolute()
    assert paths.mapping_dir.is_symlink()


def test_no_arguments_prints_help_and_succeeds(capsys):
    assert cli.main([]) == 0
    assert "paneglow" in capsys.readouterr().out


def test_invalid_timeout_returns_two_without_raising(capsys):
    assert cli.main(["start", "--timeout", "nan"]) == 2
    assert "finite" in capsys.readouterr().err


def test_hook_is_selected_before_argparse_and_writes_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("PANEGLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "cwd": "/workspace",
                }
            )
        ),
    )
    assert cli.main(["hook", "--argparse-must-not-see-this"]) == 0
    assert [record.session_id for record in store.read_all(tmp_path / "home" / "state")] \
        == ["session-1"]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", ["{not json", "null", "[]"])
def test_hook_malformed_input_is_always_zero_and_silent(
    payload: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("PANEGLOW_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert cli.main(["hook"]) == 0
    assert capsys.readouterr() == ("", "")


def test_hook_import_or_path_failure_is_always_zero_and_silent(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    def fail(_cls):
        print("private output")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.RuntimePaths, "from_env", classmethod(fail))
    assert cli.main(["hook"]) == 0
    assert capsys.readouterr() == ("", "")


def test_atomic_runtime_json_is_mode_0600_and_round_trips(tmp_path: Path):
    path = tmp_path / "runtime" / "snapshot.json"
    expected = snapshot_for()
    cli._atomic_write_json(path, expected)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cli._read_snapshot(path, status_poll_ms=1000) == expected


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("owner"),
        lambda value: value.update(written_at=float("nan")),
        lambda value: value.update(slots=value["slots"][:5]),
        lambda value: value["pad"].update(secret="raw exception"),
        lambda value: value["pad"].update(error_code="Traceback: private"),
        lambda value: value.update(last_input_result="private path"),
    ],
    ids=[
        "unknown",
        "missing",
        "nonfinite",
        "wrong-slot-count",
        "nested-unknown",
        "unsafe-pad-error",
        "unsafe-input-result",
    ],
)
def test_snapshot_schema_rejects_unknown_missing_or_unsafe_fields(mutate):
    value = snapshot_for()
    mutate(value)
    with pytest.raises(cli.RuntimeDataError):
        cli._validate_snapshot(value)


def test_snapshot_reader_rejects_non_private_mode(tmp_path: Path):
    path = tmp_path / "snapshot.json"
    cli._atomic_write_json(path, snapshot_for())
    path.chmod(0o644)
    with pytest.raises(cli.RuntimeDataError, match="0600"):
        cli._read_snapshot(path, status_poll_ms=1000)


def test_snapshot_reader_rejects_duplicate_json_fields(tmp_path: Path):
    path = tmp_path / "snapshot.json"
    path.write_text('{"schema_version":1,"schema_version":1}')
    path.chmod(0o600)
    with pytest.raises(cli.RuntimeDataError, match="duplicate"):
        cli._read_private_json(path)


def test_snapshot_reader_rejects_stale_and_future_data(tmp_path: Path):
    path = tmp_path / "snapshot.json"
    cli._atomic_write_json(path, snapshot_for(written_at=10.0))
    with pytest.raises(cli.RuntimeDataError, match="stale"):
        cli._read_snapshot(path, status_poll_ms=1000, now=14.01)
    cli._atomic_write_json(path, snapshot_for(written_at=20.0))
    with pytest.raises(cli.RuntimeDataError, match="stale"):
        cli._read_snapshot(path, status_poll_ms=1000, now=14.0)
    assert cli._read_snapshot(
        path, status_poll_ms=1000, now=14.0, allow_stale=True
    )["written_at"] == 20.0


def test_snapshot_reports_active_fault_feedback_as_current_ambient_decision():
    daemon = SimpleNamespace(
        generation=3,
        last_status_at=123.0,
        owner="claude",
        frontmost_ok=True,
        frontmost_id="com.anthropic.claudefordesktop",
        pad=SimpleNamespace(
            connected=True,
            status_verified=True,
            transport="USB",
            epoch=1,
            firmware_version="v0.4.1",
        ),
        verified_layer=1,
        pad_error_code=None,
        session_snapshot=SimpleNamespace(authoritative=True, sessions=()),
        session_diagnostics=(),
        slots=[None] * 6,
        effective_states={},
        effective_reasons={},
        causes=("input_feedback", "paint_ambient"),
        keys_reclaim_due=None,
        ambient_reclaim_due=None,
        feedback_active=True,
        cfg=SimpleNamespace(
            underglow_scope="outside",
            layer_underglow="keep",
            underglow_claude=0xFF6D00,
            underglow_codex=0x304FFE,
            effect_normal="solid",
            effect_alert="pulse",
            effect_fault="blink",
        ),
        last_input_result="open_failed",
    )

    result = cli._validate_snapshot(cli._snapshot_from_daemon(daemon, 0, None))
    assert result["zones"]["ambient"] == {
        "color": 0xFF6D00,
        "effect": "blink",
        "reason": "input_feedback",
    }
    assert result["last_input_result"] == "open_failed"


def test_pid_file_is_atomic_private_and_contains_instance_identity(tmp_path: Path):
    path = tmp_path / "runtime" / "pid.json"
    expected = identity()
    cli._write_pid(path, expected)
    assert cli._read_pid(path) == expected
    assert set(json.loads(path.read_text())) == {
        "schema_version", "pid", "instance_id", "started_at"
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pid_reader_rejects_bool_pid_and_unknown_fields(tmp_path: Path):
    path = tmp_path / "pid.json"
    valid = {
        "schema_version": 1,
        "pid": 12,
        "instance_id": uuid.uuid4().hex,
        "started_at": time.time(),
    }
    cli._atomic_write_json(path, {**valid, "pid": True})
    with pytest.raises(cli.RuntimeDataError):
        cli._read_pid(path)
    cli._atomic_write_json(path, {**valid, "extra": "x"})
    with pytest.raises(cli.RuntimeDataError):
        cli._read_pid(path)
    cli._atomic_write_json(path, {**valid, "started_at": -1})
    with pytest.raises(cli.RuntimeDataError):
        cli._read_pid(path)


class FakeRuntime:
    def __init__(self, *, run_error: BaseException | None = None,
                 close_error: BaseException | None = None) -> None:
        self.run_error = run_error
        self.close_error = close_error
        self.flushes: list[float] = []
        self.ran = False

    def run(self, stop_event, publish):
        self.ran = True
        payload = snapshot_for()
        payload["generation"] = 7
        payload["last_causes"] = ["test"]
        publish(payload)
        stop_event.set()
        if self.run_error is not None:
            raise self.run_error

    def close(self, flush_seconds=1.0):
        self.flushes.append(flush_seconds)
        if self.close_error is not None:
            raise self.close_error


def test_run_owns_lock_writes_identity_and_always_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    runtime = FakeRuntime()
    monkeypatch.setattr(cli, "_runtime_factory", lambda _cfg, _paths: runtime)
    stdout, stderr = io.StringIO(), io.StringIO()

    assert cli._cmd_run(paths, stdout=stdout, stderr=stderr) == 0
    assert runtime.ran is True and runtime.flushes == [1.0]
    assert not paths.pid_path.exists()
    stopped = cli._read_snapshot(
        paths.snapshot_path, status_poll_ms=1000, allow_stale=True
    )
    assert stopped["running"] is False
    assert stopped["generation"] == 7
    assert "stopped" in stopped["last_causes"]
    assert cli._lock_is_held(paths.lock_path) is False
    assert cli._lock_is_held(paths.transition_lock_path) is False
    assert stat.S_IMODE(paths.snapshot_path.stat().st_mode) == 0o600


def test_run_failure_still_closes_snapshots_removes_pid_and_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    runtime = FakeRuntime(run_error=RuntimeError("private"),
                          close_error=RuntimeError("private close"))
    monkeypatch.setattr(cli, "_runtime_factory", lambda _cfg, _paths: runtime)
    stderr = io.StringIO()
    assert cli._cmd_run(paths, stdout=io.StringIO(), stderr=stderr) == 1
    assert "private" not in stderr.getvalue()
    assert runtime.flushes == [1.0]
    assert not paths.pid_path.exists()
    stopped = cli._read_snapshot(
        paths.snapshot_path, status_poll_ms=1000, allow_stale=True
    )
    assert stopped["running"] is False
    assert stopped["pad"]["error_code"] == "close_failed"
    assert cli._lock_is_held(paths.lock_path) is False


def test_run_publishes_daemon_close_diagnostics_before_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)

    class ReportingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.closed = False

        def close(self, flush_seconds=1.0):
            super().close(flush_seconds)
            self.closed = True

        def snapshot(self):
            payload = snapshot_for()
            payload["generation"] = 8
            payload["last_causes"] = ["shutdown"]
            if self.closed:
                payload["pad"]["error_code"] = "close_failed"
            return payload

    runtime = ReportingRuntime()
    monkeypatch.setattr(cli, "_runtime_factory", lambda _cfg, _paths: runtime)
    assert cli._cmd_run(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 1
    stopped = cli._read_snapshot(
        paths.snapshot_path, status_poll_ms=1000, allow_stale=True
    )
    assert stopped["running"] is False
    assert stopped["generation"] == 8
    assert stopped["pad"]["error_code"] == "close_failed"


def test_run_uses_nonblocking_single_instance_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    lock = cli._LifetimeLock(paths.lock_path)
    lock.acquire()
    try:
        called = False

        def factory(_cfg, _paths):
            nonlocal called
            called = True
            return FakeRuntime()

        monkeypatch.setattr(cli, "_runtime_factory", factory)
        assert cli._cmd_run(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 1
        assert called is False
    finally:
        lock.close()


def test_run_replaces_stale_identity_before_runtime_construction_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    stale = identity(8765)
    cli._write_pid(paths.pid_path, stale)
    cli._atomic_write_json(paths.snapshot_path, snapshot_for(stale))
    signals = []
    observed = {}

    def factory(_cfg, _paths):
        current = cli._read_pid(paths.pid_path)
        initial = cli._read_snapshot(
            paths.snapshot_path, status_poll_ms=1000, allow_stale=True
        )
        observed.update(current=current, initial=initial)
        assert cli._cmd_stop(
            paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
        ) == 1
        return FakeRuntime()

    monkeypatch.setattr(cli, "_runtime_factory", factory)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert cli._cmd_run(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 0

    current = observed["current"]
    initial = observed["initial"]
    assert current.pid == os.getpid()
    assert current.instance_id != stale.instance_id
    assert initial["pid"] == current.pid
    assert initial["instance_id"] == current.instance_id
    assert initial["running"] is True and initial["generation"] == 0
    assert signals == [(current.pid, 0), (current.pid, signal.SIGTERM)]
    assert all(pid != stale.pid for pid, _sig in signals)


def test_run_transition_guard_blocks_stop_before_stale_identity_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    stale = identity(8765)
    cli._write_pid(paths.pid_path, stale)
    cli._atomic_write_json(paths.snapshot_path, snapshot_for(stale))
    signals = []
    observed = {}
    original_write_pid = cli._write_pid

    def write_pid(path, current):
        observed["stop_result"] = cli._cmd_stop(
            paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
        )
        observed["still_stale"] = cli._read_pid(paths.pid_path)
        original_write_pid(path, current)

    monkeypatch.setattr(cli, "_write_pid", write_pid)
    monkeypatch.setattr(cli, "_runtime_factory", lambda _cfg, _paths: FakeRuntime())
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert cli._cmd_run(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert observed == {"stop_result": 1, "still_stale": stale}
    assert signals == []


def test_run_transition_guard_blocks_stop_and_readiness_during_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    signals = []
    observed = {}

    class TeardownRuntime(FakeRuntime):
        def close(self, flush_seconds=1.0):
            current = cli._read_pid(paths.pid_path)
            observed["stop_result"] = cli._cmd_stop(
                paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
            )
            observed["readiness"] = cli._identity_ready(
                paths, child_pid=current.pid, status_poll_ms=1000
            )
            super().close(flush_seconds)

    runtime = TeardownRuntime()
    monkeypatch.setattr(cli, "_runtime_factory", lambda _cfg, _paths: runtime)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert cli._cmd_run(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert observed == {"stop_result": 1, "readiness": None}
    assert signals == []
    assert not paths.pid_path.exists()
    assert cli._lock_is_held(paths.lock_path) is False
    assert cli._lock_is_held(paths.transition_lock_path) is False


def test_stop_with_free_lock_never_signals_stale_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    stale = identity(8765)
    cli._write_pid(paths.pid_path, stale)
    cli._atomic_write_json(paths.snapshot_path, snapshot_for(stale))
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert cli._cmd_stop(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert signals == []


def test_start_uses_absolute_detached_prefix_and_waits_for_matching_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    expected = identity(7654)
    captured = {}

    class Child:
        pid = expected.pid

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(
        cli, "_identity_ready",
        lambda _paths, child_pid, status_poll_ms: (
            expected if child_pid == expected.pid and status_poll_ms == 1000 else None
        ),
    )

    def spawn(command, log_fd):
        captured["command"] = command
        captured["log_fd"] = log_fd
        return Child()

    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    output = io.StringIO()
    assert cli._cmd_start(paths, 1.0, stdout=output, stderr=io.StringIO()) == 0
    command = captured["command"]
    assert Path(command[0]).is_absolute()
    assert command[-3:] == ("-m", "paneglow.cli", "run")
    assert "started" in output.getvalue()


def test_command_prefix_preserves_virtualenv_executable_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(sys.executable))
    monkeypatch.setattr(cli.sys, "executable", str(executable))

    assert cli._absolute_command_prefix() == (
        str(executable),
        "-m",
        "paneglow.cli",
    )


def test_start_never_reads_or_spawns_during_identity_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    transition = cli._LifetimeLock(paths.transition_lock_path)
    transition.acquire()
    monkeypatch.setattr(
        cli,
        "_spawn_detached",
        lambda *_args: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    try:
        assert cli._cmd_start(
            paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
        ) == 1
    finally:
        transition.close()


def test_readiness_requires_first_daemon_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    expected = identity(7654)
    cli._write_pid(paths.pid_path, expected)
    starting = snapshot_for(expected)
    cli._atomic_write_json(paths.snapshot_path, starting)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    assert cli._identity_ready(
        paths, child_pid=expected.pid, status_poll_ms=1000
    ) is None
    starting["generation"] = 1
    starting["written_at"] = time.time()
    cli._atomic_write_json(paths.snapshot_path, starting)
    assert cli._identity_ready(
        paths, child_pid=expected.pid, status_poll_ms=1000
    ) == expected


def test_readiness_never_observes_identity_during_transition(tmp_path: Path):
    paths = paths_for(tmp_path)
    expected = identity(7654)
    cli._write_pid(paths.pid_path, expected)
    ready = snapshot_for(expected)
    ready["generation"] = 1
    cli._atomic_write_json(paths.snapshot_path, ready)
    lifetime = cli._LifetimeLock(paths.lock_path)
    transition = cli._LifetimeLock(paths.transition_lock_path)
    lifetime.acquire()
    transition.acquire()
    try:
        assert cli._identity_ready(
            paths, child_pid=expected.pid, status_poll_ms=1000
        ) is None
    finally:
        lifetime.close()
        transition.close()


def test_stop_requires_held_lock_pid_and_snapshot_identity_before_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    expected = identity(8765)
    snap = snapshot_for(expected)
    lock_states = iter([True, False])
    signals = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: next(lock_states))
    monkeypatch.setattr(cli, "_read_pid", lambda _path: expected)
    monkeypatch.setattr(cli, "_read_snapshot", lambda *_args, **_kwargs: snap)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert cli._cmd_stop(paths, 1.0, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert signals == [(expected.pid, 0), (expected.pid, signal.SIGTERM)]
    assert all(sig != signal.SIGKILL for _pid, sig in signals)


def test_stop_refuses_mismatched_instance_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    expected = identity(8765)
    snap = snapshot_for(identity(expected.pid))
    signals = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(cli, "_read_pid", lambda _path: expected)
    monkeypatch.setattr(cli, "_read_snapshot", lambda *_args, **_kwargs: snap)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert cli._cmd_stop(paths, 0, stdout=io.StringIO(), stderr=io.StringIO()) == 1
    assert signals == []


def test_stop_timeout_never_escalates_to_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    expected = identity(8765)
    snap = snapshot_for(expected)
    signals = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(cli, "_read_pid", lambda _path: expected)
    monkeypatch.setattr(cli, "_read_snapshot", lambda *_args, **_kwargs: snap)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert cli._cmd_stop(paths, 0, stdout=io.StringIO(), stderr=io.StringIO()) == 1
    assert signals == [(expected.pid, 0), (expected.pid, signal.SIGTERM)]


def test_status_reads_snapshot_without_opening_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import pad

    paths = paths_for(tmp_path)
    expected = identity()
    snap = snapshot_for(expected)
    snap["frontmost"] = {"ok": True, "bundle_id": "com.example.App"}
    snap["owner"] = "claude"
    snap["pad"].update(
        connected=True,
        transport="USB",
        status_verified=True,
        layer_index=1,
        version="v0.4.1",
        last_status_at=time.time(),
        error_code=None,
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(cli, "_runtime_identity", lambda *_args, **_kwargs: (expected, snap))
    monkeypatch.setattr(
        pad.Pad, "open", lambda: (_ for _ in ()).throw(AssertionError("hardware opened"))
    )
    output = io.StringIO()
    assert cli._cmd_status(paths, stdout=output, stderr=io.StringIO()) == 0
    assert "running" in output.getvalue()
    assert "USB" in output.getvalue()


def test_status_stopped_does_not_require_snapshot_or_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(
        cli, "_runtime_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError),
    )
    output = io.StringIO()
    assert cli._cmd_status(paths, stdout=output, stderr=io.StringIO()) == 0
    assert "stopped" in output.getvalue()


def test_runtime_identity_never_reads_hints_during_transition(tmp_path: Path):
    paths = paths_for(tmp_path)
    transition = cli._LifetimeLock(paths.transition_lock_path)
    transition.acquire()
    try:
        with pytest.raises(cli.RuntimeDataError, match="transitioning"):
            cli._runtime_identity(paths, status_poll_ms=1000)
    finally:
        transition.close()


def test_install_hooks_preserves_existing_content_backs_up_and_is_idempotent(
    tmp_path: Path
):
    paths = paths_for(tmp_path)
    paths.claude_settings_path.parent.mkdir(parents=True)
    original_value = {
        "theme": "dark",
        "hooks": {
            "Stop": [
                {"matcher": "x", "hooks": [{"type": "command", "command": "/old hook"}]}
            ],
            "CustomEvent": [{"hooks": [{"type": "command", "command": "/custom"}]}],
        },
    }
    original = json.dumps(original_value, indent=2).encode()
    paths.claude_settings_path.write_bytes(original)
    output = io.StringIO()
    assert cli._cmd_install_hooks(paths, stdout=output, stderr=io.StringIO()) == 0

    installed = json.loads(paths.claude_settings_path.read_text())
    command = cli._hook_command()
    assert cli._HOOK_EVENTS == (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "Notification",
        "Stop",
        "StopFailure",
        "PreCompact",
        "SessionEnd",
    )
    assert installed["theme"] == "dark"
    assert installed["hooks"]["CustomEvent"] == original_value["hooks"]["CustomEvent"]
    assert installed["hooks"]["Stop"][0] == original_value["hooks"]["Stop"][0]
    assert cli._hooks_installed(installed, command)
    assert len(cli._HOOK_EVENTS) == 11
    for event in cli._HOOK_EVENTS:
        assert sum(
            cli._entry_has_command(entry, command)
            for entry in installed["hooks"][event]
        ) == 1
    command_parts = shlex.split(command)
    assert Path(command_parts[0]).is_absolute()
    assert command_parts[-3:] == ["-m", "paneglow.cli", "hook"]

    backup = paths.claude_settings_path.with_name(
        paths.claude_settings_path.name + ".paneglow.bak"
    )
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.claude_settings_path.stat().st_mode) == 0o600

    installed_bytes = paths.claude_settings_path.read_bytes()
    backup_bytes = backup.read_bytes()
    assert cli._cmd_install_hooks(paths, stdout=output, stderr=io.StringIO()) == 0
    assert paths.claude_settings_path.read_bytes() == installed_bytes
    assert backup.read_bytes() == backup_bytes
    assert "already installed" in output.getvalue()


def test_install_hooks_migrates_resolved_venv_command_and_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(sys.executable))
    monkeypatch.setattr(cli.sys, "executable", str(executable))
    command = cli._hook_command()
    legacy = shlex.join(
        (str(executable.resolve()), "-m", "paneglow.cli", "hook")
    )
    assert legacy != command

    unrelated = {
        "matcher": "user-owned",
        "hooks": [{"type": "command", "command": "/usr/bin/true"}],
    }
    original_value = {
        "hooks": {
            event: [cli._hook_entry(legacy)]
            for event in cli._HOOK_EVENTS
        }
    }
    original_value["hooks"]["Stop"].extend(
        [cli._hook_entry(command), unrelated]
    )
    original = json.dumps(original_value, indent=2).encode()
    paths.claude_settings_path.parent.mkdir(parents=True)
    paths.claude_settings_path.write_bytes(original)

    output = io.StringIO()
    assert cli._cmd_install_hooks(
        paths, stdout=output, stderr=io.StringIO()
    ) == 0
    installed_bytes = paths.claude_settings_path.read_bytes()
    installed = json.loads(installed_bytes)
    for event in cli._HOOK_EVENTS:
        entries = installed["hooks"][event]
        assert sum(
            cli._generated_hook_entry(entry, command) for entry in entries
        ) == 1
        assert not any(cli._entry_has_command(entry, legacy) for entry in entries)
    assert unrelated in installed["hooks"]["Stop"]

    backup = paths.claude_settings_path.with_name(
        paths.claude_settings_path.name + ".paneglow.bak"
    )
    assert backup.read_bytes() == original
    backup_bytes = backup.read_bytes()
    assert cli._cmd_install_hooks(
        paths, stdout=output, stderr=io.StringIO()
    ) == 0
    assert paths.claude_settings_path.read_bytes() == installed_bytes
    assert backup.read_bytes() == backup_bytes
    assert "already installed" in output.getvalue()


def test_install_hooks_refuses_malformed_settings_without_overwrite(tmp_path: Path):
    paths = paths_for(tmp_path)
    paths.claude_settings_path.parent.mkdir(parents=True)
    original = b"{not json"
    paths.claude_settings_path.write_bytes(original)
    assert cli._cmd_install_hooks(
        paths, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert paths.claude_settings_path.read_bytes() == original
    assert not paths.claude_settings_path.with_name(
        paths.claude_settings_path.name + ".paneglow.bak"
    ).exists()


def test_install_hooks_refuses_symlinked_settings_without_touching_target(
    tmp_path: Path
):
    paths = paths_for(tmp_path)
    target = tmp_path / "outside-settings.json"
    original = b'{"theme":"private"}'
    target.write_bytes(original)
    paths.claude_settings_path.parent.mkdir(parents=True)
    paths.claude_settings_path.symlink_to(target)

    assert cli._cmd_install_hooks(
        paths, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert target.read_bytes() == original
    assert paths.claude_settings_path.is_symlink()


def prepare_doctor_files(paths: cli.RuntimePaths) -> None:
    paths.claude_sessions_dir.mkdir(parents=True)
    paths.mapping_dir.mkdir(parents=True)
    cli._atomic_write_json(
        paths.claude_settings_path, installable_settings(cli._hook_command())
    )


def test_doctor_running_uses_fresh_snapshot_and_never_opens_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import pad

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)
    expected = identity()
    snap = snapshot_for(expected)
    snap["pad"].update(
        connected=True,
        transport="BLE",
        status_verified=True,
        layer_index=1,
        version="v0.4.1",
        last_status_at=time.time(),
        error_code=None,
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(cli, "_runtime_identity", lambda *_args, **_kwargs: (expected, snap))
    monkeypatch.setattr(
        pad.Pad, "open", lambda: (_ for _ in ()).throw(AssertionError("hardware opened"))
    )
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 0
    assert "daemon pad snapshot" in output.getvalue()


def test_doctor_reports_layer_two_as_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)
    expected = identity()
    snap = snapshot_for(expected)
    snap["pad"].update(
        connected=True,
        transport="USB",
        status_verified=True,
        layer_index=2,
        version="v0.4.1",
        last_status_at=time.time(),
        error_code=None,
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(cli, "_runtime_identity", lambda *_args, **_kwargs: (expected, snap))
    assert cli._cmd_doctor(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 1


def test_doctor_stopped_does_fresh_status_and_always_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import pad

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)

    class Device:
        transport = "USB"

        def __init__(self):
            self.closed = []

        @staticmethod
        def status(timeout=3.0):
            return {"result": {"layer_index": 1, "version": "v0.4.1"}}

        def close(self, flush_seconds=1.0, *, turn_off_keys=True,
                  turn_off_ambient=True):
            self.closed.append(
                (flush_seconds, turn_off_keys, turn_off_ambient))

    device = Device()
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(pad.Pad, "open", lambda: device)
    monkeypatch.setattr(
        cli, "_load_config",
        lambda _paths: (
            SimpleNamespace(status_poll_ms=1000),
            ["gate.mode: 'PRIVATE_VALUE' is not usable", "bad\nvalue"],
        ),
    )
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 0
    assert device.closed == [(1.0, False, False)]
    assert "fresh pad round-trip" in output.getvalue()
    assert "gate.mode" in output.getvalue()
    assert "invalid_value" in output.getvalue()
    assert "PRIVATE_VALUE" not in output.getvalue()
    assert "bad" not in output.getvalue()


def test_doctor_stopped_closes_even_when_status_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import pad

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)

    class Device:
        transport = "USB"

        def __init__(self):
            self.closed = 0

        @staticmethod
        def status(timeout=3.0):
            raise RuntimeError("private")

        def close(self, flush_seconds=1.0, *, turn_off_keys=True,
                  turn_off_ambient=True):
            self.closed += 1

    device = Device()
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(pad.Pad, "open", lambda: device)
    assert cli._cmd_doctor(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 1
    assert device.closed == 1


def test_doctor_fails_when_live_session_has_no_desktop_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import sessions

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)
    live = sessions.Session(
        session_id="11111111-1111-4111-8111-111111111111",
        cwd="/private",
        name="private",
        entrypoint="claude-code",
        pid=123,
        started_at=1.0,
    )
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda root: sessions.SessionSnapshot((live,), True, ()),
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(
        cli, "_doctor_stopped", lambda _stdout: True
    )
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "deep-link mappings unresolved (1)" in output.getvalue()
    assert live.session_id not in output.getvalue()
    assert live.cwd not in output.getvalue()


def test_doctor_resolves_every_live_session_mapping_without_printing_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import deeplink, sessions

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)
    live = sessions.Session(
        session_id="22222222-2222-4222-8222-222222222222",
        cwd="/private",
        name="private",
        entrypoint="claude-code",
        pid=123,
        started_at=1.0,
    )
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda root: sessions.SessionSnapshot((live,), True, ()),
    )
    monkeypatch.setattr(
        deeplink, "local_id_for", lambda session_id, roots: "local-ok"
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_doctor_stopped", lambda _stdout: True)
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 0
    assert "deep-link mappings resolve (1)" in output.getvalue()
    assert live.session_id not in output.getvalue()


def test_doctor_fails_closed_on_untrusted_session_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import sessions

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda root: sessions.SessionSnapshot((), False, ("private path",)),
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_doctor_stopped", lambda _stdout: True)
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "not authoritative" in output.getvalue()
    assert "private path" not in output.getvalue()


def test_doctor_rejects_symlinked_session_and_mapping_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    cli._atomic_write_json(
        paths.claude_settings_path, installable_settings(cli._hook_command())
    )
    session_target = tmp_path / "session-target"
    mapping_target = tmp_path / "mapping-target"
    session_target.mkdir()
    mapping_target.mkdir()
    paths.claude_sessions_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.claude_sessions_dir.symlink_to(session_target, target_is_directory=True)
    paths.mapping_dir.symlink_to(mapping_target, target_is_directory=True)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_doctor_stopped", lambda _stdout: True)

    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "session directory is unsafe" in output.getvalue()


def test_doctor_rejects_symlinked_mapping_root_after_trusted_session_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = paths_for(tmp_path)
    paths.claude_sessions_dir.mkdir(parents=True)
    cli._atomic_write_json(
        paths.claude_settings_path, installable_settings(cli._hook_command())
    )
    mapping_target = tmp_path / "mapping-target"
    mapping_target.mkdir()
    paths.mapping_dir.symlink_to(mapping_target, target_is_directory=True)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_doctor_stopped", lambda _stdout: True)

    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "mapping directory is missing or unsafe" in output.getvalue()


def test_doctor_sanitizes_session_mapping_and_settings_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import deeplink, sessions

    paths = paths_for(tmp_path)
    prepare_doctor_files(paths)
    secret = "/private/TOP-SECRET"
    monkeypatch.setattr(
        sessions, "scan", lambda root: (_ for _ in ()).throw(OSError(secret))
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_doctor_stopped", lambda _stdout: True)
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "session scan is unavailable" in output.getvalue()
    assert secret not in output.getvalue()

    live = sessions.Session(
        session_id="33333333-3333-4333-8333-333333333333",
        cwd=secret,
        name="private",
        entrypoint="claude-code",
        pid=123,
        started_at=1.0,
    )
    monkeypatch.setattr(
        sessions, "scan", lambda root: sessions.SessionSnapshot((live,), True, ())
    )
    monkeypatch.setattr(
        deeplink,
        "local_id_for",
        lambda *_args: (_ for _ in ()).throw(OSError(secret)),
    )
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "mapping check is unavailable" in output.getvalue()
    assert secret not in output.getvalue()

    monkeypatch.setattr(
        cli,
        "_read_settings",
        lambda _path: (_ for _ in ()).throw(OSError(secret)),
    )
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 1
    assert "settings are unreadable" in output.getvalue()
    assert secret not in output.getvalue()


def test_default_runtime_uses_the_same_session_and_mapping_roots_as_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from paneglow import daemon, deeplink, sessions

    paths = paths_for(tmp_path)
    captured = {}

    class StubDaemon:
        def __init__(self, _cfg, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(daemon, "Daemon", StubDaemon)
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda root: captured.update(scan_root=root) or "session-snapshot",
    )
    monkeypatch.setattr(
        deeplink,
        "open_session",
        lambda session_id, roots: captured.update(
            open_session_id=session_id, mapping_roots=roots
        ) or True,
    )

    cli._DefaultRuntime(SimpleNamespace(), paths)
    assert captured["scanner"]() == "session-snapshot"
    assert captured["opener"]("session-id") is True
    assert captured["state_root"] == paths.state_dir
    assert captured["scan_root"] == paths.claude_sessions_dir
    assert captured["mapping_roots"] == (paths.mapping_dir,)
    assert captured["open_session_id"] == "session-id"
    assert callable(captured["input_observer"])


class RecordingEvent:
    def __init__(self) -> None:
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stopped


def test_default_runtime_does_not_double_sleep_after_connected_tick(monkeypatch):
    runtime = object.__new__(cli._DefaultRuntime)
    runtime.cfg = SimpleNamespace(poll_ms=250)
    ticks = []

    class Daemon:
        pad = SimpleNamespace(connected=True, status_verified=True)
        verified_layer = 1

        @staticmethod
        def tick(now):
            ticks.append(now)

    runtime.daemon = Daemon()
    runtime.generation = 0
    runtime.last_status_at = None
    event = RecordingEvent()
    monkeypatch.setattr(cli, "_snapshot_from_daemon", lambda *_args: snapshot_for())
    monkeypatch.setattr(cli.time, "time", lambda: 1_900_000_000.0)
    monkeypatch.setattr(
        cli.time,
        "monotonic",
        lambda: (_ for _ in ()).throw(AssertionError("wrong clock domain")),
    )

    def publish(_payload):
        event.set()

    runtime.run(event, publish)
    assert event.waits == []
    assert ticks == [1_900_000_000.0]


def test_default_runtime_waits_when_pad_is_unavailable(monkeypatch):
    runtime = object.__new__(cli._DefaultRuntime)
    runtime.cfg = SimpleNamespace(poll_ms=250)

    class Daemon:
        pad = None
        verified_layer = None

        @staticmethod
        def tick(_now):
            pass

    runtime.daemon = Daemon()
    runtime.generation = 0
    runtime.last_status_at = None
    event = RecordingEvent()
    monkeypatch.setattr(cli, "_snapshot_from_daemon", lambda *_args: snapshot_for())

    def publish(_payload):
        event.set()

    runtime.run(event, publish)
    assert event.waits == [0.25]


def trace_request(instance_id: str, *, duration_ms: int = 5_000,
                  max_events: int = 32, request_id: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id or uuid.uuid4().hex,
        "instance_id": instance_id,
        "created_at": time.time(),
        "duration_ms": duration_ms,
        "max_events": max_events,
    }


def trace_event(**overrides):
    value = {
        "key": "A1",
        "action": 1,
        "received_at": 100.01,
        "connection_ordinal": 77,
        "owner_at_dispatch": "claude",
        "layer_one": True,
        "slot_occupied": True,
        "result": "opened",
    }
    value.update(overrides)
    return SimpleNamespace(**value)


def test_trace_runtime_paths_are_private_and_separate(tmp_path: Path):
    paths = paths_for(tmp_path)
    assert paths.trace_lock_path == paths.runtime_dir / "trace.lock"
    assert paths.trace_request_path == paths.runtime_dir / "trace.request.json"
    assert paths.trace_active_path == paths.runtime_dir / "trace.active.json"
    assert paths.trace_result_path == paths.runtime_dir / "trace.result.json"


def test_trace_parser_defaults_are_short_and_bounded():
    parsed = cli._build_parser().parse_args(["trace-input"])
    assert parsed.seconds == 5.0
    assert parsed.max_events == 32


@pytest.mark.parametrize(
    ("seconds", "max_events"),
    [(True, 1), (float("inf"), 1), (0.0, 1), (10.01, 1), (1.0, True), (1.0, 65)],
)
def test_trace_direct_bounds_fail_before_creating_runtime(
    tmp_path: Path, seconds, max_events,
):
    paths = paths_for(tmp_path)
    assert cli._cmd_trace_input(
        paths, seconds, max_events, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 2
    assert not paths.runtime_dir.exists()


def test_trace_is_completely_off_without_a_request(tmp_path: Path):
    paths = paths_for(tmp_path)
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(uuid.uuid4().hex)
    mailbox.poll(now=100.0)
    mailbox.observe(trace_event())
    mailbox.close()
    assert not paths.runtime_dir.exists()


def test_trace_mailbox_collects_only_post_start_normalized_events_and_caps(
    tmp_path: Path,
):
    paths = paths_for(tmp_path)
    instance_id = uuid.uuid4().hex
    request = trace_request(instance_id, max_events=3)
    cli._atomic_create_trace_json(paths.trace_request_path, request)
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(instance_id)
    mailbox.poll(now=100.0)
    assert paths.trace_active_path.exists()
    assert not paths.trace_request_path.exists()

    mailbox.observe(trace_event(received_at=99.999))
    mailbox.observe(trace_event(received_at=100.010, connection_ordinal=77))
    mailbox.observe(trace_event(
        key="A2", action=0, received_at=100.020,
        connection_ordinal=77, result="ignored_input",
    ))
    mailbox.observe(trace_event(
        key="other", action="other", received_at=100.030,
        connection_ordinal=900_001, owner_at_dispatch="codex",
        layer_one=False, slot_occupied=False, result="ignored_input",
    ))

    result_file = cli._read_private_trace_json(paths.trace_result_path)
    result = cli._validate_trace_result(result_file.value)
    assert result["request_id"] == request["request_id"]
    assert result["instance_id"] == instance_id
    assert result["truncated"] is True
    assert [item["sequence"] for item in result["events"]] == [1, 2, 3]
    assert [item["key"] for item in result["events"]] == ["A1", "A2", "other"]
    assert [item["connection_ordinal"] for item in result["events"]] == [1, 1, 2]
    assert [item["relative_ms"] for item in result["events"]] \
        == sorted(item["relative_ms"] for item in result["events"])
    assert result["events"][-1]["relative_ms"] <= result["duration_ms"]
    assert not paths.trace_active_path.exists()


def test_trace_duration_expiry_is_not_truncated(tmp_path: Path):
    paths = paths_for(tmp_path)
    instance_id = uuid.uuid4().hex
    request = trace_request(instance_id, duration_ms=250)
    cli._atomic_create_trace_json(paths.trace_request_path, request)
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(instance_id)
    mailbox.poll(now=50.0)
    mailbox.observe(trace_event(received_at=50.1, result="ignored_owner"))
    mailbox.poll(now=50.25)
    result = cli._validate_trace_result(
        cli._read_private_trace_json(paths.trace_result_path).value
    )
    assert result["duration_ms"] == 250
    assert result["truncated"] is False
    assert len(result["events"]) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(duration_ms=True),
        lambda value: value.update(created_at=float("nan")),
        lambda value: value.update(max_events=65),
        lambda value: value.update(request_id="not-a-uuid"),
    ],
)
def test_trace_request_schema_is_exact_and_finite(mutate):
    value = trace_request(uuid.uuid4().hex)
    mutate(value)
    with pytest.raises(cli.RuntimeDataError):
        cli._validate_trace_request(value)


def test_trace_result_rejects_non_monotonic_or_bool_as_int_events():
    base = {
        "schema_version": 1,
        "duration_ms": 10,
        "truncated": False,
        "events": [
            {
                "sequence": 1, "relative_ms": 8, "key": "A1", "action": 1,
                "owner": "claude", "layer_one": True, "slot_occupied": True,
                "result": "opened", "connection_ordinal": 1,
            },
            {
                "sequence": 2, "relative_ms": 7, "key": "A2", "action": 1,
                "owner": "claude", "layer_one": True, "slot_occupied": True,
                "result": "opened", "connection_ordinal": 1,
            },
        ],
    }
    with pytest.raises(cli.RuntimeDataError, match="monotonic"):
        cli._validate_trace_public(base)
    base["events"][1]["relative_ms"] = 9
    base["events"][1]["action"] = True
    with pytest.raises(cli.RuntimeDataError, match="action"):
        cli._validate_trace_public(base)


def test_malformed_trace_request_is_claimed_once_not_a_tick_poison(tmp_path: Path):
    paths = paths_for(tmp_path)
    cli._atomic_write_bytes(
        paths.trace_request_path,
        b'{"schema_version":1,"schema_version":1}',
    )
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(uuid.uuid4().hex)
    mailbox.poll(now=1.0)
    mailbox.poll(now=2.0)
    assert mailbox.active is None
    assert not paths.trace_request_path.exists()
    assert not paths.trace_active_path.exists()


def test_trace_files_reject_symlinks_hardlinks_and_nonprivate_parent(tmp_path: Path):
    paths = paths_for(tmp_path)
    cli._ensure_trace_parent(paths.runtime_dir)
    target = tmp_path / "target"
    target.write_text("{}")
    paths.trace_request_path.symlink_to(target)
    with pytest.raises(cli.RuntimeDataError):
        cli._trace_path_metadata(paths.trace_request_path)
    paths.trace_request_path.unlink()
    target.chmod(0o600)
    os.link(target, paths.trace_request_path)
    with pytest.raises(cli.RuntimeDataError):
        cli._trace_path_metadata(paths.trace_request_path)
    paths.trace_request_path.unlink()
    paths.runtime_dir.chmod(0o755)
    with pytest.raises(cli.RuntimeDataError):
        cli._trace_path_metadata(paths.trace_result_path)


def test_trace_atomic_publish_uses_rename_and_never_has_nlink_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    real_rename = os.rename
    transitions = []

    def checked_rename(source, destination):
        source_path, destination_path = Path(source), Path(destination)
        assert source_path.stat().st_nlink == 1
        assert not destination_path.exists()
        real_rename(source, destination)
        assert not source_path.exists()
        assert destination_path.stat().st_nlink == 1
        transitions.append((source_path, destination_path))

    monkeypatch.setattr(cli.os, "rename", checked_rename)
    monkeypatch.setattr(
        cli.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hard-link publication used")
        ),
    )
    cli._atomic_create_trace_json(
        paths.trace_request_path, trace_request(uuid.uuid4().hex)
    )
    assert len(transitions) == 1
    assert paths.trace_request_path.stat().st_nlink == 1


def test_trace_atomic_publish_interruption_after_rename_leaves_valid_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    real_rename = os.rename

    def rename_then_interrupt(source, destination):
        real_rename(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.os, "rename", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._atomic_create_trace_json(
            paths.trace_request_path, trace_request(uuid.uuid4().hex)
        )
    assert paths.trace_request_path.is_file()
    assert paths.trace_request_path.stat().st_nlink == 1
    cli._validate_trace_request(
        cli._read_private_trace_json(paths.trace_request_path).value
    )


def test_trace_atomic_publish_never_replaces_cooperative_destination(tmp_path: Path):
    paths = paths_for(tmp_path)
    first = trace_request(uuid.uuid4().hex)
    second = trace_request(uuid.uuid4().hex)
    cli._atomic_create_trace_json(paths.trace_request_path, first)
    before = paths.trace_request_path.stat()
    with pytest.raises(cli.RuntimeDataError, match="occupied"):
        cli._atomic_create_trace_json(paths.trace_request_path, second)
    after = paths.trace_request_path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert cli._validate_trace_request(
        cli._read_private_trace_json(paths.trace_request_path).value
    )["request_id"] == first["request_id"]


def test_trace_claim_rename_has_recoverable_source_or_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    request = trace_request(uuid.uuid4().hex)
    cli._atomic_create_trace_json(paths.trace_request_path, request)
    metadata = paths.trace_request_path.stat()
    trace_file = cli._TraceFile(
        value=request,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        modified_at=metadata.st_mtime,
    )
    real_rename = os.rename
    monkeypatch.setattr(
        cli.os,
        "rename",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("before rename")),
    )
    with pytest.raises(cli.RuntimeDataError):
        cli._claim_trace_inode(
            paths.trace_request_path, paths.trace_active_path, trace_file
        )
    assert paths.trace_request_path.is_file()
    assert not paths.trace_active_path.exists()
    assert paths.trace_request_path.stat().st_nlink == 1

    monkeypatch.setattr(cli.os, "rename", real_rename)
    assert cli._claim_trace_inode(
        paths.trace_request_path, paths.trace_active_path, trace_file
    ) is True
    assert not paths.trace_request_path.exists()
    assert paths.trace_active_path.is_file()
    assert paths.trace_active_path.stat().st_nlink == 1
    assert paths.trace_active_path.stat().st_ino == metadata.st_ino


@pytest.mark.parametrize("orphan_kind", ["same", "other", "semantic", "syntax"])
def test_daemon_removes_safe_orphan_active_once_then_accepts_request(
    tmp_path: Path, orphan_kind: str,
):
    paths = paths_for(tmp_path)
    instance_id = uuid.uuid4().hex
    if orphan_kind == "syntax":
        cli._atomic_write_bytes(paths.trace_active_path, b'{"broken":')
    else:
        orphan = trace_request(
            instance_id if orphan_kind == "same" else uuid.uuid4().hex
        )
        if orphan_kind == "semantic":
            orphan["duration_ms"] = True
        cli._atomic_create_trace_json(paths.trace_active_path, orphan)
    orphan_inode = paths.trace_active_path.stat().st_ino
    request = trace_request(instance_id)
    cli._atomic_create_trace_json(paths.trace_request_path, request)

    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(instance_id)
    mailbox.poll(now=100.0)

    assert mailbox.active is not None
    assert mailbox.active.request_id == request["request_id"]
    assert not paths.trace_request_path.exists()
    assert paths.trace_active_path.stat().st_ino != orphan_inode
    active = cli._validate_trace_request(
        cli._read_private_trace_json(paths.trace_active_path).value
    )
    assert active["request_id"] == request["request_id"]


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_daemon_preserves_unsafe_orphan_active_and_does_not_admit_request(
    tmp_path: Path, unsafe_kind: str,
):
    paths = paths_for(tmp_path)
    cli._ensure_trace_parent(paths.runtime_dir)
    target = tmp_path / "unsafe-active"
    target.write_text("{}")
    target.chmod(0o600)
    if unsafe_kind == "symlink":
        paths.trace_active_path.symlink_to(target)
    else:
        os.link(target, paths.trace_active_path)
    request = trace_request(uuid.uuid4().hex)
    cli._atomic_create_trace_json(paths.trace_request_path, request)

    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(request["instance_id"])
    mailbox.poll(now=100.0)

    assert mailbox.active is None
    assert paths.trace_active_path.exists() or paths.trace_active_path.is_symlink()
    assert paths.trace_request_path.exists()


def test_trace_lock_second_client_fails_without_changing_the_lock(tmp_path: Path):
    paths = paths_for(tmp_path)
    lock = cli._TraceLock(paths.trace_lock_path)
    lock.acquire()
    before = paths.trace_lock_path.stat()
    try:
        stderr = io.StringIO()
        assert cli._cmd_trace_input(
            paths, 0.1, 1, stdout=io.StringIO(), stderr=stderr
        ) == 1
        after = paths.trace_lock_path.stat()
        assert (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns) == (
            before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns,
        )
        assert "already" in stderr.getvalue()
    finally:
        lock.close()


def test_trace_cli_success_prints_only_public_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    expected = identity(os.getpid())
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(expected.instance_id)
    identity_reads = []
    monkeypatch.setattr(
        cli, "_load_config", lambda _paths: (SimpleNamespace(status_poll_ms=1000), [])
    )

    def read_identity(*_args, **kwargs):
        identity_reads.append(kwargs.get("allow_stale", False))
        return expected, snapshot_for(expected)

    monkeypatch.setattr(cli, "_runtime_identity", read_identity)

    def drive_daemon(_seconds):
        mailbox.poll()
        if mailbox.active is not None:
            mailbox.observe(trace_event(received_at=time.monotonic()))

    monkeypatch.setattr(cli.time, "sleep", drive_daemon)
    stdout, stderr = io.StringIO(), io.StringIO()
    assert cli._cmd_trace_input(
        paths, 0.1, 1, stdout=stdout, stderr=stderr
    ) == 0
    assert stderr.getvalue() == ""
    public = json.loads(stdout.getvalue())
    assert set(public) == {"schema_version", "duration_ms", "truncated", "events"}
    assert set(public["events"][0]) == cli._TRACE_EVENT_KEYS
    forbidden = [
        expected.instance_id, str(expected.pid), str(paths.home),
        "request_id", "session_id", "bundle_id", "serial", "cwd",
    ]
    assert all(secret not in stdout.getvalue() for secret in forbidden)
    assert identity_reads[0] is False
    assert True in identity_reads[1:]
    assert not paths.trace_request_path.exists()
    assert not paths.trace_active_path.exists()
    assert not paths.trace_result_path.exists()


def test_same_instance_active_marker_never_expires_under_a_blocked_opener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    expected = identity(os.getpid())
    request = trace_request(expected.instance_id, duration_ms=10_000)
    cli._atomic_create_trace_json(paths.trace_request_path, request)
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(expected.instance_id)
    mailbox.poll(now=100.0)
    old = time.time() - 3_600
    os.utime(paths.trace_active_path, (old, old))
    before = paths.trace_active_path.stat()
    monkeypatch.setattr(
        cli, "_load_config", lambda _paths: (SimpleNamespace(status_poll_ms=1000), [])
    )
    monkeypatch.setattr(
        cli, "_runtime_identity", lambda *_args, **_kwargs: (expected, snapshot_for(expected))
    )
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    stderr = io.StringIO()
    assert cli._cmd_trace_input(
        paths, 0.1, 1, stdout=io.StringIO(), stderr=stderr
    ) == 1
    after = paths.trace_active_path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert not paths.trace_request_path.exists()
    assert "verified input trace" in stderr.getvalue()


def test_new_trace_consumes_abandoned_valid_result_before_publishing_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    expected = identity(os.getpid())
    abandoned = cli._validate_trace_result({
        "schema_version": 1,
        "request_id": uuid.uuid4().hex,
        "instance_id": expected.instance_id,
        "duration_ms": 10,
        "truncated": False,
        "events": [],
    })
    cli._atomic_create_trace_json(paths.trace_result_path, abandoned)
    abandoned_inode = paths.trace_result_path.stat().st_ino
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(expected.instance_id)
    monkeypatch.setattr(
        cli, "_load_config", lambda _paths: (SimpleNamespace(status_poll_ms=1000), [])
    )
    monkeypatch.setattr(
        cli, "_runtime_identity", lambda *_args, **_kwargs: (expected, snapshot_for(expected))
    )

    def drive(_seconds):
        mailbox.poll()
        if mailbox.active is not None:
            mailbox.observe(trace_event(received_at=time.monotonic()))

    monkeypatch.setattr(cli.time, "sleep", drive)
    output = io.StringIO()
    assert cli._cmd_trace_input(
        paths, 0.1, 1, stdout=output, stderr=io.StringIO()
    ) == 0
    assert json.loads(output.getvalue())["events"]
    assert not paths.trace_result_path.exists()
    assert all(
        candidate.stat().st_ino != abandoned_inode
        for candidate in paths.runtime_dir.iterdir()
    )


def test_trace_instance_change_fails_bounded_and_cleans_own_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    first = identity(os.getpid())
    second = identity(os.getpid())
    identities = iter([first, second])
    monkeypatch.setattr(
        cli, "_load_config", lambda _paths: (SimpleNamespace(status_poll_ms=1000), [])
    )
    monkeypatch.setattr(
        cli,
        "_runtime_identity",
        lambda *_args, **_kwargs: (
            (item := next(identities)), snapshot_for(item)
        ),
    )
    stderr = io.StringIO()
    assert cli._cmd_trace_input(
        paths, 0.1, 1, stdout=io.StringIO(), stderr=stderr
    ) == 1
    assert "complete safely" in stderr.getvalue()
    assert first.instance_id not in stderr.getvalue()
    assert second.instance_id not in stderr.getvalue()
    assert not paths.trace_request_path.exists()


def test_trace_daemon_crash_cleans_claimed_active_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    expected = identity(os.getpid())
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(expected.instance_id)
    calls = 0
    monkeypatch.setattr(
        cli, "_load_config", lambda _paths: (SimpleNamespace(status_poll_ms=1000), [])
    )

    def read_identity(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return expected, snapshot_for(expected)
        mailbox.poll()
        raise cli.RuntimeDataError("daemon crashed")

    monkeypatch.setattr(cli, "_runtime_identity", read_identity)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    assert cli._cmd_trace_input(
        paths, 0.1, 2, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert mailbox.active is not None  # models memory lost with the process
    assert not paths.trace_request_path.exists()
    assert not paths.trace_active_path.exists()


def test_trace_result_publish_fault_clears_active_without_escaping(
    tmp_path: Path,
):
    paths = paths_for(tmp_path)
    instance_id = uuid.uuid4().hex
    request = trace_request(instance_id, max_events=1)
    cli._atomic_create_trace_json(paths.trace_request_path, request)
    mailbox = cli._TraceMailbox(paths)
    mailbox.bind_instance(instance_id)
    mailbox.poll(now=100.0)
    unsafe_target = tmp_path / "unsafe-result"
    unsafe_target.write_text("do not replace")
    paths.trace_result_path.symlink_to(unsafe_target)
    mailbox.observe(trace_event(received_at=100.01))
    assert mailbox.active is None
    assert not paths.trace_active_path.exists()
    assert paths.trace_result_path.is_symlink()
    assert unsafe_target.read_text() == "do not replace"


@pytest.mark.parametrize(
    "arguments",
    [
        ["trace-input", "--seconds", "0"],
        ["trace-input", "--seconds", "nan"],
        ["trace-input", "--seconds", "10.1"],
        ["trace-input", "--max-events", "0"],
        ["trace-input", "--max-events", "65"],
    ],
)
def test_trace_cli_bounds_fail_before_runtime_paths(arguments, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.RuntimePaths,
        "from_env",
        classmethod(lambda _cls: (_ for _ in ()).throw(AssertionError("paths read"))),
    )
    assert cli.main(arguments) == 2
    assert capsys.readouterr().out == ""


def test_run_binds_trace_mailbox_to_published_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = paths_for(tmp_path)
    seen = []

    class Runtime(FakeRuntime):
        def bind_instance(self, item):
            seen.append(item)

    runtime = Runtime()
    monkeypatch.setattr(cli, "_runtime_factory", lambda _cfg, _paths: runtime)
    assert cli._cmd_run(
        paths, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert len(seen) == 1
    assert isinstance(seen[0], cli.InstanceIdentity)
    assert seen[0].pid == os.getpid()
