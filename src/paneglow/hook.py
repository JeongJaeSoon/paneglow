"""Translate Claude hook events into Paneglow session records.

Hook input is an external trust boundary. Unknown or malformed events must not
change state, and processing failures must never interrupt a Claude turn.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TextIO

from paneglow import store
from paneglow.state import AgentState
from paneglow.store import SessionRecord

_WORKING_EVENTS = frozenset(
    {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}
)
_ERROR_EVENTS = frozenset(
    {"StopFailure", "PostToolUseFailure", "PermissionDenied"}
)
# A denylist would silently turn newly introduced notification types into a
# human-action state. Only the two observed interactive types are actionable.
_WAITING_NOTIFICATIONS = frozenset({"permission_prompt", "agent_needs_input"})


def classify(event: object) -> AgentState | None:
    """Return the state represented by *event*, or ``None`` for no change."""
    if not isinstance(event, dict) or "agent_type" in event:
        return None

    name = event.get("hook_event_name")
    if not isinstance(name, str):
        return None
    if name == "SessionStart":
        return AgentState.IDLE
    if name in _WORKING_EVENTS:
        # AskUserQuestion blocks on a dialog the moment PreToolUse fires, so
        # it is a human-input state, not work. Elicitations that happen inside
        # other tool calls emit no hook and cannot be classified here.
        if name == "PreToolUse" and event.get("tool_name") == "AskUserQuestion":
            return AgentState.WAITING
        return AgentState.WORKING
    if name == "Stop":
        return AgentState.DONE
    if name in _ERROR_EVENTS:
        return AgentState.ERROR
    if name == "Notification":
        notification_type = event.get("notification_type")
        if (
            isinstance(notification_type, str)
            and notification_type in _WAITING_NOTIFICATIONS
        ):
            return AgentState.WAITING
    return None


def record_from(event: object, rev: int, now: float) -> SessionRecord | None:
    """Build a record from a state-changing top-level session event.

    Claude subagent events can reuse ordinary hook event names while carrying a
    separate session id. Presence of ``agent_type`` is therefore authoritative,
    regardless of the field's value.
    """
    if not isinstance(event, dict) or "agent_type" in event:
        return None

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    state = classify(event)
    if state is None:
        return None

    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        cwd = ""
    return SessionRecord(
        session_id=session_id,
        cwd=cwd,
        state=state,
        rev=rev,
        updated_at=now,
    )


def _idle_promotion(event: object, state_dir: Path, rev: int,
                    now: float) -> SessionRecord | None:
    """Turn an idle_prompt into waiting, but only for a working session.

    Elicitations inside tool calls (credential prompts, browser pickers) emit
    no hook of their own, so a blocked session sits at working until Claude's
    60s idle notification arrives. A completed turn already moved to done via
    Stop, which keeps finished sessions from lighting up as waiting.
    """
    if not isinstance(event, dict) or "agent_type" in event:
        return None
    if event.get("hook_event_name") != "Notification":
        return None
    if event.get("notification_type") != "idle_prompt":
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    for current in store.read_all(state_dir):
        if current.session_id == session_id:
            if current.state is not AgentState.WORKING:
                return None
            return SessionRecord(
                session_id=session_id,
                cwd=current.cwd,
                state=AgentState.WAITING,
                rev=rev,
                updated_at=now,
            )
    return None


def run(stdin: TextIO, state_dir: Path) -> int:
    """Consume one stdin JSON event and persist it, always returning success.

    The hook is on Claude's critical path. Input, classification, clock, path,
    and storage failures are deliberately fail-closed and produce no output.
    """
    try:
        event = json.load(stdin)
        record = record_from(event, rev=time.time_ns(), now=time.time())
        if record is None:
            record = _idle_promotion(
                event, state_dir, rev=time.time_ns(), now=time.time()
            )
        if record is not None:
            store.write(record, state_dir)
    except Exception:
        pass
    return 0
