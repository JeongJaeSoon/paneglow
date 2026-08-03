"""Decides what to show and in what colour. Pure -- no hardware, no files."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar

from paneglow.state import AgentState, highest

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
                    working_max_seconds: float,
                    done_fade_seconds: float) -> AgentState | None:
    """Return the display state without changing the stored source record.

    A missing stop event must not leave a session blue forever, while waiting
    and error states must remain visible until a real event replaces them.
    """
    age = max(0.0, now - updated_at)
    if state is AgentState.WORKING and age >= working_max_seconds:
        return AgentState.IDLE
    if state is AgentState.DONE and age >= done_fade_seconds:
        return None
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


@dataclass(frozen=True)
class Pane:
    """Deprecated iTerm pane model kept until the compatibility cleanup."""

    tty: str
    is_claude: bool
    state: AgentState | None


def render_pane_view(panes: list[Pane]) -> list[int | None]:
    """Six key colours in on-screen order. None means dark."""
    out: list[int | None] = [None] * KEY_COUNT
    for i, pane in enumerate(panes[:KEY_COUNT]):
        if pane.is_claude and pane.state is not None:
            out[i] = PALETTE[pane.state]
    return out


def overflow(items: list[_T]) -> list[_T]:
    """Items that did not fit on the six keys."""
    return list(items[KEY_COUNT:])


def underglow_for(states: Iterable[AgentState]) -> int | None:
    """The colour for something out of sight that wants me -- waiting or errored.
    None when nothing does."""
    top = highest(states)
    return PALETTE[top] if top in _NOTABLE else None
