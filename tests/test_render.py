from paneglow.render import (
    Pane, KEY_COUNT, PALETTE, render_pane_view, overflow, underglow_for,
)
from paneglow.state import AgentState


def p(state=None, claude=True, tty="/dev/ttys0"):
    return Pane(tty=tty, is_claude=claude, state=state)


def test_always_six_slots():
    assert len(render_pane_view([])) == KEY_COUNT
    assert len(render_pane_view([p(AgentState.WORKING)])) == KEY_COUNT


def test_empty_slots_are_dark():
    out = render_pane_view([p(AgentState.WORKING)])
    assert out[0] == PALETTE[AgentState.WORKING]
    assert out[1:] == [None] * 5


def test_non_claude_pane_is_dark_but_occupies_a_slot():
    """It still takes a slot, because pressing the key must still jump there."""
    out = render_pane_view([p(claude=False), p(AgentState.WAITING)])
    assert out[0] is None
    assert out[1] == PALETTE[AgentState.WAITING]


def test_claude_without_state_is_dark():
    """Found via jobName, but its hook is not installed yet."""
    assert render_pane_view([p(state=None, claude=True)])[0] is None


def test_screen_order_is_preserved():
    out = render_pane_view([p(AgentState.IDLE), p(AgentState.ERROR), p(AgentState.DONE)])
    assert out[:3] == [PALETTE[AgentState.IDLE],
                       PALETTE[AgentState.ERROR],
                       PALETTE[AgentState.DONE]]


def test_seventh_pane_does_not_appear():
    panes = [p(AgentState.WORKING) for _ in range(7)]
    assert len(render_pane_view(panes)) == KEY_COUNT


def test_overflow_returns_panes_beyond_six():
    panes = [p(AgentState.WORKING) for _ in range(6)] + [p(AgentState.WAITING)]
    extra = overflow(panes)
    assert len(extra) == 1
    assert extra[0].state is AgentState.WAITING


def test_overflow_is_empty_when_six_or_fewer():
    assert overflow([p() for _ in range(6)]) == []


def test_underglow_lights_on_waiting():
    assert underglow_for([AgentState.WORKING, AgentState.WAITING]) == PALETTE[AgentState.WAITING]


def test_underglow_lights_on_error():
    assert underglow_for([AgentState.IDLE, AgentState.ERROR]) == PALETTE[AgentState.ERROR]


def test_underglow_prefers_waiting_over_error():
    assert underglow_for([AgentState.ERROR, AgentState.WAITING]) == PALETTE[AgentState.WAITING]


def test_underglow_is_off_when_quiet():
    """done/working/idle are not worth announcing -- always on kills the signal."""
    assert underglow_for([AgentState.WORKING, AgentState.DONE, AgentState.IDLE]) is None
    assert underglow_for([]) is None
