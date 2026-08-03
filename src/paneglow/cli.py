"""Command-line lifecycle and diagnostics for Paneglow.

The ``hook`` path is deliberately selected before argparse and before importing
any Paneglow subsystem.  Claude invokes it on the critical path of every turn:
it must stay silent and return success even when imports, input, or storage fail.

The long-running daemon owns an advisory lock for its entire lifetime.  PID and
runtime snapshot files are atomic, private, schema-checked hints; the lock is
the authority for whether an instance is alive.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TextIO


_SCHEMA_VERSION = 1
_MAX_RUNTIME_BYTES = 1 << 20
_PID_MAX = (1 << 31) - 1
_SLOT_COUNT = 6
_HOOK_EVENTS = (
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
_OWNERS = frozenset({"claude", "codex", "none"})
_STATES = frozenset({"idle", "working", "done", "error", "waiting"})
_SLOT_REASONS = frozenset(
    {"empty", "no_hook", "state", "working_timeout", "done_faded"}
)
_INPUT_RESULTS = frozenset(
    {
        "opened",
        "open_failed",
        "empty_slot",
        "ignored_owner",
        "ignored_layer",
        "ignored_input",
    }
)
_PAD_ERRORS = frozenset(
    {
        "unavailable",
        "status_unverified",
        "disconnected",
        "poll_failed",
        "send_failed",
        "reconnect_failed",
        "close_failed",
    }
)
_TRANSPORTS = frozenset({"USB", "BLE"})
_EFFECTS = frozenset({"off", "solid", "spin", "rainbow", "blink", "pulse"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CONFIG_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}\Z")
_TRACE_KEYS = frozenset({"A1", "A2", "A3", "A4", "A5", "A6", "other"})
_TRACE_REQUEST_TTL_SECONDS = 30.0
_TRACE_RESULT_TTL_SECONDS = 30.0
_TRACE_POLL_SECONDS = 0.02
_TRACE_GRACE_SECONDS = 3.0


class RuntimeDataError(ValueError):
    """A private runtime file was missing, unsafe, stale, or malformed."""


class AlreadyRunning(RuntimeError):
    """Another process owns the daemon lifetime lock."""


@dataclass(frozen=True)
class RuntimePaths:
    """All mutable and user-owned paths used by the CLI."""

    home: Path
    state_dir: Path
    config_path: Path
    runtime_dir: Path
    transition_lock_path: Path
    lock_path: Path
    pid_path: Path
    snapshot_path: Path
    trace_lock_path: Path
    trace_request_path: Path
    trace_active_path: Path
    trace_result_path: Path
    log_path: Path
    claude_settings_path: Path
    claude_sessions_dir: Path
    mapping_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimePaths":
        source = os.environ if env is None else env
        user_home = _absolute_path(source.get("HOME", str(Path.home())))
        home = _absolute_path(
            source.get("PANEGLOW_HOME", str(user_home / ".paneglow"))
        )
        runtime = home / "runtime"
        claude_settings = _absolute_path(
            source.get(
                "PANEGLOW_CLAUDE_SETTINGS",
                str(user_home / ".claude" / "settings.json"),
            )
        )
        claude_sessions = _absolute_path(
            source.get(
                "PANEGLOW_CLAUDE_SESSIONS",
                str(user_home / ".claude" / "sessions"),
            )
        )
        mapping = _absolute_path(
            source.get(
                "PANEGLOW_MAPPING_DIR",
                str(
                    user_home
                    / "Library"
                    / "Application Support"
                    / "Claude"
                    / "claude-code-sessions"
                ),
            )
        )
        return cls(
            home=home,
            state_dir=home / "state",
            config_path=home / "config.json",
            runtime_dir=runtime,
            transition_lock_path=runtime / "identity-transition.lock",
            lock_path=runtime / "daemon.lock",
            pid_path=runtime / "daemon.pid.json",
            snapshot_path=runtime / "snapshot.json",
            trace_lock_path=runtime / "trace.lock",
            trace_request_path=runtime / "trace.request.json",
            trace_active_path=runtime / "trace.active.json",
            trace_result_path=runtime / "trace.result.json",
            log_path=home / "logs" / "daemon.log",
            claude_settings_path=claude_settings,
            claude_sessions_dir=claude_sessions,
            mapping_dir=mapping,
        )


@dataclass(frozen=True)
class InstanceIdentity:
    pid: int
    instance_id: str
    started_at: float


class DaemonRuntime(Protocol):
    """Narrow seam between process lifecycle and the deterministic daemon."""

    def run(
        self,
        stop_event: threading.Event,
        publish: Callable[[Mapping[str, Any]], None],
    ) -> None: ...

    def bind_instance(self, identity: InstanceIdentity) -> None: ...

    def close(self, flush_seconds: float = ...) -> None: ...


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    # Make paths absolute without following symlinks.  The original path shape
    # is evidence used by settings/session/mapping trust boundaries below.
    return Path(value).expanduser().absolute()


def _safe_text(value: object, limit: int = 240) -> str:
    """Make local diagnostic text terminal-safe and bounded."""
    text = str(value)
    rendered = "".join(
        character if character.isprintable() and character not in "\r\n"
        else f"\\x{ord(character):02x}"
        for character in text
    )
    return rendered[:limit]


def _config_warning_category(value: object) -> str:
    """Reduce a config warning to a label without echoing the rejected value."""
    warning = str(value)
    if warning.startswith("config unreadable"):
        return "config_unreadable"
    if warning.startswith("config must be an object"):
        return "config_shape"
    label = warning.partition(":")[0]
    return label if _CONFIG_LABEL.fullmatch(label) is not None else "invalid_value"


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # A later private-file open still fails closed if the directory is not
        # usable.  chmod may be unavailable on unusual mounted filesystems.
        pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace *path* atomically with an owner-only regular file."""
    _private_directory(path.parent)
    if path.is_symlink():
        raise RuntimeDataError("refusing to replace a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RuntimeDataError("runtime data is not JSON-safe") from error
    _atomic_write_bytes(path, encoded)


def _read_private_json(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as error:
        raise RuntimeDataError("private runtime file is unavailable") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeDataError("private runtime path is not a regular file")
        if metadata.st_uid != os.getuid():
            raise RuntimeDataError("private runtime file has another owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeDataError("private runtime file mode is not 0600")
        if metadata.st_size < 0 or metadata.st_size > _MAX_RUNTIME_BYTES:
            raise RuntimeDataError("private runtime file is too large")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                return json.load(
                    stream,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_unique_json_object,
                )
            except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
                raise RuntimeDataError("private runtime JSON is malformed") from error
    finally:
        if fd >= 0:
            os.close(fd)


def _reject_json_constant(value: str) -> object:
    raise RuntimeDataError(f"non-finite JSON number: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeDataError("JSON object contains a duplicate field")
        result[key] = value
    return result


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise RuntimeDataError(f"{label} must be an object")
    if set(value) != keys:
        raise RuntimeDataError(f"{label} has missing or unknown fields")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise RuntimeDataError(f"{label} must be a boolean")
    return value


def _exact_int(value: object, label: str, minimum: int = 0,
               maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum \
            or (maximum is not None and value > maximum):
        raise RuntimeDataError(f"{label} must be a bounded integer")
    return value


def _finite_number(value: object, label: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeDataError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise RuntimeDataError(f"{label} must be finite")
    return number


def _nullable_number(value: object, label: str) -> float | None:
    return None if value is None else _finite_number(value, label)


def _safe_string(value: object, label: str, *, nullable: bool = False,
                 token: bool = False, limit: int = 512) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or len(value) > limit \
            or any(not character.isprintable() for character in value):
        raise RuntimeDataError(f"{label} must be a safe string")
    if token and _SAFE_TOKEN.fullmatch(value) is None:
        raise RuntimeDataError(f"{label} must be a safe token")
    return value


def _nullable_enum(value: object, allowed: frozenset[str], label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        raise RuntimeDataError(f"{label} has an unknown value")
    return value


def _validate_instance_id(value: object) -> str:
    if type(value) is not str:
        raise RuntimeDataError("instance_id must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise RuntimeDataError("instance_id must be a UUID") from error
    if value not in {parsed.hex, str(parsed)}:
        raise RuntimeDataError("instance_id is not canonical")
    return value


_TRACE_REQUEST_KEYS = {
    "schema_version", "request_id", "instance_id", "created_at",
    "duration_ms", "max_events",
}
_TRACE_RESULT_KEYS = {
    "schema_version", "request_id", "instance_id", "duration_ms",
    "truncated", "events",
}
_TRACE_PUBLIC_KEYS = {"schema_version", "duration_ms", "truncated", "events"}
_TRACE_EVENT_KEYS = {
    "sequence", "relative_ms", "key", "action", "owner", "layer_one",
    "slot_occupied", "result", "connection_ordinal",
}


def _validate_trace_request(value: object) -> dict[str, Any]:
    request = _exact_object(value, _TRACE_REQUEST_KEYS, "trace request")
    if _exact_int(request["schema_version"], "schema_version") != _SCHEMA_VERSION:
        raise RuntimeDataError("unsupported trace request schema")
    _validate_instance_id(request["request_id"])
    _validate_instance_id(request["instance_id"])
    _finite_number(request["created_at"], "created_at")
    _exact_int(request["duration_ms"], "duration_ms", 1, 10_000)
    _exact_int(request["max_events"], "max_events", 1, 64)
    return request


def _validate_trace_event(value: object, *, sequence: int,
                          duration_ms: int,
                          previous_relative_ms: int = 0) -> dict[str, Any]:
    event = _exact_object(value, _TRACE_EVENT_KEYS, "trace event")
    if _exact_int(event["sequence"], "sequence", 1, 64) != sequence:
        raise RuntimeDataError("trace event sequence is not contiguous")
    relative_ms = _exact_int(
        event["relative_ms"], "relative_ms", 0, duration_ms
    )
    if relative_ms < previous_relative_ms:
        raise RuntimeDataError("trace event timestamps are not monotonic")
    if type(event["key"]) is not str or event["key"] not in _TRACE_KEYS:
        raise RuntimeDataError("trace event key is unknown")
    action = event["action"]
    if not ((type(action) is int and action in {0, 1})
            or (type(action) is str and action == "other")):
        raise RuntimeDataError("trace event action is unknown")
    if type(event["owner"]) is not str or event["owner"] not in _OWNERS:
        raise RuntimeDataError("trace event owner is unknown")
    _exact_bool(event["layer_one"], "layer_one")
    _exact_bool(event["slot_occupied"], "slot_occupied")
    if type(event["result"]) is not str or event["result"] not in _INPUT_RESULTS:
        raise RuntimeDataError("trace event result is unknown")
    _exact_int(
        event["connection_ordinal"], "connection_ordinal", 1, 1_000_000
    )
    return event


def _validate_trace_public(value: object) -> dict[str, Any]:
    result = _exact_object(value, _TRACE_PUBLIC_KEYS, "trace result")
    if _exact_int(result["schema_version"], "schema_version") != _SCHEMA_VERSION:
        raise RuntimeDataError("unsupported trace result schema")
    duration_ms = _exact_int(result["duration_ms"], "duration_ms", 0, 10_000)
    _exact_bool(result["truncated"], "truncated")
    events = result["events"]
    if type(events) is not list or len(events) > 64:
        raise RuntimeDataError("trace events must be a bounded list")
    previous_relative_ms = 0
    for sequence, event in enumerate(events, start=1):
        checked = _validate_trace_event(
            event,
            sequence=sequence,
            duration_ms=duration_ms,
            previous_relative_ms=previous_relative_ms,
        )
        previous_relative_ms = int(checked["relative_ms"])
    return result


def _validate_trace_result(value: object) -> dict[str, Any]:
    result = _exact_object(value, _TRACE_RESULT_KEYS, "trace result envelope")
    _validate_instance_id(result["request_id"])
    _validate_instance_id(result["instance_id"])
    _validate_trace_public({
        "schema_version": result["schema_version"],
        "duration_ms": result["duration_ms"],
        "truncated": result["truncated"],
        "events": result["events"],
    })
    return result


def _trace_public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only the documented trace fields to the command's stdout value."""
    return _validate_trace_public({
        "schema_version": result["schema_version"],
        "duration_ms": result["duration_ms"],
        "truncated": result["truncated"],
        "events": result["events"],
    })


def _ensure_trace_parent(path: Path) -> None:
    """Create once, then require an owner-only, non-symlink runtime directory."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=0o700)
            metadata = path.lstat()
        except OSError as error:
            raise RuntimeDataError("trace runtime directory is unavailable") from error
    except OSError as error:
        raise RuntimeDataError("trace runtime directory cannot be inspected") from error
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
            or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeDataError("trace runtime directory is not owner-only")


def _trace_path_metadata(path: Path) -> os.stat_result | None:
    try:
        parent = path.parent.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeDataError("trace runtime directory cannot be inspected") from error
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() \
            or stat.S_IMODE(parent.st_mode) != 0o700:
        raise RuntimeDataError("trace runtime directory is not owner-only")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeDataError("trace runtime path cannot be inspected") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
            or stat.S_IMODE(metadata.st_mode) != 0o600 \
            or metadata.st_nlink != 1:
        raise RuntimeDataError("trace runtime path is not a private regular file")
    return metadata


@dataclass(frozen=True)
class _TraceFile:
    value: object
    device: int
    inode: int
    modified_at: float


def _read_private_trace_json(path: Path) -> _TraceFile:
    """Read one trace file and retain its inode for compare-before-unlink."""
    _ensure_trace_parent(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as error:
        raise RuntimeDataError("private trace file is unavailable") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or stat.S_IMODE(metadata.st_mode) != 0o600 \
                or metadata.st_nlink != 1:
            raise RuntimeDataError("private trace file is unsafe")
        if metadata.st_size < 0 or metadata.st_size > _MAX_RUNTIME_BYTES:
            raise RuntimeDataError("private trace file is too large")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                value = json.load(
                    stream,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_unique_json_object,
                )
            except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
                raise RuntimeDataError("private trace JSON is malformed") from error
        return _TraceFile(
            value=value,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            modified_at=metadata.st_mtime,
        )
    finally:
        if fd >= 0:
            os.close(fd)


def _atomic_create_trace_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish one nlink-1 trace file with a cooperative no-replace check.

    The trace directory is 0700 and the CLI/daemon serialize their writes. A
    hostile same-UID process racing the final check is explicitly out of scope.
    ``rename`` keeps both the pre-crash temporary and post-crash destination at
    nlink 1, unlike a link/unlink publication barrier.
    """
    _ensure_trace_parent(path.parent)
    if _trace_path_metadata(path) is not None:
        raise RuntimeDataError("trace mailbox is already occupied")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RuntimeDataError("trace data is not JSON-safe") from error
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_metadata = temporary.lstat()
        if not stat.S_ISREG(temporary_metadata.st_mode) \
                or temporary_metadata.st_uid != os.getuid() \
                or stat.S_IMODE(temporary_metadata.st_mode) != 0o600 \
                or temporary_metadata.st_nlink != 1:
            raise RuntimeDataError("trace temporary file is unsafe")
        # Re-check immediately before the atomic namespace transition. The
        # cooperative trace lock/active marker excludes another legitimate
        # writer, so an occupied destination must never be replaced.
        if _trace_path_metadata(path) is not None:
            raise RuntimeDataError("trace mailbox became occupied")
        os.rename(temporary, path)
        published = _trace_path_metadata(path)
        if published is None or (published.st_dev, published.st_ino) != (
            temporary_metadata.st_dev, temporary_metadata.st_ino
        ):
            raise RuntimeDataError("trace publication identity changed")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _trace_path_metadata(path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _cleanup_stale_trace_path(path: Path, *, ttl: float,
                              now: float | None = None) -> bool:
    """Remove only an expired owner-private regular trace artifact."""
    metadata = _trace_path_metadata(path)
    if metadata is None:
        return False
    current = time.time() if now is None else now
    age = current - metadata.st_mtime
    if age < -5.0 or age <= ttl:
        return False
    # Re-check the inode so a replacement is not unlinked after inspection.
    current_metadata = _trace_path_metadata(path)
    if current_metadata is None or (current_metadata.st_dev, current_metadata.st_ino) \
            != (metadata.st_dev, metadata.st_ino):
        return False
    path.unlink()
    return True


def _unlink_trace_inode(path: Path, trace_file: _TraceFile) -> bool:
    """Claim/remove the exact trace inode that was read, never its replacement."""
    current = _trace_path_metadata(path)
    if current is None or (current.st_dev, current.st_ino) != (
        trace_file.device, trace_file.inode
    ):
        return False
    # Runtime directory mode 0700 excludes cross-UID swaps. A hostile same-UID
    # peer remains outside the threat model after this final comparison.
    path.unlink(missing_ok=True)
    return True


def _claim_trace_inode(source: Path, destination: Path,
                       trace_file: _TraceFile) -> bool:
    """Atomically rename a request to its active marker at nlink 1.

    Before a crash the source exists; after a crash the destination exists.
    Cooperative serialization and the 0700 directory make the final absent
    destination check sufficient; hostile same-UID replacement is out of scope.
    """
    if _trace_path_metadata(destination) is not None:
        return False
    current = _trace_path_metadata(source)
    if current is None or (current.st_dev, current.st_ino) != (
        trace_file.device, trace_file.inode
    ):
        return False
    try:
        # Repeat the absent check immediately before rename so cooperative
        # callers never replace one another's active marker.
        if _trace_path_metadata(destination) is not None:
            return False
        os.rename(source, destination)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeDataError("trace request could not be claimed") from error
    claimed = _trace_path_metadata(destination)
    if claimed is None or (claimed.st_dev, claimed.st_ino) != (
        trace_file.device, trace_file.inode
    ):
        # Preserve whichever name survived for later recovery; never unlink an
        # inode whose post-rename identity could not be proven.
        raise RuntimeDataError("trace claim identity changed")
    return True


def _unlink_trace_if_owned(path: Path, *, request_id: str,
                           instance_id: str, result: bool = False) -> bool:
    """Unlink a fixed mailbox file only when its validated identity matches."""
    if _trace_path_metadata(path) is None:
        return False
    try:
        trace_file = _read_private_trace_json(path)
        value = (_validate_trace_result(trace_file.value) if result
                 else _validate_trace_request(trace_file.value))
    except RuntimeDataError:
        return False
    if value["request_id"] != request_id or value["instance_id"] != instance_id:
        return False
    return _unlink_trace_inode(path, trace_file)


_SNAPSHOT_KEYS = {
    "schema_version",
    "written_at",
    "instance_id",
    "pid",
    "running",
    "generation",
    "last_causes",
    "frontmost",
    "owner",
    "pad",
    "session_scan",
    "slots",
    "zones",
    "last_input_result",
}


def _validate_snapshot(value: object) -> dict[str, Any]:
    snapshot = _exact_object(value, _SNAPSHOT_KEYS, "snapshot")
    if _exact_int(snapshot["schema_version"], "schema_version") != _SCHEMA_VERSION:
        raise RuntimeDataError("unsupported snapshot schema")
    _finite_number(snapshot["written_at"], "written_at")
    _validate_instance_id(snapshot["instance_id"])
    _exact_int(snapshot["pid"], "pid", 1, _PID_MAX)
    _exact_bool(snapshot["running"], "running")
    _exact_int(snapshot["generation"], "generation")

    causes = snapshot["last_causes"]
    if type(causes) is not list or len(causes) > 64:
        raise RuntimeDataError("last_causes must be a bounded list")
    for cause in causes:
        _safe_string(cause, "last_causes item", token=True, limit=128)

    frontmost = _exact_object(
        snapshot["frontmost"], {"ok", "bundle_id"}, "frontmost"
    )
    _exact_bool(frontmost["ok"], "frontmost.ok")
    _safe_string(frontmost["bundle_id"], "frontmost.bundle_id", nullable=True,
                 token=True, limit=256)
    if type(snapshot["owner"]) is not str or snapshot["owner"] not in _OWNERS:
        raise RuntimeDataError("owner has an unknown value")

    pad = _exact_object(
        snapshot["pad"],
        {
            "connected",
            "transport",
            "epoch",
            "status_verified",
            "layer_index",
            "version",
            "last_status_at",
            "error_code",
        },
        "pad",
    )
    _exact_bool(pad["connected"], "pad.connected")
    _nullable_enum(pad["transport"], _TRANSPORTS, "pad.transport")
    _exact_int(pad["epoch"], "pad.epoch")
    _exact_bool(pad["status_verified"], "pad.status_verified")
    if pad["layer_index"] is not None:
        _exact_int(pad["layer_index"], "pad.layer_index", 1)
    _safe_string(pad["version"], "pad.version", nullable=True, limit=128)
    _nullable_number(pad["last_status_at"], "pad.last_status_at")
    _nullable_enum(pad["error_code"], _PAD_ERRORS, "pad.error_code")

    session_scan = _exact_object(
        snapshot["session_scan"],
        {"authoritative", "count", "diagnostics"},
        "session_scan",
    )
    _exact_bool(session_scan["authoritative"], "session_scan.authoritative")
    _exact_int(session_scan["count"], "session_scan.count")
    diagnostics = session_scan["diagnostics"]
    if type(diagnostics) is not list or len(diagnostics) > 128:
        raise RuntimeDataError("session_scan.diagnostics must be a bounded list")
    for diagnostic in diagnostics:
        _safe_string(diagnostic, "session diagnostic", limit=512)

    items = snapshot["slots"]
    if type(items) is not list or len(items) != _SLOT_COUNT:
        raise RuntimeDataError("slots must contain exactly six items")
    for index, item in enumerate(items):
        slot = _exact_object(
            item, {"session_id", "effective_state", "reason"}, f"slots[{index}]"
        )
        _safe_string(slot["session_id"], "slot.session_id", nullable=True,
                     token=True, limit=256)
        _nullable_enum(slot["effective_state"], _STATES, "slot.effective_state")
        if type(slot["reason"]) is not str or slot["reason"] not in _SLOT_REASONS:
            raise RuntimeDataError("slot.reason has an unknown value")

    zones = _exact_object(
        snapshot["zones"], {"keys_owned", "ambient", "pending_reclaim"}, "zones"
    )
    _exact_bool(zones["keys_owned"], "zones.keys_owned")
    ambient = _exact_object(
        zones["ambient"], {"color", "effect", "reason"}, "zones.ambient"
    )
    if ambient["color"] is not None:
        _exact_int(ambient["color"], "zones.ambient.color", 0, 0xFFFFFF)
    _nullable_enum(ambient["effect"], _EFFECTS, "zones.ambient.effect")
    _safe_string(ambient["reason"], "zones.ambient.reason", token=True, limit=128)
    pending = _exact_object(
        zones["pending_reclaim"], {"keys", "ambient"}, "zones.pending_reclaim"
    )
    _nullable_number(pending["keys"], "zones.pending_reclaim.keys")
    _nullable_number(pending["ambient"], "zones.pending_reclaim.ambient")
    _nullable_enum(snapshot["last_input_result"], _INPUT_RESULTS,
                   "last_input_result")
    return snapshot


def _snapshot_max_age(status_poll_ms: int) -> float:
    return max(3.0, 3.0 * status_poll_ms / 1000.0)


def _read_snapshot(path: Path, *, status_poll_ms: int, now: float | None = None,
                   allow_stale: bool = False) -> dict[str, Any]:
    snapshot = _validate_snapshot(_read_private_json(path))
    current = time.time() if now is None else now
    age = current - float(snapshot["written_at"])
    if not allow_stale and (age < -5.0 or age > _snapshot_max_age(status_poll_ms)):
        raise RuntimeDataError("runtime snapshot is stale")
    return snapshot


def _write_pid(path: Path, identity: InstanceIdentity) -> None:
    _atomic_write_json(
        path,
        {
            "schema_version": _SCHEMA_VERSION,
            "pid": identity.pid,
            "instance_id": identity.instance_id,
            "started_at": identity.started_at,
        },
    )


def _read_pid(path: Path) -> InstanceIdentity:
    raw = _exact_object(
        _read_private_json(path),
        {"schema_version", "pid", "instance_id", "started_at"},
        "pid file",
    )
    if _exact_int(raw["schema_version"], "schema_version") != _SCHEMA_VERSION:
        raise RuntimeDataError("unsupported PID schema")
    return InstanceIdentity(
        pid=_exact_int(raw["pid"], "pid", 1, _PID_MAX),
        instance_id=_validate_instance_id(raw["instance_id"]),
        started_at=_finite_number(raw["started_at"], "started_at"),
    )


def _remove_pid_if_owned(path: Path, identity: InstanceIdentity) -> None:
    try:
        current = _read_pid(path)
    except RuntimeDataError:
        return
    if current == identity:
        path.unlink(missing_ok=True)


class _LifetimeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = -1

    def acquire(self, *, blocking: bool = False) -> None:
        _private_directory(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(self.path, flags, 0o600)
        os.fchmod(self.fd, 0o600)
        try:
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(self.fd, operation)
        except OSError as error:
            os.close(self.fd)
            self.fd = -1
            if not blocking and error.errno in {
                errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK
            }:
                raise AlreadyRunning("daemon lock is already held") from error
            raise

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "_LifetimeLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _TraceLock(_LifetimeLock):
    """Fail-fast trace lock with stricter pre-existing-file validation."""

    def acquire(self, *, blocking: bool = False) -> None:
        _ensure_trace_parent(self.path.parent)
        base_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            self.fd = os.open(self.path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                self.fd = os.open(self.path, base_flags)
            except OSError as error:
                raise RuntimeDataError("trace lock is unavailable") from error
        except OSError as error:
            raise RuntimeDataError("trace lock is unavailable") from error
        try:
            if created:
                os.fchmod(self.fd, 0o600)
            metadata = os.fstat(self.fd)
            if not stat.S_ISREG(metadata.st_mode) \
                    or metadata.st_uid != os.getuid() \
                    or stat.S_IMODE(metadata.st_mode) != 0o600 \
                    or metadata.st_nlink != 1:
                raise RuntimeDataError("trace lock is not a private regular file")
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(self.fd, operation)
        except OSError as error:
            os.close(self.fd)
            self.fd = -1
            if not blocking and error.errno in {
                errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK
            }:
                raise AlreadyRunning("trace lock is already held") from error
            raise
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise


def _lock_is_held(path: Path) -> bool:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeDataError("daemon lock cannot be inspected") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeDataError("daemon lock is not a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return True
            raise RuntimeDataError("daemon lock cannot be inspected") from error
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def _empty_snapshot(identity: InstanceIdentity, *, running: bool,
                    written_at: float | None = None) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "written_at": time.time() if written_at is None else written_at,
        "instance_id": identity.instance_id,
        "pid": identity.pid,
        "running": running,
        "generation": 0,
        "last_causes": ["starting"] if running else ["stopped"],
        "frontmost": {"ok": False, "bundle_id": None},
        "owner": "none",
        "pad": {
            "connected": False,
            "transport": None,
            "epoch": 0,
            "status_verified": False,
            "layer_index": None,
            "version": None,
            "last_status_at": None,
            "error_code": "unavailable",
        },
        "session_scan": {"authoritative": False, "count": 0, "diagnostics": []},
        "slots": [
            {"session_id": None, "effective_state": None, "reason": "empty"}
            for _ in range(_SLOT_COUNT)
        ],
        "zones": {
            "keys_owned": False,
            "ambient": {"color": None, "effect": None, "reason": "starting"},
            "pending_reclaim": {"keys": None, "ambient": None},
        },
        "last_input_result": None,
    }


def _lifecycle_snapshot(
    payload: Mapping[str, Any], identity: InstanceIdentity, *, running: bool
) -> dict[str, Any]:
    candidate = dict(payload)
    candidate.update(
        {
            "schema_version": _SCHEMA_VERSION,
            "written_at": time.time(),
            "instance_id": identity.instance_id,
            "pid": identity.pid,
            "running": running,
        }
    )
    return _validate_snapshot(candidate)


def _enum_or_none(value: object, allowed: frozenset[str]) -> str | None:
    return value if type(value) is str and value in allowed else None


def _snapshot_from_daemon(daemon: object, generation: int,
                          last_status_at: float | None) -> dict[str, Any]:
    """Build the exact public snapshot without serialising private objects."""
    for method_name in ("runtime_snapshot", "snapshot"):
        method = getattr(daemon, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, Mapping):
                return dict(result)

    generation_value = getattr(daemon, "generation", generation)
    if type(generation_value) is not int or generation_value < 0:
        generation_value = generation
    status_time_value = getattr(daemon, "last_status_at", last_status_at)
    if isinstance(status_time_value, bool) or not isinstance(
        status_time_value, (int, float)
    ) or not math.isfinite(float(status_time_value)) or status_time_value < 0:
        status_time_value = None

    owner = getattr(daemon, "owner", "none")
    owner = owner if type(owner) is str and owner in _OWNERS else "none"
    frontmost_ok = type(getattr(daemon, "frontmost_ok", False)) is bool \
        and getattr(daemon, "frontmost_ok", False)
    frontmost_id = getattr(daemon, "frontmost_id", None)
    if type(frontmost_id) is not str or _SAFE_TOKEN.fullmatch(frontmost_id) is None:
        frontmost_id = None

    pad = getattr(daemon, "pad", None)
    connected = type(getattr(pad, "connected", False)) is bool \
        and getattr(pad, "connected", False)
    status_verified = type(getattr(pad, "status_verified", False)) is bool \
        and getattr(pad, "status_verified", False)
    transport = _enum_or_none(getattr(pad, "transport", None), _TRANSPORTS)
    epoch_value = getattr(pad, "epoch", 0)
    epoch = epoch_value if type(epoch_value) is int and epoch_value >= 0 else 0
    layer_value = getattr(daemon, "verified_layer", None)
    layer = layer_value if type(layer_value) is int and layer_value >= 1 else None
    version = getattr(pad, "firmware_version", None)
    if type(version) is not str or not version or len(version) > 128 \
            or any(not char.isprintable() for char in version):
        version = None
    error_code = _enum_or_none(getattr(daemon, "pad_error_code", None), _PAD_ERRORS)
    if error_code is None:
        if pad is None:
            error_code = "unavailable"
        elif not connected:
            error_code = "disconnected"
        elif not status_verified:
            error_code = "status_unverified"

    session_snapshot = getattr(daemon, "session_snapshot", None)
    authoritative = type(getattr(session_snapshot, "authoritative", False)) is bool \
        and getattr(session_snapshot, "authoritative", False)
    sessions_value = getattr(session_snapshot, "sessions", ())
    count = len(sessions_value) if isinstance(sessions_value, (tuple, list)) else 0
    raw_diagnostics = getattr(daemon, "session_diagnostics", ())
    diagnostics = [
        _safe_text(item, 512) for item in raw_diagnostics
        if type(item) is str and item
    ][:128] if isinstance(raw_diagnostics, (tuple, list)) else []

    raw_slots = getattr(daemon, "slots", ())
    raw_states = getattr(daemon, "effective_states", {})
    raw_reasons = getattr(daemon, "effective_reasons", {})
    slots: list[dict[str, Any]] = []
    for index in range(_SLOT_COUNT):
        session_id = raw_slots[index] if isinstance(raw_slots, (tuple, list)) \
            and index < len(raw_slots) else None
        if type(session_id) is not str or _SAFE_TOKEN.fullmatch(session_id) is None:
            session_id = None
        state_value = raw_states.get(session_id) if isinstance(raw_states, dict) \
            and session_id is not None else None
        effective = getattr(state_value, "value", state_value)
        effective = _enum_or_none(effective, _STATES)
        public_reason = raw_reasons.get(session_id) if isinstance(raw_reasons, dict) \
            and session_id is not None else None
        reason = public_reason if type(public_reason) is str \
            and public_reason in (_SLOT_REASONS - {"empty"}) \
            else ("empty" if session_id is None else ("state" if effective else "no_hook"))
        slots.append(
            {"session_id": session_id, "effective_state": effective, "reason": reason}
        )

    causes_value = getattr(daemon, "causes", ())
    causes = [
        item for item in causes_value
        if type(item) is str and _SAFE_TOKEN.fullmatch(item) is not None
    ][:64] if isinstance(causes_value, (tuple, list)) else []
    keys_due = getattr(daemon, "keys_reclaim_due", None)
    ambient_due = getattr(daemon, "ambient_reclaim_due", None)
    keys_due = float(keys_due) if isinstance(keys_due, (int, float)) \
        and not isinstance(keys_due, bool) and math.isfinite(float(keys_due)) \
        and keys_due >= 0 else None
    ambient_due = float(ambient_due) if isinstance(ambient_due, (int, float)) \
        and not isinstance(ambient_due, bool) and math.isfinite(float(ambient_due)) \
        and ambient_due >= 0 else None
    cfg = getattr(daemon, "cfg", None)
    keys_owned = owner == "claude" and status_verified and layer == 1
    ambient_color = None
    ambient_effect = None
    ambient_reason = "pad_unverified"
    scope = getattr(cfg, "underglow_scope", "off")
    layer_mode = getattr(cfg, "layer_underglow", "keep")
    feedback_active = type(getattr(daemon, "feedback_active", False)) is bool \
        and getattr(daemon, "feedback_active", False)
    if status_verified and layer != 1:
        ambient_reason = "layer_off" if layer_mode == "off" else "layer_keep"
    elif status_verified and feedback_active:
        candidate_color = getattr(cfg, "underglow_claude", None)
        candidate_effect = getattr(cfg, "effect_fault", None)
        if type(candidate_color) is int and 0 <= candidate_color <= 0xFFFFFF:
            ambient_color = candidate_color
        ambient_effect = _enum_or_none(candidate_effect, _EFFECTS)
        ambient_reason = "input_feedback"
    elif status_verified and owner == "none":
        ambient_reason = "owner_none"
    elif status_verified and scope == "off":
        ambient_reason = "scope_off"
    elif status_verified and owner in {"claude", "codex"} and layer == 1:
        candidate_color = getattr(
            cfg, "underglow_claude" if owner == "claude" else "underglow_codex", None
        )
        shown_ids = {
            session_id for session_id in raw_slots
            if type(session_id) is str
        } if isinstance(raw_slots, (tuple, list)) else set()
        state_items = raw_states.items() if isinstance(raw_states, dict) else ()
        notable = False
        for session_id, state_value in state_items:
            if owner == "claude" and scope == "outside" and session_id in shown_ids:
                continue
            state_name = getattr(state_value, "value", state_value)
            if state_name in {"waiting", "error"}:
                notable = True
                break
        candidate_effect = getattr(
            cfg, "effect_alert" if notable else "effect_normal", None
        )
        if type(candidate_color) is int and 0 <= candidate_color <= 0xFFFFFF:
            ambient_color = candidate_color
        ambient_effect = _enum_or_none(candidate_effect, _EFFECTS)
        ambient_reason = "alert" if notable else "normal"

    return {
        "schema_version": _SCHEMA_VERSION,
        "written_at": time.time(),
        "instance_id": uuid.uuid4().hex,  # overwritten by lifecycle owner
        "pid": os.getpid(),               # overwritten by lifecycle owner
        "running": True,                  # overwritten by lifecycle owner
        "generation": generation_value,
        "last_causes": causes,
        "frontmost": {"ok": frontmost_ok, "bundle_id": frontmost_id},
        "owner": owner,
        "pad": {
            "connected": connected,
            "transport": transport,
            "epoch": epoch,
            "status_verified": status_verified,
            "layer_index": layer,
            "version": version,
            "last_status_at": status_time_value,
            "error_code": error_code,
        },
        "session_scan": {
            "authoritative": authoritative,
            "count": count,
            "diagnostics": diagnostics,
        },
        "slots": slots,
        "zones": {
            "keys_owned": keys_owned,
            "ambient": {
                "color": ambient_color,
                "effect": ambient_effect,
                "reason": ambient_reason,
            },
            "pending_reclaim": {"keys": keys_due, "ambient": ambient_due},
        },
        "last_input_result": _enum_or_none(
            getattr(daemon, "last_input_result", None), _INPUT_RESULTS
        ),
    }


@dataclass
class _ActiveTrace:
    request_id: str
    instance_id: str
    started_at: float
    deadline: float
    requested_duration_ms: int
    max_events: int
    events: list[dict[str, Any]]
    connection_ordinals: dict[int, int]


class _TraceMailbox:
    """Opt-in, one-shot trace collector owned by the running daemon.

    Merely constructing or polling this object never creates a mailbox file.
    A capture exists only after a separate CLI process publishes an exact,
    private request bound to this daemon instance.
    """

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.instance_id: str | None = None
        self.active: _ActiveTrace | None = None

    def bind_instance(self, instance_id: str) -> None:
        self.instance_id = _validate_instance_id(instance_id)

    def _start_request(self, now: float) -> None:
        if self.instance_id is None or self.active is not None:
            return
        _cleanup_stale_trace_path(
            self.paths.trace_request_path, ttl=_TRACE_REQUEST_TTL_SECONDS
        )
        _cleanup_stale_trace_path(
            self.paths.trace_result_path, ttl=_TRACE_RESULT_TTL_SECONDS
        )
        orphan_metadata = _trace_path_metadata(self.paths.trace_active_path)
        if orphan_metadata is not None:
            # No in-memory capture owns this marker. Remove the exact safe inode
            # once regardless of its JSON contents, then admit a new request in
            # this same poll. Unsafe symlinks/hardlinks fail above and remain.
            orphan = _TraceFile(
                value=None,
                device=orphan_metadata.st_dev,
                inode=orphan_metadata.st_ino,
                modified_at=orphan_metadata.st_mtime,
            )
            if not _unlink_trace_inode(self.paths.trace_active_path, orphan):
                return
        if _trace_path_metadata(self.paths.trace_request_path) is None:
            return
        request_metadata = _trace_path_metadata(self.paths.trace_request_path)
        if request_metadata is None:
            return
        claimed_inode = _TraceFile(
            value=None,
            device=request_metadata.st_dev,
            inode=request_metadata.st_ino,
            modified_at=request_metadata.st_mtime,
        )
        # Claim the exact inode under an active marker. This serializes a later
        # CLI even if the requesting CLI crashes and releases its advisory lock.
        if not _claim_trace_inode(
            self.paths.trace_request_path,
            self.paths.trace_active_path,
            claimed_inode,
        ):
            return
        try:
            trace_file = _read_private_trace_json(self.paths.trace_active_path)
            request = _validate_trace_request(trace_file.value)
        except BaseException:
            _unlink_trace_inode(self.paths.trace_active_path, claimed_inode)
            raise
        created_at = float(request["created_at"])
        wall_age = time.time() - created_at
        if wall_age < -5.0 or wall_age > _TRACE_REQUEST_TTL_SECONDS:
            _unlink_trace_inode(self.paths.trace_active_path, trace_file)
            return
        if request["instance_id"] != self.instance_id:
            _unlink_trace_inode(self.paths.trace_active_path, trace_file)
            return
        duration_ms = int(request["duration_ms"])
        self.active = _ActiveTrace(
            request_id=str(request["request_id"]),
            instance_id=self.instance_id,
            started_at=now,
            deadline=now + duration_ms / 1000.0,
            requested_duration_ms=duration_ms,
            max_events=int(request["max_events"]),
            events=[],
            connection_ordinals={},
        )

    def _finish(self, finished_at: float, *, truncated: bool) -> None:
        active = self.active
        if active is None:
            return
        # Clear first so validation or filesystem faults cannot poison every
        # future observer call with the same capture.
        self.active = None
        try:
            elapsed_ms = int(
                max(0.0, (finished_at - active.started_at) * 1000.0)
            )
            duration_ms = min(active.requested_duration_ms, elapsed_ms)
            envelope = _validate_trace_result({
                "schema_version": _SCHEMA_VERSION,
                "request_id": active.request_id,
                "instance_id": active.instance_id,
                "duration_ms": duration_ms,
                "truncated": truncated,
                "events": active.events,
            })
            _cleanup_stale_trace_path(
                self.paths.trace_result_path, ttl=_TRACE_RESULT_TTL_SECONDS
            )
            existing = _trace_path_metadata(self.paths.trace_result_path)
            if existing is not None:
                previous = _validate_trace_result(
                    _read_private_trace_json(self.paths.trace_result_path).value
                )
                if previous["request_id"] == active.request_id \
                        and previous["instance_id"] == active.instance_id:
                    return
                raise RuntimeDataError("another trace result occupies the mailbox")
            _atomic_create_trace_json(self.paths.trace_result_path, envelope)
        except BaseException:
            # The diagnostic result may be lost, but daemon routing continues.
            pass
        finally:
            _unlink_trace_if_owned(
                self.paths.trace_active_path,
                request_id=active.request_id,
                instance_id=active.instance_id,
            )

    def poll(self, now: float | None = None) -> None:
        """Accept a request or complete an expired one without escaping errors."""
        try:
            current = time.monotonic() if now is None else now
            if self.active is None:
                self._start_request(current)
            active = self.active
            if active is not None and current >= active.deadline:
                self._finish(active.deadline, truncated=False)
        except BaseException:
            # Tracing is diagnostic-only and must never affect daemon routing.
            pass

    def observe(self, event: object) -> None:
        """Collect one already-normalized daemon input disposition."""
        try:
            active = self.active
            if active is None:
                return
            received = getattr(event, "received_at")
            if isinstance(received, bool) or not isinstance(received, (int, float)) \
                    or not math.isfinite(float(received)):
                return
            received_at = float(received)
            if received_at < active.started_at:
                return
            if received_at > active.deadline:
                self._finish(active.deadline, truncated=False)
                return
            raw_ordinal = getattr(event, "connection_ordinal")
            if type(raw_ordinal) is not int:
                return
            ordinal = active.connection_ordinals.get(raw_ordinal)
            if ordinal is None:
                ordinal = len(active.connection_ordinals) + 1
                active.connection_ordinals[raw_ordinal] = ordinal
            relative_ms = min(
                active.requested_duration_ms,
                int((received_at - active.started_at) * 1000.0),
            )
            item = {
                "sequence": len(active.events) + 1,
                "relative_ms": relative_ms,
                "key": getattr(event, "key"),
                "action": getattr(event, "action"),
                "owner": getattr(event, "owner_at_dispatch"),
                "layer_one": getattr(event, "layer_one"),
                "slot_occupied": getattr(event, "slot_occupied"),
                "result": getattr(event, "result"),
                "connection_ordinal": ordinal,
            }
            _validate_trace_event(
                item, sequence=len(active.events) + 1,
                duration_ms=active.requested_duration_ms,
                previous_relative_ms=(
                    int(active.events[-1]["relative_ms"])
                    if active.events else 0
                ),
            )
            active.events.append(item)
            if len(active.events) >= active.max_events:
                self._finish(received_at, truncated=True)
        except BaseException:
            # An unsafe mailbox or malformed event cannot interfere with input.
            pass

    def close(self) -> None:
        """Remove only this instance's active request; never synthesize success."""
        active, self.active = self.active, None
        if active is None:
            return
        try:
            _unlink_trace_if_owned(
                self.paths.trace_active_path,
                request_id=active.request_id,
                instance_id=active.instance_id,
            )
        except BaseException:
            pass


class _DefaultRuntime:
    def __init__(self, cfg: object, paths: RuntimePaths) -> None:
        # This is the only lifecycle/daemon coupling point.  Keeping it lazy
        # makes ``hook`` and ``status`` independent of AppKit and IOKit imports.
        from paneglow import daemon as daemon_module, deeplink, sessions

        self.cfg = cfg
        self.trace_mailbox = _TraceMailbox(paths)
        self.daemon = daemon_module.Daemon(
            cfg,
            state_root=paths.state_dir,
            scanner=lambda: sessions.scan(root=paths.claude_sessions_dir),
            opener=lambda session_id: deeplink.open_session(
                session_id, (paths.mapping_dir,)
            ),
            input_observer=self.trace_mailbox.observe,
        )
        self.generation = 0
        self.last_status_at: float | None = None

    def bind_instance(self, identity: InstanceIdentity) -> None:
        self.trace_mailbox.bind_instance(identity.instance_id)

    def run(self, stop_event: threading.Event,
            publish: Callable[[Mapping[str, Any]], None]) -> None:
        interval = max(0.001, getattr(self.cfg, "poll_ms", 250) / 1000.0)
        while not stop_event.is_set():
            trace_mailbox = getattr(self, "trace_mailbox", None)
            if trace_mailbox is not None:
                trace_mailbox.poll()
            # Session records and Claude's mapping files use Unix epoch time.
            # The daemon also uses this value for retry deadlines, so every
            # comparison must stay in the same clock domain.
            now = time.time()
            self.daemon.tick(now)
            if trace_mailbox is not None:
                trace_mailbox.poll()
            self.generation += 1
            pad = getattr(self.daemon, "pad", None)
            if bool(getattr(pad, "status_verified", False)):
                self.last_status_at = time.time()
            publish(_snapshot_from_daemon(
                self.daemon, self.generation, self.last_status_at
            ))
            # A connected/verified daemon pumps the CFRunLoop for poll_ms inside
            # tick(); sleeping again would double input and repaint latency.
            # Failed discovery/status paths return quickly, so only those need
            # an explicit wait to avoid a retry busy loop.
            if getattr(self.daemon, "pad", None) is None \
                    or getattr(self.daemon, "verified_layer", None) is None:
                stop_event.wait(interval)

    def close(self, flush_seconds: float = 1.0) -> None:
        # Daemon.close delegates to Pad.close(), whose default performs the
        # measured flush.  The adapter accepts the explicit lifecycle contract
        # even though the deterministic daemon intentionally has no CLI concern.
        trace_mailbox = getattr(self, "trace_mailbox", None)
        if trace_mailbox is not None:
            trace_mailbox.close()
        self.daemon.close()

    def snapshot(self) -> dict[str, Any]:
        return _snapshot_from_daemon(
            self.daemon, self.generation, self.last_status_at
        )


def _runtime_factory(cfg: object, paths: RuntimePaths) -> DaemonRuntime:
    return _DefaultRuntime(cfg, paths)


def _load_config(paths: RuntimePaths) -> tuple[object, list[str]]:
    from paneglow import config

    return config.load(paths.config_path)


@contextlib.contextmanager
def _signal_stop_event(stop_event: threading.Event):
    previous: dict[signal.Signals, Any] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        for item in (signal.SIGTERM, signal.SIGINT):
            previous[item] = signal.getsignal(item)
            signal.signal(item, request_stop)
    try:
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


def _cmd_run(paths: RuntimePaths, *, stdout: TextIO | None = None,
             stderr: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    identity = InstanceIdentity(os.getpid(), uuid.uuid4().hex, time.time())
    transition = _LifetimeLock(paths.transition_lock_path)
    lifetime = _LifetimeLock(paths.lock_path)
    try:
        transition.acquire()
    except AlreadyRunning:
        print("paneglow: daemon identity transition is already in progress",
              file=stderr)
        return 1
    except (OSError, RuntimeDataError):
        print("paneglow: could not acquire the private identity transition lock",
              file=stderr)
        return 1
    try:
        lifetime.acquire()
    except AlreadyRunning:
        transition.close()
        print("paneglow: daemon is already running", file=stderr)
        return 1
    except (OSError, RuntimeDataError):
        transition.close()
        print("paneglow: could not acquire the private daemon lock", file=stderr)
        return 1

    stop_event = threading.Event()
    runtime: DaemonRuntime | None = None
    latest = _empty_snapshot(identity, running=True)
    failed = False
    try:
        with _signal_stop_event(stop_event):
            try:
                # The transition lock makes lifetime-lock acquisition and
                # identity publication one observable step for start/stop.
                _write_pid(paths.pid_path, identity)
                _atomic_write_json(paths.snapshot_path, _validate_snapshot(latest))
                transition.close()
                cfg, _warnings = _load_config(paths)
                runtime = _runtime_factory(cfg, paths)
                bind_instance = getattr(runtime, "bind_instance", None)
                if callable(bind_instance):
                    bind_instance(identity)

                def publish(payload: Mapping[str, Any]) -> None:
                    nonlocal latest
                    latest = _lifecycle_snapshot(payload, identity, running=True)
                    _atomic_write_json(paths.snapshot_path, latest)

                runtime.run(stop_event, publish)
            except BaseException:
                failed = True
                stop_event.set()
                print("paneglow: daemon runtime failed", file=stderr)
            finally:
                stop_event.set()
                # Mirror startup's guarded publication.  While stop owns this
                # lock the daemon cannot tear down and release its PID for
                # reuse between kill(pid, 0) and SIGTERM.
                if transition.fd < 0:
                    try:
                        transition.acquire(blocking=True)
                    except BaseException:
                        failed = True
                close_failed = False
                if runtime is not None:
                    try:
                        runtime.close(flush_seconds=1.0)
                    except BaseException:
                        close_failed = True
                        failed = True
                    snapshot_method = getattr(runtime, "snapshot", None)
                    if callable(snapshot_method):
                        try:
                            post_close = snapshot_method()
                            if not isinstance(post_close, Mapping):
                                raise RuntimeDataError(
                                    "runtime snapshot is not an object")
                            latest = _lifecycle_snapshot(
                                post_close, identity, running=True
                            )
                            if latest["pad"]["error_code"] == "close_failed":
                                failed = True
                        except BaseException:
                            failed = True
                try:
                    stopped = _lifecycle_snapshot(latest, identity, running=False)
                    stopped["last_causes"] = list(stopped["last_causes"])
                    if "stopped" not in stopped["last_causes"]:
                        stopped["last_causes"].append("stopped")
                    if close_failed:
                        stopped["pad"] = dict(stopped["pad"])
                        stopped["pad"]["error_code"] = "close_failed"
                    _atomic_write_json(
                        paths.snapshot_path, _validate_snapshot(stopped))
                except BaseException:
                    failed = True
                try:
                    _remove_pid_if_owned(paths.pid_path, identity)
                except BaseException:
                    failed = True
    finally:
        # Keep this order: while the transition lock is held, no stop command
        # can combine a held lifetime lock with old or partially-written hints.
        try:
            lifetime.close()
        except BaseException:
            failed = True
        try:
            transition.close()
        except BaseException:
            failed = True
    return 1 if failed else 0


def _absolute_command_prefix() -> tuple[str, ...]:
    # Keep a virtual environment's executable path intact.  Resolving its
    # ``bin/python`` symlink selects the base interpreter, which then cannot
    # import the package installed only in that environment.
    executable = _absolute_path(sys.executable)
    if not executable.is_absolute():
        raise RuntimeError("Python executable is not absolute")
    return (str(executable), "-m", "paneglow.cli")


def _spawn_detached(command: tuple[str, ...], log_fd: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        cwd="/",
        close_fds=True,
        start_new_session=True,
    )


def _open_log(path: Path) -> int:
    _private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _identity_ready(paths: RuntimePaths, *, child_pid: int,
                    status_poll_ms: int) -> InstanceIdentity | None:
    transition = _LifetimeLock(paths.transition_lock_path)
    try:
        transition.acquire()
    except AlreadyRunning:
        return None
    except (OSError, RuntimeDataError) as error:
        raise RuntimeDataError("identity transition lock is unsafe") from error
    try:
        if not _lock_is_held(paths.lock_path):
            return None
        identity = _read_pid(paths.pid_path)
        snapshot = _read_snapshot(
            paths.snapshot_path, status_poll_ms=status_poll_ms)
        if identity.pid != child_pid or not snapshot["running"] \
                or snapshot["generation"] < 1 \
                or snapshot["pid"] != identity.pid \
                or snapshot["instance_id"] != identity.instance_id:
            return None
        return identity
    finally:
        transition.close()


def _launch_agent_spec(paths: RuntimePaths,
                       env: Mapping[str, str] | None = None):
    """Build the current login service spec without importing it on hook paths."""
    from paneglow import launch_agent

    source = os.environ if env is None else env
    account_home = launch_agent.current_account_home()
    overrides: dict[str, Path] = {}
    candidates = {
        "PANEGLOW_HOME": (paths.home, account_home / ".paneglow"),
        "PANEGLOW_CLAUDE_SETTINGS": (
            paths.claude_settings_path, account_home / ".claude" / "settings.json"
        ),
        "PANEGLOW_CLAUDE_SESSIONS": (
            paths.claude_sessions_dir, account_home / ".claude" / "sessions"
        ),
        "PANEGLOW_MAPPING_DIR": (
            paths.mapping_dir,
            account_home / "Library" / "Application Support" / "Claude"
            / "claude-code-sessions",
        ),
    }
    for key, (value, default) in candidates.items():
        if key in source or value != default:
            overrides[key] = value
    return launch_agent.build_spec(
        command_prefix=_absolute_command_prefix(),
        runtime_home=paths.home,
        log_path=paths.log_path,
        runtime_environment=overrides,
        account_home=account_home,
    )


def _service_ready(paths: RuntimePaths, *, status_poll_ms: int) \
        -> InstanceIdentity | None:
    try:
        identity, snapshot = _runtime_identity(
            paths, status_poll_ms=status_poll_ms
        )
    except RuntimeDataError:
        return None
    return identity if snapshot["generation"] >= 1 else None


def _wait_service_ready(paths: RuntimePaths, *, timeout: float,
                        status_poll_ms: int) -> InstanceIdentity | None:
    deadline = time.monotonic() + timeout
    while True:
        identity = _service_ready(paths, status_poll_ms=status_poll_ms)
        if identity is not None:
            return identity
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _rollback_launch_agent(
    paths: RuntimePaths,
    spec: object,
    controller: object,
    previous: object,
    previous_loaded: bool,
    manual_was_running: bool,
    *,
    timeout: float,
    status_poll_ms: int,
) -> bool:
    """Restore the exact prior lifecycle owner, or report rollback failure."""
    from paneglow import launch_agent

    try:
        if controller.loaded():
            controller.bootout()
            if not _wait_runtime_stopped(paths, timeout):
                return False
        current = launch_agent.inspect_manifest(spec)
        if current.status in {"unsafe", "unknown"}:
            return False
        if previous.owned and previous.payload is not None:
            launch_agent.atomic_write_manifest(spec, previous.payload)
            restored = launch_agent.inspect_manifest(spec)
            if restored.payload != previous.payload or not restored.owned:
                return False
            if previous_loaded:
                controller.bootstrap()
                controller.kickstart()
                if _wait_service_ready(
                    paths, timeout=timeout, status_poll_ms=status_poll_ms
                ) is None:
                    return False
        else:
            if current.owned:
                launch_agent.remove_manifest(spec, current)
            if launch_agent.inspect_manifest(spec).status != "missing":
                return False
        if not previous_loaded and manual_was_running and _cmd_start_locked(
                paths,
                timeout,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                manual_only=True,
            ) != 0:
            return False
        return True
    except (OSError, RuntimeError, launch_agent.LaunchAgentError):
        return False


def _cmd_autostart_install(
    paths: RuntimePaths,
    timeout: float,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    spec: object | None = None,
    controller: object | None = None,
) -> int:
    from paneglow import launch_agent

    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    rollback_attempted = False
    rollback_ok = False
    try:
        selected = _launch_agent_spec(paths) if spec is None else spec
        service = launch_agent.Controller(selected) if controller is None else controller
        launch_agent.validate_program(selected)
        launch_agent.ensure_install_directories(selected)
        launch_agent.ensure_private_log(selected)
        with launch_agent.InstallLock(selected):
            existing = launch_agent.inspect_manifest(selected)
            if existing.status in {"unsafe", "unknown"}:
                raise launch_agent.LaunchAgentError(
                    "existing LaunchAgent manifest is not owned by Paneglow")
            loaded = service.loaded()
            if existing.status == "missing" and loaded:
                raise launch_agent.LaunchAgentError(
                    "loaded service has no recognized manifest")
            cfg, _warnings = _load_config(paths)
            poll_ms = getattr(cfg, "status_poll_ms", 1000)
            if existing.status == "current" and loaded:
                ready = _service_ready(paths, status_poll_ms=poll_ms)
                if ready is not None:
                    print(
                        f"paneglow: login autostart already installed "
                        f"(pid {ready.pid})",
                        file=stdout,
                    )
                    return 0

            previous_loaded = loaded
            manual_was_running = False
            try:
                # Never bootstrap a KeepAlive job beside another daemon. A
                # loaded service is torn down by launchd first; only an
                # unloaded/manual daemon is ever sent a verified SIGTERM.
                if loaded:
                    service.bootout()
                    if not _wait_runtime_stopped(paths, timeout):
                        raise launch_agent.LaunchAgentError(
                            "LaunchAgent did not stop after bootout")
                else:
                    try:
                        manual_was_running = _lock_is_held(paths.lock_path)
                    except RuntimeDataError as error:
                        raise launch_agent.LaunchAgentError(
                            "daemon lock is unsafe") from error
                    if manual_was_running and _cmd_stop_runtime(
                        paths,
                        timeout,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    ) != 0:
                        raise launch_agent.LaunchAgentError(
                            "existing daemon could not be stopped safely")

                # Bind replacement to the exact state inspected under the
                # installer lock. A concurrently introduced unknown plist is
                # never overwritten.
                before_write = launch_agent.inspect_manifest(selected)
                if existing.status == "missing":
                    unchanged = before_write.status == "missing"
                else:
                    unchanged = bool(
                        before_write.status == existing.status
                        and before_write.payload == existing.payload
                        and before_write.device == existing.device
                        and before_write.inode == existing.inode
                    )
                if not unchanged:
                    raise launch_agent.LaunchAgentError(
                        "LaunchAgent manifest changed during install")
                launch_agent.atomic_write_manifest(selected)
                if launch_agent.inspect_manifest(selected).status != "current":
                    raise launch_agent.LaunchAgentError(
                        "installed LaunchAgent manifest could not be verified")
                service.bootstrap()
                service.kickstart()
                identity = _wait_service_ready(
                    paths, timeout=timeout, status_poll_ms=poll_ms
                )
                if identity is None:
                    raise launch_agent.LaunchAgentError(
                        "LaunchAgent daemon did not become ready")
            except (OSError, RuntimeError, launch_agent.LaunchAgentError):
                rollback_attempted = True
                rollback_ok = _rollback_launch_agent(
                    paths,
                    selected,
                    service,
                    existing,
                    previous_loaded,
                    manual_was_running,
                    timeout=timeout,
                    status_poll_ms=poll_ms,
                )
                raise
    except (OSError, RuntimeError, launch_agent.LaunchAgentError):
        if rollback_attempted and not rollback_ok:
            message = (
                "paneglow: login autostart failed and the previous runtime "
                "could not be fully restored"
            )
        elif rollback_attempted:
            message = (
                "paneglow: login autostart was not installed; "
                "the previous state was restored"
            )
        else:
            message = (
                "paneglow: login autostart was not installed; "
                "the existing lifecycle state was not changed"
            )
        print(message, file=stderr)
        return 1
    print(f"paneglow: installed login autostart (pid {identity.pid})", file=stdout)
    return 0


def _cmd_autostart_status(
    paths: RuntimePaths,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    spec: object | None = None,
    controller: object | None = None,
) -> int:
    from paneglow import launch_agent

    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        selected = _launch_agent_spec(paths) if spec is None else spec
        service = launch_agent.Controller(selected) if controller is None else controller
        existing = launch_agent.inspect_manifest(selected)
        if existing.status == "missing":
            if service.loaded():
                print(
                    "autostart   loaded without a recognized manifest",
                    file=stderr,
                )
                return 1
            print("autostart   not installed", file=stdout)
            return 1
        if existing.status in {"unsafe", "unknown"}:
            print("autostart   unsafe or unrecognized manifest", file=stderr)
            return 1
        loaded = service.loaded()
    except (OSError, RuntimeError, launch_agent.LaunchAgentError):
        print("autostart   unavailable", file=stderr)
        return 1
    freshness = "current" if existing.status == "current" else "stale"
    load_state = "loaded" if loaded else "not loaded"
    print(f"autostart   installed | {freshness} | {load_state}", file=stdout)
    return 0 if freshness == "current" and loaded else 1


def _cmd_autostart_uninstall(
    paths: RuntimePaths,
    timeout: float,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    spec: object | None = None,
    controller: object | None = None,
) -> int:
    from paneglow import launch_agent

    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    rollback_attempted = False
    rollback_ok = False
    try:
        selected = _launch_agent_spec(paths) if spec is None else spec
        service = launch_agent.Controller(selected) if controller is None else controller
        initial = launch_agent.inspect_manifest(selected)
        if initial.status == "unsafe":
            raise launch_agent.LaunchAgentError(
                "LaunchAgent manifest ancestors are unsafe")
        # An absent LaunchAgents directory cannot contain our serialization
        # lock. Keep an already-uninstalled command read-only in this case.
        if not launch_agent.validate_manifest_ancestors(selected):
            if service.loaded():
                raise launch_agent.LaunchAgentError(
                    "loaded service has no recognized manifest")
            print("paneglow: login autostart already uninstalled", file=stdout)
            return 0
        launch_agent.ensure_lock_directory(selected)
        with launch_agent.InstallLock(selected):
            existing = launch_agent.inspect_manifest(selected)
            loaded = service.loaded()
            if existing.status == "missing":
                if loaded:
                    raise launch_agent.LaunchAgentError(
                        "loaded service has no recognized manifest")
                print("paneglow: login autostart already uninstalled", file=stdout)
                return 0
            if not existing.owned:
                raise launch_agent.LaunchAgentError(
                    "LaunchAgent manifest is not owned by Paneglow")
            manual_was_running = False
            lifecycle_changed = False
            try:
                if loaded:
                    lifecycle_changed = True
                    service.bootout()
                    if not _wait_runtime_stopped(paths, timeout):
                        # The job has been booted out; never bypass launchd by
                        # signaling an identity that it previously owned.
                        raise launch_agent.LaunchAgentError(
                            "LaunchAgent did not stop after bootout")
                else:
                    try:
                        manual_was_running = _lock_is_held(paths.lock_path)
                    except RuntimeDataError as error:
                        raise launch_agent.LaunchAgentError(
                            "daemon lock is unsafe") from error
                    if manual_was_running:
                        # A verified SIGTERM may take effect even when the
                        # bounded stop command reports a timeout. Treat the
                        # lifecycle as changed before sending it so rollback
                        # re-establishes the previous manual owner.
                        lifecycle_changed = True
                        if _cmd_stop_runtime(
                            paths,
                            timeout,
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                        ) != 0:
                            raise launch_agent.LaunchAgentError(
                                "existing daemon could not be stopped safely")
                lifecycle_changed = True
                launch_agent.remove_manifest(selected, existing)
                if launch_agent.inspect_manifest(selected).status != "missing":
                    raise launch_agent.LaunchAgentError(
                        "LaunchAgent manifest remains after removal")
            except (OSError, RuntimeError, launch_agent.LaunchAgentError):
                if lifecycle_changed:
                    rollback_attempted = True
                    rollback_ok = False
                    try:
                        current = launch_agent.inspect_manifest(selected)
                        if current.status in {"unsafe", "unknown"}:
                            raise launch_agent.LaunchAgentError(
                                "manifest changed during uninstall rollback")
                        if current.status == "missing":
                            launch_agent.atomic_write_manifest(
                                selected, existing.payload
                            )
                        restored = launch_agent.inspect_manifest(selected)
                        if restored.payload != existing.payload \
                                or not restored.owned:
                            raise launch_agent.LaunchAgentError(
                                "manifest rollback verification failed")
                        if loaded:
                            if not service.loaded():
                                service.bootstrap()
                            service.kickstart()
                            cfg, _warnings = _load_config(paths)
                            if _wait_service_ready(
                                paths,
                                timeout=timeout,
                                status_poll_ms=getattr(
                                    cfg, "status_poll_ms", 1000
                                ),
                            ) is None:
                                raise launch_agent.LaunchAgentError(
                                    "service rollback readiness failed")
                        elif manual_was_running and _cmd_start_locked(
                            paths,
                            timeout,
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                            manual_only=True,
                        ) != 0:
                            raise launch_agent.LaunchAgentError(
                                "manual daemon rollback failed")
                        rollback_ok = True
                    except (OSError, RuntimeError, launch_agent.LaunchAgentError):
                        rollback_ok = False
                raise
    except (OSError, RuntimeError, launch_agent.LaunchAgentError):
        if rollback_attempted and not rollback_ok:
            message = (
                "paneglow: login autostart removal failed and the previous "
                "lifecycle could not be fully restored"
            )
        elif rollback_attempted:
            message = (
                "paneglow: login autostart was not removed; "
                "the previous lifecycle was restored"
            )
        else:
            message = (
                "paneglow: login autostart was not removed; "
                "the existing lifecycle state was not changed"
            )
        print(message, file=stderr)
        return 1
    print("paneglow: uninstalled login autostart", file=stdout)
    return 0


def _cmd_start_locked(paths: RuntimePaths, timeout: float, *,
                      stdout: TextIO | None = None,
                      stderr: TextIO | None = None,
                      manual_only: bool = False) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    transition = _LifetimeLock(paths.transition_lock_path)
    try:
        transition.acquire()
    except AlreadyRunning:
        print("paneglow: daemon identity transition is in progress", file=stderr)
        return 1
    except (OSError, RuntimeDataError):
        print("paneglow: identity transition lock is unsafe", file=stderr)
        return 1
    try:
        try:
            cfg, _warnings = _load_config(paths)
            poll_ms = getattr(cfg, "status_poll_ms", 1000)
            if _lock_is_held(paths.lock_path):
                identity = _read_pid(paths.pid_path)
                snapshot = _read_snapshot(
                    paths.snapshot_path, status_poll_ms=poll_ms
                )
                if snapshot["running"] and snapshot["generation"] >= 1 \
                        and snapshot["pid"] == identity.pid \
                        and snapshot["instance_id"] == identity.instance_id:
                    print(f"paneglow: already running (pid {identity.pid})",
                          file=stdout)
                    return 0
                print("paneglow: daemon lock is held but identity is invalid",
                      file=stderr)
                return 1
        except RuntimeDataError:
            print("paneglow: existing runtime state is unsafe or malformed",
                  file=stderr)
            return 1
    finally:
        transition.close()

    # Once a current login service is installed, keep one lifecycle owner.
    # Falling back to a detached child here would make launchd's crash retry
    # contend for the same lifetime lock.
    if not manual_only:
        from paneglow import launch_agent
        service = None
        service_activated = False
        try:
            service_spec = _launch_agent_spec(paths)
            service_manifest = launch_agent.inspect_manifest(service_spec)
            service = launch_agent.Controller(service_spec)
            if service_manifest.status == "missing":
                if service.loaded():
                    print(
                        "paneglow: loaded service has no recognized manifest",
                        file=stderr,
                    )
                    return 1
                service = None
            else:
                if service_manifest.status != "current":
                    print(
                        "paneglow: login autostart is stale or unsafe; reinstall it",
                        file=stderr,
                    )
                    return 1
                if not service.loaded():
                    service.bootstrap()
                # A successful bootstrap may start KeepAlive immediately, and
                # an already-loaded job is ours once this start command elects
                # to kick it. Any later failure must boot it out.
                service_activated = True
                service.kickstart()
                identity = _wait_service_ready(
                    paths, timeout=timeout, status_poll_ms=poll_ms
                )
                if identity is None:
                    service.bootout()
                    _wait_runtime_stopped(paths, timeout)
                    print(
                        "paneglow: LaunchAgent daemon did not become ready",
                        file=stderr,
                    )
                    return 1
                print(f"paneglow: started (pid {identity.pid})", file=stdout)
                return 0
        except (OSError, RuntimeError, launch_agent.LaunchAgentError):
            if service_activated and service is not None:
                try:
                    if service.loaded():
                        service.bootout()
                        _wait_runtime_stopped(paths, timeout)
                except launch_agent.LaunchAgentError:
                    pass
            print("paneglow: could not inspect or start login autostart", file=stderr)
            return 1

    command = (*_absolute_command_prefix(), "run")
    log_fd = -1
    try:
        log_fd = _open_log(paths.log_path)
        child = _spawn_detached(command, log_fd)
    except (OSError, RuntimeError):
        print("paneglow: could not start detached daemon", file=stderr)
        return 1
    finally:
        if log_fd >= 0:
            os.close(log_fd)

    deadline = time.monotonic() + timeout
    while True:
        if child.poll() is not None:
            print("paneglow: daemon exited before becoming ready", file=stderr)
            return 1
        try:
            identity = _identity_ready(
                paths, child_pid=child.pid, status_poll_ms=poll_ms
            )
        except RuntimeDataError:
            identity = None
        if identity is not None:
            print(f"paneglow: started (pid {identity.pid})", file=stdout)
            return 0
        if time.monotonic() >= deadline:
            try:
                os.kill(child.pid, signal.SIGTERM)
            except OSError:
                pass
            print("paneglow: daemon did not become ready", file=stderr)
            return 1
        time.sleep(0.05)


def _cmd_start(paths: RuntimePaths, timeout: float, *,
               stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    from paneglow import launch_agent

    try:
        spec = _launch_agent_spec(paths)
    except launch_agent.LaunchAgentError:
        # Explicit custom/manual runtimes outside the login-home LaunchAgent
        # boundary retain the historical detached lifecycle.
        return _cmd_start_locked(
            paths, timeout, stdout=stdout, stderr=stderr, manual_only=True
        )
    try:
        launch_agent.ensure_lock_directory(spec)
        with launch_agent.InstallLock(spec):
            return _cmd_start_locked(
                paths, timeout, stdout=stdout, stderr=stderr
            )
    except launch_agent.LaunchAgentError:
        target = sys.stderr if stderr is None else stderr
        print("paneglow: autostart lifecycle is unsafe", file=target)
        return 1
    except (OSError, BlockingIOError):
        target = sys.stderr if stderr is None else stderr
        print("paneglow: autostart lifecycle is busy or unsafe", file=target)
        return 1


def _cmd_stop_runtime(paths: RuntimePaths, timeout: float, *,
                      stdout: TextIO | None = None,
                      stderr: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    transition = _LifetimeLock(paths.transition_lock_path)
    try:
        transition.acquire()
    except AlreadyRunning:
        print("paneglow: identity transition in progress; no signal was sent",
              file=stderr)
        return 1
    except (OSError, RuntimeDataError):
        print("paneglow: identity transition lock is unsafe; no signal was sent",
              file=stderr)
        return 1
    try:
        try:
            if not _lock_is_held(paths.lock_path):
                print("paneglow: not running", file=stdout)
                return 0
            cfg, _warnings = _load_config(paths)
            identity = _read_pid(paths.pid_path)
            snapshot = _read_snapshot(
                paths.snapshot_path,
                status_poll_ms=getattr(cfg, "status_poll_ms", 1000),
                allow_stale=True,
            )
            if not snapshot["running"] or snapshot["pid"] != identity.pid \
                    or snapshot["instance_id"] != identity.instance_id:
                raise RuntimeDataError("daemon identity mismatch")
            os.kill(identity.pid, 0)
        except ProcessLookupError:
            print("paneglow: daemon identity names a missing process", file=stderr)
            return 1
        except (PermissionError, RuntimeDataError, OSError):
            print("paneglow: refusing to signal an unverified daemon", file=stderr)
            return 1

        try:
            os.kill(identity.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            print("paneglow: could not send SIGTERM", file=stderr)
            return 1
    finally:
        try:
            transition.close()
        except BaseException:
            pass

    deadline = time.monotonic() + timeout
    while True:
        try:
            held = _lock_is_held(paths.lock_path)
        except RuntimeDataError:
            held = True
        if not held:
            print("paneglow: stopped", file=stdout)
            return 0
        if time.monotonic() >= deadline:
            print("paneglow: SIGTERM timed out; no SIGKILL was sent", file=stderr)
            return 1
        time.sleep(0.05)


def _wait_runtime_stopped(paths: RuntimePaths, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            held = _lock_is_held(paths.lock_path)
        except RuntimeDataError:
            return False
        if not held:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _cmd_stop(paths: RuntimePaths, timeout: float, *,
              stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Stop launchd-owned jobs with bootout; signal only detached daemons."""
    from paneglow import launch_agent

    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        spec = _launch_agent_spec(paths)
    except launch_agent.LaunchAgentError:
        return _cmd_stop_runtime(
            paths, timeout, stdout=stdout, stderr=stderr
        )
    try:
        launch_agent.ensure_lock_directory(spec)
        with launch_agent.InstallLock(spec):
            manifest = launch_agent.inspect_manifest(spec)
            if manifest.status == "missing":
                service = launch_agent.Controller(spec)
                if service.loaded():
                    print(
                        "paneglow: loaded service has no recognized manifest; "
                        "no signal was sent",
                        file=stderr,
                    )
                    return 1
                return _cmd_stop_runtime(
                    paths, timeout, stdout=stdout, stderr=stderr
                )
            if not manifest.owned:
                print(
                    "paneglow: login autostart is unsafe; no signal was sent",
                    file=stderr,
                )
                return 1
            service = launch_agent.Controller(spec)
            if not service.loaded():
                return _cmd_stop_runtime(
                    paths, timeout, stdout=stdout, stderr=stderr
                )
            service.bootout()
            if not _wait_runtime_stopped(paths, timeout):
                print(
                    "paneglow: LaunchAgent stop timed out; no direct signal was sent",
                    file=stderr,
                )
                return 1
            print("paneglow: stopped", file=stdout)
            return 0
    except (OSError, BlockingIOError, launch_agent.LaunchAgentError):
        print("paneglow: autostart lifecycle is busy or unsafe", file=stderr)
        return 1


def _runtime_identity(paths: RuntimePaths, *, status_poll_ms: int,
                      allow_stale: bool = False) -> tuple[InstanceIdentity, dict[str, Any]]:
    transition = _LifetimeLock(paths.transition_lock_path)
    try:
        transition.acquire()
    except AlreadyRunning as error:
        raise RuntimeDataError("daemon identity is transitioning") from error
    except (OSError, RuntimeDataError) as error:
        raise RuntimeDataError("identity transition lock is unsafe") from error
    try:
        if not _lock_is_held(paths.lock_path):
            raise RuntimeDataError("daemon is not running")
        identity = _read_pid(paths.pid_path)
        snapshot = _read_snapshot(
            paths.snapshot_path,
            status_poll_ms=status_poll_ms,
            allow_stale=allow_stale,
        )
        if not snapshot["running"] or snapshot["pid"] != identity.pid \
                or snapshot["instance_id"] != identity.instance_id:
            raise RuntimeDataError("runtime identity mismatch")
        return identity, snapshot
    finally:
        transition.close()


def _cmd_status(paths: RuntimePaths, *, stdout: TextIO | None = None,
                stderr: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    cfg, _warnings = _load_config(paths)
    try:
        held = _lock_is_held(paths.lock_path)
    except RuntimeDataError:
        print("daemon      invalid runtime lock", file=stderr)
        return 1
    if not held:
        print("daemon      stopped", file=stdout)
        return 0
    try:
        identity, snapshot = _runtime_identity(
            paths, status_poll_ms=getattr(cfg, "status_poll_ms", 1000)
        )
    except RuntimeDataError:
        print("daemon      running, but runtime snapshot is unavailable", file=stderr)
        return 1

    print(f"daemon      running (pid {identity.pid})", file=stdout)
    pad = snapshot["pad"]
    if pad["connected"] and pad["status_verified"]:
        details = ["connected", str(pad["transport"]), str(pad["version"] or "unknown")]
        details.append(f"layer {pad['layer_index']}")
        print(f"pad         {' | '.join(details)}", file=stdout)
    else:
        print(f"pad         unavailable ({pad['error_code'] or 'unknown'})", file=stdout)
    frontmost = snapshot["frontmost"]
    front_label = frontmost["bundle_id"] if frontmost["ok"] else "unknown"
    print(f"owner       {snapshot['owner']} (frontmost: {front_label})", file=stdout)
    scan = snapshot["session_scan"]
    trust = "authoritative" if scan["authoritative"] else "partial"
    print(f"sessions    {scan['count']} ({trust})", file=stdout)
    for diagnostic in scan["diagnostics"]:
        print(f"            diagnostic: {diagnostic}", file=stdout)

    reason_labels = {
        "empty": "empty slot",
        "no_hook": "dim: no hook state",
        "state": "state",
        "working_timeout": "dim: working timed out",
        "done_faded": "dim: done faded",
    }
    for index, slot in enumerate(snapshot["slots"], start=1):
        session_id = slot["session_id"] or "-"
        state = slot["effective_state"] or "dim"
        reason = reason_labels[slot["reason"]]
        print(f"A{index}          {state:<8} {session_id} ({reason})", file=stdout)
    ambient = snapshot["zones"]["ambient"]
    colour = "off" if ambient["color"] is None else f"#{ambient['color']:06X}"
    effect = ambient["effect"] or "off"
    print(f"border      {colour} {effect} ({ambient['reason']})", file=stdout)
    if snapshot["last_input_result"] is not None:
        print(f"last input  {snapshot['last_input_result']}", file=stdout)
    return 0


def _valid_trace_bounds(seconds: object, max_events: object) -> bool:
    return (
        not isinstance(seconds, bool)
        and isinstance(seconds, (int, float))
        and math.isfinite(float(seconds))
        and 0 < float(seconds) <= 10
        and type(max_events) is int
        and 1 <= max_events <= 64
    )


def _cmd_trace_input(paths: RuntimePaths, seconds: float, max_events: int, *,
                     stdout: TextIO | None = None,
                     stderr: TextIO | None = None) -> int:
    """Request one bounded input trace from the already-running daemon."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if not _valid_trace_bounds(seconds, max_events):
        print(
            "paneglow: trace bounds require 0 < seconds <= 10 and 1 <= max-events <= 64",
            file=stderr,
        )
        return 2
    trace_lock = _TraceLock(paths.trace_lock_path)
    try:
        trace_lock.acquire()
    except AlreadyRunning:
        print("paneglow: another input trace is already running", file=stderr)
        return 1
    except (OSError, RuntimeDataError):
        print("paneglow: trace runtime state is unsafe", file=stderr)
        return 1

    request_id = uuid.uuid4().hex
    instance_id = ""
    request_created_at = time.time()
    status_poll_ms = 1000
    try:
        try:
            cfg, _warnings = _load_config(paths)
            status_poll_ms = getattr(cfg, "status_poll_ms", 1000)
            identity, _snapshot = _runtime_identity(
                paths, status_poll_ms=status_poll_ms
            )
            instance_id = identity.instance_id
            _cleanup_stale_trace_path(
                paths.trace_request_path, ttl=_TRACE_REQUEST_TTL_SECONDS
            )
            _cleanup_stale_trace_path(
                paths.trace_result_path, ttl=_TRACE_RESULT_TTL_SECONDS
            )
            if _trace_path_metadata(paths.trace_active_path) is not None:
                active_file = _read_private_trace_json(paths.trace_active_path)
                active_request = _validate_trace_request(active_file.value)
                if active_request["instance_id"] == instance_id:
                    raise RuntimeDataError("trace capture is active")
                if not _unlink_trace_inode(paths.trace_active_path, active_file):
                    raise RuntimeDataError("trace active marker changed")
            if _trace_path_metadata(paths.trace_request_path) is not None:
                raise RuntimeDataError("trace mailbox is occupied")
            # Under the sole client lock, consume a completed result abandoned
            # by a client that exited before reading it. Never replace it blind.
            if _trace_path_metadata(paths.trace_result_path) is not None:
                prior_file = _read_private_trace_json(paths.trace_result_path)
                _validate_trace_result(prior_file.value)
                if not _unlink_trace_inode(paths.trace_result_path, prior_file):
                    raise RuntimeDataError("prior trace result changed")
            duration_ms = max(1, int(math.ceil(seconds * 1000.0)))
            request = _validate_trace_request({
                "schema_version": _SCHEMA_VERSION,
                "request_id": request_id,
                "instance_id": instance_id,
                "created_at": request_created_at,
                "duration_ms": duration_ms,
                "max_events": max_events,
            })
            _atomic_create_trace_json(paths.trace_request_path, request)
        except (OSError, RuntimeDataError):
            print("paneglow: could not start a verified input trace", file=stderr)
            return 1

        deadline = time.monotonic() + seconds + _TRACE_GRACE_SECONDS
        while True:
            try:
                if _trace_path_metadata(paths.trace_result_path) is not None:
                    trace_file = _read_private_trace_json(paths.trace_result_path)
                    result = _validate_trace_result(trace_file.value)
                    if result["request_id"] != request_id \
                            or result["instance_id"] != instance_id \
                            or trace_file.modified_at + 1.0 < request_created_at \
                            or result["duration_ms"] > duration_ms \
                            or len(result["events"]) > max_events \
                            or (result["truncated"]
                                and len(result["events"]) != max_events):
                        raise RuntimeDataError("trace result identity is invalid")
                    public = _trace_public_result(result)
                    if not _unlink_trace_inode(paths.trace_result_path, trace_file):
                        raise RuntimeDataError("trace result changed while reading")
                    _unlink_trace_if_owned(
                        paths.trace_request_path,
                        request_id=request_id,
                        instance_id=instance_id,
                    )
                    print(
                        json.dumps(
                            public,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        file=stdout,
                    )
                    return 0
                current, _snapshot = _runtime_identity(
                    paths, status_poll_ms=status_poll_ms, allow_stale=True
                )
                if current.instance_id != instance_id:
                    raise RuntimeDataError("daemon instance changed")
            except (OSError, RuntimeDataError):
                print("paneglow: input trace did not complete safely", file=stderr)
                return 1
            if time.monotonic() >= deadline:
                print("paneglow: input trace timed out", file=stderr)
                return 1
            time.sleep(_TRACE_POLL_SECONDS)
    finally:
        if instance_id:
            try:
                _unlink_trace_if_owned(
                    paths.trace_request_path,
                    request_id=request_id,
                    instance_id=instance_id,
                )
                _unlink_trace_if_owned(
                    paths.trace_result_path,
                    request_id=request_id,
                    instance_id=instance_id,
                    result=True,
                )
                # Once the verified daemon is gone or has a new identity, its
                # old in-memory capture cannot still own this active marker.
                cleanup_active = not _lock_is_held(paths.lock_path)
                if not cleanup_active:
                    try:
                        current, _snapshot = _runtime_identity(
                            paths,
                            status_poll_ms=status_poll_ms,
                            allow_stale=True,
                        )
                    except RuntimeDataError:
                        current = None
                    cleanup_active = current is not None \
                        and current.instance_id != instance_id
                if cleanup_active:
                    _unlink_trace_if_owned(
                        paths.trace_active_path,
                        request_id=request_id,
                        instance_id=instance_id,
                    )
            except BaseException:
                pass
        trace_lock.close()


def _hook_command() -> str:
    return shlex.join((*_absolute_command_prefix(), "hook"))


def _read_settings(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if path.is_symlink() or not path.is_file():
        raise RuntimeDataError("Claude settings path is unsafe")
    raw = path.read_bytes()
    if len(raw) > _MAX_RUNTIME_BYTES:
        raise RuntimeDataError("Claude settings file is too large")
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimeDataError("Claude settings JSON is malformed") from error
    if type(value) is not dict:
        raise RuntimeDataError("Claude settings must be an object")
    return value, raw


def _hook_entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def _entry_has_command(value: object, command: str) -> bool:
    if type(value) is not dict:
        return False
    hooks = value.get("hooks")
    if type(hooks) is not list:
        return False
    return any(
        type(item) is dict
        and item.get("type") == "command"
        and item.get("command") == command
        for item in hooks
    )


def _is_paneglow_hook_command(value: object) -> bool:
    """Recognize only the command shape emitted by this installer."""
    if type(value) is not str:
        return False
    try:
        arguments = shlex.split(value)
    except ValueError:
        return False
    return (
        len(arguments) == 4
        and Path(arguments[0]).is_absolute()
        and arguments[1:] == ["-m", "paneglow.cli", "hook"]
    )


def _generated_hook_entry(value: object, command: str | None = None) -> bool:
    """Match the exact standalone entry Paneglow owns and may migrate."""
    if type(value) is not dict or set(value) != {"hooks"}:
        return False
    hooks = value.get("hooks")
    if type(hooks) is not list or len(hooks) != 1:
        return False
    hook = hooks[0]
    if type(hook) is not dict or set(hook) != {"type", "command"} \
            or hook.get("type") != "command":
        return False
    installed = hook.get("command")
    if command is not None:
        return installed == command
    return _is_paneglow_hook_command(installed)


def _normalize_hook_entries(
    entries: list[object], command: str
) -> tuple[list[object], bool]:
    """Migrate and deduplicate only standalone entries owned by Paneglow."""
    normalized: list[object] = []
    installed = False
    changed = False
    canonical = _hook_entry(command)
    for entry in entries:
        if not _generated_hook_entry(entry):
            normalized.append(entry)
            continue
        if installed:
            changed = True
            continue
        normalized.append(canonical)
        installed = True
        if entry != canonical:
            changed = True
    if not installed:
        normalized.append(canonical)
        changed = True
    return normalized, changed


def _hooks_installed(settings: object, command: str) -> bool:
    if type(settings) is not dict or type(settings.get("hooks")) is not dict:
        return False
    hooks = settings["hooks"]
    for event in _HOOK_EVENTS:
        entries = hooks.get(event)
        if type(entries) is not list \
                or not any(
                    _generated_hook_entry(entry, command) for entry in entries
                ):
            return False
    return True


def _cmd_install_hooks(paths: RuntimePaths, *, stdout: TextIO | None = None,
                       stderr: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        settings, original = _read_settings(paths.claude_settings_path)
        command = _hook_command()
        hooks = settings.get("hooks")
        if hooks is None:
            hooks = {}
        if type(hooks) is not dict:
            raise RuntimeDataError("Claude hooks setting must be an object")

        changed = False
        merged = dict(hooks)
        for event in _HOOK_EVENTS:
            existing = merged.get(event, [])
            if type(existing) is not list:
                raise RuntimeDataError("Claude hook event must be a list")
            entries, event_changed = _normalize_hook_entries(
                list(existing), command
            )
            changed = changed or event_changed
            merged[event] = entries
        if not changed:
            print("paneglow: hooks already installed", file=stdout)
            return 0

        updated = dict(settings)
        updated["hooks"] = merged
        if original is not None:
            backup = paths.claude_settings_path.with_name(
                paths.claude_settings_path.name + ".paneglow.bak"
            )
            _atomic_write_bytes(backup, original)
        _atomic_write_json(paths.claude_settings_path, updated)
    except (OSError, RuntimeDataError):
        print("paneglow: hooks were not installed; settings are unchanged", file=stderr)
        return 1
    print(f"paneglow: installed {_SLOT_COUNT + 5} hooks", file=stdout)
    return 0


def _doctor_hooks(paths: RuntimePaths, command: str, stdout: TextIO) -> bool:
    try:
        settings, _raw = _read_settings(paths.claude_settings_path)
    except (OSError, RuntimeDataError):
        print("[FAIL] Claude settings are unreadable", file=stdout)
        return False
    if _hooks_installed(settings, command):
        print("[PASS] all 11 Claude hooks are installed", file=stdout)
        return True
    print("[FAIL] one or more Claude hooks are missing", file=stdout)
    return False


def _doctor_desktop_sessions(paths: RuntimePaths, stdout: TextIO) -> bool:
    """Check live Claude sessions and their unambiguous Desktop mappings."""
    from paneglow import deeplink, sessions

    if paths.claude_sessions_dir.is_symlink():
        print("[FAIL] Claude session directory is unsafe", file=stdout)
        return False
    try:
        snapshot = sessions.scan(root=paths.claude_sessions_dir)
    except Exception:
        print("[FAIL] live Claude session scan is unavailable", file=stdout)
        return False
    if not snapshot.authoritative:
        print("[FAIL] live Claude session scan is not authoritative", file=stdout)
        return False
    print(
        f"[PASS] live Claude session scan is authoritative ({len(snapshot.sessions)})",
        file=stdout,
    )
    if paths.mapping_dir.is_symlink() or not paths.mapping_dir.is_dir():
        print("[FAIL] Claude Desktop mapping directory is missing or unsafe",
              file=stdout)
        return False
    if not snapshot.sessions:
        print(
            "[WARN] no live Claude session; deep-link mapping was not exercised",
            file=stdout,
        )
        return True

    try:
        mapped = sum(
            deeplink.local_id_for(session.session_id, (paths.mapping_dir,)) is not None
            for session in snapshot.sessions
        )
    except Exception:
        print("[FAIL] Claude Desktop deep-link mapping check is unavailable",
              file=stdout)
        return False
    if mapped == len(snapshot.sessions):
        print(f"[PASS] Claude Desktop deep-link mappings resolve ({mapped})",
              file=stdout)
        return True
    print(
        f"[FAIL] Claude Desktop deep-link mappings unresolved "
        f"({len(snapshot.sessions) - mapped})",
        file=stdout,
    )
    return False


def _doctor_running(snapshot: Mapping[str, Any], stdout: TextIO) -> bool:
    pad = snapshot["pad"]
    good = bool(
        pad["connected"]
        and pad["status_verified"]
        and pad["transport"] in _TRANSPORTS
        and pad["layer_index"] == 1
    )
    if good:
        print(
            f"[PASS] daemon pad snapshot: {pad['transport']} layer {pad['layer_index']}",
            file=stdout,
        )
        return True
    print(f"[FAIL] daemon pad snapshot: {pad['error_code'] or 'unverified'}", file=stdout)
    return False


def _doctor_stopped(stdout: TextIO) -> bool:
    # Importing pad loads ctypes declarations and may touch macOS frameworks;
    # status never calls this path, and doctor calls it only while stopped.
    from paneglow import pad as pad_module

    device = None
    healthy = False
    try:
        device = pad_module.Pad.open()
        if device is None:
            print("[FAIL] Codex Micro is unavailable", file=stdout)
        else:
            reply = device.status(timeout=3.0)
            result = reply.get("result") if isinstance(reply, dict) else None
            transport = getattr(device, "transport", None)
            layer = result.get("layer_index") if isinstance(result, dict) else None
            if transport not in _TRANSPORTS or layer != 1:
                print(
                    "[FAIL] device.status did not return a trusted transport/layer 1",
                    file=stdout,
                )
            else:
                print(f"[PASS] fresh pad round-trip: {transport} layer {layer}",
                      file=stdout)
                healthy = True
    except BaseException:
        print("[FAIL] fresh pad round-trip failed", file=stdout)
    finally:
        if device is not None:
            try:
                # Doctor is observational.  Flushing/disposing the channel is
                # required, but clearing either LED zone would mutate the pad.
                device.close(
                    flush_seconds=1.0,
                    turn_off_keys=False,
                    turn_off_ambient=False,
                )
            except BaseException:
                print("[FAIL] pad close/flush failed", file=stdout)
                healthy = False
    return healthy


def _cmd_doctor(paths: RuntimePaths, *, stdout: TextIO | None = None,
                stderr: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    cfg, warnings = _load_config(paths)
    ok = True
    for category in dict.fromkeys(
            _config_warning_category(warning) for warning in warnings):
        print(f"[WARN] config: {category}", file=stdout)

    try:
        command = _hook_command()
    except (OSError, RuntimeError):
        print("[FAIL] Claude hook command is unavailable", file=stdout)
        ok = False
    else:
        ok = _doctor_hooks(paths, command, stdout) and ok
    ok = _doctor_desktop_sessions(paths, stdout) and ok

    try:
        held = _lock_is_held(paths.lock_path)
    except RuntimeDataError:
        print("[FAIL] daemon lock is unsafe", file=stdout)
        return 1
    if held:
        try:
            _identity, snapshot = _runtime_identity(
                paths, status_poll_ms=getattr(cfg, "status_poll_ms", 1000)
            )
        except RuntimeDataError:
            print("[FAIL] running daemon snapshot is unavailable or stale", file=stdout)
            ok = False
        else:
            ok = _doctor_running(snapshot, stdout) and ok
    else:
        ok = _doctor_stopped(stdout) and ok
    return 0 if ok else 1


def _hook_main() -> int:
    # Redirect before imports: an unexpected import-time print must not corrupt
    # Claude's hook protocol.  BaseException is intentional on this one path.
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            paths = RuntimePaths.from_env()
            from paneglow import hook

            _private_directory(paths.state_dir)
            hook.run(sys.stdin, paths.state_dir)
    except BaseException:
        pass
    return 0


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="paneglow",
        description=(
            "Monitor and open parallel Claude Code Desktop sessions "
            "from Codex Micro on macOS."
        ),
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("hook", help="consume one Claude hook event (always succeeds)")
    commands.add_parser("run", help="run the daemon in the foreground")
    start = commands.add_parser(
        "start", help="start the daemon using its installed lifecycle owner"
    )
    start.add_argument("--timeout", type=float, default=5.0)
    stop = commands.add_parser(
        "stop", help="stop the verified daemon without force killing"
    )
    stop.add_argument("--timeout", type=float, default=5.0)
    commands.add_parser("status", help="read the daemon runtime snapshot")
    trace = commands.add_parser(
        "trace-input", help="capture a bounded, privacy-safe input trace"
    )
    trace.add_argument("--seconds", type=float, default=5.0)
    trace.add_argument("--max-events", type=int, default=32)
    commands.add_parser("doctor", help="check configuration and integration")
    commands.add_parser("install-hooks", help="merge all Claude hook events")
    autostart = commands.add_parser(
        "autostart", help="manage the per-user login LaunchAgent"
    )
    autostart_commands = autostart.add_subparsers(
        dest="autostart_command", required=True
    )
    autostart_install = autostart_commands.add_parser(
        "install", help="install and start login autostart"
    )
    autostart_install.add_argument("--timeout", type=float, default=10.0)
    autostart_commands.add_parser(
        "status", help="inspect login autostart without changing it"
    )
    autostart_uninstall = autostart_commands.add_parser(
        "uninstall", help="stop and remove login autostart"
    )
    autostart_uninstall.add_argument("--timeout", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Keep this before argparse and every Paneglow import.  Extra arguments are
    # ignored as another fail-safe: a bad hook command must not block a turn.
    if arguments and arguments[0] == "hook":
        return _hook_main()

    parser = _build_parser()
    if not arguments:
        parser.print_help()
        return 0
    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code)
    if parsed.command in {"start", "stop"} and (
        not math.isfinite(parsed.timeout) or parsed.timeout < 0
    ):
        print("paneglow: --timeout must be a finite non-negative number",
              file=sys.stderr)
        return 2
    if parsed.command == "trace-input" and not _valid_trace_bounds(
        parsed.seconds, parsed.max_events
    ):
        print(
            "paneglow: trace bounds require 0 < seconds <= 10 and 1 <= max-events <= 64",
            file=sys.stderr,
        )
        return 2
    if parsed.command == "autostart" \
            and parsed.autostart_command in {"install", "uninstall"} \
            and (not math.isfinite(parsed.timeout) or parsed.timeout < 0):
        print("paneglow: --timeout must be a finite non-negative number",
              file=sys.stderr)
        return 2

    paths = RuntimePaths.from_env()
    if parsed.command == "run":
        return _cmd_run(paths)
    if parsed.command == "start":
        return _cmd_start(paths, parsed.timeout)
    if parsed.command == "stop":
        return _cmd_stop(paths, parsed.timeout)
    if parsed.command == "status":
        return _cmd_status(paths)
    if parsed.command == "trace-input":
        return _cmd_trace_input(paths, parsed.seconds, parsed.max_events)
    if parsed.command == "doctor":
        return _cmd_doctor(paths)
    if parsed.command == "install-hooks":
        return _cmd_install_hooks(paths)
    if parsed.command == "autostart":
        if parsed.autostart_command == "install":
            return _cmd_autostart_install(paths, parsed.timeout)
        if parsed.autostart_command == "status":
            return _cmd_autostart_status(paths)
        if parsed.autostart_command == "uninstall":
            return _cmd_autostart_uninstall(paths, parsed.timeout)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
