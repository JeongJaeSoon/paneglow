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
    return cli.InstanceIdentity(pid=pid, instance_id=uuid.uuid4().hex)


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
            paths.lock_path,
            paths.pid_path,
            paths.snapshot_path,
            paths.log_path,
            paths.claude_settings_path,
            paths.mapping_dir,
        )
    )
    assert paths.state_dir == paths.home / "state"
    assert paths.snapshot_path == paths.home / "runtime" / "snapshot.json"


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


def test_pid_file_is_atomic_private_and_contains_instance_identity(tmp_path: Path):
    path = tmp_path / "runtime" / "pid.json"
    expected = identity()
    cli._write_pid(path, expected)
    assert cli._read_pid(path) == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pid_reader_rejects_bool_pid_and_unknown_fields(tmp_path: Path):
    path = tmp_path / "pid.json"
    cli._atomic_write_json(path, {"pid": True, "instance_id": uuid.uuid4().hex})
    with pytest.raises(cli.RuntimeDataError):
        cli._read_pid(path)
    cli._atomic_write_json(
        path, {"pid": 12, "instance_id": uuid.uuid4().hex, "extra": "x"}
    )
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


def prepare_doctor_files(paths: cli.RuntimePaths) -> None:
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

        def close(self, flush_seconds=1.0):
            self.closed.append(flush_seconds)

    device = Device()
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(pad.Pad, "open", lambda: device)
    monkeypatch.setattr(
        cli, "_load_config",
        lambda _paths: (SimpleNamespace(status_poll_ms=1000), ["bad\nvalue"]),
    )
    output = io.StringIO()
    assert cli._cmd_doctor(paths, stdout=output, stderr=io.StringIO()) == 0
    assert device.closed == [1.0]
    assert "fresh pad round-trip" in output.getvalue()
    assert "bad\\x0avalue" in output.getvalue()


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

        def close(self, flush_seconds=1.0):
            self.closed += 1

    device = Device()
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(pad.Pad, "open", lambda: device)
    assert cli._cmd_doctor(paths, stdout=io.StringIO(), stderr=io.StringIO()) == 1
    assert device.closed == 1


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
