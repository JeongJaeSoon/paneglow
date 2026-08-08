"""Assign live Claude sessions to six stable hardware slots."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from paneglow.sessions import Session
from paneglow.state import PRIORITY, AgentState

COUNT = 6


def _finite_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def activity_times(sessions: Iterable[Session],
                   state_updated_at: Mapping[str, float]) -> dict[str, float]:
    """Map live session IDs to activity time, preferring hook state updates."""
    activity: dict[str, float] = {}
    for session in sessions:
        started_at = _finite_time(session.started_at)
        if started_at is None:
            continue
        updated_at = _finite_time(state_updated_at.get(session.session_id))
        activity[session.session_id] = (
            updated_at if updated_at is not None else started_at)
    return activity


def _normalise_live(live: Mapping[str, float]) -> dict[str, float]:
    normalised: dict[str, float] = {}
    for session_id, value in live.items():
        activity = _finite_time(value)
        if isinstance(session_id, str) and session_id and activity is not None:
            normalised[session_id] = activity
    return normalised


def _normalise_prev(prev: Sequence[str | None] | None,
                    live: Mapping[str, float]) -> list[str | None]:
    if prev is None or isinstance(prev, (str, bytes)) or not isinstance(prev, Sequence):
        values: list[object] = []
    else:
        values = list(prev[:COUNT])
    values.extend([None] * (COUNT - len(values)))

    seen: set[str] = set()
    out: list[str | None] = []
    for value in values[:COUNT]:
        if isinstance(value, str) and value in live and value not in seen:
            seen.add(value)
            out.append(value)
        else:
            out.append(None)
    return out


def _recent_ids(live: Mapping[str, float]) -> list[str]:
    return sorted(live, key=lambda session_id: (-live[session_id], session_id))


def _evict_rank(live: Mapping[str, float],
                states: Mapping[str, AgentState] | None):
    """Lowest rank loses its slot: a finished session before a merely quiet one.

    A finished session keeps its lit key, so ranking by activity alone would
    evict a live session that had simply been quiet for longer.
    """
    states = states or {}
    return lambda session_id: (
        0 if states.get(session_id) is AgentState.DONE else 1,
        live[session_id])


def _sticky(prev: Sequence[str | None] | None,
            live: Mapping[str, float],
            states: Mapping[str, AgentState] | None = None) -> list[str | None]:
    out = _normalise_prev(prev, live)
    held = {session_id for session_id in out if session_id is not None}
    incoming = [session_id for session_id in _recent_ids(live)
                if session_id not in held]
    rank = _evict_rank(live, states)

    for session_id in incoming:
        try:
            empty = out.index(None)
        except ValueError:
            first_out = min(
                range(COUNT),
                key=lambda index: (rank(out[index]), index),  # type: ignore[arg-type]
            )
            current = out[first_out]
            if current is not None and rank(session_id) > rank(current):
                out[first_out] = session_id
        else:
            out[empty] = session_id
    return out


def _ordered_recent(live: Mapping[str, float]) -> list[str | None]:
    ranked = _recent_ids(live)[:COUNT]
    return [*ranked, *([None] * (COUNT - len(ranked)))]


def _ordered_priority(live: Mapping[str, float],
                      states: Mapping[str, AgentState] | None) -> list[str | None]:
    states = states or {}

    def priority(session_id: str) -> int:
        state = states.get(session_id)
        return PRIORITY.get(state, 0) if isinstance(state, AgentState) else 0

    ranked = sorted(
        live,
        key=lambda session_id: (
            -priority(session_id), -live[session_id], session_id),
    )[:COUNT]
    return [*ranked, *([None] * (COUNT - len(ranked)))]


def assign(prev: Sequence[str | None] | None,
           live: Mapping[str, float],
           policy: str = "recent_sticky",
           states: Mapping[str, AgentState] | None = None) -> list[str | None]:
    """Assign live session IDs while preserving stable positions by default."""
    normalised_live = _normalise_live(live)
    if policy == "recent":
        return _ordered_recent(normalised_live)
    if policy == "priority":
        return _ordered_priority(normalised_live, states)
    return _sticky(prev, normalised_live, states)
