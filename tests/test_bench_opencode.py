"""The OpenCode bench arm preparation: the spec-implement args and the harness probe files.

Every test uses the real `ck` CLI in a real temp git clone (tests/bench_helpers.py); no OpenCode
session and no model is started.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from bench_helpers import CK, ck_project, git

from claude_kiro import bench
from claude_kiro import bench_opencode as bo

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "opencode"

ARGS_KEYS = {
    "spec", "brief", "root", "worktreeDir", "into", "targetDir",
    "wave", "titles", "tracks", "risk", "complexity", "deps", "remaining",
    "agentSlots", "finishFirst", "interactive",
    "maxRetries", "onFailure", "maxConcurrent", "fullGateEvery", "mergeBatch",
    "agents",
}


def clone_project(tmp_path: Path) -> Path:
    """A clone of a real ck project, on main, with the calc spec and no model."""
    repo = ck_project(tmp_path)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(repo), str(clone))
    return clone


def env() -> dict:
    return dict(os.environ)


def ck_json(cwd: Path, *args: str) -> dict:
    proc = subprocess.run([*CK, *args], cwd=cwd, env=env(), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_workflow_args_match_ck_plan_and_ck_agents(tmp_path):
    clone = clone_project(tmp_path)
    plan = ck_json(clone, "plan", "calc", "--json")
    resolved = ck_json(clone, "agents", "--json")

    args = bo.workflow_args(clone, "calc", CK, env())

    assert set(args) == ARGS_KEYS
    assert args["spec"] == "calc"
    assert args["root"] == str(clone)
    assert args["brief"] == str(clone / ".opencode" / "ck" / "spec-implement-brief.md")

    # Everything the plan supplies is copied as it is.
    for key in ("wave", "titles", "tracks", "risk", "complexity", "deps", "remaining", "worktreeDir"):
        assert args[key] == plan[key], key
    assert args["worktreeDir"] == str(clone / ".ck" / "worktrees")
    assert args["wave"] == {"1": 1, "2": 2}
    assert args["deps"] == {"1": [], "2": ["1"]}
    assert args["remaining"] == ["1", "2"]
    assert args["risk"] == {"1": "normal", "2": "normal"}

    # The default merge target is the integration worktree the skill creates.
    target = clone / ".ck" / "worktrees" / "calc-integration"
    assert args["into"] == "integrate/calc"
    assert Path(args["targetDir"]) == target
    assert target.is_dir()
    assert git(target, "branch", "--show-current") == "integrate/calc"

    # Roles, slots and finish-first come from `ck agents --json`.
    assert args["agents"] == resolved["roles"]
    assert args["agentSlots"] == resolved["runtime"]["agentSlots"]
    assert args["finishFirst"] is resolved["roles"]["run"]["finish_first"] is True
    assert args["maxConcurrent"] == resolved["roles"]["run"]["max_concurrent"] is None

    # The skill's run limits, with an unattended (non-interactive) default.
    assert args["interactive"] is False
    assert args["maxRetries"] == 1
    assert args["onFailure"] == "continue"
    assert args["fullGateEvery"] == 10
    assert args["mergeBatch"] == 4

    # JSON-safe as written to disk.
    assert json.loads(json.dumps(args)) == args


def test_interactive_is_opt_in(tmp_path):
    clone = clone_project(tmp_path)
    args = bo.workflow_args(clone, "calc", CK, env(), interactive=True)
    assert args["interactive"] is True


def test_workflow_args_reuses_an_existing_integration_worktree(tmp_path):
    clone = clone_project(tmp_path)
    first = bo.workflow_args(clone, "calc", CK, env())
    second = bo.workflow_args(clone, "calc", CK, env())
    assert first["into"] == second["into"] == "integrate/calc"
    assert first["targetDir"] == second["targetDir"]
    assert Path(second["targetDir"]).is_dir()


def test_prepare_probe_writes_the_files_and_excludes_them(tmp_path):
    clone = clone_project(tmp_path)
    state = tmp_path / "opencode-state"
    args = bo.workflow_args(clone, "calc", CK, env())

    args_path = bo.prepare_probe(clone, PACKAGE_ROOT, args, state)

    assert args_path == clone / ".bench-args.json"
    assert json.loads(args_path.read_text()) == args
    assert json.loads((clone / ".bench-state.json").read_text()) == {"stateDir": str(state)}

    plugin = clone / ".opencode" / "plugins" / "claude-kiro.js"
    assert plugin.read_text().strip() == f'export {{ default }} from "{PACKAGE_ROOT / "src" / "index.ts"}"'
    probe = clone / ".opencode" / "plugins" / "bench-probe.js"
    assert probe.read_bytes() == (PACKAGE_ROOT / "scripts" / "bench-probe.mjs").read_bytes()

    lines = (clone / ".git" / "info" / "exclude").read_text().splitlines()
    for entry in (".opencode/agents/", ".opencode/ck/", "AGENTS.md", ".bench-args.json"):
        assert entry in lines
    # The plugin loader must still see .opencode/plugins; excluding the whole directory hides it.
    assert ".opencode/" not in lines
    assert ".opencode/plugins/" not in lines

    # A second preparation is idempotent and does not duplicate exclude entries.
    bo.prepare_probe(clone, PACKAGE_ROOT, args, state)
    again = (clone / ".git" / "info" / "exclude").read_text().splitlines()
    for entry in (".opencode/agents/", ".opencode/ck/", "AGENTS.md", ".bench-args.json"):
        assert again.count(entry) == 1

    # The setup-generated files do not dirty the setup commit; the probe files stay visible to the
    # OpenCode loader (excluding .opencode/plugins/ would hide them) and are untracked.
    status = git(clone, "status", "--porcelain")
    assert ".opencode/" in status
    assert "AGENTS.md" not in status
    assert ".opencode/agents/" not in status


def test_workflow_args_raises_bench_error_when_ck_plan_fails(tmp_path):
    clone = clone_project(tmp_path)
    with pytest.raises(bench.BenchError, match="ck plan failed"):
        bo.workflow_args(clone, "missing-spec", CK, env())
