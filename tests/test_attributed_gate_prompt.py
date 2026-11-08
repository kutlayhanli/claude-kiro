"""The gate commands the real spec-implement script puts in its prompts, run through the real ck (TC-21, TC-33 b).

The script runs under the fake runtime (the bridge) on the Claude Code path (no `ck` global), against a real
`attr_project`. The test takes the prompt the script produced, extracts the `ck ...` command line from its text
and runs that command through the real CLI. Nothing here compares prompt strings to expected strings.
"""

import json
import re
import shlex
from pathlib import Path

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)
from unattended_helpers import (
    ALL_DONE,
    attr_project,
    ck_run,
    commit_all,
    failure_errors,
    integration_worktree,
    task_worktree,
)


CK_LINE = re.compile(r"^\s*(ck (?:-C \S+ )?gate\b[^\n`\"]*)$", re.M)


def gate_commands(prompt: str):
    """Every `ck [-C dir] gate ...` line of the prompt, as argv lists after the `ck` word."""
    return [shlex.split(m.group(1))[1:] for m in CK_LINE.finditer(prompt)]


def run_command(argv, cwd: Path):
    """Run one extracted ck argv through the real CLI: (exit code, parsed stdout or None, stderr)."""
    out = ck_run(cwd, *argv)
    try:
        obj = json.loads(out.stdout)
    except ValueError:
        obj = None
    return out.returncode, obj, out.stderr


def render(bridge, root: Path, *, tasks, args=None):  # noqa: F811
    project = {"deps": {t: [] for t in tasks}, "durations": {t: 1 for t in tasks}}
    base = {"root": str(root), "into": "integrate/demo", "targetDir": str(integration_worktree(root)),
            "fullGateEvery": 1, "worktreeDir": str(root / ".ck" / "worktrees")}
    return bridge.run(project, {**base, **(args or {})}, clock="virtual")


def test_tc21_thin_gate_prompt_command_runs_through_real_ck(bridge, tmp_path):  # noqa: F811
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc1_a": "pass", "test_tc2_b": "fail"})
    out = render(bridge, root, tasks=["2"])
    prompts = {label: p for label, p in out["prompts"].items() if label == "final gate" or label.startswith("full gate")}
    assert prompts, f"the script started no gate agent: {sorted(out['prompts'])}"
    for label, prompt in prompts.items():
        commands = [c for c in gate_commands(prompt) if "--attribute" in c and "--json" in c]
        assert commands, f"the {label!r} prompt holds no `ck gate --attribute --json` command line:\n{prompt}"
        argv = commands[0]
        code, obj, err = run_command(argv, root)
        assert code in (0, 1), (argv, code, err[-500:])
        assert isinstance(obj, dict), f"{argv} did not print JSON: {err[-500:]}"
        assert isinstance(obj.get("failures"), list), obj
        assert obj["failures"], "the project has a failing test, so the gate must list it"
        for entry in obj["failures"]:
            assert failure_errors(entry) == [], (entry, failure_errors(entry))


def test_tc33b_task_prompt_command_prints_one_entry_per_failure(bridge, tmp_path):  # noqa: F811
    root = attr_project(tmp_path, statuses={}, tests={})
    out = render(bridge, root, tasks=["2"])
    prompt = next((p for label, p in out["prompts"].items() if label.startswith("task 2")), None)
    assert prompt is not None, f"no task 2 prompt: {sorted(out['prompts'])}"
    commands = [c for c in gate_commands(prompt) if "--task" in c and "--json" in c]
    assert commands, f"the task prompt holds no `ck gate --task 2 --json` command line:\n{prompt}"
    argv = commands[0]
    # The command's -C directory is the task worktree; make it a real one holding a 3-failure test file.
    wt = task_worktree(root, 2)
    assert argv[:2] == ["-C", str(wt)], argv
    code, _, err = run_command(["worktree", "create", "demo", "2"], root)
    assert code == 0, err
    (wt / "tests").mkdir(exist_ok=True)
    (wt / "tests" / "test_calc_tc.py").write_text(
        "import pytest\n\n\n"
        "def test_tc1_assertion():\n    assert 1 == 2\n\n\n"
        "def test_tc1_value():\n    raise ValueError('boom')\n\n\n"
        "def test_tc2_msg():\n    assert False, 'm' * 40\n\n\n"
        "def test_tc2_ok():\n    assert True\n"
    )
    commit_all(wt, "add failing tests")
    code, obj, err = run_command(argv, root)
    assert code == 1, (argv, code, err[-500:])
    assert isinstance(obj, dict), f"{argv} did not print JSON: {err[-500:]}"
    assert len(obj["failures"]) == 3, obj["failures"]
    for entry in obj["failures"]:
        assert failure_errors(entry) == [], (entry, failure_errors(entry))
