"""The OpenCode bench runner: clone, configure, launch a probe, import the record.

The args/probe module is exercised separately in `test_bench_opencode.py`; here it is stubbed so the
runner's record discovery, import, native token/cost overlay and fail-closed rules are tested without
a model or a server.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import types
from pathlib import Path

import pytest
from bench_helpers import bench_setup

from claude_kiro import bench
from claude_kiro.cli.main import cli
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENCODE = """#!/usr/bin/env python3
import json, os, pathlib, time

state = pathlib.Path(os.environ["CK_BENCH_STATE"])
run = state / "runs" / "wf-fake"
run.mkdir(parents=True, exist_ok=True)
now = int(time.time() * 1000)
report = {
    "outcome": "complete", "into": "integrate/calc", "targetGreen": True,
    "merged": ["1"], "failed": [], "notStarted": [],
    "timing": {"start": "2026-10-10T10:00:00Z", "end": "2026-10-10T10:01:00Z", "minutes": 1.0},
    "finalGate": {"green": True}, "fullGates": [], "stats": {"merged": 1, "failed": 0, "notStarted": 0},
}
record = {
    "schema": "ck-opencode-run/1", "runId": "wf-fake", "harness": "opencode",
    "workflow": "spec-implement", "collectorVersion": "0.11.0", "ckVersion": "0.11.0",
    "claudeCodeVersion": None, "status": "done", "startedAt": now, "endedAt": now + 60000,
    "projectRoot": os.getcwd(), "spec": "calc", "into": "integrate/calc",
    "args": {"spec": "calc"}, "result": report, "throttles": 0, "retries": 0,
    "failureLabel": None, "degraded": [],
    "guard": {"denials": 0, "postCheckCatches": 0, "guardEscalations": 0, "headlessDenials": 0, "prompts": 0},
    "agents": [],
}
(run / "record.json").write_text(json.dumps(record))
(run / "bench-output.json").write_text(json.dumps({
    "result": report, "status": "done", "durationMs": 60000, "workflowProgress": [],
}))
"""


def _fake_args_module(monkeypatch):
    module = types.ModuleType("claude_kiro.bench_opencode")

    def workflow_args(clone, spec, ck, env, *, interactive=False):
        return {"spec": spec, "root": str(clone), "interactive": interactive}

    def prepare_probe(clone, package_root, args, state):
        path = Path(clone) / ".bench-args.json"
        path.write_text(json.dumps(args))
        return path

    module.workflow_args = workflow_args
    module.prepare_probe = prepare_probe
    monkeypatch.setitem(sys.modules, "claude_kiro.bench_opencode", module)


def _opencode_config(tmp_path, plugin: str = str(ROOT / "opencode")) -> tuple[Path, dict]:
    config_path = bench_setup(tmp_path)
    cfg = json.loads(config_path.read_text())
    cfg["configs"] = [{
        "name": "beam", "model": "opencode/fake",
        "agents": {"implementer.model": "sonnet", "reviewer.enabled": "false"},
        "run_args": [],
    }]
    cfg["opencode"] = {"plugin": plugin, "bin": str(tmp_path / "opencode")}
    cfg["timeout_minutes"] = 1
    path = tmp_path / "bench-opencode.json"
    path.write_text(json.dumps(cfg))
    return path, cfg


def _fake_bin(tmp_path) -> Path:
    path = tmp_path / "opencode"
    path.write_text(FAKE_OPENCODE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_run_arm_opencode_imports_the_record_and_bench_output(tmp_path, monkeypatch):
    _fake_args_module(monkeypatch)
    _fake_bin(tmp_path)
    config_path, _ = _opencode_config(tmp_path)
    cfg = bench.load_config(config_path)
    arm = bench.plan_arms(cfg)[0]

    result = bench.run_arm_opencode(cfg, arm, echo=lambda _: None)

    assert result["source"] == "opencode"
    assert result["harness"] == "opencode"
    assert result["run_id"] == "wf-fake"
    assert result["record"].endswith("record.json")
    assert result["report"]["merged"] == ["1"]
    assert result["ck_version"] == "0.11.0"
    assert result["wall_minutes"] == 1.0
    assert result["fail_closed"] is False
    assert result["guard"]["denials"] == 0
    assert (Path(cfg["results_dir"]) / f"{arm['id']}.json").is_file()
    clone = Path(result["clone"])
    assert (clone / ".bench-args.json").is_file()


def test_run_arm_opencode_fails_without_a_record(tmp_path, monkeypatch):
    _fake_args_module(monkeypatch)
    fake = tmp_path / "opencode-no-record"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    config_path, _ = _opencode_config(tmp_path)
    cfg = bench.load_config(config_path)
    cfg["opencode"]["bin"] = str(fake)
    arm = bench.plan_arms(cfg)[0]

    with pytest.raises(bench.BenchError, match="no OpenCode run record"):
        bench.run_arm_opencode(cfg, arm, echo=lambda _: None)


def test_run_arm_opencode_requires_a_full_model_ref(tmp_path, monkeypatch):
    _fake_args_module(monkeypatch)
    _fake_bin(tmp_path)
    config_path, cfg_raw = _opencode_config(tmp_path)
    cfg_raw["configs"][0].pop("model")
    config_path.write_text(json.dumps(cfg_raw))
    cfg = bench.load_config(config_path)
    arm = bench.plan_arms(cfg)[0]

    with pytest.raises(bench.BenchError, match="full `model`"):
        bench.run_arm_opencode(cfg, arm, echo=lambda _: None)


def test_cli_dry_run_names_the_opencode_harness(tmp_path):
    config_path, _ = _opencode_config(tmp_path)
    result = CliRunner().invoke(cli, ["bench", "run", str(config_path), "--harness", "opencode", "--dry-run"])
    assert result.exit_code == 0
    assert "opencode harness" in result.output


def test_cli_opencode_requires_a_plugin(tmp_path, monkeypatch):
    monkeypatch.delenv("CK_OPENCODE_PLUGIN", raising=False)
    config_path, cfg = _opencode_config(tmp_path)
    cfg["opencode"].pop("plugin", None)
    config_path.write_text(json.dumps(cfg))
    result = CliRunner().invoke(cli, ["bench", "run", str(config_path), "--harness", "opencode", "--dry-run"])
    assert result.exit_code == 2
    assert "opencode.plugin" in result.output
