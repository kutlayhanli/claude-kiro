"""TC-130 and TC-131 (a): bench arm role maps under OpenCode, and Claude Code ignoring `opencode.roles`.

The real bench preparation runs (clone, `ck agents set`, config write, args module); only the
`opencode` binary is faked. The fake reads the clone's `specs/ck.json` back and stores it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from bench_helpers import CK, bench_setup, ck_project

from claude_kiro import bench

ROOT = Path(__file__).resolve().parents[1]
ARM_MODEL = "opencode-go/muse-spark-1.3-contributor"
REVIEWER = "opencode-go/deepseek-v4.1-flash#medium"
ROLES = ("implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "orchestrator", "resolver")

FAKE_OPENCODE = """#!/usr/bin/env python3
import json, os, pathlib, time

cwd = pathlib.Path(os.environ.get("PWD") or os.getcwd())
agents = cwd / ".opencode" / "agents"
agents.mkdir(parents=True, exist_ok=True)
(agents / "ck-worker.md").write_text("---\\nmode: subagent\\n---\\n")
state = pathlib.Path(os.environ["CK_BENCH_STATE"])
# Read back the config the bench wrote into the clone.
(state / "seen-ck.json").write_text((cwd / "specs" / "ck.json").read_text())
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
(run / "bench-output.json").write_text(json.dumps({"result": report, "status": "done", "durationMs": 60000, "workflowProgress": []}))
"""


def _run_arm(tmp_path, arm_conf: dict) -> tuple[dict, dict]:
    fake = tmp_path / "opencode"
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    config_path = bench_setup(tmp_path)
    raw = json.loads(config_path.read_text())
    raw["configs"] = [{"name": "beam", "model": ARM_MODEL, "agents": {"reviewer.enabled": "false"}, "run_args": [], **arm_conf}]
    raw["opencode"] = {"plugin": str(ROOT / "opencode"), "bin": str(fake)}
    raw["timeout_minutes"] = 1
    config_path.write_text(json.dumps(raw))
    cfg = bench.load_config(config_path)
    arm = bench.plan_arms(cfg)[0]
    result = bench.run_arm_opencode(cfg, arm, echo=lambda _: None)
    seen = tmp_path / "runs" / "opencode-state" / arm["id"] / "claude-kiro" / "opencode" / "seen-ck.json"
    return result, json.loads(seen.read_text())


def _effective(oc: dict, role: str) -> str:
    """The model a role resolves to: the role map first, then the alias map (as the plugin does)."""
    roles = oc.get("roles") or {}
    if role in roles:
        return roles[role]
    return oc["models"]["sonnet"]


def test_tc130_arm_roles_map_the_reviewer_and_leave_the_rest_on_the_arm_model(tmp_path):
    result, seen = _run_arm(tmp_path, {"roles": {"reviewer": REVIEWER}})
    assert result["source"] == "opencode"
    oc = seen["opencode"]
    assert oc["roles"]["reviewer"] == REVIEWER
    for role in ROLES:
        if role != "reviewer":
            assert _effective(oc, role) == ARM_MODEL, role
    assert _effective(oc, "reviewer") == REVIEWER


def test_tc130_arm_without_roles_keeps_every_role_on_the_arm_model(tmp_path):
    _, seen = _run_arm(tmp_path, {})
    for role in ROLES:
        assert _effective(seen["opencode"], role) == ARM_MODEL, role


def _agents_json(root: Path) -> dict:
    p = subprocess.run([*CK, "agents", "--json"], cwd=root, check=True, capture_output=True, text=True, env=os.environ.copy())
    return json.loads(p.stdout)


def test_tc131a_opencode_roles_do_not_change_ck_agents(tmp_path):
    root = ck_project(tmp_path)
    before = _agents_json(root)
    path = root / "specs" / "ck.json"
    data = json.loads(path.read_text()) if path.is_file() else {}
    data["opencode"] = {"roles": {"reviewer": REVIEWER, "implementer": ARM_MODEL + "#medium"}}
    path.write_text(json.dumps(data, indent=2) + "\n")
    assert _agents_json(root) == before
