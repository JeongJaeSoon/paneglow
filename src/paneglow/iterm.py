"""iTerm2 어댑터.

pane 발견은 iTerm2 가 한다 — jobName 으로 Claude Code 가 떠 있는지 알 수 있어서
훅이 아직 안 붙은 세션도 자리를 차지한다. 훅은 '상태' 에만 필요하다.
"""
from __future__ import annotations

import re
from typing import Any

from paneglow.render import Pane

#: Claude Code 는 jobName 이 버전 문자열로 나온다. 이 표기가 바뀌면 판별이 깨진다.
_VERSION = re.compile(r"^\d+\.\d+\.\d+")


def is_claude_job(job_name: str | None) -> bool:
    return bool(job_name) and bool(_VERSION.match(job_name))


def _place(node: Any, leaf: type,
           x: float, y: float, w: float, h: float) -> list[tuple[float, float, Any]]:
    """분할 트리를 (세로위치, 가로위치, 세션) 목록으로 편다.

    깊이 우선 순회만으로는 화면 순서가 나오지 않는다. iTerm2 는 열을 먼저
    묶기 때문에 2x2 를 훑으면 좌상·좌하·우상·우하 순으로 나오는데, 사람은
    좌상·우상·좌하·우하로 읽는다. 그래서 좌표를 세어야 한다.
    """
    if isinstance(node, leaf):
        return [(y, x, node)]
    children = list(getattr(node, "children", []))
    if not children:
        return []
    # ponytail: 분할은 균등하다고 본다. 좌표 API 가 없어 크기는 알 수 없지만,
    # 순서만 필요하므로 구분선을 옮겨도 결과는 같다.
    vertical = getattr(node, "vertical", True)   # 구분선이 세로 = 좌우 배치
    out: list[tuple[float, float, Any]] = []
    step = (w if vertical else h) / len(children)
    for i, child in enumerate(children):
        if vertical:
            out += _place(child, leaf, x + i * step, y, step, h)
        else:
            out += _place(child, leaf, x, y + i * step, w, step)
    return out


def flatten(node: Any, leaf: type) -> list:
    """분할 트리를 읽기 순서(위→아래, 좌→우)로 편다."""
    placed = _place(node, leaf, 0.0, 0.0, 1.0, 1.0)
    return [session for _, _, session in sorted(placed, key=lambda t: (t[0], t[1]))]


async def _pane_of(session) -> Pane:
    tty = await session.async_get_variable("tty")
    job = await session.async_get_variable("jobName")
    return Pane(tty=tty or "", is_claude=is_claude_job(job), state=None)


def _current_window(app):
    """가장 최근 활성 창 하나만 다룬다. 다중 창은 범위 밖이다."""
    return app.current_terminal_window


async def current_tab_panes(app) -> list[Pane]:
    import iterm2
    window = _current_window(app)
    if window is None:
        return []
    sessions = flatten(window.current_tab.root, leaf=iterm2.Session)
    return [await _pane_of(s) for s in sessions]


async def tab_count(app) -> int:
    window = _current_window(app)
    return len(window.tabs) if window else 0


async def live_ttys(app) -> set[str]:
    """지금 살아 있는 pane 의 tty 전부. 세션 생존 판정의 기준이다."""
    import iterm2
    out: set[str] = set()
    for window in app.windows:
        for tab in window.tabs:
            for session in flatten(tab.root, leaf=iterm2.Session):
                tty = await session.async_get_variable("tty")
                if tty:
                    out.add(tty)
    return out


async def focus_pane(app, tty: str, bring_to_front: bool = False) -> bool:
    """그 tty 의 pane 을 고른다. 못 찾으면 아무것도 하지 않는다."""
    import iterm2
    for window in app.windows:
        for tab in window.tabs:
            for session in flatten(tab.root, leaf=iterm2.Session):
                if await session.async_get_variable("tty") == tty:
                    await session.async_activate(
                        select_tab=True, order_window_front=bring_to_front)
                    return True
    return False
