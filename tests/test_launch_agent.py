from __future__ import annotations

import io
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from paneglow import cli, launch_agent


def make_spec(tmp_path: Path, *, program: Path | None = None) -> launch_agent.Spec:
    account = tmp_path / "user"
    library = account / "Library"
    library.mkdir(parents=True, mode=0o700, exist_ok=True)
    library.chmod(0o700)
    runtime_home = account / ".paneglow"
    selected_program = Path(sys.executable) if program is None else program
    return launch_agent.build_spec(
        command_prefix=(str(selected_program), "-m", "paneglow.cli"),
        runtime_home=runtime_home,
        log_path=runtime_home / "logs" / "daemon.log",
        account_home=account,
        uid=os.getuid(),
    )


def prepare(spec: launch_agent.Spec) -> None:
    launch_agent.ensure_install_directories(spec)
    launch_agent.ensure_private_log(spec)


class FakeController:
    def __init__(self, *, loaded: bool = False, fail: str | None = None) -> None:
        self.is_loaded = loaded
        self.fail = fail
        self.calls: list[str] = []

    def loaded(self) -> bool:
        self.calls.append("loaded")
        return self.is_loaded

    def bootstrap(self) -> None:
        self.calls.append("bootstrap")
        if self.fail == "bootstrap":
            raise launch_agent.LaunchAgentError("failed")
        self.is_loaded = True

    def bootout(self) -> None:
        self.calls.append("bootout")
        if self.fail == "bootout":
            raise launch_agent.LaunchAgentError("failed")
        self.is_loaded = False

    def kickstart(self) -> None:
        self.calls.append("kickstart")
        if self.fail == "kickstart":
            raise launch_agent.LaunchAgentError("failed")


def test_manifest_is_exact_and_keeps_lexical_venv_interpreter(tmp_path: Path):
    target = Path(sys.executable)
    program = tmp_path / "venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.symlink_to(target)
    spec = make_spec(tmp_path, program=program)

    assert spec.plist_path == (
        tmp_path / "user" / "Library" / "LaunchAgents" /
        "io.github.jeongjaesoon.paneglow.plist"
    )
    assert spec.manifest == {
        "Label": launch_agent.LABEL,
        "Program": str(program),
        "ProgramArguments": [str(program), "-m", "paneglow.cli", "run"],
        "WorkingDirectory": "/",
        "EnvironmentVariables": {"HOME": str(tmp_path / "user")},
        "KeepAlive": {"SuccessfulExit": False},
        "Umask": "077",
        "ExitTimeOut": 5,
        "StandardOutPath": str(tmp_path / "user/.paneglow/logs/daemon.log"),
        "StandardErrorPath": str(tmp_path / "user/.paneglow/logs/daemon.log"),
    }
    assert plistlib.loads(spec.payload) == spec.manifest
    assert Path(spec.command[0]) == program
    assert Path(spec.command[0]).resolve() == target.resolve()
    launch_agent.validate_program(spec)


def test_manifest_rejects_non_paneglow_command_and_environment(tmp_path: Path):
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.build_spec(
            command_prefix=(str(Path(sys.executable)), "script.py"),
            runtime_home=tmp_path,
            log_path=tmp_path / "log",
            account_home=tmp_path,
            uid=os.getuid(),
        )
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.build_spec(
            command_prefix=(str(Path(sys.executable)), "-m", "paneglow.cli"),
            runtime_home=tmp_path,
            log_path=tmp_path / "log",
            runtime_environment={"PYTHONPATH": tmp_path},
            account_home=tmp_path,
            uid=os.getuid(),
        )


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "not_executable", "shared_writable"]
)
def test_program_validation_requires_executable_regular_target(
    tmp_path: Path, kind: str
):
    program = tmp_path / "python"
    if kind == "directory":
        program.mkdir()
    elif kind == "not_executable":
        program.write_text("python")
        program.chmod(0o600)
    elif kind == "shared_writable":
        program.write_text("python")
        program.chmod(0o777)
    spec = make_spec(tmp_path, program=program)
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.validate_program(spec)


