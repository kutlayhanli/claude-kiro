"""The harness registry in `ck bench run` and `ck doctor` (TC-150 to TC-153).

Real bench and doctor CLIs through CliRunner. The fakes are the external binaries (the fake `ck run` of
`bench_helpers`, the fake `opencode` of `test_bench_opencode_run`) and one adapter registered in-process.
`claude_kiro.harnesses` is imported inside the tests, so the file collects before the module exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bench_helpers import bench_setup
from click.testing import CliRunner

from claude_kiro.cli.main import cli

BASE_NAMES = ["claude", "opencode"]


def _harnesses():
    from claude_kiro import harnesses

    return harnesses


def _registered_names() -> list:
    return list(_harnesses().registered())


@pytest.fixture
def fakeh(tmp_path):
    """Register a fake harness `fakeh` whose arm delegates to the real Claude arm runner; drop it afterwards."""
    from claude_kiro import bench as b

    h = _harnesses()
    calls = {"run_arm": [], "doctor": []}

    def run_arm(cfg, arm, echo=print):
        calls["run_arm"].append(arm["id"])
        return b.run_arm(cfg, arm, echo=echo)

    def doctor(project_dir=None):
        calls["doctor"].append(project_dir)
        click_out = "fakeh doctor ran"
        print(click_out)
        return 0

    h.register(h.Harness(name="fakeh", run_arm=run_arm, doctor=doctor, available=lambda: None))
    try:
        yield calls
    finally:
        reg = h.registered()
        if hasattr(h, "unregister"):
            h.unregister("fakeh")
        elif isinstance(reg, dict):
            reg.pop("fakeh", None)


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args))


def test_tc150_unknown_harness_in_bench_lists_every_registered_name(tmp_path):
    cfg = bench_setup(tmp_path)
    result = _invoke("bench", "run", str(cfg), "--harness", "nope", "--dry-run")
    assert result.exit_code != 0
    names = _registered_names()
    assert set(BASE_NAMES) <= set(names)
    for name in names:
        assert name in result.output, result.output
    assert "nope" in result.output


def test_tc150_unknown_harness_in_doctor_lists_every_registered_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _invoke("doctor", "--harness", "nope")
    assert result.exit_code != 0
    for name in _registered_names():
        assert name in result.output, result.output
    assert "nope" in result.output


def test_tc150_help_lists_exactly_the_registered_names(fakeh):
    names = _registered_names()
    assert "fakeh" in names
    for args in (["bench", "run", "--help"], ["doctor", "--help"]):
        out = _invoke(*args).output
        option = out[out.index("--harness"):]
        option = option.split("\n  --")[0] if "\n  --" in option else option
        for name in names:
            assert name in option, (args, option)
        assert "nope" not in option
    err = _invoke("doctor", "--harness", "zzz").output
    assert "registered: " + ", ".join(names) in err, err


def test_tc151_registered_fake_harness_runs_a_bench_arm(tmp_path, fakeh):
    config_path = bench_setup(tmp_path)
    cfg = json.loads(config_path.read_text())
    cfg["configs"] = cfg["configs"][:1]
    config_path.write_text(json.dumps(cfg))

    result = _invoke("bench", "run", str(config_path), "--harness", "fakeh")

    assert result.exit_code == 0, result.output
    assert len(fakeh["run_arm"]) == 1
    written = list((tmp_path / "results").glob("*.json"))
    assert [p.stem for p in written] == fakeh["run_arm"]


def test_tc151_registered_fake_harness_is_selectable_in_doctor(tmp_path, monkeypatch, fakeh):
    monkeypatch.chdir(tmp_path)
    result = _invoke("doctor", "--harness", "fakeh")
    assert result.exit_code == 0, result.output
    assert len(fakeh["doctor"]) == 1


def test_tc152_runner_with_opencode_harness_fails_with_todays_message(tmp_path):
    cfg = bench_setup(tmp_path)
    result = _invoke("bench", "run", str(cfg), "--harness", "opencode", "--runner", "fake-runner", "--dry-run")
    assert result.exit_code == 2
    assert "--runner is only for the Claude harness; OpenCode arms use opencode.bin in the config" in result.output


def test_tc153_arm_results_share_one_key_set(tmp_path, fakeh):
    from test_bench_opencode_run import _fake_bin, _opencode_config

    # claude and fakeh: the fake `ck run` bench
    claude_dir = tmp_path / "claude"
    fake_dir = tmp_path / "fake"
    claude_dir.mkdir()
    fake_dir.mkdir()
    keys = {}
    for label, d, harness in (("claude", claude_dir, "claude"), ("fakeh", fake_dir, "fakeh")):
        config_path = bench_setup(d)
        cfg = json.loads(config_path.read_text())
        cfg["configs"] = cfg["configs"][:1]
        config_path.write_text(json.dumps(cfg))
        assert _invoke("bench", "run", str(config_path), "--harness", harness).exit_code == 0
        (path,) = list((d / "results").glob("*.json"))
        keys[label] = set(json.loads(path.read_text()))

    # opencode: the fake `opencode` binary
    oc_dir = tmp_path / "oc"
    oc_dir.mkdir()
    _fake_bin(oc_dir)
    config_path, _ = _opencode_config(oc_dir)
    result = _invoke("bench", "run", str(config_path), "--harness", "opencode")
    assert result.exit_code == 0, result.output
    (path,) = list((oc_dir / "results").glob("*.json"))
    keys["opencode"] = set(json.loads(path.read_text()))

    assert keys["claude"] == keys["fakeh"], keys["claude"] ^ keys["fakeh"]
    assert keys["claude"] == keys["opencode"], keys["claude"] ^ keys["opencode"]
