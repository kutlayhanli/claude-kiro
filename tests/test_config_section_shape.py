"""Non-object config sections: load_config stays tolerant, the strict readers raise ConfigError.

Covers the review finding on task 28: `{"test_track": 5}` and `{"test_track": null}` must not
crash load_config (every command loads it), and test_track_validate must name test_track.validate.
"""

import json

import pytest

import claude_kiro.config as cfg
from claude_kiro.config import ConfigError, DEFAULT_CONFIG, fixture_globs, load_config


def _write_config(tmp_path, data):
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "ck.json").write_text(json.dumps(data))
    return tmp_path


def test_default_test_track_has_no_validate_key(tmp_path):
    assert DEFAULT_CONFIG["test_track"] == {}
    assert cfg.test_track_validate(load_config(tmp_path)) == []


@pytest.mark.parametrize("section", [5, None, "validate", ["validate"], True])
def test_scalar_or_null_test_track_loads_and_raises_config_error(tmp_path, section):
    project = _write_config(tmp_path, {"test_track": section})
    config = load_config(project)
    with pytest.raises(ConfigError) as info:
        cfg.test_track_validate(config)
    assert "test_track.validate" in str(info.value)


def test_explicit_null_validate_raises_config_error(tmp_path):
    config = load_config(_write_config(tmp_path, {"test_track": {"validate": None}}))
    with pytest.raises(ConfigError) as info:
        cfg.test_track_validate(config)
    assert "test_track.validate" in str(info.value)


def test_declared_validate_string_is_read(tmp_path):
    config = load_config(_write_config(tmp_path, {"test_track": {"validate": "pytest -q tests/x.py"}}))
    assert cfg.test_track_validate(config) == ["pytest -q tests/x.py"]


@pytest.mark.parametrize("section", [5, None, "recorded", ["recorded"]])
def test_non_object_fixtures_raises_config_error_naming_recorded(tmp_path, section):
    config = load_config(_write_config(tmp_path, {"fixtures": section}))
    with pytest.raises(ConfigError) as info:
        fixture_globs(config)
    assert "fixtures.recorded" in str(info.value)
