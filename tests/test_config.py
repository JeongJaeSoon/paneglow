import json
from pathlib import Path

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
    assert cfg.gate_mode == "frontmost"      # 손대지 않은 값은 기본값


def test_shared_keycap_is_rejected_with_warning(tmp_path: Path):
    """C5·C6 은 넓은 캡 하나를 공유해 두 id 가 함께 온다."""
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
    """keep 은 직전을 참조하므로 when_other 에서만 뜻이 선다."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"underglow": {"when_iterm": {"mode": "keep"}}}))
    cfg, warnings = load(p)
    assert cfg.underglow_iterm == "outside"
    assert any("keep" in w for w in warnings)


def test_non_numeric_timing_falls_back_instead_of_crashing(tmp_path: Path):
    """설정 하나가 틀렸다고 기동을 막지 않는다."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"timing": {"poll_ms": "fast"}}))
    cfg, warnings = load(p)
    assert cfg.poll_ms == 250
    assert any("poll_ms" in w for w in warnings)
