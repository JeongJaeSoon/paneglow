"""무엇을 어떤 색으로 보여줄지 계산한다. 하드웨어도 파일도 모르는 순수 함수."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from paneglow.state import AgentState, highest

KEY_COUNT = 6

#: 공장 정품 값. 벤더가 Codex 에 쓰는 색과 같아야 눈이 헷갈리지 않는다.
PALETTE: dict[AgentState, int] = {
    AgentState.IDLE: 0xFFFFFF,
    AgentState.WORKING: 0x304FFE,
    AgentState.WAITING: 0xFF6D00,
    AgentState.DONE: 0x00FF4C,
    AgentState.ERROR: 0xFF0033,
}

#: 테두리에 띄울 가치가 있는 상태. 나머지는 켜두면 신호가 죽는다.
_NOTABLE = (AgentState.WAITING, AgentState.ERROR)


@dataclass(frozen=True)
class Pane:
    tty: str
    is_claude: bool
    state: AgentState | None


def render_pane_view(panes: list[Pane]) -> list[int | None]:
    """화면 배치 순서대로 6키 색. None 은 소등."""
    out: list[int | None] = [None] * KEY_COUNT
    for i, pane in enumerate(panes[:KEY_COUNT]):
        if pane.is_claude and pane.state is not None:
            out[i] = PALETTE[pane.state]
    return out


def overflow(panes: list[Pane]) -> list[Pane]:
    """6키에 못 올라간 pane. 테두리 집계에 넣어야 사라지지 않는다."""
    return list(panes[KEY_COUNT:])


def underglow_for(states: Iterable[AgentState]) -> int | None:
    """화면 밖에서 나를 기다리는 것이 있으면 그 색, 없으면 None."""
    top = highest(states)
    return PALETTE[top] if top in _NOTABLE else None
