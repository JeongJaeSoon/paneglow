from __future__ import annotations

import time
from dataclasses import FrozenInstanceError, dataclass, fields
from pathlib import Path

import pytest

from paneglow import daemon, pad as pad_module, protocol, sessions, store
from paneglow.config import Config
from paneglow.state import AgentState


CLAUDE = "com.anthropic.claudefordesktop"
CODEX = "com.openai.codex"


def live(session_id: str, started_at: float) -> sessions.Session:
    return sessions.Session(
        session_id=session_id, cwd="/private", name="private", entrypoint="x",
        pid=123, started_at=started_at,
    )


def record(session_id: str, state: AgentState,
           updated_at: float) -> store.SessionRecord:
    return store.SessionRecord(
        session_id=session_id, cwd="/private", state=state,
        rev=1, updated_at=updated_at,
    )


@dataclass
class Box:
    value: str | None


class FakePad:
    def __init__(self, layers=(1,), *, connected=True, epoch=1) -> None:
        self.connected = connected
        self.status_verified = False
        self.layer_index = None
        self.epoch = epoch
        self.layers = list(layers)
        self.messages: list[object] = []
        self.fail_poll = False
        self.on_poll = None
        self.poll_durations: list[float] = []
        self.writes: list[dict] = []
        self.status_calls = 0
        self.reconnect_calls = 0
        self.reconnect_layers: list[int | None] = []
        self.close_calls = 0
        self.close_options: list[tuple[bool, bool]] = []

    def status(self, timeout=1.0):
        self.status_calls += 1
        layer = self.layers.pop(0) if self.layers else None
        if type(layer) is not int or layer < 1:
            self.status_verified = False
            self.layer_index = None
            return None if layer is None else {
                "result": {"layer_index": layer}, "method": "device.status",
            }
        self.status_verified = True
        self.layer_index = layer
        return {"result": {"layer_index": layer}, "method": "device.status"}

    def reconnect(self, timeout=1.0):
        self.reconnect_calls += 1
        layer = self.reconnect_layers.pop(0) if self.reconnect_layers else None
        if type(layer) is not int or layer < 1:
            self.connected = False
            self.status_verified = False
            self.layer_index = None
            return False
        self.connected = True
        self.status_verified = True
        self.layer_index = layer
        self.epoch += 1
        return True

    def poll(self, seconds):
        return [received.message for received in self.poll_received(seconds)]

    def poll_received(self, seconds):
        self.poll_durations.append(seconds)
        if self.on_poll is not None:
            self.on_poll()
        if self.fail_poll:
            raise RuntimeError("poll failed")
        messages, self.messages = self.messages, []
        return [
            message if isinstance(message, pad_module.ReceivedMessage)
            else pad_module.ReceivedMessage(
                message=message, received_at=0.0,
                connection_epoch=self.epoch,
            )
            for message in messages
        ]

    def send(self, message):
        self.writes.append(message)

    def discard_hid_inputs(self):
        before = len(self.messages)
        self.messages = [
            message for message in self.messages
            if not (isinstance(message, dict)
                    and message.get("m") == "v.oai.hid")
        ]
        return before - len(self.messages)

    def close(self, flush_seconds=1.0, *, turn_off_keys=True,
              turn_off_ambient=True):
        self.close_calls += 1
        self.close_options.append((turn_off_keys, turn_off_ambient))
        self.connected = False


def build(
    *, cfg=Config(), pad=None, frontmost=CLAUDE,
    snapshot=None, records=(), opener=lambda _sid: True,
    prunes=None, factory=lambda: None, input_observer=None,
):
    snapshot = snapshot or sessions.SessionSnapshot((), True, ())
    prunes = [] if prunes is None else prunes
    kwargs = {}
    if input_observer is not None:
        kwargs["input_observer"] = input_observer
    return daemon.Daemon(
        cfg, pad,
        state_root=Path("/unused"),
        pad_factory=factory,
        scanner=lambda: snapshot,
        record_reader=lambda _root: list(records),
        pruner=lambda root, live_ids, ttl, now: (
            prunes.append((root, live_ids, ttl, now)) or 0),
        frontmost=(lambda: frontmost) if not isinstance(frontmost, Box)
        else (lambda: frontmost.value),
        opener=opener,
        **kwargs,
    )


