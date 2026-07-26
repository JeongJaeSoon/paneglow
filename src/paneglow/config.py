"""Config loading. Bad values fall back to defaults and collect a warning --
nothing here may block startup."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_GATE_MODES = {"frontmost", "always", "off"}
_UNDERGLOW_MODES = {"outside", "all_claude", "current_tab", "off"}
#: C5 and C6 share one wide keycap, so pressing it reports both ids.
_MOD_KEYS = {"C1", "C2", "C3", "C4", "C7", "KNOB_PRESS"}


@dataclass(frozen=True)
class Config:
    gate_mode: str = "frontmost"
    yield_to: tuple[str, ...] = ("com.openai.chat",)
    own_when: tuple[str, ...] = ("com.googlecode.iterm2",)
    mod_key: str = "C7"
    knob_tab_switch: bool = True
    mod_direct_tab: bool = True
    underglow_iterm: str = "outside"
    underglow_codex: str = "all_claude"
    ttl_minutes: int = 30
    done_fade_seconds: int = 180
    poll_ms: int = 250
    mod_release_timeout_ms: int = 5000


def _reject(value, default, label: str, warnings: list[str]):
    warnings.append(f"{label}: {value!r} is not usable, fell back to {default!r}")
    return default


def _section(raw: dict, key: str, label: str, warnings: list[str]) -> dict:
    """A section must be an object. Anything else is ignored -- but say so, or a
    whole block of the user's config vanishes without a word."""
    value = raw.get(key)
    if value is None or isinstance(value, dict):
        return value or {}
    warnings.append(f"{label}: expected an object, got {type(value).__name__}; ignored")
    return {}


def _pick(value, allowed: set[str], default: str, label: str,
          warnings: list[str]) -> str:
    """Enum-ish string. Non-strings never reach the set -- `[] in {...}` raises."""
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        return _reject(value, default, label, warnings)
    return value


def _int(source: dict, key: str, default: int, label: str,
         warnings: list[str], minimum: int = 0) -> int:
    """Coerce to int, falling back when that is impossible. Floats truncate
    (30.7 -> 30) rather than being rejected. One bad value must not block startup.

    Every setting here is a duration or a count, so ``minimum`` guards the values
    that are nonsense below it -- poll_ms=0 turns the daemon into a busy loop,
    and 0.5 truncates straight into it.
    """
    if key not in source:
        return default
    value = source[key]
    if isinstance(value, bool):     # bool is an int in Python; almost never intended here
        return _reject(value, default, label, warnings)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return _reject(value, default, label, warnings)
    if number < minimum:
        return _reject(value, default, label, warnings)
    return number


def _bool(source: dict, key: str, default: bool, label: str,
          warnings: list[str]) -> bool:
    """Only real JSON booleans. bool("false") is True, which is never what was meant."""
    if key not in source:
        return default
    value = source[key]
    return value if isinstance(value, bool) else _reject(value, default, label, warnings)


def _strings(value, default: tuple[str, ...], label: str,
             warnings: list[str]) -> tuple[str, ...]:
    """A list of strings. A bare string would otherwise be shredded into characters."""
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return _reject(value, default, label, warnings)
    return tuple(value)


def load(path: Path | None) -> tuple[Config, list[str]]:
    warnings: list[str] = []
    raw: dict = {}

    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:
            warnings.append(f"config unreadable, using all defaults: {exc}")
            raw = {}

    # JSON only guarantees syntax. A file can parse cleanly and still be a list,
    # or hold a list where a string belongs -- every value below is shape-checked.
    if not isinstance(raw, dict):
        warnings.append(f"config must be an object, got {type(raw).__name__}; "
                        "using all defaults")
        raw = {}

    gate = _section(raw, "gate", "gate", warnings)
    glow = _section(raw, "underglow", "underglow", warnings)
    timing = _section(raw, "timing", "timing", warnings)
    state = _section(raw, "state", "state", warnings)
    tabs = _section(raw, "tab_switch", "tab_switch", warnings)

    return Config(
        gate_mode=_pick(gate.get("mode"), _GATE_MODES, "frontmost", "gate.mode", warnings),
        yield_to=_strings(gate.get("yield_to"), ("com.openai.chat",),
                          "gate.yield_to", warnings),
        own_when=_strings(gate.get("own_when"), ("com.googlecode.iterm2",),
                          "gate.own_when", warnings),
        mod_key=_pick(raw.get("mod_key"), _MOD_KEYS, "C7", "mod_key", warnings),
        knob_tab_switch=_bool(tabs, "knob", True, "tab_switch.knob", warnings),
        mod_direct_tab=_bool(tabs, "mod_direct", True, "tab_switch.mod_direct", warnings),
        underglow_iterm=_pick(
            _section(glow, "when_iterm", "underglow.when_iterm", warnings).get("mode"),
            _UNDERGLOW_MODES, "outside", "underglow.when_iterm.mode", warnings),
        underglow_codex=_pick(
            _section(glow, "when_codex", "underglow.when_codex", warnings).get("mode"),
            _UNDERGLOW_MODES, "all_claude", "underglow.when_codex.mode", warnings),
        ttl_minutes=_int(state, "ttl_minutes", 30, "state.ttl_minutes",
                         warnings, minimum=1),
        # 0 is meaningful here: fade off immediately.
        done_fade_seconds=_int(state, "done_fade_seconds", 180,
                               "state.done_fade_seconds", warnings, minimum=0),
        poll_ms=_int(timing, "poll_ms", 250, "timing.poll_ms",
                     warnings, minimum=1),
        mod_release_timeout_ms=_int(timing, "mod_release_timeout_ms", 5000,
                                    "timing.mod_release_timeout_ms",
                                    warnings, minimum=1),
    ), warnings
