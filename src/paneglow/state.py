"""상태 어휘와 우선순위. 하드웨어도 파일도 모른다."""
from __future__ import annotations

from enum import Enum
from typing import Iterable


class AgentState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"
    WAITING = "waiting"


#: 높을수록 먼저 보여준다. waiting 이 최상위인 이유는 그것만이 사람을 기다리기 때문이다.
PRIORITY: dict[AgentState, int] = {
    AgentState.IDLE: 0,
    AgentState.WORKING: 1,
    AgentState.DONE: 2,
    AgentState.ERROR: 3,
    AgentState.WAITING: 4,
}


def highest(states: Iterable[AgentState]) -> AgentState | None:
    """가장 먼저 보여줘야 할 상태. 비어 있으면 None."""
    ranked = sorted(states, key=lambda s: PRIORITY[s], reverse=True)
    return ranked[0] if ranked else None