def methods(pad: FakePad, method: str) -> list[dict]:
    return [message for message in pad.writes if message.get("m") == method]


def on_next_poll(pad: FakePad, *messages: object) -> None:
    def deliver() -> None:
        pad.on_poll = None
        pad.messages.extend(messages)

    pad.on_poll = deliver


def received(message: object, received_at: float, connection: int = 1):
    return pad_module.ReceivedMessage(
        message=message, received_at=received_at,
        connection_epoch=connection,
    )


@pytest.mark.parametrize(
    ("bundle", "previous", "expected"),
    [
        (CLAUDE, "none", "claude"),
        (CODEX, "claude", "codex"),
        ("com.google.Chrome", "claude", "claude"),
        ("com.google.Chrome", "codex", "codex"),
        (None, "none", "none"),
    ],
)
def test_owner_for_exact_transitions(bundle, previous, expected):
    assert daemon.owner_for(bundle, previous, Config()) == expected


def test_owner_for_off_and_always_modes():
    assert daemon.owner_for(CLAUDE, "claude", Config(gate_mode="off")) == "none"
    assert daemon.owner_for(CODEX, "none", Config(gate_mode="always")) == "claude"


def test_frontmost_failure_fails_closed():
    def broken():
        raise RuntimeError("private failure")

    d = build(frontmost=CLAUDE)
    d._frontmost = broken
    d.tick(1.0)
    assert d.owner == "none" and d.frontmost_ok is False


@pytest.mark.parametrize(
    ("mode", "expected"), [("frontmost", "none"), ("always", "claude"),
                           ("off", "none")],
)
def test_frontmost_failure_still_respects_non_frontmost_gate_modes(mode, expected):
    def broken():
        raise RuntimeError("lookup failed")

    d = build(cfg=Config(gate_mode=mode))
    d._frontmost = broken
    d.tick(0)
    assert d.owner == expected
    assert d.frontmost_ok is False


def test_default_frontmost_wrapper_uses_native_module(monkeypatch):
    monkeypatch.setattr(daemon.frontmost_module, "bundle_id", lambda: CLAUDE)
    assert daemon.frontmost_bundle_id() == CLAUDE


@pytest.mark.parametrize("authoritative", [True, False])
def test_scan_trust_controls_prune_contract(authoritative):
    prunes = []
    snapshot = sessions.SessionSnapshot((live("s1", 1),), authoritative,
                                        ("partial",) if not authoritative else ())
    d = build(snapshot=snapshot, prunes=prunes)
    d.tick(10.0)
    assert prunes[0][1] == ({"s1"} if authoritative else None)
    assert d.session_snapshot is snapshot
    assert d.session_diagnostics == snapshot.diagnostics


def test_activity_prefers_record_update_and_slots_stay_sticky():
    snapshot = sessions.SessionSnapshot(
        (live("old", 1), live("new", 20)), True, ())
    records = (record("old", AgentState.WAITING, 30),)
    d = build(snapshot=snapshot, records=records)
    d.tick(30)
    assert d.slots[:2] == ["old", "new"]
    first = list(d.slots)
    d.tick(31)
    assert d.slots == first


def test_effective_working_and_done_do_not_mutate_source_records():
    snapshot = sessions.SessionSnapshot(
        (live("work", 1), live("done", 2)), True, ())
    work = record("work", AgentState.WORKING, 0)
    done = record("done", AgentState.DONE, 0)
    cfg = Config(working_max_seconds=5, done_fade_seconds=5)
    d = build(cfg=cfg, snapshot=snapshot, records=(work, done))
    d.tick(5)
    assert d.effective_states == {"work": AgentState.IDLE, "done": None}
    assert d.effective_reasons == {
        "work": "working_timeout", "done": "done_faded",
    }
    assert work.state is AgentState.WORKING and done.state is AgentState.DONE


