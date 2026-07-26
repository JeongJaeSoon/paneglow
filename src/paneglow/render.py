"""Decides what to show and in what colour. Pure -- no hardware, no files."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

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


@dataclass(frozen=True)
class Pane:
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


def overflow(panes: list[Pane]) -> list[Pane]:
    """Panes that did not fit on the six keys. Feed these to the border tally or
    a seventh pane becomes invisible everywhere."""
    return list(panes[KEY_COUNT:])


def underglow_for(states: Iterable[AgentState]) -> int | None:
    """The colour for something out of sight that wants me -- waiting or errored.
    None when nothing does."""
    top = highest(states)
    return PALETTE[top] if top in _NOTABLE else None
