import pytest

from paneglow.render import (
    Session, KEY_COUNT, PALETTE, effective_state, render_keys,
    overflow, alert_level,
)
from paneglow.state import AgentState


def test_session_keys_take_palette_colours_and_keep_empty_slots_dark():
    out = render_keys([Session("one", AgentState.WAITING), Session("two", None)])
    assert out[:2] == [PALETTE[AgentState.WAITING], None]
    assert out[2:] == [None] * 4


def test_session_keys_always_return_exactly_six_slots():
    assert len(render_keys([])) == KEY_COUNT
    assert len(render_keys([
        Session(f"s{i}", AgentState.WORKING) for i in range(8)
    ])) == KEY_COUNT


def test_session_overflow_preserves_items_beyond_six():
    sessions = [Session(f"s{i}", AgentState.IDLE) for i in range(8)]
    assert [s.session_id for s in overflow(sessions)] == ["s6", "s7"]


def test_session_overflow_is_empty_at_or_below_six():
    assert overflow([]) == []
    assert overflow([Session(f"s{i}", None) for i in range(6)]) == []


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


def test_session_keys_use_a_supplied_palette_over_the_factory_one():
    palette = dict(PALETTE) | {AgentState.WAITING: 0x123456}
    out = render_keys([Session("one", AgentState.WAITING)], palette=palette)
    assert out[0] == 0x123456
