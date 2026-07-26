"""iTerm2 adapter.

iTerm2 does the discovering -- jobName tells us Claude Code is running, so a
session whose hook is not installed yet still occupies a key. Hooks are only
needed for the *state*.
"""
from __future__ import annotations

import re
from typing import Any

from paneglow.render import Pane

#: Claude Code reports its version string as jobName. If that changes, this breaks.
_VERSION = re.compile(r"^\d+\.\d+\.\d+")


def is_claude_job(job_name: str | None) -> bool:
    return bool(job_name) and bool(_VERSION.match(job_name))


def _extent(node: Any, leaf: type, horizontal: bool) -> int:
    """Size of a subtree in character cells, along one axis."""
    if isinstance(node, leaf):
        grid = node.grid_size
        return grid.width if horizontal else grid.height
    children = list(getattr(node, "children", []))
    if not children:
        return 0
    sizes = [_extent(c, leaf, horizontal) for c in children]
    # Children laid out along this axis add up; across it they overlap.
    return sum(sizes) if getattr(node, "vertical", True) == horizontal else max(sizes)


def _weights(children: list, leaf: type, horizontal: bool) -> list[int]:
    """Relative sizes for splitting the space. Falls back to equal shares when
    the tree carries no grid_size (unit tests, or an API that stops exposing it)."""
    try:
        sizes = [_extent(c, leaf, horizontal) for c in children]
    except AttributeError:
        return [1] * len(children)
    return sizes if all(s > 0 for s in sizes) else [1] * len(children)


def _place(node: Any, leaf: type,
           x: float, y: float, w: float, h: float) -> list[tuple[float, float, Any]]:
    """Flatten a split tree into (top, left, session) triples.

    A depth-first walk alone does not give on-screen order. iTerm2 groups by
    column, so walking a 2x2 yields top-left, bottom-left, top-right, bottom-right
    while a person reads top-left, top-right, bottom-left, bottom-right.

    Splits are weighted by actual cell counts, not assumed even. Sibling columns
    can be divided at different heights, and then a pane in one column really
    does sit below a pane in the next -- measured: a left column split 69/23
    against a right column split 46/46 puts the right-bottom pane above the
    left-bottom one. Even shares get that pair backwards.
    """
    if isinstance(node, leaf):
        return [(y, x, node)]
    children = list(getattr(node, "children", []))
    if not children:
        return []

    vertical = getattr(node, "vertical", True)   # vertical divider = side by side
    weights = _weights(children, leaf, vertical)
    total = sum(weights)

    out: list[tuple[float, float, Any]] = []
    offset = 0.0
    for child, weight in zip(children, weights):
        span = (w if vertical else h) * weight / total
        if vertical:
            out += _place(child, leaf, x + offset, y, span, h)
        else:
            out += _place(child, leaf, x, y + offset, w, span)
        offset += span
    return out


def flatten(node: Any, leaf: type) -> list:
    """Flatten a split tree into reading order: top to bottom, left to right."""
    placed = _place(node, leaf, 0.0, 0.0, 1.0, 1.0)
    return [session for _, _, session in sorted(placed, key=lambda t: (t[0], t[1]))]


async def _pane_of(session) -> Pane:
    tty = await session.async_get_variable("tty")
    job = await session.async_get_variable("jobName")
    return Pane(tty=tty or "", is_claude=is_claude_job(job), state=None)


def _current_window(app):
    """Only the most recently active window. Multiple windows are out of scope."""
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
    """Every tty currently alive. This is what decides whether a session lives."""
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
    """Select the pane on that tty. Does nothing if it is not found."""
    import iterm2
    for window in app.windows:
        for tab in window.tabs:
            for session in flatten(tab.root, leaf=iterm2.Session):
                if await session.async_get_variable("tty") == tty:
                    await session.async_activate(
                        select_tab=True, order_window_front=bring_to_front)
                    return True
    return False
