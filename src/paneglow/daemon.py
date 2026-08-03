"""Foreground coordinator for sessions, the HID pad, and ownership gates.

The daemon deliberately keeps platform discovery and process lifecycle outside
this module.  Every side effect is injectable so one call to :meth:`Daemon.tick`
is deterministic in tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Protocol

from paneglow import deeplink, frontmost as frontmost_module
from paneglow import pad as pad_module, protocol, render, sessions, slots, store
from paneglow.config import Config
from paneglow.state import AgentState


Owner = Literal["none", "claude", "codex"]
PadErrorCode = Literal[
    "unavailable", "status_unverified", "disconnected", "poll_failed",
    "send_failed", "reconnect_failed", "close_failed",
]
SlotReason = Literal[
    "empty", "no_hook", "state", "working_timeout", "done_faded",
]
_OWNERS = {"none", "claude", "codex"}
_UNSET = object()
_STATUS_TIMEOUT_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0
_FEEDBACK_SECONDS = 0.3


class PadPort(Protocol):
    connected: bool
    status_verified: bool
    layer_index: int | None
    epoch: int

    def status(self, timeout: float = ...) -> dict | None: ...
    def reconnect(self, timeout: float = ...) -> bool: ...
    def poll(self, seconds: float) -> list[dict]: ...
    def discard_hid_inputs(self) -> int: ...
    def send(self, message: dict) -> None: ...
    def close(self, flush_seconds: float = ..., *,
              turn_off_keys: bool = ...,
              turn_off_ambient: bool = ...) -> None: ...


def owner_for(bundle_id: str | None, previous: str, cfg: Config) -> Owner:
    """Return the exact three-state owner transition for the frontmost app."""
    prior: Owner = previous if previous in _OWNERS else "none"  # type: ignore[assignment]
    if cfg.gate_mode == "off":
        return "none"
    if cfg.gate_mode == "always":
        return "claude"
    if bundle_id in cfg.own_when:
        return "claude"
    if bundle_id in cfg.yield_to:
        return "codex"
    return prior


def frontmost_bundle_id() -> str | None:
    """Read NSWorkspace through the lazy standard-library native adapter."""
    return frontmost_module.bundle_id()


class Daemon:
    """One deterministic foreground daemon state machine."""

    def __init__(
        self,
        cfg: Config,
        pad: PadPort | None = None,
        *,
        state_root: Path | None = None,
        pad_factory: Callable[[], PadPort | None] = pad_module.Pad.open,
        scanner: Callable[[], sessions.SessionSnapshot] = sessions.scan,
        opener: Callable[[str], bool] = deeplink.open_session,
        frontmost: Callable[[], str | None] = frontmost_bundle_id,
        record_reader: Callable[[Path], list[store.SessionRecord]] = store.read_all,
        pruner: Callable[[Path, set[str] | None, float, float], int] = store.prune,
    ) -> None:
        self.cfg = cfg
        self.pad: PadPort | None = pad
        self.state_root = state_root or (Path.home() / ".paneglow" / "state")
        self._pad_factory = pad_factory
        self._scanner = scanner
        self._opener = opener
        self._frontmost = frontmost
        self._record_reader = record_reader
        self._pruner = pruner

        self.owner: Owner = "none"
        self.frontmost_ok = False
        self.frontmost_id: str | None = None
        self.slots: list[str | None] = [None] * slots.COUNT
        self.effective_states: dict[str, AgentState | None] = {}
        self.effective_reasons: dict[str, SlotReason] = {}
        self.last_input_result: Literal[
            "opened", "open_failed", "empty_slot", "ignored_owner",
            "ignored_layer", "ignored_input",
        ] | None = None
        self.session_snapshot = sessions.SessionSnapshot((), False, ())
        self.session_diagnostics: tuple[str, ...] = ()
        self.causes: tuple[str, ...] = ()
        self.generation = 0
        self.last_status_at: float | None = None
        self.pad_error_code: PadErrorCode | None = None
        self._tick_causes: list[str] = []

        self.keys_reclaim_due: float | None = None
        self.ambient_reclaim_due: float | None = None
        self._feedback_until: float | None = None
        self._last_keys: object | tuple[int | None, ...] = _UNSET
        self._last_ambient: object = _UNSET
        self._dirty_keys = True
        self._dirty_ambient = True

        self._verified_epoch: int | None = None
        self._verified_layer: int | None = None
        self._next_status_due = 0.0
        self._needs_reconnect = False
        self._next_retry_due = 0.0
        self._retry_seconds = 1.0
        self._closed = False
        self._session_fingerprint: object = None
        self._state_fingerprint: object = None

    @property
    def verified_layer(self) -> int | None:
        return self._verified_layer

    @property
    def feedback_active(self) -> bool:
        """Whether the current desired border is the short fault feedback."""
        return self._feedback_until is not None

    def _cause(self, value: str) -> None:
        if value not in self._tick_causes:
            self._tick_causes.append(value)

    def _set_pad_error(self, code: PadErrorCode | None, *,
                       clear_status: bool = False) -> None:
        changed = code != self.pad_error_code
        self.pad_error_code = code
        if clear_status and self.last_status_at is not None:
            self.last_status_at = None
            changed = True
        if changed:
            self._cause("pad")

    def _refresh_owner(self) -> None:
        old_frontmost = (self.frontmost_ok, self.frontmost_id)
        try:
            bundle_id = self._frontmost()
            if bundle_id is not None and type(bundle_id) is not str:
                raise ValueError("frontmost bundle id must be a string or None")
        except Exception:
            self.frontmost_ok = False
            self.frontmost_id = None
            # Frontmost mode fails closed, while policies that do not depend on
            # NSWorkspace must continue to mean exactly what they say.
            new_owner = owner_for(None, "none", self.cfg)
        else:
            self.frontmost_ok = True
            self.frontmost_id = bundle_id
            new_owner = owner_for(bundle_id, self.owner, self.cfg)

        if old_frontmost != (self.frontmost_ok, self.frontmost_id):
            self._cause("frontmost")

        if new_owner != self.owner:
            self.owner = new_owner
            self._cause("owner")
            self._dirty_keys = True
            self._dirty_ambient = True

    def _refresh_sessions(self, now: float) -> None:
        try:
            snapshot = self._scanner()
            if not isinstance(snapshot, sessions.SessionSnapshot):
                raise TypeError("invalid session snapshot")
        except Exception:
            snapshot = sessions.SessionSnapshot((), False, ("scan failed",))
        self.session_snapshot = snapshot
        self.session_diagnostics = snapshot.diagnostics

        live_ids = {session.session_id for session in snapshot.sessions}
        prune_ids = live_ids if snapshot.authoritative else None
        try:
            self._pruner(
                self.state_root, prune_ids,
                self.cfg.ttl_minutes * 60.0, now,
            )
        except Exception:
            pass

        try:
            records = self._record_reader(self.state_root)
        except Exception:
            records = []
        joined = {record.session_id: record for record in records
                  if record.session_id in live_ids}

        effective: dict[str, AgentState | None] = {}
        reasons: dict[str, SlotReason] = {}
        updated_at: dict[str, float] = {}
        for session in snapshot.sessions:
            record = joined.get(session.session_id)
            if record is None:
                effective[session.session_id] = None
                reasons[session.session_id] = "no_hook"
                continue
            updated_at[session.session_id] = record.updated_at
            effective[session.session_id] = render.effective_state(
                record.state,
                updated_at=record.updated_at,
                now=now,
                working_max_seconds=self.cfg.working_max_seconds,
                done_fade_seconds=self.cfg.done_fade_seconds,
            )
            derived = effective[session.session_id]
            if record.state is AgentState.WORKING and derived is AgentState.IDLE:
                reasons[session.session_id] = "working_timeout"
            elif record.state is AgentState.DONE and derived is None:
                reasons[session.session_id] = "done_faded"
            else:
                reasons[session.session_id] = "state"

        activity = slots.activity_times(snapshot.sessions, updated_at)
        priority_states = {session_id: state for session_id, state in effective.items()
                           if state is not None}
        new_slots = slots.assign(
            self.slots, activity, self.cfg.slots_order, priority_states)

        session_fingerprint = (
            snapshot.authoritative,
            snapshot.diagnostics,
            tuple((item.session_id, item.started_at) for item in snapshot.sessions),
        )
        state_fingerprint = tuple(sorted(
            (session_id, state.value if state is not None else None,
             joined[session_id].updated_at if session_id in joined else None)
            for session_id, state in effective.items()
        ))
        if session_fingerprint != self._session_fingerprint:
            self._dirty_keys = self._dirty_ambient = True
            self._session_fingerprint = session_fingerprint
            self._cause("session")
        if state_fingerprint != self._state_fingerprint:
            self._dirty_keys = self._dirty_ambient = True
            self._state_fingerprint = state_fingerprint
            self._cause("state")
        if new_slots != self.slots:
            self._dirty_keys = self._dirty_ambient = True
            self._cause("slot")
        self.slots = new_slots
        self.effective_states = effective
        self.effective_reasons = reasons

    def _invalidate_pad(self, *, reconnect: bool, now: float,
                        error_code: PadErrorCode | None = None,
                        clear_status: bool = True) -> None:
        self._verified_epoch = None
        self._verified_layer = None
        self._last_keys = _UNSET
        self._last_ambient = _UNSET
        self._dirty_keys = self._dirty_ambient = True
        if error_code is not None:
            self._set_pad_error(error_code, clear_status=clear_status)
        if reconnect:
            self._needs_reconnect = True
            self._next_retry_due = max(self._next_retry_due, now + self._retry_seconds)
            self._retry_seconds = min(_RETRY_MAX_SECONDS, self._retry_seconds * 2.0)

    def _accept_pad_status(self, old_epoch: int | None, old_layer: int | None,
                           now: float) -> bool:
        current = self.pad
        if current is None or not bool(getattr(current, "connected", False)) \
                or not bool(getattr(current, "status_verified", False)):
            return False
        epoch = getattr(current, "epoch", None)
        layer = getattr(current, "layer_index", None)
        if type(epoch) is not int or epoch < 1 \
                or type(layer) is not int or layer < 1:
            return False
        try:
            current.discard_hid_inputs()
        except Exception:
            return False
        self._verified_epoch = epoch
        self._verified_layer = layer
        self.last_status_at = now
        self._set_pad_error(None)
        self._cause("status")
        self._next_status_due = now + max(0.001, self.cfg.status_poll_ms / 1000.0)
        self._needs_reconnect = False
        self._retry_seconds = 1.0
        self._next_retry_due = now
        if epoch != old_epoch or layer != old_layer or old_layer is None:
            self._last_keys = _UNSET
            self._last_ambient = _UNSET
            self._dirty_keys = self._dirty_ambient = True
            if epoch != old_epoch:
                self._cause("epoch")
            if layer != old_layer:
                self._cause("layer")
        return True

    def _verify_status(self, now: float) -> None:
        current = self.pad
        if current is None:
            return
        old_epoch, old_layer = self._verified_epoch, self._verified_layer
        self._verified_epoch = self._verified_layer = None
        try:
            reply = current.status(timeout=_STATUS_TIMEOUT_SECONDS)
        except Exception:
            self._invalidate_pad(
                reconnect=True, now=now, error_code="status_unverified")
            return
        result = reply.get("result") if isinstance(reply, dict) else None
        layer = result.get("layer_index") if isinstance(result, dict) else None
        if type(layer) is not int or layer < 1 \
                or not self._accept_pad_status(old_epoch, old_layer, now):
            self._invalidate_pad(
                reconnect=False, now=now, error_code="status_unverified",
                clear_status=False)
            self._next_status_due = now + max(0.001, self.cfg.status_poll_ms / 1000.0)

    def _ensure_pad(self, now: float) -> None:
        if self.pad is None:
            if now < self._next_retry_due:
                return
            try:
                self.pad = self._pad_factory()
            except Exception:
                self.pad = None
            if self.pad is None:
                self._set_pad_error("unavailable", clear_status=True)
                self._next_retry_due = now + self._retry_seconds
                self._retry_seconds = min(_RETRY_MAX_SECONDS, self._retry_seconds * 2.0)
                return
            self._needs_reconnect = False
            self._next_status_due = now

        current = self.pad
        if current is None:
            return
        if self._needs_reconnect or not bool(getattr(current, "connected", False)):
            if now < self._next_retry_due:
                if not self._needs_reconnect:
                    self._set_pad_error("disconnected", clear_status=True)
                return
            old_epoch, old_layer = self._verified_epoch, self._verified_layer
            try:
                reconnected = current.reconnect(timeout=_STATUS_TIMEOUT_SECONDS)
            except Exception:
                reconnected = False
            if reconnected and self._accept_pad_status(old_epoch, old_layer, now):
                return
            self._invalidate_pad(
                reconnect=True, now=now, error_code="reconnect_failed")
            return

        if now >= self._next_status_due:
            self._verify_status(now)

    def _gate_layer_one(self) -> bool:
        current = self.pad
        return (current is not None
                and bool(getattr(current, "connected", False))
                and bool(getattr(current, "status_verified", False))
                and self._verified_epoch == getattr(current, "epoch", None)
                and self._verified_layer == 1)

    def _schedule_reclaim(self, message: dict, now: float) -> None:
        if not pad_module.is_vendor_write(message):
            return
        method = message.get("method")
        delay = self.cfg.reclaim_delay_ms / 1000.0
        if method in {"v.oai.rgbcfg", "lights.preview"}:
            if self.ambient_reclaim_due is None:
                self.ambient_reclaim_due = now + delay
                self._cause("vendor_ambient")
        elif method == "v.oai.thstatus" and self.owner == "claude" \
                and self._gate_layer_one():
            if self.keys_reclaim_due is None:
                self.keys_reclaim_due = now + delay
                self._cause("vendor_keys")

    def _handle_input(self, message: dict, now: float) -> None:
        if message.get("m") != "v.oai.hid":
            return
        params = message.get("p")
        if not isinstance(params, dict):
            self.last_input_result = "ignored_input"
            self._cause("input")
            return
        key, action = params.get("k"), params.get("act")
        if type(key) is not str or key not in {f"AG{i:02d}" for i in range(6)} \
                or type(action) is not int or action != 1:
            self.last_input_result = "ignored_input"
            self._cause("input")
            return

        # Re-read ownership at dispatch time; an app switch must close the gate.
        self._refresh_owner()
        if self.owner != "claude":
            self.last_input_result = "ignored_owner"
            self._cause("input")
            return
        if not self._gate_layer_one():
            self.last_input_result = "ignored_layer"
            self._cause("input")
            return
        session_id = self.slots[int(key[-2:])]
        if session_id is None:
            self.last_input_result = "empty_slot"
            self._feedback_until = now + _FEEDBACK_SECONDS
            self._dirty_ambient = True
            self._cause("input_feedback")
            return
        try:
            opened = bool(self._opener(session_id))
        except Exception:
            opened = False
        if opened:
            self.last_input_result = "opened"
            self._cause("input")
            return
        self.last_input_result = "open_failed"
        self._feedback_until = now + _FEEDBACK_SECONDS
        self._dirty_ambient = True
        self._cause("input_feedback")

    def _handle_messages(self, messages: object, now: float) -> None:
        if not isinstance(messages, list):
            return
        for message in messages:
            if not isinstance(message, dict):
                continue
            self._schedule_reclaim(message, now)
            self._handle_input(message, now)

    def _key_colours(self) -> tuple[int | None, ...]:
        colours: list[int | None] = []
        for session_id in self.slots:
            state = self.effective_states.get(session_id) if session_id else None
            colours.append(render.PALETTE[state] if state is not None else None)
        return tuple(colours)

    def _ambient_value(self, now: float) -> int | None | tuple[int, str]:
        if self._verified_layer != 1 and self.cfg.layer_underglow == "off":
            return None
        if self._feedback_until is not None and now < self._feedback_until:
            return (self.cfg.underglow_claude, self.cfg.effect_fault)
        if self.owner == "none" or self.cfg.underglow_scope == "off":
            return None
        colour = (self.cfg.underglow_claude if self.owner == "claude"
                  else self.cfg.underglow_codex)
        states = self.effective_states
        if self.cfg.underglow_scope == "all_sessions" or self.owner == "codex":
            alert_states = states.values()
        else:
            shown = {session_id for session_id in self.slots if session_id}
            alert_states = (state for session_id, state in states.items()
                            if session_id not in shown)
        effect = (self.cfg.effect_alert
                  if render.alert_level(alert_states) == "alert"
                  else self.cfg.effect_normal)
        return (colour, effect)

    def _send(self, message: dict, now: float) -> bool:
        current = self.pad
        if current is None:
            return False
        try:
            current.send(message)
            return True
        except Exception:
            self._invalidate_pad(
                reconnect=True, now=now, error_code="send_failed")
            return False

    def _paint(self, now: float) -> None:
        current = self.pad
        if current is None or self._verified_epoch is None \
                or self._verified_layer is None:
            return

        force_keys = self.keys_reclaim_due is not None \
            and now >= self.keys_reclaim_due
        force_ambient = self.ambient_reclaim_due is not None \
            and now >= self.ambient_reclaim_due
        if force_keys:
            self.keys_reclaim_due = None
            self._cause("reclaim_keys")
        if force_ambient:
            self.ambient_reclaim_due = None
            self._cause("reclaim_ambient")

        if self._verified_layer == 1:
            desired_keys: tuple[int | None, ...] | None
            if self.owner == "claude":
                desired_keys = self._key_colours()
            elif self.owner == "none":
                desired_keys = (None,) * slots.COUNT
            else:
                # Codex owns the A-zone. Even a one-shot off write destroys its
                # display, so yielding means zero thstatus writes.
                desired_keys = None

            if desired_keys is not None and (force_keys or self._dirty_keys
                                             or desired_keys != self._last_keys):
                if not self._send(protocol.thstatus(list(desired_keys)), now):
                    return
                self._last_keys = desired_keys
                self._dirty_keys = False
                self._cause("paint_keys")
            elif self.owner == "codex" and desired_keys is None:
                self._dirty_keys = False
        elif force_keys:
            # Never carry a key reclaim across a layer where the A-zone is not ours.
            self.keys_reclaim_due = None

        if self._verified_layer != 1:
            # "keep" retains the physical border without touching this layer.
            if self.cfg.layer_underglow == "keep":
                return
            # "off" writes once on entry. Owner/session changes may dirty the
            # layer-one desired border, but must not spam duplicate off writes.
            # An observed vendor ambient write is the one reason to reclaim.
            needs_off = (force_ambient or self._last_ambient is _UNSET
                         or self._last_ambient is not None)
            if needs_off:
                if not self._send(protocol.rgbcfg(ambient=None), now):
                    return
                self._last_ambient = None
                self._cause("paint_ambient")
            self._dirty_ambient = False
            return
        desired_ambient = self._ambient_value(now)
        if force_ambient or self._dirty_ambient or desired_ambient != self._last_ambient:
            if self._send(protocol.rgbcfg(ambient=desired_ambient), now):
                self._last_ambient = desired_ambient
                self._dirty_ambient = False
                self._cause("paint_ambient")

    def tick(self, now: float) -> None:
        """Advance every deterministic state machine once at epoch/deadline ``now``."""
        if self._closed:
            return
        self._tick_causes = []
        self._refresh_sessions(now)
        self._ensure_pad(now)

        if self.pad is not None and self._verified_epoch is not None:
            try:
                # A zero-duration CFRunLoop call is not evidence that pending
                # HID callbacks were delivered.  This positive poll is the
                # daemon's cadence; the CLI must not add another full sleep
                # while a verified pad is connected.
                messages = self.pad.poll(self.cfg.poll_ms / 1000.0)
            except Exception:
                self._invalidate_pad(
                    reconnect=True, now=now, error_code="poll_failed")
            else:
                # Ownership is intentionally sampled after the blocking HID
                # poll. An app switch during that interval must close the gate
                # before ACK reclaim, input dispatch, or painting.
                self._refresh_owner()
                self._handle_messages(messages, now)
                if messages:
                    # Deep-link launch can itself block for several seconds.
                    # Sample again so an app switch during that call cannot
                    # paint the A-zone with a stale Claude owner afterward.
                    self._refresh_owner()

        if self.pad is None or self._verified_epoch is None:
            self._refresh_owner()

        if self._feedback_until is not None and now >= self._feedback_until:
            self._feedback_until = None
            self._dirty_ambient = True
            self._cause("input_feedback_restore")
        self._paint(now)
        self.causes = tuple(self._tick_causes)
        if self.causes:
            self.generation += 1

    def close(self) -> None:
        """Close the pad once; cleanup failures do not escape daemon shutdown."""
        if self._closed:
            return
        self._tick_causes = ["shutdown"]
        # SIGTERM may arrive after an app switch but before the next tick.  A
        # final native sample prevents shutdown from clearing Codex's A-zone.
        self._refresh_owner()
        self._closed = True
        current, self.pad = self.pad, None
        if current is not None:
            verified_layer_one = (
                bool(getattr(current, "connected", False))
                and bool(getattr(current, "status_verified", False))
                and self._verified_epoch == getattr(current, "epoch", None)
                and self._verified_layer == 1
            )
            try:
                current.close(
                    turn_off_keys=(verified_layer_one and self.owner == "claude"),
                    turn_off_ambient=verified_layer_one,
                )
            except Exception:
                self._set_pad_error("close_failed", clear_status=True)
            else:
                self._set_pad_error(None, clear_status=True)
        self._verified_epoch = self._verified_layer = None
        self.causes = tuple(self._tick_causes)
        self.generation += 1
