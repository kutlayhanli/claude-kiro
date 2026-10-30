"""Self-tests of the Node test harness: execCk fake agents, the create op and the virtual clock (Task 3)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

JS = Path(__file__).parent / "js"
pytestmark = [pytest.mark.usefixtures("no_network"), pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]


def node(script: str) -> object:
    """Run an ES-module snippet in tests/js and parse the JSON it prints."""
    proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=JS, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


DRY = "const dry = { cmd: ['ck'], env: {}, dry: true }\n"


def agent_log(prompts_and_labels) -> list:
    """Feed (label, prompt) pairs to a dry-mode fake project; return each agent call's recorded argv list."""
    snippet = (
        "import { project } from './fake_project.mjs'\n" + DRY
        + "const p = project({ deps: { '1': [], '2': ['1'] }, execCk: dry })\n"
        + "const out = []\n"
        + f"for (const [label, prompt] of {json.dumps(prompts_and_labels)}) {{\n"
        + "  const before = p.state.execLog.length\n  await p.agent(prompt, { label })\n  out.push(p.state.execLog.slice(before))\n}\n"
        + "console.log(JSON.stringify(out))"
    )
    return node(snippet)  # type: ignore[return-value]


def test_execck_dry_claim_prompts_bare_and_quoted_forms():
    bare = "Claim it, on its own line:\nck -C /wt/demo-task-1 worktree claim demo 1 --progress-run R1 --progress-event task-start --progress-model sonnet --attempt 1\nthen work"
    quoted = 'Claim with "ck -C /wt/demo-task-1 worktree claim demo 1 --takeover --progress-run R1 --progress-event retry --progress-model opus --attempt 2", fix it'
    plain = "Claim it, on its own line:\nck -C /wt/demo-task-1 worktree claim demo 1\nthen work"
    # The task agent also needs a plan-free label; the dry run answers the task normally.
    logs = agent_log([["task 1", bare], ["task 1 retry", quoted], ["task 1 plain", plain]])
    assert logs[0] == [["progress", "event", "demo", "task-start", "--run", "R1", "--task", "1", "--model", "sonnet", "--attempt", "1"]]
    assert logs[1] == [["progress", "event", "demo", "retry", "--run", "R1", "--task", "1", "--model", "opus", "--attempt", "2"]]
    assert logs[2] == []  # no progress flags: nothing is recorded


def test_execck_dry_land_prompt_uses_the_fake_projects_own_result():
    land = "Command:  ck -C /repo worktree land demo 1 2 3 --into integrate/demo --gate --install --release --progress-run R1 --json"
    snippet = (
        "import { project } from './fake_project.mjs'\n" + DRY
        + "const p = project({ deps: { '1': [], '2': [], '3': [] }, checkRed: ['2'], protectedTasks: ['3'], execCk: dry })\n"
        + f"const ans = await p.agent({json.dumps(land)}, {{ label: 'land 1 2 3' }})\n"
        + "console.log(JSON.stringify({ log: p.state.execLog, ans }))"
    )
    out = node(snippet)
    assert out["log"] == [
        ["progress", "event", "demo", "landing", "--run", "R1", "--json", "--tasks", "1,2,3", "--merged", "1", "--red", "2", "--protected", "3"],
        ["progress", "event", "demo", "hand-landing", "--run", "R1", "--json", "--task", "3"],  # the fake project lists PROTECTED first
        ["progress", "event", "demo", "merge", "--run", "R1", "--json", "--task", "1"],
    ]
    assert sorted(t["state"] for t in out["ans"]["tasks"]) == ["MERGED", "PROTECTED", "RED"]


def test_execck_eta_answer_overrides():
    land = "ck -C /repo worktree land demo 1 --into x --progress-run R1 --json"
    for mode, check in (("null", lambda a: a["eta"] is None), ("number", lambda a: isinstance(a["eta"], int)), ("missing", lambda a: "eta" not in a)):
        snippet = (
            "import { project } from './fake_project.mjs'\n" + DRY
            + f"const p = project({{ deps: {{ '1': [] }}, etaAnswer: '{mode}', execCk: dry }})\n"
            + f"console.log(JSON.stringify(await p.agent({json.dumps(land)}, {{ label: 'land 1' }})))"
        )
        assert check(node(snippet)), mode


REAL = "{ cmd: [" + json.dumps(sys.executable) + ", '-m', 'claude_kiro.cli'], env: {} }"


def real_run(prompt: str, label: str = "task 1") -> dict:
    """Feed one prompt to a fake project that runs commands through the real CLI; return execLog and execResults."""
    snippet = (
        "import { project } from './fake_project.mjs'\n"
        f"const p = project({{ deps: {{ '1': [] }}, execCk: {REAL} }})\n"
        f"await p.agent({json.dumps(prompt)}, {{ label: {json.dumps(label)} }})\n"
        "console.log(JSON.stringify({ log: p.state.execLog, results: p.state.execResults }))"
    )
    return node(snippet)  # type: ignore[return-value]


