"""Shared fakes for the `ck bench` tests: a ck project, a fake `ck run`, hidden tests.

The fake `ck run` never calls Claude. It reads the agent settings `ck bench`
wrote with `ck agents set --project`, implements subtract() on the
integration branch (correctly when the implementer is opus, wrongly
otherwise), and leaves behind what a real run leaves: the `claude -p
--output-format json` result in the log, the workflow's persisted run file and
a subagent transcript under $CLAUDE_CONFIG_DIR/projects/.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CK = [sys.executable, "-m", "claude_kiro.cli"]

TASKS = """# Implementation Tasks: Calc

### Task 1: Tests for subtract
**Status:** Not Started
**Track:** test
**Dependencies:** None

### Task 2: Implement subtract
**Status:** Not Started
**Track:** impl
**Dependencies:** Task 1
"""

FAKE_RUN = r'''
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
assert args[0] == "run", args
spec = args[1]
opt = lambda name: args[args.index(name) + 1]
sid, log, fmt = opt("--session-id"), pathlib.Path(opt("--log")), opt("--output-format")
assert fmt == "json", fmt
cwd = pathlib.Path.cwd()
model = json.loads((cwd / "specs" / "ck.json").read_text()).get("agents", {}).get("implementer", {}).get("model", "sonnet")
good = model == "opus"

def git(*a):
    subprocess.run(["git", "-c", "user.email=f@f", "-c", "user.name=fake", *a], cwd=cwd, check=True, capture_output=True)

git("checkout", "-q", "-b", f"integrate/{spec}")
body = "a - b" if good else "a + b"
(cwd / "calc.py").write_text(f"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return {body}\n")
git("commit", "-qam", "task 2: subtract")
git("checkout", "-q", "-")

full = {"opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}.get(model, "claude-sonnet-5-5")
report = {
    "halted": False, "reason": None, "into": f"integrate/{spec}", "targetGreen": True,
    "agents": {"implementer": f"{model}/medium"},
    "merged": ["1", "2"], "failed": [], "notStarted": [],
    "escalated": [] if good else [{"task": "2", "models": ["opus"], "ok": True}],
    "timing": {"start": "2026-10-10T10:00:00Z", "end": "2026-10-10T10:12:00Z", "minutes": 12.0, "phases": []},
    "tasks": [
        {"task": "1", "ok": True, "attempts": 1, "escalations": [], "review": {"verdict": "approve", "rounds": 0}},
        {"task": "2", "ok": True, "attempts": 2 if not good else 1,
         "escalations": [] if good else [{"kind": "retry", "model": "opus"}],
         "review": {"verdict": "approve", "rounds": 0 if good else 1}},
    ],
    "finalGate": {"green": True}, "fullGates": [],
    "stats": {"merged": 2, "failed": 0, "notStarted": 0},
}
progress = [
    {"type": "workflow_phase", "index": 1, "title": "Plan"},
    {"type": "workflow_agent", "index": 1, "label": "task 2", "agentId": "a1", "model": full,
     "state": "done", "tokens": 1500, "toolCalls": 3, "durationMs": 60000},
]
home = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-fake-slug" / sid
(home / "workflows").mkdir(parents=True)
(home / "workflows" / "wf_fake-1.json").write_text(json.dumps({
    "runId": "wf_fake-1", "workflowName": "spec-implement", "status": "completed", "timestamp": "2026-10-10T10:12:00Z",
    "durationMs": 720000, "result": report, "workflowProgress": progress, "agentCount": 1,
}))
sub = home / "subagents" / "workflows" / "wf_fake-1"
sub.mkdir(parents=True)
usage = {"input_tokens": 1000000, "output_tokens": 1000000, "cache_read_input_tokens": 1000000, "cache_creation_input_tokens": 0}
lines = [
    # Two lines of the same request (one per content block): counted once.
    {"type": "assistant", "requestId": "r1", "message": {"model": full, "usage": usage, "content": [{"type": "text", "text": "hi"}]}},
    {"type": "assistant", "requestId": "r1", "message": {"model": full, "usage": usage, "content": [{"type": "tool_use", "name": "Bash", "id": "t1"}]}},
]
if os.environ.get("FAKE_ASK"):
    lines.append({"type": "assistant", "requestId": "r2", "message": {"model": full, "usage": {"input_tokens": 0, "output_tokens": 0},
                  "content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "t2"}]}})
(sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
(sub / "agent-a1.meta.json").write_text(json.dumps({"description": "task 2"}))
log.parent.mkdir(parents=True, exist_ok=True)
log.write_text(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "done",
    "total_cost_usd": 3.0 if good else 1.0, "duration_ms": 720000,
    "permission_denials": [{"tool_name": "Bash", "tool_use_id": "x"}] if os.environ.get("FAKE_DENY") else [],
}))
print("fake run finished")
'''

HIDDEN_TEST = """from calc import sub


def test_task2_subtract():
    assert sub(5, 3) == 2


def test_task2_subtract_negative():
    assert sub(-1, -1) == 0
"""

INVARIANT_TEST = """from calc import add


def test_add_still_works():
    assert add(2, 2) == 4
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def ck_project(tmp_path: Path) -> Path:
    """A ck-initialised project with a calc spec, committed on main."""
    root = tmp_path / "src-repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    (root / "specs" / "calc").mkdir(parents=True)
    (root / "specs" / "calc" / "tasks.md").write_text(TASKS)
    subprocess.run([*CK, "init", "--no-allow-workflow"], cwd=root, check=True, capture_output=True)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def bench_setup(tmp_path: Path, reps: int = 1) -> Path:
    """Project, hidden tests and invariants outside it, fake runner, and a bench.json. Returns the config path."""
    root = ck_project(tmp_path)
    hidden = tmp_path / "hidden" / "calc"
    hidden.mkdir(parents=True)
    (hidden / "test_task2_subtract.py").write_text(HIDDEN_TEST)
    inv = tmp_path / "invariants"
    inv.mkdir()
    (inv / "test_invariants.py").write_text(INVARIANT_TEST)
    fake = tmp_path / "fake_ck_run.py"
    fake.write_text(FAKE_RUN)
    cfg = {
        "repo": str(root),
        "base": "main",
        "seed": 7,
        "reps": reps,
        "ck": CK,
        "runner": [sys.executable, str(fake)],
        "bench_dir": str(tmp_path / "runs"),
        "results_dir": str(tmp_path / "results"),
        "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home")},
        "specs": [{"name": "calc", "hidden_tests": str(hidden), "invariants": str(inv), "test_cmd": [sys.executable, "-m", "pytest"]}],
        "configs": [
            {"name": "opus", "agents": {"implementer.model": "opus", "reviewer.rounds": "2"}, "run_args": ["--", "--max-concurrent", "2"]},
            {"name": "haiku", "agents": {"implementer.model": "haiku"}, "run_args": []},
        ],
    }
    path = tmp_path / "bench.json"
    path.write_text(json.dumps(cfg, indent=2))
    return path


def invoke(*args: str, env: dict = None):
    from click.testing import CliRunner

    from claude_kiro.cli.main import cli

    old = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        return CliRunner().invoke(cli, list(args), catch_exceptions=False)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
