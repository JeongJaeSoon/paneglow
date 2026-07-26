"""설정 로드. 틀린 값은 기본값으로 떨어뜨리고 경고를 모은다 — 기동을 막지 않는다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_GATE_MODES = {"frontmost", "always", "off"}
_UNDERGLOW_MODES = {"outside", "all_claude", "current_tab", "off"}
#: C5·C6 은 넓은 캡 하나를 공유해 누르면 두 id 가 함께 온다.
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


def _pick(value, allowed: set[str], default: str, label: str,
          warnings: list[str]) -> str:
    if value is None:
        return default
    if value not in allowed:
        warnings.append(f"{label}: {value!r} 은 쓸 수 없어 {default!r} 로 대체했습니다")
        return default
    return value


def _int(source: dict, key: str, default: int, label: str,
         warnings: list[str]) -> int:
    """숫자가 아니면 기본값으로 떨어뜨린다. 설정 하나 때문에 못 뜨면 안 된다."""
    if key not in source:
        return default
    try:
        return int(source[key])
    except (TypeError, ValueError):
        warnings.append(f"{label}: {source[key]!r} 은 숫자가 아니라 {default} 로 대체했습니다")
        return default


def load(path: Path | None) -> tuple[Config, list[str]]:
    warnings: list[str] = []
    raw: dict = {}

    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:
            warnings.append(f"설정을 읽지 못해 전부 기본값을 씁니다: {exc}")
            raw = {}

    gate = raw.get("gate") or {}
    glow = raw.get("underglow") or {}
    timing = raw.get("timing") or {}
    state = raw.get("state") or {}
    tabs = raw.get("tab_switch") or {}

    return Config(
        gate_mode=_pick(gate.get("mode"), _GATE_MODES, "frontmost", "gate.mode", warnings),
        yield_to=tuple(gate.get("yield_to") or ("com.openai.chat",)),
        own_when=tuple(gate.get("own_when") or ("com.googlecode.iterm2",)),
        mod_key=_pick(raw.get("mod_key"), _MOD_KEYS, "C7", "mod_key", warnings),
        knob_tab_switch=bool(tabs.get("knob", True)),
        mod_direct_tab=bool(tabs.get("mod_direct", True)),
        underglow_iterm=_pick((glow.get("when_iterm") or {}).get("mode"),
                              _UNDERGLOW_MODES, "outside",
                              "underglow.when_iterm.mode", warnings),
        underglow_codex=_pick((glow.get("when_codex") or {}).get("mode"),
                              _UNDERGLOW_MODES, "all_claude",
                              "underglow.when_codex.mode", warnings),
        ttl_minutes=_int(state, "ttl_minutes", 30, "state.ttl_minutes", warnings),
        done_fade_seconds=_int(state, "done_fade_seconds", 180,
                               "state.done_fade_seconds", warnings),
        poll_ms=_int(timing, "poll_ms", 250, "timing.poll_ms", warnings),
        mod_release_timeout_ms=_int(timing, "mod_release_timeout_ms", 5000,
                                    "timing.mod_release_timeout_ms", warnings),
    ), warnings
