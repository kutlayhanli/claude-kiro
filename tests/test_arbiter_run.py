"""Which stops go to the arbiter (run-principles Story 6; TC-42 to 52, TC-200 to 204, 206, 207).

Every case runs the real `plugin/workflows/spec-implement.js` under the fake runtime, with the real `ck` CLI through
`exec_ck` on a real git repo (`arbiter_project`). The assertions are on the workflow's actual label sequence, its
prompts, the `ck` commands it ran and its result, never on mocks of project code.

TC-51 and TC-201 wrap the fake agent in a small node runner: TC-51 makes the `arbiter task 1` call sleep on the
virtual clock, so the ruling really is pending while the independent tasks run.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from arbiter_helpers import DEFAULT_TASKS, FAILURE_FORMS, arbiter_project, decisions_entries, seam
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

JS = Path(__file__).parent / "js"
SCRIPT = Path(__file__).parent.parent / "plugin" / "workflows" / "spec-implement.js"
PRINCIPLE = "project:test-intent"

CONFLICT = {"file": "tests/test_greet.py", "test": "test_greeting_printed", "criterion": "1.1",
            "reason": "the test compares a rounded greeting with exact equality", "fix": "compare the printed text"}
CONFLICT_B = {"file": "tests/test_other.py", "test": "test_other_printed", "criterion": "1.1",
              "reason": "the second test also cannot pass", "fix": "compare loosely"}
CMD = "greet new"


def roles(model="haiku", escalate=(), reviewer=None, risky_model=None):
    base = {"model": model, "effort": "medium", "escalate": list(escalate)}
    return {
        "implementer": {**base, **({"risky_model": risky_model} if risky_model else {})},
        "test_writer": dict(base),
        "fixer": dict(base),
        "reviewer": reviewer or {"model": "haiku", "effort": "medium", "enabled": False, "rounds": 1},
        "arbiter": {"model": "opus", "effort": "high", "risky_model": "opus", "risky_effort": "max"},
    }


REVIEW_ON = {"model": "opus", "effort": "high", "enabled": True, "rounds": 1}


def ruling(outcome, **extra):
    # "@key": the fake arbiter copies the claim key from its prompt (tests/js/fake_project.mjs), as 7.1 requires
    out = {"claim": "@key", "status": "ruled", "reasoning": f"ruled {outcome} because the principle says so", "outcome": outcome, "principle": PRINCIPLE}
    if outcome == "requirements":
        out["requirement"] = {"kind": "change", "wording": "the greeting is printed in full", "reason": "the test cannot pass as worded"}
    if outcome == "test_repair":
        out["testRepair"] = {"file": CONFLICT["file"], "test": CONFLICT["test"], "change": "compare the printed text", "why": "the test is wrong"}
    if outcome == "code":
        out["code"] = "print the greeting before exiting"
    if outcome == "spec_edit":
        out["changed"] = "rewired tasks.md"
    out.update(extra)
    return out


REQ = ruling("requirements")


def run_args(project, **extra):
    return {**project.run_args, "spec": "demo", "arbiterAgent": "spec:arbiter", "auditorAgent": "spec:auditor", **extra}


def run(bridge, project, options, **args):
    return bridge.run(options, args=run_args(project, **args), exec_ck=project.exec_ck)


def attempts(events, n="1"):
    return [e for e in events if re.match(rf"^task {n}( |$)", e) and not e.endswith(" end")]


def arbiter_calls(events, n="1"):
    return [e for e in events if re.match(rf"^arbiter task {n}( |$)", e) and not e.endswith(" end")]


def any_arbiter(events):
    return [e for e in events if e.startswith("arbiter")]


def guard_labels(events):
    return [e for e in events if re.search(r" guard \d", e)]


def models_of(out, n="1"):
    return [out["opts"][e]["model"] for e in attempts(out["events"], n)]


def needs(out, n="1"):
    return [e for e in (out["result"].get("needsInput") or []) if str(e.get("task")) == n]


def argv_text(out):
    return "\n".join(" ".join(map(str, a)) for a in out["execLog"])


def missing(providers, file="tests/test_cli.py"):
    return [{"node": f"{file}::test_greet_new", "file": file, "kind": "error", "message": "No such command 'greet'.", "truncated": False,
             "command": "uv run pytest -q", "missingCommand": {"command": CMD, "providers": providers}}]


def graph_args(deps, **extra):
    d = {str(k): [str(x) for x in v] for k, v in deps.items()}
    return {"deps": d, "remaining": list(d), **extra}


# --------------------------------------------------------------------------- TC-42 to TC-45


def test_tc42_a_test_conflict_on_the_first_attempt_goes_straight_to_the_arbiter(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT}}, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    ev = out["events"]
    assert attempts(ev) == ["task 1"], "no retry, escalation or confirmation label: " + ", ".join(ev)
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert ev.index("arbiter task 1") > ev.index("task 1"), ", ".join(ev)
    assert not any(e.startswith("testfix") for e in ev), "a requirements ruling is not a test repair"
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))


def test_tc43_a_guard_stop_goes_to_the_arbiter_with_no_guard_label(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, {"deps": {"1": []}, "guardStops": {"1": "always"}, "rulings": {"1": [REQ]}}, agents=roles("haiku", []))
    ev = out["events"]
    assert guard_labels(ev) == [], ", ".join(ev)
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert attempts(ev) == ["task 1"], ", ".join(ev)
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))


def test_tc44_blocking_findings_after_the_rounds_on_an_individually_reviewed_task_go_to_the_arbiter(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    opts = {"deps": {"1": []}, "risk": {"1": "safety"}, "reviews": {"1": ["changes"] * 6}, "rulings": {"1": [REQ]}}
    # A safety task starts at implementer.risky_model; starting on haiku with [opus] above it leaves a rung, so today's
    # reviewLoop emits `task 1 revise 2 (opus)` here and the negative assertion below can fail.
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"], REVIEW_ON, risky_model="haiku"))
    ev = out["events"]
    assert [e for e in ev if re.search(r"revise \d+ \(", e)] == [], "no escalated revision: " + ", ".join(ev)
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert any(e.startswith("review 1") for e in ev) and ev.index("arbiter task 1") > max(i for i, e in enumerate(ev) if e.startswith("review 1")), ", ".join(ev)
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))


def test_tc45_blocking_findings_flagged_by_a_landing_batch_review_go_to_the_arbiter(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    flagged = {"verdict": "changes", "findings": [{"task": "1", "severity": "blocking", "file": "src/greet.py", "issue": "the greeting is never printed"}]}
    opts = {"deps": {"1": []}, "reviews": {"1": [flagged, "changes", "changes", "changes"]}, "rulings": {"1": [REQ]}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"], REVIEW_ON))
    ev = out["events"]
    assert any(e.startswith("review-wave") for e in ev), "the batch review ran: " + ", ".join(ev)
    assert [e for e in ev if re.search(r"revise \d+ \(", e)] == [], ", ".join(ev)
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))


# --------------------------------------------------------------------------- TC-46 to TC-49


def test_tc46_a_plain_gate_failure_keeps_todays_ladder_and_the_arbiter_follows_the_top_model(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, {"deps": {"1": []}, "failUntil": {"1": 99}, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    ev = out["events"]
    assert models_of(out) == ["haiku", "haiku", "opus"], ", ".join(ev)
    assert re.fullmatch(r"task 1 retry \d+ \(opus\)", attempts(ev)[-1]), ", ".join(ev)
    sequence = [e for e in ev if (e.startswith("task 1") or e.startswith("arbiter task 1")) and not e.endswith(" end")]
    assert sequence[-1] == "arbiter task 1" and sequence[:-1] == attempts(ev), ", ".join(ev)
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))


def test_tc47_with_no_escalate_list_the_arbiter_follows_the_last_retry(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, {"deps": {"1": []}, "failUntil": {"1": 99}, "rulings": {"1": [REQ]}}, agents=roles("haiku", []))
    ev = out["events"]
    assert attempts(ev) == ["task 1", "task 1 retry 1"], ", ".join(ev)
    sequence = [e for e in ev if (e.startswith("task 1") or e.startswith("arbiter task 1")) and not e.endswith(" end")]
    assert sequence == ["task 1", "task 1 retry 1", "arbiter task 1"], ", ".join(ev)


def test_tc48_a_retry_or_escalation_that_passes_means_no_arbiter(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    one = run(bridge, project, {"deps": {"1": []}, "failUntil": {"1": 1}}, agents=roles("haiku", ["opus"]))
    assert any_arbiter(one["events"]) == [], ", ".join(one["events"])
    assert one["result"]["merged"] == ["1"]
    other = arbiter_project(tmp_path / "second")
    two = run(bridge, other, {"deps": {"1": []}, "failUntil": {"1": 2}}, agents=roles("haiku", ["opus"]))
    assert models_of(two) == ["haiku", "haiku", "opus"], ", ".join(two["events"])
    assert any_arbiter(two["events"]) == [], ", ".join(two["events"])
    assert two["result"]["merged"] == ["1"]


@pytest.mark.parametrize("state", ["BUSY", "ERROR"])
def test_tc49_a_busy_or_error_worktree_means_no_arbiter(bridge, tmp_path, state):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, {"deps": {"1": []}, "worktree": {"1": {"state": state, "detail": "elsewhere"}}}, agents=roles("haiku", ["opus"]))
    assert any_arbiter(out["events"]) == [], ", ".join(out["events"])


# --------------------------------------------------------------------------- TC-50


def test_tc50_the_arbiter_prompt_holds_the_claim_both_layers_and_the_spec_paths(bridge, tmp_path):  # noqa: F811
    plan = "# Plan: Demo\n\n## Principles\n\n- **small-steps:** ship the change in small steps.\n"
    project = arbiter_project(tmp_path, plan=plan)
    out = run(bridge, project, {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT}}, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    prompt = out["prompts"]["arbiter task 1"]
    for needle in ("test-conflict", CONFLICT["reason"], CONFLICT["file"], CONFLICT["test"], CONFLICT["criterion"],
                   "a test states what the requirement says", "ship the change in small steps",
                   "specs/demo/requirements.md", "specs/demo/design.md", "specs/demo/test-plan.md", "specs/demo/tasks.md"):
        assert needle in prompt, f"the arbiter prompt lacks {needle!r}:\n{prompt}"
    assert "haiku" in prompt, "the models tried are named:\n" + prompt
    assert re.search(r"\b[Tt]ask:? 1\b|task 1\b", prompt)
    other = arbiter_project(tmp_path / "gate")
    failures = [{"node": "tests/test_greet.py::test_tc1_greeting", "file": "tests/test_greet.py", "kind": "assertion", "message": "assert 'hi' == 'hello'", "truncated": False, "command": "pytest"}]
    got = run(bridge, other, {"deps": {"1": []}, "failUntil": {"1": 99}, "taskFailures": {"1": failures}, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    gp = got["prompts"]["arbiter task 1"]
    for needle in ("tests/test_greet.py::test_tc1_greeting", "assert 'hi' == 'hello'", "haiku", "opus", "gate-failure"):
        assert needle in gp, f"the gate-failure prompt lacks {needle!r}:\n{gp}"


# --------------------------------------------------------------------------- TC-51, TC-52


TC51_JS = """
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { VirtualClock } from './virtual_clock.mjs'
import { SCRIPT } from './load_decisions.mjs'
const req = JSON.parse(process.env.REQ)
const clock = new VirtualClock()
const p = project({ ...req.project, sleep: clock.sleep, external: q => clock.external(q), execCk: req.execCk })
// A slow arbiter: the ruling on task 1 stays pending for 200 virtual ms before the fake answers it.
const agent = async (prompt, opts) => {
  if (opts.label === 'arbiter task 1') await clock.sleep(200)
  return p.agent(prompt, opts)
}
const { result } = await runWorkflow(SCRIPT, { agent, args: req.args, clock })
console.log(JSON.stringify({ result, events: p.state.events, prompts: p.state.prompts, execLog: p.state.execLog }))
"""


def test_tc51_a_task_awaiting_the_arbiter_holds_its_dependents_while_independent_tasks_run(tmp_path):
    project = arbiter_project(tmp_path)
    deps = {"1": [], "2": ["1"], "3": [], "4": []}
    request = {
        "project": {"deps": deps, "testConflicts": {"1": {"conflict": CONFLICT}}, "rulings": {"1": [REQ]}, "durations": {"1": 5, "3": 50, "4": 10}},
        "execCk": project.exec_ck, "args": run_args(project, agents=roles("haiku", []), maxConcurrent=2),
    }
    out = run_node(TC51_JS, request)
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    ruling_at = ev.index("arbiter task 1")
    assert "task 3 end" in ev[:ruling_at], "independent task 3 finished while the ruling was pending: " + ", ".join(ev)
    assert "3" in out["result"]["merged"], out["result"]["merged"]
    assert attempts(ev, "4") and ev.index(attempts(ev, "4")[0]) < ruling_at, "the slot was released first: task 4 started while the ruling was pending: " + ", ".join(ev)
    assert [e for e in attempts(ev, "2")] == [], "task 1's dependent never started: " + ", ".join(ev)
    assert "2" not in out["result"]["merged"]


@pytest.mark.parametrize("form", FAILURE_FORMS, ids=lambda f: type(f).__name__ + (f if isinstance(f, str) else ""))
@pytest.mark.parametrize("claim", ["test-conflict", "gate-failure"])
def test_tc52_a_failed_arbiter_call_is_one_needs_input_entry_and_no_old_ladder(bridge, tmp_path, claim, form):  # noqa: F811
    project = arbiter_project(tmp_path)
    if claim == "test-conflict":
        opts = {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT}}, "rulings": {"1": [form]}}
        expected_attempts = ["task 1"]
    else:
        opts = {"deps": {"1": []}, "failUntil": {"1": 99}, "rulings": {"1": [form]}}
        expected_attempts = ["task 1", "task 1 retry 1"]
    out = run(bridge, project, opts, agents=roles("haiku", []))
    ev = out["events"]
    assert arbiter_calls(ev), "the arbiter was called: " + ", ".join(ev)
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))
    assert guard_labels(ev) == []
    assert attempts(ev) == expected_attempts, "no guard or confirmation label follows: " + ", ".join(ev)
    assert not any(e.startswith("testfix") for e in ev)


# --------------------------------------------------------------------------- TC-200 to TC-207


def revise_opts(**extra):
    return {"deps": {"1": []}, "risk": {"1": "safety"}, "guardStops": {"1": "always"}, "guardStopIn": {"1": "revise"}, "reviews": {"1": ["changes", "approve"]}, **extra}


def test_tc200_a_guard_stop_on_a_review_revision_reaches_the_arbiter(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, revise_opts(rulings={"1": [ruling("code")]}), agents=roles("haiku", ["opus"], REVIEW_ON))
    ev = out["events"]
    assert guard_labels(ev) == [], ", ".join(ev)
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    revisions = [e for e in attempts(ev) if " revise " in e]
    assert len(revisions) == 2 and revisions[0].split(" (")[0] == revisions[1].split(" (")[0], "the same revision ran again: " + ", ".join(ev)
    assert any("print the greeting before exiting" in out["prompts"][e] for e in revisions), "the ruling text is in the re-run prompt"
    assert [e for e in attempts(ev) if " retry " in e] == [], "no attempt spent: " + ", ".join(ev)
    assert "review 1 round 2" in ev, "the review continues: " + ", ".join(ev)
    assert out["result"]["merged"] == ["1"]
    other = arbiter_project(tmp_path / "requirements")
    asked = run(bridge, other, revise_opts(rulings={"1": [REQ]}), agents=roles("haiku", ["opus"], REVIEW_ON))
    assert guard_labels(asked["events"]) == []
    entries = needs(asked)
    assert len(entries) == 1, json.dumps(asked["result"].get("needsInput"))
    assert REQ["requirement"]["wording"] in json.dumps(entries[0]), json.dumps(entries[0])


TC201_JS = """
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { VirtualClock } from './virtual_clock.mjs'
import { SCRIPT } from './load_decisions.mjs'
const req = JSON.parse(process.env.REQ)
const clock = new VirtualClock()
const p = project({ ...req.project, sleep: clock.sleep, external: q => clock.external(q), execCk: req.execCk })
// The retry that follows a landed test fix guard-stops (the fake's own guardStops would stop the first attempt too).
const agent = async (prompt, opts) => {
  if (/^task 1 retry/.test(opts.label) && p.state.events.includes('testfix 1 (Task 1)')) {
    p.state.events.push(opts.label); p.state.prompts[opts.label] = prompt; p.state.opts[opts.label] = opts
    p.state.events.push(`${opts.label} end`)
    return { task: '1', status: 'blocked', gatePassed: false, summary: 'guard stop', commits: '', blocker: 'guard stop',
      guardStop: { file: 'tests/test_guarded.py', kind: 'test', test: 'tests/test_guarded.py::test_x', requirement: '1.1' } }
  }
  return p.agent(prompt, opts)
}
const { result } = await runWorkflow(SCRIPT, { agent, args: req.args, clock })
console.log(JSON.stringify({ result, events: p.state.events, prompts: p.state.prompts, execLog: p.state.execLog }))
"""


def run_node(script, request):
    env = {**os.environ, "REQ": json.dumps(request)}
    proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=JS, capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_tc201_a_guard_stop_on_the_retry_after_a_landed_test_fix_reaches_the_arbiter(tmp_path):
    project = arbiter_project(tmp_path)
    request = {
        "project": {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT}}, "rulings": {"1": [ruling("test_repair"), REQ]},
                    "testFix": {CONFLICT["file"]: {"verdict": "fixed"}}},
        "execCk": project.exec_ck, "args": run_args(project, agents=roles("haiku", ["opus"])),
    }
    out = run_node(TC201_JS, request)
    ev = out["events"]
    assert "testfix 1 (Task 1)" in ev, ", ".join(ev)
    retry = [e for e in ev if re.match(r"^task 1 retry", e) and not e.endswith(" end")]
    assert retry, "the task retried after the landed fix: " + ", ".join(ev)
    assert guard_labels(ev) == [], ", ".join(ev)
    assert len(arbiter_calls(ev)) == 2 and ev.index(arbiter_calls(ev)[1]) > ev.index(retry[0]), "the guard stop reached the arbiter: " + ", ".join(ev)


def command_claim_options(providers, deps, **extra):
    return {"deps": deps, "failUntil": {"1": 1}, "taskFailures": {"1": missing(providers)}, "provides": {CMD: [p for p in providers]}, **extra}


def test_tc202_a_missing_command_an_unfinished_task_provides_is_a_live_edge_and_no_arbiter(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    deps = {"1": [], "2": []}
    opts = command_claim_options(["2"], deps, durations={"1": 5, "2": 300}, mergeDurations={"2": 40})
    out = run(bridge, project, opts, **graph_args(deps, maxConcurrent=2))
    ev = out["events"]
    assert any_arbiter(ev) == [], ", ".join(ev)
    edge = [e for e in (out["result"].get("liveEdges") or []) if str(e["task"]) == "1"]
    assert edge and str(edge[0]["dependsOn"]) == "2", json.dumps(out["result"].get("liveEdges"))
    assert [e for e in attempts(ev) if " retry " in e] == [], "no retry spent: " + ", ".join(ev)


def test_tc203_a_missing_command_no_task_provides_goes_to_the_arbiter_as_a_test_conflict(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    deps = {"1": []}
    opts = {"deps": deps, "failUntil": {"1": 99}, "taskFailures": {"1": missing([])}, "rulings": {"1": [REQ]}}
    out = run(bridge, project, opts, **graph_args(deps))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert [e for e in attempts(ev) if " retry " in e] == [], "no confirmation label: " + ", ".join(ev)
    assert "test_greet_new" in out["prompts"]["arbiter task 1"], "the claim names the invoking test"
    assert (out["result"].get("liveEdges") or []) == []
    assert len(needs(out)) == 1, json.dumps(out["result"].get("needsInput"))


def test_tc204_a_command_cycle_is_a_claim_and_a_landed_spec_edit_resumes_the_task(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    rewired = DEFAULT_TASKS.replace("**Dependencies:** Task 1", "**Dependencies:** None")
    assert rewired != DEFAULT_TASKS
    deps = {"1": [], "2": ["1"]}
    opts = command_claim_options(["2"], deps, rulings={"1": [ruling("spec_edit")]}, arbiterEdits={"1": {"files": {"specs/demo/tasks.md": rewired}}})
    out = run(bridge, project, opts, **graph_args(deps, maxRetries=0))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert f"1|command:2:{CMD}" in argv_text(out), "the claim is keyed <task>|command:<provider>:<command>:\n" + argv_text(out)
    assert (out["result"].get("liveEdges") or []) == [], "no live edge is recorded for a cycle"
    assert not any("command cycle" in str(f.get("reason")) for f in out["result"].get("failed", [])), json.dumps(out["result"].get("failed"))
    assert len(attempts(ev)) >= 2, "the task resumed after the landed spec edit: " + ", ".join(ev)
    assert "1" in out["result"]["merged"], out["result"]["merged"]


def two_conflicts(second):
    return {"deps": {"1": []}, "testConflictsList": {"1": {"conflicts": [CONFLICT, CONFLICT_B], "attempts": 1}},
            "rulings": {"1": [ruling("test_repair"), second]}, "testFix": {CONFLICT["file"]: {"verdict": "fixed"}}}


def test_tc206_two_conflicts_in_one_attempt_are_each_ruled_and_the_fix_still_reaches_the_fixer(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, two_conflicts(REQ), agents=roles("haiku", []))
    ev = out["events"]
    assert len(arbiter_calls(ev)) == 2, ", ".join(ev)
    assert "testfix ledger 1" in ev and "testfix 1 (Task 1)" in ev, "the first ruling still reaches the fixer: " + ", ".join(ev)
    assert any(REQ["requirement"]["wording"] in json.dumps(e) for e in needs(out)), json.dumps(out["result"].get("needsInput"))
    last_fix = max(i for i, e in enumerate(ev) if e.startswith("testfix"))
    assert [e for e in ev[last_fix:] if re.match(r"^task 1 retry", e)] == [], "no task retry after the fix: " + ", ".join(ev)
    other = arbiter_project(tmp_path / "code")
    got = run(bridge, other, two_conflicts(ruling("code")), agents=roles("haiku", []))
    gev = got["events"]
    retries = [e for e in gev if re.match(r"^task 1 retry", e) and not e.endswith(" end")]
    assert len(retries) == 1, "the task retries once: " + ", ".join(gev)
    at = gev.index(retries[0])
    assert len([e for e in gev[:at] if e.startswith("arbiter task 1") and not e.endswith(" end")]) == 2, "after both claims: " + ", ".join(gev)
    assert len([e for e in gev[:at] if re.match(r"^arbiter[- ]record", e)]) >= 2, "both claims were recorded before the retry: " + ", ".join(gev)
    recorded = [a for a in got["execLog"] if "arbiter" in map(str, a) and "record" in map(str, a)]
    assert len(recorded) >= 2, "two `ck ... arbiter record` commands ran:\n" + argv_text(got)
    prompt = got["prompts"][retries[0]]
    assert "print the greeting before exiting" in prompt, "the retry carries the code ruling's text"
    assert re.search(r"were fixed and landed on", prompt), "the retry carries afterFixNote"


def test_tc207_a_test_repair_ruling_on_a_guard_stopped_revision_goes_through_the_one_hand_off(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    opts = revise_opts(rulings={"1": [ruling("test_repair")]}, testFix={CONFLICT["file"]: {"verdict": "fixed"}})
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"], REVIEW_ON))
    ev = out["events"]
    assert guard_labels(ev) == [], ", ".join(ev)
    assert "testfix ledger 1" in ev and "testfix 1 (Task 1)" in ev, ", ".join(ev)
    revisions = [e for e in attempts(ev) if " revise " in e]
    assert len(revisions) == 2, "the same revision ran again: " + ", ".join(ev)
    assert ev.index("testfix 1 (Task 1)") < ev.index(revisions[1]), ", ".join(ev)
    assert [e for e in attempts(ev) if " retry " in e] == [], "no attempt spent: " + ", ".join(ev)
    assert "review 1 round 2" in ev, "the review continues: " + ", ".join(ev)
    source = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("//"))
    sites = re.findall(r"(?<!function )handOffTestRepair\(", source)
    assert len(sites) == 1, f"handOffTestRepair has one call site, found {len(sites)}"


# --------------------------------------------------------------------------- TC-7, TC-9, TC-11, TC-12 (the principle layers)

PLAN_TEXT = "# Plan: Demo\n\n## Principles\n\n- **small-steps:** ship the change in small steps.\n"
RESERVES_PLAN = "# Plan: Demo\n\n## Principles\n\n- **Reserves design.md:** interface changes are mine to approve.\n"
CONFLICT_OPTS = {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT}}}


def test_tc7_the_arbiter_prompt_holds_the_text_of_both_layers(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path, plan=PLAN_TEXT)
    out = run(bridge, project, {**CONFLICT_OPTS, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    assert arbiter_calls(out["events"]) == ["arbiter task 1"], ", ".join(out["events"])
    prompt = out["prompts"]["arbiter task 1"]
    for needle in ("a test states what the requirement says", "remove before you optimize", "ship the change in small steps"):
        assert needle in prompt, f"the arbiter prompt lacks {needle!r}:\n{prompt}"


def test_tc9_a_ruling_naming_a_reservation_that_needs_a_design_edit_is_a_needs_input_entry_with_no_landing(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path, plan=RESERVES_PLAN)
    edited = "# Design: Demo\n\nA greeting, with a changed interface.\n"
    parse_principles = seam("arbiter.effective_principles", "parse_principles")
    reserved_by = next(p.id for p in parse_principles(RESERVES_PLAN.split("# Plan: Demo\n\n", 1)[1], "plan") if p.reserves)
    answer = ruling("spec_edit", reservedBy=reserved_by, changed="design.md: the interface changed")
    opts = {**CONFLICT_OPTS, "rulings": {"1": [answer]}, "arbiterEdits": {"1": {"files": {"specs/demo/design.md": edited}}}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"]))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert [str(e["task"]) for e in needs(out)] == ["1"], json.dumps(out["result"].get("needsInput"))
    assert [e for e in ev if e.startswith(("arbiter-land", "land arbiter"))] == [], "no landing ran: " + ", ".join(ev)
    assert "worktree land demo arbiter" not in argv_text(out), argv_text(out)
    assert (project.target / "specs" / "demo" / "design.md").read_text() != edited, "design.md is unchanged"
    assert "interface" in json.dumps(needs(out)[0]) or "design.md" in json.dumps(needs(out)[0]), json.dumps(needs(out)[0])


def make_unreadable(path):
    path.chmod(0)


def make_directory(path):
    path.unlink()
    path.mkdir()


def make_missing(path):
    path.unlink()


@pytest.mark.parametrize("damage", [make_missing, make_directory, make_unreadable], ids=lambda f: f.__name__)
def test_tc11_a_principles_file_that_is_missing_a_directory_or_unreadable_stops_the_call_and_names_the_file(bridge, tmp_path, damage):  # noqa: F811
    if damage is make_unreadable and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.fail("mode 000 cannot be tested as root: run the suite as a normal user")
    project = arbiter_project(tmp_path)
    for checkout in (project.root, project.target):
        damage(checkout / "specs" / "PRINCIPLES.md")
    try:
        out = run(bridge, project, {**CONFLICT_OPTS, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    finally:
        for checkout in (project.root, project.target):
            path = checkout / "specs" / "PRINCIPLES.md"
            if path.exists() and not path.is_dir():
                path.chmod(0o644)
    ev = out["events"]
    assert [e for e in ev if e.startswith("arbiter task")] == [], "the arbiter agent did not run: " + ", ".join(ev)
    assert "worktree land demo arbiter" not in argv_text(out), argv_text(out)
    entries = needs(out)
    assert len(entries) == 1, json.dumps(out["result"].get("needsInput"))
    assert "PRINCIPLES.md" in json.dumps(entries[0]), json.dumps(entries[0])


def plan_layer_of(project):
    entries = decisions_entries(project, "target") or decisions_entries(project, "branch")
    assert entries, "the ruling was recorded in decisions.md"
    return entries[-1].get("Plan layer", "")


@pytest.mark.parametrize("plan", [None, "# Plan: Demo\n\n## Goals\n\nSay hello.\n"], ids=["no-plan", "no-section"])
def test_tc12_without_a_plan_principles_section_the_arbiter_rules_and_the_entry_records_the_plan_layer_absent(bridge, tmp_path, plan):  # noqa: F811
    project = arbiter_project(tmp_path, plan=plan)
    out = run(bridge, project, {**CONFLICT_OPTS, "rulings": {"1": [ruling("code")]}}, agents=roles("haiku", ["opus"]))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], "the arbiter ran: " + ", ".join(ev)
    assert "a test states what the requirement says" in out["prompts"]["arbiter task 1"], "the project layer alone"
    assert "absent" in plan_layer_of(project), decisions_entries(project, "target")


# --------------------------------------------------------------------------- TC-37 to TC-41, TC-157 (the tier)

NORMAL_TIER = ("sonnet", "medium")
RISKY_TIER = ("opus", "max")
CLAIM = {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT}}}
# The claim key of CLAIM, in design.md's identity form (`19|test:tests/test_x.py::test_a`).
KEY = f"1|test:{CONFLICT['file']}::{CONFLICT['test']}"
# A valid "needs a stronger look" answer (7.1): the claim it answers, its reasoning and a citable principle.
STRONGER = {"status": "needs_stronger_look", "claim": KEY, "reasoning": "the first reading cannot tell the wording from the intent",
            "principle": PRINCIPLE, "established": "the greeting test compares a rounded value with exact equality"}


def tier_roles(complexity_routing=False):
    """Roles whose arbiter normal and risky tiers differ in both model and effort, so the tier shows in the agent options."""
    base = roles("haiku", [])
    base["implementer"]["complexity_routing"] = complexity_routing
    return {**base, "arbiter": {"model": NORMAL_TIER[0], "effort": NORMAL_TIER[1], "risky_model": RISKY_TIER[0], "risky_effort": RISKY_TIER[1]}}


def tier_of(out, label="arbiter task 1"):
    assert label in out["opts"], f"no {label!r} agent call: " + ", ".join(out["events"])
    return (out["opts"][label]["model"], out["opts"][label]["effort"])


def tier_run(bridge, project, options, complexity=None, complexity_routing=False, **args):
    """Pass risk and Complexity as a real run does: as workflow args (what the workflow reads) and in the plan answer."""
    if options.get("risk"):
        args["risk"] = options["risk"]
    if complexity:
        args["complexity"] = complexity
        options = {**options, "planSeq": [{"complexity": complexity}] * 8}
    return run(bridge, project, options, agents=tier_roles(complexity_routing), **args)


def tc37_start(bridge, tmp_path, name, **options):
    project = arbiter_project(tmp_path / name)
    out = tier_run(bridge, project, {**CLAIM, "rulings": {"1": [REQ]}, **options})
    assert arbiter_calls(out["events"]) == ["arbiter task 1"], name + ": " + ", ".join(out["events"])
    return tier_of(out)


def test_tc37_safety_guard_stop_and_repeat_claim_start_at_the_risky_tier_and_a_plain_conflict_at_the_normal_one(bridge, tmp_path):  # noqa: F811
    assert tc37_start(bridge, tmp_path, "plain") == NORMAL_TIER, "a plain conflict on a normal task"
    assert tc37_start(bridge, tmp_path, "safety", risk={"1": "safety"}) == RISKY_TIER, "a safety task"
    guard = arbiter_project(tmp_path / "guard")
    stopped = tier_run(bridge, guard, {"deps": {"1": []}, "guardStops": {"1": "always"}, "rulings": {"1": [REQ]}})
    assert arbiter_calls(stopped["events"]) == ["arbiter task 1"], ", ".join(stopped["events"])
    assert tier_of(stopped) == RISKY_TIER, "a guard stop"
    repeat = arbiter_project(tmp_path / "repeat")
    first = tier_run(bridge, repeat, {**CLAIM, "rulings": {"1": [None]}})
    assert tier_of(first) == NORMAL_TIER, "the first time the claim is seen"
    second = tier_run(bridge, repeat, {**CLAIM, "rulings": {"1": [REQ]}})
    assert arbiter_calls(second["events"]) == ["arbiter task 1"], ", ".join(second["events"])
    assert tier_of(second) == RISKY_TIER, "the same claim again after an unruled call"


def test_tc38_a_claim_returning_after_a_failed_arbiter_call_runs_at_the_risky_tier_across_a_resume(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    first = tier_run(bridge, project, {**CLAIM, "rulings": {"1": ["throw"]}})
    assert arbiter_calls(first["events"]) == ["arbiter task 1"], ", ".join(first["events"])
    assert tier_of(first) == NORMAL_TIER
    assert len(needs(first)) == 1, json.dumps(first["result"].get("needsInput"))
    resumed = tier_run(bridge, project, {**CLAIM, "rulings": {"1": [REQ]}})
    assert arbiter_calls(resumed["events"]) == ["arbiter task 1"], ", ".join(resumed["events"])
    assert tier_of(resumed) == RISKY_TIER, "the claim returned after a failed call"


def test_tc39_a_high_complexity_task_with_no_other_signal_gets_the_normal_tier(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = tier_run(bridge, project, {**CLAIM, "rulings": {"1": [REQ]}}, complexity={"1": "High"})
    assert arbiter_calls(out["events"]) == ["arbiter task 1"], ", ".join(out["events"])
    assert tier_of(out) == NORMAL_TIER


def test_tc40_needs_a_stronger_look_at_the_normal_tier_reruns_once_at_the_risky_tier_with_the_first_reasoning(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    risky_req = ruling("requirements", claim=KEY)
    out = tier_run(bridge, project, {**CLAIM, "rulings": {"1": [STRONGER, risky_req]}})
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1", "arbiter task 1 risky"], "exactly one re-run, at the risky tier: " + ", ".join(ev)
    assert tier_of(out) == NORMAL_TIER
    assert tier_of(out, "arbiter task 1 risky") == RISKY_TIER
    assert STRONGER["reasoning"] in out["prompts"]["arbiter task 1 risky"], out["prompts"]["arbiter task 1 risky"]
    entries = needs(out)
    assert len(entries) == 1, "the risky requirements ruling ends the run with one needs-input entry: " + json.dumps(out["result"].get("needsInput"))
    assert "invalid-ruling" not in json.dumps(entries[0]), "both answers are valid rulings: " + json.dumps(entries[0])


def test_tc41_needs_a_stronger_look_at_the_risky_tier_makes_no_further_call_and_the_entry_holds_what_was_established(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = tier_run(bridge, project, {**CLAIM, "risk": {"1": "safety"}, "rulings": {"1": [STRONGER, REQ]}})
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], "one call, no re-run: " + ", ".join(ev)
    assert tier_of(out) == RISKY_TIER
    entries = needs(out)
    assert len(entries) == 1, json.dumps(out["result"].get("needsInput"))
    assert STRONGER["established"] in json.dumps(entries[0]), json.dumps(entries[0])
    assert "invalid-ruling" not in json.dumps(entries[0]), "a valid stronger-look answer is routed as exhausted (5.7), not invalid: " + json.dumps(entries[0])


def untagged_risk_path_project(tmp_path, complexity):
    block = ("### Task 1: Gate\n**Status:** Not Started\n**Track:** impl\n**Requirements:** 1.1\n**Description:** Gate.\n"
             f"**Files:**\n- `src/shop/gate.py` - the gate\n**Verify:** `true`\n**Dependencies:** None\n**Complexity:** {complexity}\n\n---\n")
    project = arbiter_project(tmp_path, tasks="# Implementation Tasks: Demo\n\n## Task Breakdown\n\n" + block)
    config = {**json.loads((project.root / "specs" / "ck.json").read_text()), "risk_paths": ["**/gate.py"]}
    (project.root / "specs" / "ck.json").write_text(json.dumps(config))
    return project


def test_tc157_a_risk_path_match_with_no_risk_tag_starts_at_the_risky_tier_and_complexity_never_changes_it(bridge, tmp_path):  # noqa: F811
    project = untagged_risk_path_project(tmp_path / "paths", "Medium")
    planned = project.ck("plan", "demo", "--json")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    risk = json.loads(planned.stdout)["risk"]
    assert risk == {"1": "safety"}, "ck plan's risk map counts the risk_paths match: " + planned.stdout
    matched = tier_run(bridge, project, {**CLAIM, "risk": risk, "rulings": {"1": [REQ]}})
    assert arbiter_calls(matched["events"]) == ["arbiter task 1"], ", ".join(matched["events"])
    assert tier_of(matched) == RISKY_TIER
    tiers = []
    for level in ("High", "Low"):
        plain = arbiter_project(tmp_path / level)
        out = tier_run(bridge, plain, {**CLAIM, "rulings": {"1": [REQ]}}, complexity={"1": level})
        assert arbiter_calls(out["events"]) == ["arbiter task 1"], ", ".join(out["events"])
        tiers.append(tier_of(out))
    assert tiers[0] == tiers[1] == NORMAL_TIER, tiers
    routed = []
    for level in ("High", "Low"):
        plain = arbiter_project(tmp_path / f"routed-{level}")
        out = tier_run(bridge, plain, {**CLAIM, "rulings": {"1": [REQ]}}, complexity={"1": level}, complexity_routing=True)
        assert arbiter_calls(out["events"]) == ["arbiter task 1"], ", ".join(out["events"])
        routed.append(tier_of(out))
    assert routed[0] == routed[1] == NORMAL_TIER, "complexity routing on still never changes the arbiter tier: " + repr(routed)


# --------------------------------------------------------------------------- TC-54 to TC-60 (validity, claim identity, limits)

REWIRED = DEFAULT_TASKS.replace("**Dependencies:** Task 1", "**Dependencies:** None")


def conflict_of(name):
    return {**CONFLICT, "test": f"test_{name}", "reason": f"the {name} test cannot pass"}


def key_of(name):
    return f"1|test:{CONFLICT['file']}::test_{name}"


def code_ruling(name):
    return ruling("code", reasoning=f"reasoning-{name}-held", claim=key_of(name))


def one_claim(name, answers, attempts=1, **extra):
    return {"deps": {"1": []}, "testConflicts": {"1": {"conflict": conflict_of(name), "attempts": attempts}}, "rulings": {"1": answers}, **extra}


def many_claims(names, answers, **extra):
    return {"deps": {"1": []}, "testConflictsList": {"1": {"conflicts": [conflict_of(n) for n in names], "attempts": 1}},
            "rulings": {"1": answers}, **extra}


def needs_text(out):
    return json.dumps(needs(out))


def invalid_forms():
    def spec_edit(**extra):
        built = ruling("spec_edit", claim=KEY, reasoning=f"marker-{extra.pop('mark')}-held")
        built.update(extra)
        return built

    no_principle = spec_edit(mark="no-principle")
    del no_principle["principle"]
    return {
        "no-principle": no_principle,
        "empty-principle": spec_edit(mark="empty-principle", principle=""),
        "unknown-principle": spec_edit(mark="unknown-principle", principle="project:made-up"),
        "unknown-outcome": spec_edit(mark="unknown-outcome", outcome="revert"),
        "two-outcomes": spec_edit(mark="two-outcomes", outcome=["code", "spec_edit"]),
        "malformed": {"status": "ruled", "claim": KEY, "reasoning": "marker-malformed-held", "outcome": {"kind": "code"}, "principle": PRINCIPLE},
    }


@pytest.mark.parametrize("form", list(invalid_forms()))
def test_tc54_an_invalid_ruling_is_not_applied_not_recorded_and_goes_to_me_with_the_arbiters_output(bridge, tmp_path, form):  # noqa: F811
    project = arbiter_project(tmp_path)
    answer = invalid_forms()[form]
    opts = {**CONFLICT_OPTS, "rulings": {"1": [answer]}, "arbiterEdits": {"1": {"files": {"specs/demo/tasks.md": REWIRED}}}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"]))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert [e for e in ev if e.startswith(("arbiter-land", "land arbiter"))] == [], "no landing ran: " + ", ".join(ev)
    assert "worktree land demo arbiter" not in argv_text(out), argv_text(out)
    for where in ("target", "root", "branch"):
        assert decisions_entries(project, where) == [], f"nothing was recorded ({where})"
    assert not (project.target / "specs" / "demo" / "decisions.md").exists(), "decisions.md is absent"
    assert (project.target / "specs" / "demo" / "tasks.md").read_text() == DEFAULT_TASKS, "tasks.md is unchanged"
    entries = needs(out)
    assert len(entries) == 1, json.dumps(out["result"].get("needsInput"))
    text = json.dumps(entries[0])
    mark = "marker-malformed-held" if form == "malformed" else f"marker-{form}-held"
    assert mark in text, f"the entry carries the arbiter's output ({mark!r}): " + text
    assert out["result"].get("rulings", []) == [], "an invalid ruling is not counted as a ruling"


def test_tc55_the_same_claim_on_attempt_two_is_the_same_claim_and_a_different_test_is_not(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, one_claim("alpha", [code_ruling("alpha"), REQ], attempts=2), agents=roles("haiku", []))
    ev = out["events"]
    assert len(attempts(ev)) >= 2, "the claim came back on a later attempt: " + ", ".join(ev)
    assert arbiter_calls(ev) == ["arbiter task 1"], "the same claim on attempt 2 is not ruled again: " + ", ".join(ev)
    assert len(decisions_entries(project)) == 1, decisions_entries(project)
    again = run(bridge, project, one_claim("alpha", [REQ]), agents=roles("haiku", []))
    assert arbiter_calls(again["events"]) == [], "the same claim in a second run on the same repo is the same claim: " + ", ".join(again["events"])
    assert len(needs(again)) == 1, json.dumps(again["result"].get("needsInput"))
    assert len(decisions_entries(project)) == 1, "nothing new was recorded"
    other = run(bridge, project, one_claim("beta", [code_ruling("beta")]), agents=roles("haiku", []))
    assert arbiter_calls(other["events"]) == ["arbiter task 1"], "a different test is a different claim: " + ", ".join(other["events"])
    assert len(decisions_entries(project)) == 2, decisions_entries(project)


def test_tc56_a_claim_returning_after_its_ruling_was_applied_gets_no_call_and_the_entry_holds_the_ruling_and_the_new_evidence(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, one_claim("alpha", [code_ruling("alpha"), REQ], attempts=2), agents=roles("haiku", []))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    entries = needs(out)
    assert len(entries) == 1, json.dumps(out["result"].get("needsInput"))
    text = json.dumps(entries[0])
    assert "reasoning-alpha-held" in text, "the entry holds the earlier ruling: " + text
    assert "print the greeting before exiting" in text, text
    assert conflict_of("alpha")["reason"] in json.dumps(entries[0].get("evidence")), "the entry's evidence field holds the new report: " + text
    assert REQ["requirement"]["wording"] not in text, "the second scripted answer was never asked for: " + text


def test_tc57_a_different_claim_on_a_task_with_one_ruling_gets_its_own_ruling(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    out = run(bridge, project, many_claims(["alpha", "beta"], [code_ruling("alpha"), code_ruling("beta")]), agents=roles("haiku", []))
    ev = out["events"]
    assert len(arbiter_calls(ev)) == 2, "each claim got its own call: " + ", ".join(ev)
    entries = decisions_entries(project)
    assert len(entries) == 2, entries
    joined = json.dumps(entries)
    assert "reasoning-alpha-held" in joined and "reasoning-beta-held" in joined, joined


def test_tc58_with_three_rulings_a_fourth_claim_makes_no_call_and_the_entry_lists_the_three(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    names = ["alpha", "beta", "gamma", "delta"]
    answers = [code_ruling(n) for n in names[:3]] + [REQ]
    out = run(bridge, project, many_claims(names, answers), agents=roles("haiku", []))
    ev = out["events"]
    assert len(arbiter_calls(ev)) == 3, "the fourth claim made no arbiter call: " + ", ".join(ev)
    assert len(decisions_entries(project)) == 3, decisions_entries(project)
    entries = needs(out)
    assert entries, json.dumps(out["result"].get("needsInput"))
    text = json.dumps(entries)
    for n in names[:3]:
        assert f"reasoning-{n}-held" in text, f"the entry lists the earlier ruling {n}: " + text
    assert REQ["requirement"]["wording"] not in text, text


def test_tc59_a_normal_call_plus_a_risky_re_run_counts_one_ruling(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    first = run(bridge, project, one_claim("alpha", [{**STRONGER, "claim": key_of("alpha")}, code_ruling("alpha")]), agents=tier_roles())
    assert arbiter_calls(first["events"]) == ["arbiter task 1", "arbiter task 1 risky"], ", ".join(first["events"])
    assert len(decisions_entries(project)) == 1, "two calls, one recorded ruling: " + str(decisions_entries(project))
    second = run(bridge, project, one_claim("beta", [code_ruling("beta")]), agents=tier_roles())
    assert arbiter_calls(second["events"]) == ["arbiter task 1"], ", ".join(second["events"])
    third = run(bridge, project, one_claim("gamma", [code_ruling("gamma")]), agents=tier_roles())
    assert arbiter_calls(third["events"]) == ["arbiter task 1"], "three claims, so far two rulings by the count of one per claim: " + ", ".join(third["events"])
    assert len(decisions_entries(project)) == 3, decisions_entries(project)
    fourth = run(bridge, project, one_claim("delta", [REQ]), agents=tier_roles())
    assert arbiter_calls(fourth["events"]) == [], "the fourth claim hits the limit of 3: " + ", ".join(fourth["events"])
    text = needs_text(fourth)
    for n in ("alpha", "beta", "gamma"):
        assert f"reasoning-{n}-held" in text, text


def test_tc60_rulings_from_an_earlier_run_on_disk_count_toward_the_limits_in_a_new_run(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    earlier = run(bridge, project, many_claims(["alpha", "beta"], [code_ruling("alpha"), code_ruling("beta")]), agents=roles("haiku", []))
    assert len(arbiter_calls(earlier["events"])) == 2, ", ".join(earlier["events"])
    assert len(decisions_entries(project)) == 2, "the earlier run's rulings are on disk"
    later = run(bridge, project, many_claims(["gamma", "delta"], [code_ruling("gamma"), REQ]), agents=roles("haiku", []))
    assert len(arbiter_calls(later["events"])) == 1, "the new run's second claim was the 4th ruling: " + ", ".join(later["events"])
    assert len(decisions_entries(project)) == 3, decisions_entries(project)
    text = needs_text(later)
    for n in ("alpha", "beta", "gamma"):
        assert f"reasoning-{n}-held" in text, f"the entry lists {n}: " + text


# --------------------------------------------------------------------------- TC-62, TC-63, TC-74, TC-75, TC-146, TC-210 (how rulings are applied)

TASK_3 = ("### Task 3: Farewell\n**Status:** Not Started\n**Track:** impl\n**Requirements:** 1.1\n**Description:** Print the farewell.\n"
          "**Files:**\n- `src/bye.py` - the farewell\n**Verify:** `true`\n**Dependencies:** None\n\n---\n")
THREE_TASKS = DEFAULT_TASKS + "\n" + TASK_3
THREE_REWIRED = THREE_TASKS.replace("**Description:** Print the farewell.\n**Files:**\n- `src/bye.py` - the farewell\n**Verify:** `true`\n**Dependencies:** None",
                                    "**Description:** Print the farewell.\n**Files:**\n- `src/bye.py` - the farewell\n**Verify:** `true`\n**Dependencies:** Task 2")
SPEC_EDIT = ruling("spec_edit", changed="tasks.md: rewired the dependencies")


def after(ev, label):
    return ev[ev.index(label) + 1:]


def test_tc62_after_a_spec_edit_lands_the_task_resumes_with_attempts_and_escalations_as_before(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    opts = {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT, "attempts": 1}}, "failUntil": {"1": 99},
            "rulings": {"1": [SPEC_EDIT, REQ]}, "arbiterEdits": {"1": {"files": {"specs/demo/tasks.md": REWIRED}}}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"]))
    ev = out["events"]
    assert arbiter_calls(ev)[0] == "arbiter task 1", ", ".join(ev)
    assert (project.target / "specs" / "demo" / "tasks.md").read_text() == REWIRED, "the spec edit landed on the integration branch"
    resumed = [e for e in after(ev, "arbiter task 1") if re.match(r"^task 1( |$)", e) and not e.endswith(" end")]
    assert resumed, "the task resumed after the landing: " + ", ".join(ev)
    assert "(opus)" not in resumed[0], "the resume itself spent no escalation: " + resumed[0]
    # The conflicting attempt, the resume, then the whole ladder as a run with no ruling has it: retry 1, then opus.
    assert models_of(out) == ["haiku", "haiku", "haiku", "opus"], ", ".join(ev)
    plain = arbiter_project(tmp_path / "plain")
    base = run(bridge, plain, {"deps": {"1": []}, "failUntil": {"1": 99}, "rulings": {"1": [REQ]}}, agents=roles("haiku", ["opus"]))
    assert models_of(base) == ["haiku", "haiku", "opus"], ", ".join(base["events"])
    assert models_of(out)[2:] == models_of(base)[1:], "the ruling spent neither a retry nor an escalation"


def test_tc63_a_landed_edit_that_adds_a_dependency_on_a_not_started_task_changes_the_next_plans_schedule(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path, tasks=THREE_TASKS)

    def schedule():
        planned = project.ck("plan", "demo", "--json", cwd=project.target)
        assert planned.returncode == 0, planned.stdout + planned.stderr
        return json.loads(planned.stdout)

    before = schedule()
    assert "3" in before["ready"], "task 3 is schedulable before the edit: " + json.dumps(before)
    # The in-run plans: before the landing the fake holds task 3 back behind task 1, so task 3 cannot start while the
    # ruling is pending (6.9 frees the slot then). Once the arbiter's commit lands, the plans carry the landed wiring
    # (`landedDeps`, as `ck plan` reading the landed tasks.md): task 3 waits on task 2. Task 1 resumes and merges, task 2
    # never passes its gate, so under the new wiring task 3 never starts; under the old wiring it would start once task 1
    # merged and task 2 was parked.
    opts = {"deps": {"1": [], "2": ["1"], "3": ["1"]}, "testConflicts": {"1": {"conflict": CONFLICT, "attempts": 1}},
            "failUntil": {"2": 99}, "rulings": {"1": [SPEC_EDIT], "2": [REQ]},
            "arbiterEdits": {"1": {"files": {"specs/demo/tasks.md": THREE_REWIRED}}}, "landedDeps": {"1": {"3": ["2"]}}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"]), maxConcurrent=1)
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert (project.target / "specs" / "demo" / "tasks.md").read_text() == THREE_REWIRED, "the edit landed"
    lands = [i for i, e in enumerate(ev) if e.startswith(("arbiter-land", "land arbiter")) and not e.endswith(" end")]
    assert lands, "the arbiter's edit went through a landing step: " + ", ".join(ev)
    land_at = lands[0]
    replans = [e for e in ev[land_at + 1:] if re.match(r"^plan \d+", e)]
    assert replans, "the workflow planned again after the landing: " + ", ".join(ev)
    assert attempts(ev, "3") == [], "task 3 now waits on task 2, which never merged, so it never started: " + ", ".join(ev)
    assert "3" not in out["result"]["merged"], json.dumps(out["result"])
    later = schedule()
    assert later != before, "the next plan differs"
    assert "3" not in later["ready"], "task 3 now waits on task 2: " + json.dumps(later)
    assert [str(d) for d in later["deps"]["3"]] == ["2"], json.dumps(later["deps"])


def test_tc74_a_code_ruling_puts_the_ruling_in_the_retry_prompt_and_a_failing_gate_after_it_leaves_the_task_unfinished(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    # The conflict comes once: the retry after the ruling is a plain gate failure, never passing (failUntil).
    opts = {"deps": {"1": []}, "testConflicts": {"1": {"conflict": CONFLICT, "attempts": 1}}, "failUntil": {"1": 99},
            "rulings": {"1": [ruling("code"), REQ]}}
    out = run(bridge, project, opts, agents=roles("haiku", []))
    ev = out["events"]
    assert arbiter_calls(ev)[0] == "arbiter task 1", ", ".join(ev)
    retries = [e for e in after(ev, "arbiter task 1") if re.match(r"^task 1( |$)", e) and not e.endswith(" end")]
    assert retries, "the task was retried after the ruling: " + ", ".join(ev)
    assert "print the greeting before exiting" in out["prompts"][retries[0]], out["prompts"][retries[0]]
    assert "1" not in out["result"]["merged"], "the gate kept failing, so the task is unfinished: " + json.dumps(out["result"])
    assert needs(out), "the unfinished task is an entry for me: " + json.dumps(out["result"].get("needsInput"))


def deadlock(issue, file="src/greet.py"):
    return {"verdict": "changes", "findings": [{"task": "1", "severity": "blocking", "file": file, "issue": issue}]}


F1 = "the greeting is never printed"
F2 = "the greeting is printed twice"
F3 = "the farewell is never printed"


def review_labels(ev):
    return [e for e in ev if re.match(r"^review 1( |$)", e) and not e.endswith(" end")]


def test_tc75_the_re_review_prompt_holds_the_ruling_and_the_settled_finding_raised_again_does_not_block(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    opts = {"deps": {"1": []}, "reviews": {"1": [deadlock(F1)] * 6}, "rulings": {"1": [ruling("code"), REQ]}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"], REVIEW_ON))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], "the settled finding is not ruled on again: " + ", ".join(ev)
    later = [e for e in after(ev, "arbiter task 1") if e in review_labels(ev)]
    assert later, "the task was reviewed again after the ruling: " + ", ".join(ev)
    assert "print the greeting before exiting" in out["prompts"][later[0]], out["prompts"][later[0]]
    assert not needs(out), "the raised-again finding was settled, so it did not block: " + json.dumps(out["result"].get("needsInput"))
    assert out["result"]["merged"] == ["1"]


def finding_ruling(mark):
    return ruling("code", reasoning=f"reasoning-{mark}-held", code=f"code-{mark}-text")


def test_tc146_a_different_blocking_finding_gets_its_own_ruling_a_settled_one_raised_again_makes_no_call_and_a_mixed_deadlock_splits(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    agents = roles("haiku", ["opus"], REVIEW_ON)
    first = run(bridge, project, {"deps": {"1": []}, "reviews": {"1": [deadlock(F1)] * 2 + [deadlock(F2)] * 6},
                                  "rulings": {"1": [finding_ruling("f1"), finding_ruling("f2")]}}, agents=agents)
    assert len(arbiter_calls(first["events"])) == 2, ", ".join(first["events"])
    assert len(decisions_entries(project)) == 2, "the different finding on the same file got its own ruling: " + str(decisions_entries(project))
    # The same finding raised again in a new deadlock is covered by its ruling: no call, an entry holding the ruling.
    again = run(bridge, project, {"deps": {"1": []}, "reviews": {"1": [deadlock(F1)] * 6}, "rulings": {"1": [REQ]}}, agents=agents)
    assert arbiter_calls(again["events"]) == [], "no arbiter call: " + ", ".join(again["events"])
    entries = needs(again)
    assert len(entries) == 1, json.dumps(again["result"].get("needsInput"))
    assert "reasoning-f1-held" in json.dumps(entries[0]), json.dumps(entries[0])
    assert len(decisions_entries(project)) == 2, "nothing new was recorded"
    # A deadlock mixing the ruled finding and a new one: the ruled one goes to me with its ruling, only the new one to the arbiter.
    mixed_review = {"verdict": "changes", "findings": deadlock(F1)["findings"] + deadlock(F3)["findings"]}
    mixed = run(bridge, project, {"deps": {"1": []}, "reviews": {"1": [mixed_review] * 6}, "rulings": {"1": [finding_ruling("f3")]}}, agents=agents)
    calls = arbiter_calls(mixed["events"])
    assert len(calls) == 1, "one call, for the new finding: " + ", ".join(mixed["events"])
    prompt = mixed["prompts"][calls[0]]
    assert F3 in prompt, prompt
    assert F1 not in prompt, "the ruled finding is not sent to the arbiter:\n" + prompt
    to_me = json.dumps(needs(mixed))
    assert "reasoning-f1-held" in to_me and F1 in to_me, "the ruled finding went to me with its ruling: " + to_me


def test_tc210_a_review_deadlock_ruled_test_repair_goes_to_the_fixer_and_the_task_is_reviewed_again_with_no_revision_or_attempt_spent(bridge, tmp_path):  # noqa: F811
    project = arbiter_project(tmp_path)
    opts = {"deps": {"1": []}, "reviews": {"1": [deadlock(F1)] * 2}, "rulings": {"1": [ruling("test_repair", testRepair={**ruling("test_repair")["testRepair"], "change": "change-tc210-text"}), REQ]},
            "testFix": {CONFLICT["file"]: {"verdict": "fixed"}}}
    out = run(bridge, project, opts, agents=roles("haiku", ["opus"], REVIEW_ON))
    ev = out["events"]
    assert arbiter_calls(ev) == ["arbiter task 1"], ", ".join(ev)
    assert "testfix 1 (Task 1)" in ev, "the fixer ran: " + ", ".join(ev)
    rest = after(ev, "testfix 1 (Task 1)")
    spent = [e for e in rest if re.match(r"^task 1( |$)", e) and not e.endswith(" end")]
    assert spent == [], "no revision or attempt after the fix: " + ", ".join(ev)
    rereviews = [e for e in rest if e in review_labels(ev)]
    assert rereviews, "the task was reviewed again after the fix landed: " + ", ".join(ev)
    prompt = out["prompts"][rereviews[0]]
    assert "change-tc210-text" in prompt, "the re-review prompt carries the ruling:\n" + prompt
    assert out["result"]["merged"] == ["1"], json.dumps(out["result"])