def test_execck_runs_a_backticked_real_ck_command():
    out = real_run("Check the CLI with `ck --version` now")
    assert out["log"] == [["--version"]]
    (res,) = out["results"]
    assert res["code"] == 0, res
    assert res["stdout"].strip() and res["error"] is None  # the CLI really ran and printed its version
    direct = subprocess.run([sys.executable, "-m", "claude_kiro.cli", "--version"], capture_output=True, text=True, timeout=60)
    assert res["stdout"] == direct.stdout


def test_execck_surfaces_a_failing_command():
    # `ck plan` fails for a spec with no tasks.md whatever its options; `ck progress event` always exits 0 (design),
    # so it cannot show a failure.
    out = real_run("Run `ck plan no-such-spec --failed 1` now")
    assert out["log"] == [["plan", "no-such-spec", "--failed", "1"]]
    (res,) = out["results"]
    assert res["code"] != 0 and res["error"] and res["stderr"].strip(), res


def test_execck_runs_commands_written_in_land_prompts_and_ignores_a_closing_backtick():
    land = "ck -C /repo worktree land demo 1 --into x --progress-run R1 --json\nAlso `ck --version`"
    out = real_run(land, "land 1")
    assert ["--version"] in out["log"]
    claim = "Claim: `ck -C /wt worktree claim demo 1 --progress-run R1 --progress-event task-start --attempt 1`"
    snippet = (
        "import { findClaim } from './fake_project.mjs'\n"
        f"console.log(JSON.stringify(findClaim({json.dumps(claim)})))"
    )
    assert node(snippet)["flags"]["attempt"] == "1"


def test_bridge_real_clock_runs_with_real_timers(bridge):  # noqa: F811
    out = bridge.run({"deps": {"1": []}, "durations": {"1": 5}}, clock="real")
    assert out["result"]


def test_virtual_clock_fails_fast_when_idle_with_nothing_pending():
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", "import { VirtualClock } from './virtual_clock.mjs'\nconst c = new VirtualClock()\ntry { await c.drive(new Promise(() => {})); console.log('no') } catch (e) { console.log(e.message) }"],
        cwd=JS, capture_output=True, text=True, timeout=60,
    )
    assert "virtual clock idle" in proc.stdout, proc.stdout + proc.stderr


def test_create_op_runs_spec_create_to_completion(bridge):  # noqa: F811
    out = bridge.create({"mode": "create"})
    assert out["result"]["review"]["fixed"] == ["R1"]
    assert len(out["result"]["files"]) == 5
    assert "write requirements.md" in [e["label"] for e in out["trace"] if e["ev"] == "start"]
    assert out["execLog"] == [] and out["execResults"] == []


def test_virtual_clock_orders_timers_by_due_time_then_creation(bridge):  # noqa: F811
    out = node(
        "import { VirtualClock } from './virtual_clock.mjs'\n"
        "const c = new VirtualClock()\nconst order = []\n"
        "const mk = (name, ms) => c.sleep(ms).then(() => order.push([name, c.now()]))\n"
        "await c.drive(Promise.all([mk('a5', 5), mk('b2', 2), mk('c5', 5), mk('d0', 0)]))\n"
        "const t0 = c.now()\nawait c.drive((async () => { await c.sleep(7); await c.sleep(3) })())\n"
        "console.log(JSON.stringify({ order, t0, t1: c.now() }))"
    )
    assert out["order"] == [["d0", 0], ["b2", 2], ["a5", 5], ["c5", 5]]
    assert out["t1"] - out["t0"] == 10


def test_virtual_clock_advances_only_after_microtasks_drain():
    out = node(
        "import { VirtualClock } from './virtual_clock.mjs'\n"
        "const c = new VirtualClock()\nconst seen = []\n"
        "const p = (async () => {\n  const s = c.sleep(5)\n  for (let i = 0; i < 50; i++) await Promise.resolve()\n  seen.push(c.now())\n  await s\n  seen.push(c.now())\n})()\n"
        "await c.drive(p)\nconsole.log(JSON.stringify(seen))"
    )
    assert out == [0, 5]


def test_bridge_virtual_clock_gives_exact_durations_and_identical_traces(bridge):  # noqa: F811
    project = {"deps": {"1": []}, "durations": {"1": 5}, "mergeDurations": {"1": 2}}
    a = bridge.run(project, clock="virtual")
    b = bridge.run(project, clock="virtual")
    assert a["trace"] == b["trace"]
    ts = {(e["label"], e["ev"]): e["t"] for e in a["trace"]}
    assert ts[("task 1", "end")] - ts[("task 1", "start")] == 5
