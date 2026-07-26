import json
from pathlib import Path

import pytest

from paneglow.config import Config, load


def test_missing_file_gives_defaults(tmp_path: Path):
    cfg, warnings = load(tmp_path / "nope.json")
    assert cfg.mod_key == "C7"
    assert cfg.gate_mode == "frontmost"
    assert warnings == []


def test_user_values_override(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mod_key": "C4", "timing": {"poll_ms": 500}}))
    cfg, _ = load(p)
    assert cfg.mod_key == "C4"
    assert cfg.poll_ms == 500
    assert cfg.gate_mode == "frontmost"      # untouched values keep defaults


def test_shared_keycap_is_rejected_with_warning(tmp_path: Path):
    """C5 and C6 share one wide keycap, so pressing it reports both ids."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mod_key": "C5"}))
    cfg, warnings = load(p)
    assert cfg.mod_key == "C7"
    assert any("C5" in w for w in warnings)


def test_bad_value_falls_back_and_warns(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"mode": "sideways"}}))
    cfg, warnings = load(p)
    assert cfg.gate_mode == "frontmost"
    assert any("mode" in w for w in warnings)


def test_broken_json_does_not_raise(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    cfg, warnings = load(p)
    assert cfg.mod_key == "C7"
    assert warnings != []


def test_keep_is_only_allowed_for_other(tmp_path: Path):
    """keep refers to the previous state, so it only means anything for when_other."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"underglow": {"when_iterm": {"mode": "keep"}}}))
    cfg, warnings = load(p)
    assert cfg.underglow_iterm == "outside"
    assert any("keep" in w for w in warnings)


def test_non_numeric_timing_falls_back_instead_of_crashing(tmp_path: Path):
    """One wrong setting must not stop startup."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"timing": {"poll_ms": "fast"}}))
    cfg, warnings = load(p)
    assert cfg.poll_ms == 250
    assert any("poll_ms" in w for w in warnings)


# JSON only guarantees syntax. Every one of these parses cleanly and used to
# either raise or silently produce a nonsense Config.
@pytest.mark.parametrize("body", [
    "[]",                                        # root is not an object
    '"just a string"',                           # root is a scalar
    '{"gate": []}',                              # section is not an object
    '{"mod_key": []}',                           # unhashable -- `[] in {...}` raises
    '{"mod_key": 7}',                            # wrong scalar type
    '{"underglow": {"when_iterm": []}}',         # nested section is not an object
    '{"timing": {"poll_ms": 1e400}}',            # overflows int()
    '{"timing": {"poll_ms": true}}',             # bool is an int in Python
    '{"tab_switch": {"knob": "false"}}',         # truthy string
    '{"gate": {"yield_to": "com.openai.chat"}}',  # bare string, not a list
    '{"gate": {"own_when": [1, 2]}}',            # list of non-strings
])
def test_any_shape_of_json_still_starts(tmp_path: Path, body: str):
    p = tmp_path / "config.json"
    p.write_text(body)
    cfg, warnings = load(p)          # must not raise
    assert isinstance(cfg, Config)
    assert warnings, f"{body} should have warned"


def test_bare_string_is_not_shredded_into_characters(tmp_path: Path):
    """tuple("abc") gives ('a','b','c') -- silently worse than crashing, because
    the gate would then compare bundle ids against single letters forever."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"yield_to": "com.openai.chat"}}))
    cfg, _ = load(p)
    assert cfg.yield_to == ("com.openai.chat",)


def test_string_false_does_not_enable_the_option(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"tab_switch": {"knob": "false"}}))
    cfg, _ = load(p)
    assert cfg.knob_tab_switch is True   # default, not the truthy string


def test_real_booleans_still_work(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"tab_switch": {"knob": False, "mod_direct": False}}))
    cfg, warnings = load(p)
    assert cfg.knob_tab_switch is False
    assert cfg.mod_direct_tab is False
    assert warnings == []


@pytest.mark.parametrize("value", [0, -100, 0.5, -1])
def test_poll_ms_must_be_positive(tmp_path: Path, value):
    """poll_ms=0 turns the daemon into a busy loop, and 0.5 truncates into it."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"timing": {"poll_ms": value}}))
    cfg, warnings = load(p)
    assert cfg.poll_ms == 250
    assert any("poll_ms" in w for w in warnings)


def test_done_fade_may_be_zero(tmp_path: Path):
    """Unlike the intervals, 0 means something here: stop showing done at once."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"state": {"done_fade_seconds": 0}}))
    cfg, warnings = load(p)
    assert cfg.done_fade_seconds == 0
    assert warnings == []


def test_valid_string_list_is_kept(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"yield_to": ["a.b", "c.d"]}}))
    cfg, warnings = load(p)
    assert cfg.yield_to == ("a.b", "c.d")
    assert warnings == []
