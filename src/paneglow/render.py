"""Decides what to show and in what colour. Pure -- no hardware, no files."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar

from paneglow.state import AgentState

KEY_COUNT = 6

#: Factory values. Matching what the vendor uses for Codex keeps the eye honest.
PALETTE: dict[AgentState, int] = {
    AgentState.IDLE: 0xFFFFFF,
    AgentState.WORKING: 0x304FFE,
    AgentState.WAITING: 0xFF6D00,
    AgentState.DONE: 0x00FF4C,
    AgentState.ERROR: 0xFF0033,
}

#: Worth lighting the border for. Anything else on means the signal is dead.
_NOTABLE = (AgentState.WAITING, AgentState.ERROR)

_T = TypeVar("_T")


@dataclass(frozen=True)
class Session:
    session_id: str
    state: AgentState | None


def effective_state(state: AgentState | None, *, updated_at: float, now: float,
                    working_max_seconds: float) -> AgentState | None:
    """Return the display state without changing the stored source record.

    A missing stop event must not leave a session blue forever, so working ages
    into idle.  Nothing else expires: one live session is one lit key, and only
    the session process going away -- which drops it from
    :func:`paneglow.sessions.scan` -- turns a key off.
    """
    if state is AgentState.WORKING and max(0.0, now - updated_at) >= working_max_seconds:
        return AgentState.IDLE
    return state


def render_keys(sessions: list[Session]) -> list[int | None]:
    """Render six session colours in slot order. ``None`` means dark."""
    out: list[int | None] = [None] * KEY_COUNT
    for index, session in enumerate(sessions[:KEY_COUNT]):
        if session.state is not None:
            out[index] = PALETTE[session.state]
    return out


def alert_level(states: Iterable[AgentState | None]) -> str:
    """Return ``alert`` only when a hidden session needs attention."""
    return "alert" if any(state in _NOTABLE for state in states) else "normal"


def overflow(items: list[_T]) -> list[_T]:
    """Items that did not fit on the six keys."""
    return list(items[KEY_COUNT:])