def test_atomic_manifest_is_private_current_and_byte_idempotent(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    first = spec.plist_path.read_bytes()
    launch_agent.atomic_write_manifest(spec)

    metadata = spec.plist_path.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.getuid()
    assert metadata.st_nlink == 1
    assert spec.plist_path.read_bytes() == first == spec.payload
    assert launch_agent.inspect_manifest(spec).status == "current"


def test_old_interpreter_manifest_is_recognized_for_migration(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    old = dict(spec.manifest)
    old_program = "/old/location/.venv/bin/python"
    old["Program"] = old_program
    old["ProgramArguments"] = [old_program, "-m", "paneglow.cli", "run"]
    payload = plistlib.dumps(old, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)

    inspection = launch_agent.inspect_manifest(spec)
    assert inspection.status == "recognized"
    assert inspection.owned


def test_unknown_manifest_is_not_owned_or_replaced(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    unknown = dict(spec.manifest)
    unknown["ProgramArguments"] = ["/bin/sh", "-c", "payload"]
    payload = plistlib.dumps(unknown, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)

    inspection = launch_agent.inspect_manifest(spec)
    assert inspection.status == "unknown"
    assert not inspection.owned
    assert spec.plist_path.read_bytes() == payload


def test_manifest_recognition_rejects_boolean_type_confusion(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    confused = dict(spec.manifest)
    confused["KeepAlive"] = {"SuccessfulExit": 0}
    payload = plistlib.dumps(confused, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)

    assert launch_agent.inspect_manifest(spec).status == "unknown"


@pytest.mark.parametrize("kind", ["symlink", "mode", "hardlink", "malformed"])
def test_unsafe_manifest_is_never_recognized(tmp_path: Path, kind: str):
    spec = make_spec(tmp_path)
    prepare(spec)
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(spec.payload)
        spec.plist_path.symlink_to(target)
    else:
        spec.plist_path.write_bytes(
            b"not plist" if kind == "malformed" else spec.payload
        )
        spec.plist_path.chmod(0o600)
        if kind == "mode":
            spec.plist_path.chmod(0o644)
        elif kind == "hardlink":
            os.link(spec.plist_path, tmp_path / "second-link")
    assert launch_agent.inspect_manifest(spec).status == "unsafe"


def test_remove_manifest_is_bound_to_inspected_inode(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    inspection = launch_agent.inspect_manifest(spec)
    launch_agent.atomic_write_manifest(spec)

    with pytest.raises(launch_agent.LaunchAgentError, match="changed"):
        launch_agent.remove_manifest(spec, inspection)
    assert spec.plist_path.exists()


def test_remove_owned_manifest_and_missing_inspection(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    launch_agent.remove_manifest(spec, launch_agent.inspect_manifest(spec))
    assert launch_agent.inspect_manifest(spec).status == "missing"


def test_private_log_and_directories_reject_links(tmp_path: Path):
    spec = make_spec(tmp_path)
    launch_agent.ensure_install_directories(spec)
    target = tmp_path / "target-log"
    target.write_text("safe")
    spec.log_path.symlink_to(target)
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.ensure_private_log(spec)
    assert target.read_text() == "safe"


def test_install_lock_rejects_hardlink(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    spec.lock_path.touch(mode=0o600)
    os.link(spec.lock_path, tmp_path / "lock-link")
    with pytest.raises(launch_agent.LaunchAgentError, match="lock"):
        with launch_agent.InstallLock(spec):
            pass


def test_install_lock_is_account_global_across_runtime_homes(tmp_path: Path):
    account = tmp_path / "user"
    (account / "Library").mkdir(parents=True, mode=0o700)
    first_home = account / ".paneglow"
    second_home = account / ".paneglow-alt"
    common = {
        "command_prefix": (str(Path(sys.executable)), "-m", "paneglow.cli"),
        "account_home": account,
        "uid": os.getuid(),
    }
    first = launch_agent.build_spec(
        runtime_home=first_home,
        log_path=first_home / "logs" / "daemon.log",
        **common,
    )
    second = launch_agent.build_spec(
        runtime_home=second_home,
        log_path=second_home / "logs" / "daemon.log",
        runtime_environment={"PANEGLOW_HOME": second_home},
        **common,
    )
    launch_agent.ensure_lock_directory(first)

    assert first.plist_path == second.plist_path
    assert first.lock_path == second.lock_path
    with launch_agent.InstallLock(first):
        with pytest.raises(BlockingIOError):
            with launch_agent.InstallLock(second):
                pass


def test_install_hardens_legacy_runtime_home_mode(tmp_path: Path):
    spec = make_spec(tmp_path)
    runtime_home = spec.account_home / ".paneglow"
    runtime_home.mkdir(mode=0o755)
    runtime_home.chmod(0o755)

    launch_agent.ensure_install_directories(spec)

    assert stat.S_IMODE(runtime_home.stat().st_mode) == 0o700


def test_recognized_manifest_cannot_move_runtime_outside_account(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    outside = tmp_path / "outside"
    manifest = dict(spec.manifest)
    manifest["EnvironmentVariables"] = {
        "HOME": str(spec.account_home),
        "PANEGLOW_HOME": str(outside),
    }
    manifest["StandardOutPath"] = str(outside / "logs" / "daemon.log")
    manifest["StandardErrorPath"] = str(outside / "logs" / "daemon.log")
    launch_agent.atomic_write_manifest(
        spec, plistlib.dumps(manifest, fmt=plistlib.FMT_XML, sort_keys=True)
    )

    assert launch_agent.inspect_manifest(spec).status == "unknown"


@pytest.mark.parametrize("kind", ["library_symlink", "agents_symlink", "shared"])
def test_manifest_ancestor_chain_is_validated_without_following_links(
    tmp_path: Path, kind: str
):
    account = tmp_path / "user"
    account.mkdir(mode=0o700)
    runtime_home = account / ".paneglow"
    spec = launch_agent.build_spec(
        command_prefix=(str(Path(sys.executable)), "-m", "paneglow.cli"),
        runtime_home=runtime_home,
        log_path=runtime_home / "logs" / "daemon.log",
        account_home=account,
        uid=os.getuid(),
    )
    if kind == "library_symlink":
        real_library = tmp_path / "real-library"
        agents = real_library / "LaunchAgents"
        agents.mkdir(parents=True, mode=0o700)
        (account / "Library").symlink_to(real_library, target_is_directory=True)
    else:
        library = account / "Library"
        library.mkdir(mode=0o700)
        if kind == "agents_symlink":
            agents = tmp_path / "real-agents"
            agents.mkdir(mode=0o700)
            (library / "LaunchAgents").symlink_to(
                agents, target_is_directory=True
            )
        else:
            library.chmod(0o777)
            agents = library / "LaunchAgents"
            agents.mkdir(mode=0o700)
    manifest_path = agents / launch_agent.PLIST_NAME
    manifest_path.write_bytes(spec.payload)
    manifest_path.chmod(0o600)

    assert launch_agent.inspect_manifest(spec).status == "unsafe"
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.ensure_lock_directory(spec)


def test_controller_uses_exact_launchctl_argv_and_discards_output(tmp_path: Path):
    spec = make_spec(tmp_path)
    calls = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)

    controller = launch_agent.Controller(spec, runner=runner)
    assert controller.loaded() is True
    controller.bootstrap()
    controller.bootout()
    controller.kickstart()

    assert [call[0] for call in calls] == [
        ["/bin/launchctl", "print", spec.domain],
        ["/bin/launchctl", "print", spec.target],
        ["/bin/launchctl", "bootstrap", spec.domain, str(spec.plist_path)],
        ["/bin/launchctl", "bootout", spec.target],
        ["/bin/launchctl", "kickstart", spec.target],
    ]
    assert all(kwargs["stdin"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all(kwargs["stdout"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all(kwargs["stderr"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all("shell" not in kwargs for _, kwargs in calls)


def test_install_current_loaded_ready_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    expected = cli.InstanceIdentity(44, "0" * 32, 1.0)
    monkeypatch.setattr(cli, "_service_ready", lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(
        cli, "_cmd_stop", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)
    )

    output = io.StringIO()
    assert cli._cmd_autostart_install(
        cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")}),
        0,
        spec=spec,
        controller=controller,
        stdout=output,
        stderr=io.StringIO(),
    ) == 0
    assert controller.calls == ["loaded"]
    assert "already installed" in output.getvalue()


def test_install_stops_manual_daemon_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    controller = FakeController(loaded=False)
    actions: list[str] = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        cli, "_cmd_stop_runtime",
        lambda *_args, **_kwargs: actions.append("stop") or 0,
    )
    expected = cli.InstanceIdentity(45, "1" * 32, 1.0)
    monkeypatch.setattr(cli, "_service_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_wait_service_ready", lambda *_args, **_kwargs: expected)

    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert actions == ["stop"]
    assert controller.calls == ["loaded", "bootstrap", "kickstart"]
    assert launch_agent.inspect_manifest(spec).status == "current"


def test_install_failure_removes_new_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    controller = FakeController(loaded=False, fail="bootstrap")
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_service_ready", lambda *_args, **_kwargs: None)
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})

    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert launch_agent.inspect_manifest(spec).status == "missing"


def test_install_failure_restores_preexisting_manual_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    controller = FakeController(loaded=False, fail="bootstrap")
    lock_states = iter([True, False])
    actions: list[str] = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: next(lock_states, False))
    monkeypatch.setattr(
        cli, "_cmd_stop_runtime",
        lambda *_args, **_kwargs: actions.append("stop-manual") or 0,
    )
    monkeypatch.setattr(
        cli, "_cmd_start_locked",
        lambda *_args, **kwargs: (
            actions.append(f"start-manual:{kwargs.get('manual_only')}") or 0
        ),
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    error = io.StringIO()

    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=error
    ) == 1
    assert actions == ["stop-manual", "start-manual:True"]
    assert launch_agent.inspect_manifest(spec).status == "missing"
    assert "previous state was restored" in error.getvalue()


def test_install_surfaces_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    controller = FakeController(loaded=False, fail="bootstrap")
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(cli, "_cmd_stop_runtime", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "_cmd_start_locked", lambda *_args, **_kwargs: 1)
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    error = io.StringIO()

    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=error
    ) == 1
    assert "could not be fully restored" in error.getvalue()


def test_install_refuses_loaded_service_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    controller = FakeController(loaded=True)
    monkeypatch.setattr(
        controller, "bootout", lambda: (_ for _ in ()).throw(AssertionError)
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert launch_agent.inspect_manifest(spec).status == "missing"


def test_install_migrates_recognized_old_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    old = dict(spec.manifest)
    old["Program"] = "/missing/old/python"
    old["ProgramArguments"] = [
        "/missing/old/python", "-m", "paneglow.cli", "run"
    ]
    launch_agent.atomic_write_manifest(
        spec, plistlib.dumps(old, fmt=plistlib.FMT_XML, sort_keys=True)
    )
    controller = FakeController(loaded=False)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_service_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli, "_wait_service_ready",
        lambda *_args, **_kwargs: cli.InstanceIdentity(46, "2" * 32, 1.0),
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})

    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert launch_agent.inspect_manifest(spec).status == "current"


def test_unknown_manifest_is_untouched_by_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    unknown = dict(spec.manifest)
    unknown["Label"] = "example.other"
    payload = plistlib.dumps(unknown, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)
    controller = FakeController(loaded=False)
    monkeypatch.setattr(
        controller, "loaded", lambda: (_ for _ in ()).throw(AssertionError)
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})

    assert cli._cmd_autostart_install(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert spec.plist_path.read_bytes() == payload


def test_status_and_idempotent_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    output = io.StringIO()
    assert cli._cmd_autostart_status(
        paths, spec=spec, controller=controller,
        stdout=output, stderr=io.StringIO()
    ) == 0
    assert "current | loaded" in output.getvalue()

    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    assert cli._cmd_autostart_uninstall(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert not spec.plist_path.exists()
    assert "bootout" in controller.calls
    assert cli._cmd_autostart_uninstall(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0


def test_uninstall_rechecks_loaded_under_lock_and_restores_on_remove_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    monkeypatch.setattr(cli, "_wait_runtime_stopped", lambda *_args: True)
    monkeypatch.setattr(
        launch_agent, "remove_manifest",
        lambda *_args: (_ for _ in ()).throw(
            launch_agent.LaunchAgentError("remove failed")
        ),
    )
    monkeypatch.setattr(
        cli, "_wait_service_ready",
        lambda *_args, **_kwargs: cli.InstanceIdentity(48, "4" * 32, 1.0),
    )
    error = io.StringIO()

    assert cli._cmd_autostart_uninstall(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=error
    ) == 1
    assert controller.calls == [
        "loaded", "bootout", "loaded", "bootstrap", "kickstart"
    ]
    assert controller.is_loaded
    assert launch_agent.inspect_manifest(spec).owned
    assert "previous lifecycle was restored" in error.getvalue()


def test_uninstall_bootout_error_rolls_back_possible_partial_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)

    def partial_bootout() -> None:
        controller.calls.append("bootout")
        controller.is_loaded = False
        raise launch_agent.LaunchAgentError("launchctl result was uncertain")

    monkeypatch.setattr(controller, "bootout", partial_bootout)
    monkeypatch.setattr(
        cli,
        "_wait_service_ready",
        lambda *_args, **_kwargs: cli.InstanceIdentity(49, "5" * 32, 1.0),
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    error = io.StringIO()

    assert cli._cmd_autostart_uninstall(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=error
    ) == 1
    assert controller.calls == [
        "loaded", "bootout", "loaded", "bootstrap", "kickstart"
    ]
    assert controller.is_loaded
    assert launch_agent.inspect_manifest(spec).status == "current"
    assert "previous lifecycle was restored" in error.getvalue()


def test_uninstall_stops_unloaded_manual_daemon_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=False)
    actions: list[str] = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        cli,
        "_cmd_stop_runtime",
        lambda *_args, **_kwargs: actions.append("stop-manual") or 0,
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})

    assert cli._cmd_autostart_uninstall(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert actions == ["stop-manual"]
    assert launch_agent.inspect_manifest(spec).status == "missing"


def test_uninstall_manual_stop_timeout_restores_previous_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=False)
    actions: list[str] = []
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        cli,
        "_cmd_stop_runtime",
        lambda *_args, **_kwargs: actions.append("stop-timeout") or 1,
    )
    monkeypatch.setattr(
        cli,
        "_cmd_start_locked",
        lambda *_args, **kwargs: (
            actions.append(f"restore:{kwargs.get('manual_only')}") or 0
        ),
    )
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    error = io.StringIO()

    assert cli._cmd_autostart_uninstall(
        paths, 0, spec=spec, controller=controller,
        stdout=io.StringIO(), stderr=error
    ) == 1
    assert actions == ["stop-timeout", "restore:True"]
    assert launch_agent.inspect_manifest(spec).status == "current"
    assert "previous lifecycle was restored" in error.getvalue()


def test_loaded_launch_agent_stop_boots_out_without_direct_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    monkeypatch.setattr(launch_agent, "Controller", lambda _spec: controller)
    monkeypatch.setattr(cli, "_wait_runtime_stopped", lambda *_args: True)
    monkeypatch.setattr(
        cli, "_cmd_stop_runtime", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct SIGTERM path used")
        )
    )

    assert cli._cmd_stop(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert controller.calls == ["loaded", "bootout"]


def test_start_routes_through_current_loaded_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    expected = cli.InstanceIdentity(47, "3" * 32, 1.0)
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    monkeypatch.setattr(launch_agent, "Controller", lambda _spec: controller)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_wait_service_ready", lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(
        cli, "_spawn_detached", lambda *_args: (_ for _ in ()).throw(AssertionError)
    )

    assert cli._cmd_start(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    assert controller.calls == ["loaded", "kickstart"]


def test_start_timeout_boots_out_newly_bootstrapped_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=False)
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    monkeypatch.setattr(launch_agent, "Controller", lambda _spec: controller)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_wait_service_ready", lambda *_args, **_kwargs: None)

    assert cli._cmd_start(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert controller.calls == ["loaded", "bootstrap", "kickstart", "bootout"]
    assert not controller.is_loaded


def test_start_timeout_boots_out_already_loaded_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    monkeypatch.setattr(launch_agent, "Controller", lambda _spec: controller)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(cli, "_wait_service_ready", lambda *_args, **_kwargs: None)

    assert cli._cmd_start(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert controller.calls == ["loaded", "kickstart", "bootout"]
    assert not controller.is_loaded


def test_start_bootstrap_then_kickstart_failure_boots_out_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=False, fail="kickstart")
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    monkeypatch.setattr(launch_agent, "Controller", lambda _spec: controller)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)

    assert cli._cmd_start(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert controller.calls == [
        "loaded", "bootstrap", "kickstart", "loaded", "bootout"
    ]
    assert not controller.is_loaded


def test_missing_manifest_loaded_service_never_falls_back_to_detached_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    launch_agent.ensure_lock_directory(spec)
    controller = FakeController(loaded=True)
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    monkeypatch.setattr(launch_agent, "Controller", lambda _spec: controller)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)
    monkeypatch.setattr(
        cli, "_spawn_detached", lambda *_args: (_ for _ in ()).throw(
            AssertionError("detached fallback used")
        )
    )

    assert cli._cmd_start(
        paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
    ) == 1
    assert controller.calls == ["loaded"]


@pytest.mark.parametrize("command", ["start", "install", "uninstall"])
def test_mutating_commands_are_serialized_by_install_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    paths = cli.RuntimePaths.from_env({"HOME": str(tmp_path / "user")})
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    controller = FakeController(loaded=True)
    monkeypatch.setattr(cli, "_launch_agent_spec", lambda _paths: spec)
    held = launch_agent.InstallLock(spec)
    held.__enter__()
    try:
        if command == "start":
            result = cli._cmd_start(
                paths, 0, stdout=io.StringIO(), stderr=io.StringIO()
            )
        elif command == "install":
            result = cli._cmd_autostart_install(
                paths, 0, spec=spec, controller=controller,
                stdout=io.StringIO(), stderr=io.StringIO()
            )
        else:
            result = cli._cmd_autostart_uninstall(
                paths, 0, spec=spec, controller=controller,
                stdout=io.StringIO(), stderr=io.StringIO()
            )
    finally:
        held.__exit__()
    assert result == 1


def test_spoofed_home_cannot_make_self_inconsistent_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    account = tmp_path / "account"
    (account / "Library").mkdir(parents=True, mode=0o700)
    spoofed = tmp_path / "spoofed"
    paths = cli.RuntimePaths.from_env({"HOME": str(spoofed)})
    monkeypatch.setattr(launch_agent, "current_account_home", lambda: account)

    with pytest.raises(launch_agent.LaunchAgentError, match="inside"):
        cli._launch_agent_spec(paths, {"HOME": str(spoofed)})


@pytest.mark.parametrize("kind", ["symlink", "shared"])
def test_runtime_home_unsafe_ancestor_is_rejected(tmp_path: Path, kind: str):
    account = tmp_path / "user"
    (account / "Library").mkdir(parents=True, mode=0o700)
    spec = make_spec(tmp_path)
    runtime_home = account / ".paneglow"
    if kind == "symlink":
        target = account / "target"
        target.mkdir(mode=0o700)
        runtime_home.symlink_to(target, target_is_directory=True)
    else:
        runtime_home.mkdir(mode=0o777)
        runtime_home.chmod(0o777)
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.ensure_install_directories(spec)


def test_autostart_parser_routes_commands(monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(
        cli, "_cmd_autostart_install",
        lambda _paths, timeout: calls.append(("install", timeout)) or 0,
    )
    monkeypatch.setattr(
        cli, "_cmd_autostart_status",
        lambda _paths: calls.append(("status", None)) or 0,
    )
    monkeypatch.setattr(
        cli, "_cmd_autostart_uninstall",
        lambda _paths, timeout: calls.append(("uninstall", timeout)) or 0,
    )
    assert cli.main(["autostart", "install"]) == 0
    assert cli.main(["autostart", "status"]) == 0
    assert cli.main(["autostart", "uninstall"]) == 0
    assert calls == [("install", 10.0), ("status", None), ("uninstall", 5.0)]
    assert cli.main(["autostart", "install", "--timeout", "nan"]) == 2


def test_lifecycle_help_is_service_aware():
    parser = cli._build_parser()
    help_text = parser.format_help()

    assert "start the daemon using its installed lifecycle owner" in help_text
    assert "stop the verified daemon without force killing" in help_text
    assert "start a detached daemon" not in help_text
