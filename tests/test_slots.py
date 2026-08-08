from paneglow import slots
from paneglow.sessions import Session
from paneglow.state import AgentState


EMPTY = [None] * 6


def _session(session_id: str, started_at: float) -> Session:
    return Session(session_id, "/workspace", session_id, "claude-desktop", 1, started_at)


def test_new_sessions_take_earliest_free_slots_in_deterministic_recency_order():
    got = slots.assign(EMPTY, {"old": 1.0, "z-new": 9.0, "a-new": 9.0})
    assert got[:3] == ["a-new", "z-new", "old"]


def test_existing_live_sessions_never_move_under_sticky_policy():
    prev = [None, "held", None, None, None, None]
    got = slots.assign(prev, {"held": 1.0, "new": 9.0})
    assert got == ["new", "held", None, None, None, None]


def test_dead_session_frees_a_slot_and_new_session_reuses_it():
    prev = ["dead", "held", None, None, None, None]
    got = slots.assign(prev, {"held": 1.0, "new": 2.0})
    assert got == ["new", "held", None, None, None, None]


def test_full_board_evicts_oldest_only_for_a_strictly_newer_session():
    prev = [f"s{i}" for i in range(6)]
    live = {f"s{i}": float(i) for i in range(6)} | {"fresh": 99.0}
    assert slots.assign(prev, live) == ["fresh", "s1", "s2", "s3", "s4", "s5"]

    tied = {f"s{i}": float(i) for i in range(6)} | {"same-age": 0.0}
    assert slots.assign(prev, tied) == prev


def test_eviction_takes_a_done_session_before_a_quieter_live_one():
    """A finished session yields its key; a long-quiet live one keeps it."""
    prev = [f"s{i}" for i in range(6)]
    live = {f"s{i}": float(i) for i in range(6)} | {"fresh": 3.5}
    states = {"s5": AgentState.DONE}
    assert slots.assign(prev, live, states=states) == [
        "s0", "s1", "s2", "s3", "s4", "fresh",
    ]


def test_a_done_newcomer_does_not_push_out_a_live_session():
    prev = [f"s{i}" for i in range(6)]
    live = {f"s{i}": float(i) for i in range(6)} | {"finished": 99.0}
    assert slots.assign(prev, live, states={"finished": AgentState.DONE}) == prev


def test_seven_sessions_from_empty_keep_the_six_most_recent():
    live = {f"s{i}": float(i) for i in range(7)}
    assert slots.assign(EMPTY, live) == ["s6", "s5", "s4", "s3", "s2", "s1"]


def test_duplicate_and_invalid_previous_entries_are_normalized():
    prev = ["a", "a", 7, "missing", "b", None, "too-far"]
    got = slots.assign(prev, {"a": 1.0, "b": 2.0, "new": 3.0})  # type: ignore[arg-type]
    assert got == ["a", "new", None, None, "b", None]


def test_non_sequence_previous_value_is_treated_as_empty():
    assert slots.assign(None, {"a": 1.0}) == ["a", None, None, None, None, None]
    got = slots.assign("not-slots", {"a": 1.0})  # type: ignore[arg-type]
    assert got == ["a", None, None, None, None, None]


def test_recent_policy_reorders_and_breaks_ties_by_session_id():
    prev = ["old", "new", None, None, None, None]
    live = {"old": 1.0, "z": 9.0, "a": 9.0}
    assert slots.assign(prev, live, policy="recent")[:3] == ["a", "z", "old"]


def test_priority_policy_uses_priority_then_activity_then_session_id():
    live = {"working": 100.0, "z-wait": 1.0, "a-wait": 1.0}
    states = {
        "working": AgentState.WORKING,
        "z-wait": AgentState.WAITING,
        "a-wait": AgentState.WAITING,
    }
    assert slots.assign(EMPTY, live, policy="priority", states=states)[:3] == [
        "a-wait", "z-wait", "working",
    ]


def test_unknown_policy_falls_back_to_recent_sticky():
    prev = [None, "held", None, None, None, None]
    live = {"held": 1.0, "new": 9.0}
    assert slots.assign(prev, live, policy="unknown") == slots.assign(prev, live)


def test_activity_times_prefers_state_update_and_ignores_non_live_updates():
    live = [_session("old", 1.0), _session("new", 9.0)]
    assert slots.activity_times(live, {"old": 20.0, "gone": 100.0}) == {
        "old": 20.0,
        "new": 9.0,
    }


def test_state_activity_update_changes_which_sticky_slot_is_evicted():
    sessions = [_session(f"s{i}", float(i)) for i in range(6)]
    sessions.append(_session("fresh", 50.0))
    activity = slots.activity_times(sessions, {"s0": 100.0})
    got = slots.assign([f"s{i}" for i in range(6)], activity)
    assert got == ["s0", "fresh", "s2", "s3", "s4", "s5"]
