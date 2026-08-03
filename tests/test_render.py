import pytest

from paneglow.render import (
    Pane, Session, KEY_COUNT, PALETTE, effective_state, render_keys,
    render_pane_view, overflow, alert_level, underglow_for,
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


def test_session_keys_take_palette_colours_and_keep_empty_slots_dark():
    out = render_keys([Session("one", AgentState.WAITING), Session("two", None)])
    assert out[:2] == [PALETTE[AgentState.WAITING], None]
    assert out[2:] == [None] * 4


def test_session_overflow_preserves_items_beyond_six():
    sessions = [Session(f"s{i}", AgentState.IDLE) for i in range(8)]
    assert [s.session_id for s in overflow(sessions)] == ["s6", "s7"]


def test_alert_level_only_fires_for_waiting_or_error():
    assert alert_level([None, AgentState.WORKING, AgentState.DONE]) == "normal"
    assert alert_level([AgentState.ERROR]) == "alert"
    assert alert_level([AgentState.WAITING]) == "alert"


@pytest.mark.parametrize("now", [100.0, 109.999, 90.0])
def test_working_stays_working_before_its_limit_and_across_clock_skew(now):
    assert effective_state(
        AgentState.WORKING, updated_at=100.0, now=now,
        working_max_seconds=10, done_fade_seconds=180,
    ) is AgentState.WORKING


@pytest.mark.parametrize("limit", [10, 0])
def test_working_becomes_idle_at_its_limit(limit):
    assert effective_state(
        AgentState.WORKING, updated_at=100.0, now=100.0 + limit,
        working_max_seconds=limit, done_fade_seconds=180,
    ) is AgentState.IDLE


def test_done_stays_visible_before_fade_then_turns_off_at_the_boundary():
    assert effective_state(
        AgentState.DONE, updated_at=100.0, now=109.999,
        working_max_seconds=900, done_fade_seconds=10,
    ) is AgentState.DONE
    assert effective_state(
        AgentState.DONE, updated_at=100.0, now=110.0,
        working_max_seconds=900, done_fade_seconds=10,
    ) is None


def test_zero_done_fade_turns_done_off_immediately():
    assert effective_state(
        AgentState.DONE, updated_at=100.0, now=100.0,
        working_max_seconds=900, done_fade_seconds=0,
    ) is None


@pytest.mark.parametrize("state", [AgentState.IDLE, AgentState.WAITING, AgentState.ERROR])
def test_non_transient_states_do_not_expire(state):
    assert effective_state(
        state, updated_at=0.0, now=1e9,
        working_max_seconds=0, done_fade_seconds=0,
    ) is state


def test_unknown_state_stays_unknown():
    assert effective_state(
        None, updated_at=0.0, now=1e9,
        working_max_seconds=0, done_fade_seconds=0,
    ) is None