def test_done_faded_sessions_release_slots_instead_of_leaving_dark_holes():
    # Field case from issue #48: seven sessions, three of them done-faded, so
    # three of the six slots were held by keys the render had already darkened.
    ids = [f"s{index}" for index in range(7)]
    faded = {"s0", "s2", "s4"}
    snapshot = sessions.SessionSnapshot(
        tuple(live(session_id, float(index))
              for index, session_id in enumerate(ids)), True, ())
    records = tuple(
        record(session_id, AgentState.DONE, 0.0) if session_id in faded
        else record(session_id, AgentState.IDLE, 100.0)
        for session_id in ids)
    d = build(cfg=Config(done_fade_seconds=5), snapshot=snapshot, records=records)
    d.tick(100.0)

    assert d.slots == ["s1", "s3", "s5", "s6", None, None]
    # Every occupied slot lights up: no hole between lit agent keys.
    assert all(d.effective_states[session_id] is not None
               for session_id in d.slots if session_id is not None)


def test_effective_reason_distinguishes_no_hook_from_a_real_state():
    snapshot = sessions.SessionSnapshot(
        (live("unknown", 1), live("waiting", 2)), True, ())
    d = build(snapshot=snapshot,
              records=(record("waiting", AgentState.WAITING, 2),))
    d.tick(2)
    assert d.effective_reasons == {"unknown": "no_hook", "waiting": "state"}


def test_startup_status_layer_one_then_valid_agent_input_opens_slot():
    p = FakePad([1])
    opened = []
    snapshot = sessions.SessionSnapshot((live("s1", 1),), True, ())
    on_next_poll(p, {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}})
    d = build(pad=p, snapshot=snapshot, opener=lambda sid: opened.append(sid) or True)
    d.tick(0)
    assert opened == ["s1"]
    assert d.last_input_result == "opened"
    assert d.verified_layer == 1
    assert p.poll_durations == [pytest.approx(Config().poll_ms / 1000.0)]


def test_observer_keeps_same_poll_order_receive_times_and_event_dispositions():
    p = FakePad([1], epoch=41)
    front = Box(CLAUDE)
    observed: list[daemon.InputEvent] = []
    opened: list[str] = []
    snapshot = sessions.SessionSnapshot(
        (live("s1", 1), live("s2", 2)), True, ())
    messages = [
        {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG01", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG01", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG01", "act": 0}},
    ]
    timestamps = [12.0, 12.03, 12.08, 12.11]
    on_next_poll(p, *[
        received(message, timestamp, connection=41)
        for message, timestamp in zip(messages, timestamps, strict=True)
    ])

    def blocking_open(session_id: str) -> bool:
        opened.append(session_id)
        time.sleep(0.01)
        front.value = CODEX
        return True

    d = build(
        pad=p, frontmost=front, snapshot=snapshot,
        opener=blocking_open, input_observer=observed.append,
    )
    d.tick(20.0)

    assert opened == [d.slots[0]]
    assert [event.key for event in observed] == ["A1", "A2", "A2", "A2"]
    assert [event.action for event in observed] == [1, 1, 1, 0]
    assert [event.received_at for event in observed] == timestamps
    assert [event.connection_ordinal for event in observed] == [1, 1, 1, 1]
    assert [event.owner_at_dispatch for event in observed] == [
        "claude", "codex", "codex", "codex",
    ]
    assert [event.result for event in observed] == [
        "opened", "ignored_owner", "ignored_owner", "ignored_input",
    ]
    assert all(event.layer_one for event in observed)
    assert all(event.slot_occupied for event in observed)
    assert d.last_input_result == "ignored_input"


def test_input_event_is_fixed_shape_immutable_and_privacy_bounded():
    assert [field.name for field in fields(daemon.InputEvent)] == [
        "key", "action", "received_at", "connection_ordinal",
        "owner_at_dispatch", "layer_one", "slot_occupied", "result",
    ]
    event = daemon.InputEvent(
        key="other", action="other", received_at=1.0,
        connection_ordinal=1, owner_at_dispatch="none",
        layer_one=False, slot_occupied=False, result="ignored_input",
    )
    assert not hasattr(event, "__dict__")
    with pytest.raises(FrozenInstanceError):
        event.result = "opened"


def test_default_observer_is_off_and_observer_failures_do_not_stop_dispatch():
    press = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    snapshot = sessions.SessionSnapshot((live("s1", 1),), True, ())

    default_pad = FakePad([1])
    on_next_poll(default_pad, press)
    default = build(pad=default_pad, snapshot=snapshot)
    default.tick(0)
    assert default.last_input_result == "opened"

    calls: list[daemon.InputEvent] = []
    opened: list[str] = []

    def broken_observer(event: daemon.InputEvent) -> None:
        calls.append(event)
        raise RuntimeError("trace sink unavailable")

    failing_pad = FakePad([1])
    on_next_poll(failing_pad, press, press)
    failing = build(
        pad=failing_pad, snapshot=snapshot,
        opener=lambda session_id: opened.append(session_id) or True,
        input_observer=broken_observer,
    )
    failing.tick(0)
    assert len(calls) == 2
    assert opened == ["s1", "s1"]
    assert failing.last_input_result == "opened"


def test_observer_normalizes_untrusted_key_and_action_without_raw_payload():
    p = FakePad([1])
    observed: list[daemon.InputEvent] = []
    on_next_poll(
        p,
        {"m": "v.oai.hid", "p": {"k": "C1", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG00", "act": True}},
        {"m": "v.oai.hid", "p": []},
    )
    d = build(pad=p, input_observer=observed.append)
    d.tick(0)
    assert [(event.key, event.action, event.result) for event in observed] == [
        ("other", 1, "ignored_input"),
        ("A1", "other", "ignored_input"),
        ("other", "other", "ignored_input"),
    ]


def test_observer_connection_ordinal_changes_on_reconnect_without_raw_epoch():
    p = FakePad([1], epoch=17)
    observed: list[daemon.InputEvent] = []
    press = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    on_next_poll(p, received(press, 1.0, connection=17))
    d = build(pad=p, input_observer=observed.append)
    d.tick(0)

    p.connected = False
    p.reconnect_layers = [1]
    on_next_poll(p, received(press, 2.0, connection=18))
    d.tick(1)

    assert [event.connection_ordinal for event in observed] == [1, 2]
    assert [event.received_at for event in observed] == [1.0, 2.0]


@pytest.mark.parametrize(
    ("message", "result"),
    [
        ({"m": "v.oai.hid", "p": {"k": "AG00", "act": 0}}, "ignored_input"),
        ({"m": "v.oai.hid", "p": {"k": "AG00", "act": True}}, "ignored_input"),
        ({"m": "v.oai.hid", "p": {"k": "C1", "act": 1}}, "ignored_input"),
        ({"m": "v.oai.hid", "p": {"k": "KNOB", "act": 1}}, "ignored_input"),
        ({"m": "v.oai.hid", "p": []}, "ignored_input"),
    ],
)
def test_input_shape_rejects(message, result):
    p = FakePad([1])
    on_next_poll(p, message)
    d = build(pad=p)
    d.tick(0)
    assert d.last_input_result == result


def test_input_reports_owner_layer_and_empty_slot_separately():
    press = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}

    codex = FakePad([1])
    on_next_poll(codex, press)
    d = build(pad=codex, frontmost=CODEX)
    d.tick(0)
    assert d.last_input_result == "ignored_owner"

    layer_two = FakePad([2])
    on_next_poll(layer_two, press)
    d = build(pad=layer_two)
    d.tick(0)
    assert d.last_input_result == "ignored_layer"

    empty = FakePad([1])
    on_next_poll(empty, press)
    d = build(pad=empty)
    d.tick(0)
    assert d.last_input_result == "empty_slot"


def test_open_failure_shows_fault_ambient_then_restores():
    p = FakePad([1])
    snapshot = sessions.SessionSnapshot((live("s1", 1),), True, ())
    press = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    on_next_poll(p, press)
    d = build(pad=p, snapshot=snapshot, opener=lambda _sid: False)
    d.tick(0)
    assert d.last_input_result == "open_failed"
    assert methods(p, "v.oai.rgbcfg")[-1]["p"]["ambient"]["e"] \
        == protocol.EFFECTS[Config().effect_fault]
    d.tick(0.31)
    assert methods(p, "v.oai.rgbcfg")[-1]["p"]["ambient"]["e"] \
        == protocol.EFFECTS[Config().effect_normal]


def test_layer_one_to_two_to_one_never_writes_a_zone_on_layer_two():
    p = FakePad([1, 2, 1])
    snapshot = sessions.SessionSnapshot((live("s1", 1),), True, ())
    d = build(cfg=Config(status_poll_ms=1000), pad=p, snapshot=snapshot,
              records=(record("s1", AgentState.WORKING, 0),))
    d.tick(0)
    first = len(methods(p, "v.oai.thstatus"))
    d.tick(1)
    assert d.verified_layer == 2
    assert len(methods(p, "v.oai.thstatus")) == first
    layer_two_write_count = len(p.writes)
    d.tick(1.5)
    assert len(p.writes) == layer_two_write_count
    d.tick(2)
    assert d.verified_layer == 1
    assert len(methods(p, "v.oai.thstatus")) == first + 1


def test_layer_two_off_ambient_writes_once_until_vendor_reclaim():
    p = FakePad([2])
    cfg = Config(layer_underglow="off", reclaim_delay_ms=200)
    front = Box(CLAUDE)
    d = build(cfg=cfg, pad=p, frontmost=front)
    d.tick(0)
    assert len(methods(p, "v.oai.thstatus")) == 0
    assert len(methods(p, "v.oai.rgbcfg")) == 1
    assert methods(p, "v.oai.rgbcfg")[-1]["p"]["ambient"]["e"] == 0

    # Ownership changes can make the desired layer-one border dirty, but the
    # already-off physical border on layer two needs no duplicate write.
    front.value = CODEX
    d.tick(0.1)
    assert len(methods(p, "v.oai.rgbcfg")) == 1

    p.messages.append({
        "result": {"ok": 1}, "id": 9, "method": "v.oai.rgbcfg",
    })
    d.tick(0.2)
    d.tick(0.41)
    assert len(methods(p, "v.oai.rgbcfg")) == 2
    assert methods(p, "v.oai.rgbcfg")[-1]["p"]["ambient"]["e"] == 0


def test_status_timeout_discards_previous_layer_and_never_replays_input():
    p = FakePad([1, None, 1])
    snapshot = sessions.SessionSnapshot((live("s1", 1),), True, ())
    opened = []
    d = build(
        cfg=Config(status_poll_ms=1000), pad=p, snapshot=snapshot,
        opener=lambda session_id: opened.append(session_id) or True,
    )
    d.tick(0)
    p.messages.append({"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}})
    d.tick(1)
    assert d.verified_layer is None
    assert d.last_input_result is None  # poll is not armed after failed status
    d.tick(2)
    assert d.verified_layer == 1
    assert opened == []
    assert d.last_input_result is None


def test_codex_owner_never_writes_thstatus_even_on_transition_or_vendor_ack():
    front = Box(CLAUDE)
    p = FakePad([1])
    d = build(pad=p, frontmost=front)
    d.tick(0)
    before = len(methods(p, "v.oai.thstatus"))
    front.value = CODEX
    p.messages.append({"result": {"ok": 1}, "id": 9,
                       "method": "v.oai.thstatus"})
    d.tick(0.1)
    d.tick(1.0)
    assert len(methods(p, "v.oai.thstatus")) == before
    assert d.keys_reclaim_due is None


def test_app_switch_during_blocking_poll_closes_gate_before_reclaim_or_paint():
    front = Box(CLAUDE)
    p = FakePad([1])
    d = build(pad=p, frontmost=front)
    d.tick(0)
    p.writes.clear()

    p.messages.append({
        "result": {"ok": 1}, "id": 9, "method": "v.oai.thstatus",
    })
    p.on_poll = lambda: setattr(front, "value", CODEX)
    d.tick(0.1)

    assert d.owner == "codex"
    assert methods(p, "v.oai.thstatus") == []
    assert d.keys_reclaim_due is None


def test_app_switch_during_deeplink_open_closes_gate_before_paint():
    front = Box(CLAUDE)
    p = FakePad([1])
    on_next_poll(p, {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}})
    snapshot = sessions.SessionSnapshot((live("s1", 1),), True, ())

    def opening(_session_id):
        front.value = CODEX
        return True

    d = build(pad=p, frontmost=front, snapshot=snapshot, opener=opening)
    d.tick(0)
    assert d.last_input_result == "opened"
    assert d.owner == "codex"
    assert methods(p, "v.oai.thstatus") == []


def test_vendor_ack_reclaims_only_its_zone_and_own_ack_does_not_loop():
    p = FakePad([1])
    d = build(cfg=Config(reclaim_delay_ms=200), pad=p)
    d.tick(0)
    keys_before = len(methods(p, "v.oai.thstatus"))
    ambient_before = len(methods(p, "v.oai.rgbcfg"))

    p.messages.extend([
        {"result": {"ok": 1}, "id": None, "method": "v.oai.rgbcfg"},
        {"result": {"ok": 1}, "id": 3, "method": "v.oai.rgbcfg"},
        {"result": {"ok": 1}, "id": 4, "method": "v.oai.thstatus"},
    ])
    d.tick(0.1)
    assert d.ambient_reclaim_due == pytest.approx(0.3)
    assert d.keys_reclaim_due == pytest.approx(0.3)
    d.tick(0.31)
    assert len(methods(p, "v.oai.rgbcfg")) == ambient_before + 1
    assert len(methods(p, "v.oai.thstatus")) == keys_before + 1


def test_reconnect_uses_exponential_deadline_and_new_epoch_full_repaint():
    p = FakePad(connected=False, epoch=4)
    p.reconnect_layers = [None, 1]
    d = build(pad=p)
    d.tick(0)
    assert p.reconnect_calls == 1
    d.tick(0.5)
    assert p.reconnect_calls == 1
    d.tick(1.0)
    assert p.reconnect_calls == 2
    assert p.epoch == 5 and d.verified_layer == 1
    assert methods(p, "v.oai.thstatus") and methods(p, "v.oai.rgbcfg")
    assert "epoch" in d.causes


def test_first_zone_send_failure_prevents_later_zone_write_same_tick():
    class FailingPad(FakePad):
        def send(self, message):
            self.writes.append(message)
            raise RuntimeError("first send failed")

    p = FailingPad([1])
    d = build(pad=p)
    d.tick(0)
    assert [message["m"] for message in p.writes] == ["v.oai.thstatus"]
    assert d.verified_layer is None


def test_poll_failure_discards_gate_then_reconnects_with_full_epoch_repaint():
    p = FakePad([1], epoch=7)
    d = build(pad=p)
    d.tick(0)
    assert d.verified_layer == 1
    p.writes.clear()

    p.fail_poll = True
    d.tick(0.1)
    assert d.verified_layer is None
    assert p.writes == []
    assert p.reconnect_calls == 0

    p.fail_poll = False
    p.reconnect_layers = [1]
    d.tick(1.0)
    assert p.reconnect_calls == 0
    d.tick(1.11)
    assert p.reconnect_calls == 1
    assert p.epoch == 8 and d.verified_layer == 1
    assert methods(p, "v.oai.thstatus") and methods(p, "v.oai.rgbcfg")
    assert "epoch" in d.causes


def test_public_pad_diagnostics_are_allowlisted_and_generation_tracks_diffs():
    p = FakePad([1, 1, None])
    d = build(cfg=Config(status_poll_ms=1000), pad=p)

    d.tick(0)
    first_generation = d.generation
    assert first_generation == 1
    assert d.last_status_at == 0
    assert d.pad_error_code is None

    d.tick(0.5)
    assert d.causes == ()
    assert d.generation == first_generation

    d.tick(1.0)
    assert d.last_status_at == 1.0
    assert "status" in d.causes
    assert d.generation == first_generation + 1

    d.tick(2.0)
    assert d.last_status_at == 1.0  # last proof remains useful to status/doctor
    assert d.pad_error_code == "status_unverified"
    assert d.verified_layer is None


def test_pad_error_codes_cover_unavailable_poll_send_and_close() -> None:
    missing = build(factory=lambda: None)
    missing.tick(0)
    assert missing.pad_error_code == "unavailable"

    polling = FakePad([1])
    polling.fail_poll = True
    d = build(pad=polling)
    d.tick(0)
    assert d.pad_error_code == "poll_failed"
    assert d.last_status_at is None

    class FailingSendPad(FakePad):
        def send(self, message):
            raise RuntimeError("send failed")

    d = build(pad=FailingSendPad([1]))
    d.tick(0)
    assert d.pad_error_code == "send_failed"

    class FailingClosePad(FakePad):
        def close(self, flush_seconds=1.0):
            raise RuntimeError("close failed")

    d = build(pad=FailingClosePad([1]))
    d.tick(0)
    before = d.generation
    d.close()
    assert d.pad_error_code == "close_failed"
    assert d.causes == ("shutdown", "pad")
    assert d.generation == before + 1


def test_none_factory_retries_with_backoff():
    calls = []
    d = build(factory=lambda: calls.append(1) or None)
    d.tick(0)
    d.tick(0.5)
    d.tick(1)
    assert len(calls) == 2


def test_malformed_device_messages_do_not_crash_and_close_is_once():
    p = FakePad([1])
    on_next_poll(p, None, [], 1, {"m": "v.oai.hid", "p": None})
    d = build(pad=p)
    d.tick(0)
    assert d.last_input_result == "ignored_input"
    assert d.session_snapshot.authoritative is True
    assert {"owner", "session", "state"}.issubset(set(d.causes))
    d.close()
    d.close()
    assert p.close_calls == 1
    assert p.close_options == [(True, True)]


@pytest.mark.parametrize(
    ("owner", "layer", "expected"),
    [
        (CODEX, 1, (False, True)),
        (CLAUDE, 2, (False, False)),
    ],
)
def test_daemon_close_only_clears_verified_zones_it_owns(owner, layer, expected):
    p = FakePad([layer])
    d = build(pad=p, frontmost=owner)
    d.tick(0)
    d.close()
    assert p.close_options == [expected]


def test_daemon_close_writes_no_zones_after_status_becomes_unverified():
    p = FakePad([1, None])
    d = build(cfg=Config(status_poll_ms=1000), pad=p)
    d.tick(0)
    d.tick(1)
    d.close()
    assert p.close_options == [(False, False)]


def test_daemon_close_resamples_owner_after_last_tick():
    front = Box(CLAUDE)
    p = FakePad([1])
    d = build(pad=p, frontmost=front)
    d.tick(0)
    front.value = CODEX
    d.close()
    assert d.owner == "codex"
    assert p.close_options == [(False, True)]


def test_daemon_close_fails_closed_when_frontmost_lookup_breaks():
    p = FakePad([1])
    d = build(pad=p, frontmost=CLAUDE)
    d.tick(0)

    def broken():
        raise RuntimeError("frontmost unavailable")

    d._frontmost = broken
    d.close()
    assert d.owner == "none"
    assert p.close_options == [(False, True)]
