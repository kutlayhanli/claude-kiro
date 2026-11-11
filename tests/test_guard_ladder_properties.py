"""P-14, P-15 and P-16: the guard-stop route and parking, by Hypothesis through the Node bridge and the real CLI.

Migrated for run-principles (Task 50; 6.2, 6.10, test-plan P-12 and P-16): a guard stop goes to the arbiter and the
workflow no longer climbs the guard ladder.

- P-14: `agents.guard_ladder` (the function and its CLI, which stay) and `ck agents --guard-ladder` agree for every alias,
  whatever `implementer.escalate` is. The Workflow's `guardLadder` mirror and the bridge `ladder` op retire with the
  ladder, so what is left of the bridge half is that the decisions block no longer holds `guardLadder`.
- P-15: for any guard stop, the event right after the attempt that stops is the arbiter step: no `guard <k>` label, no
  retry or escalation spent, run through the real `spec-implement.js` under the fake runtime and the real `ck arbiter`.
- P-16: when the arbiter cannot be called or returns nothing valid, the claim is a needs-input entry that blocks exactly
  the dependents of its task, and no guard attempt runs.
"""

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from hypothesis import HealthCheck, given, settings, strategies as st

import pytest

from guard_helpers import ck_run_cmd
from landing_bridge import bridge, dags  # noqa: F401  (bridge is a module-scoped fixture)

PROPERTIES = {
    "P-14": "test_p14_python_bridge_and_cli_ladders_agree",
    "P-15": "test_p15_attempts_follow_the_ladder_rules",
    "P-16": "test_p16_parking_blocks_dependents_only",
}

PURE = dict(max_examples=200, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
CLI = dict(max_examples=40, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
RUN = dict(max_examples=30, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
LADDER = ["sonnet", "opus", "fable"]
MODELS = ["haiku", "sonnet", "opus", "fable", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-5",
          "claude-opus-5-5[1m]", "HAIKU", "Sonnet", "CLAUDE-OPUS-5-5", "FABLE", "unknown-model-x", "gpt-9"]
ESCALATES = [[], ["opus"], ["sonnet", "opus"], ["fable"], ["haiku"]]


def expected_ladder(model: Optional[str]) -> List[str]:
    """The strict-above list, written from the property, not from the implementation."""
    family = next((m for m in ("haiku", "sonnet", "opus", "fable") if model and m in model.lower()), None)
    return LADDER if family is None else [m for m in LADDER if RANK[m] > RANK[family]]


# --------------------------------------------------------------------------- P-14


@given(model=st.one_of(st.none(), st.sampled_from(MODELS)))
@settings(**PURE)
def test_p14_python_and_bridge_ladders_agree(bridge, model):  # noqa: F811
    from claude_kiro.agents import guard_ladder

    want = expected_ladder(model)
    assert list(guard_ladder(model)) == want
    # The workflow's mirror is gone: design.md removes `guardLadder` from the decisions block (6.2).
    with pytest.raises(Exception, match="guardLadder"):
        bridge.call("guardLadder", model)


@given(model=st.sampled_from(MODELS), escalate=st.sampled_from(ESCALATES))
@settings(**CLI)
def test_p14_python_bridge_and_cli_ladders_agree(model, escalate):
    from claude_kiro.agents import guard_ladder

    want = expected_ladder(model)
    assert list(guard_ladder(model)) == want
    with tempfile.TemporaryDirectory() as raw:
        import os

        env = {**os.environ, "CK_CONFIG_HOME": str(Path(raw) / "cfg")}
        if escalate:
            done = ck_run_cmd(Path(raw), "agents", "set", "implementer.escalate", ",".join(escalate), env=env)
            assert done.returncode == 0, done.stdout + done.stderr
        got = ck_run_cmd(Path(raw), "agents", "--guard-ladder", model, "--json", env=env)
        assert got.returncode == 0, got.stdout + got.stderr
        assert json.loads(got.stdout) == want, (model, escalate)


def test_p14_bridge_ladder_independent_of_escalate(bridge):  # noqa: F811
    # The function takes only the attempted model, so it cannot depend on implementer.escalate; the workflow has no mirror.
    from claude_kiro.agents import guard_ladder

    assert list(guard_ladder("haiku")) == LADDER and list(guard_ladder("fable")) == []
    with pytest.raises(Exception, match="guardLadder"):
        bridge.call("guardLadder", "haiku")


# --------------------------------------------------------------------------- P-15

TASK_LABEL = re.compile(r"^task (\w+)(?: \[test\])?(?: guard (\d+) \((\w[\w.\-\[\]]*)\))?$")
RETRY_LABEL = re.compile(r"^task (\w+)(?: \[test\])? retry ")
GUARD_LABEL = re.compile(r" guard \d")
CODE_RULING = {"status": "ruled", "outcome": "code", "reasoning": "the test states the requirement", "principle": "project:test-intent", "code": "keep the test, change the code"}


def roles(model: str, escalate: List[str], review: bool) -> Dict[str, Any]:
    return {
        "implementer": {"model": model, "effort": "medium", "escalate": escalate},
        "test_writer": {"model": model, "effort": "medium", "escalate": escalate},
        "fixer": {"model": model, "effort": "medium", "escalate": escalate},
        "reviewer": {"model": "opus", "effort": "high", "enabled": review, "rounds": 2},
        "arbiter": {"model": "opus", "effort": "high", "risky_model": "fable", "risky_effort": "high"},
    }


def tasks_md(deps: Dict[str, List[str]]) -> str:
    """A tasks.md holding every drawn task, so the real `ck arbiter open` finds each one."""
    blocks = []
    for n, ds in deps.items():
        after = ", ".join(f"Task {d}" for d in ds if d in deps) or "None"
        blocks.append(f"### Task {n}: Work {n}\n**Status:** Not Started\n**Track:** impl\n**Requirements:** 1.1\n**Description:** Do work {n}.\n"
                      f"**Files:**\n- `src/work{n}.py` - the work\n**Verify:** `uv run pytest -q tests/test_guarded.py`\n**Dependencies:** {after}\n\n---\n")
    return "# Implementation Tasks: Demo\n\n**Status:** Not Started\n\n## Task Breakdown\n\n" + "\n".join(blocks)


def bridge_run(deps: Dict[str, List[str]], project: Dict[str, Any], args: Dict[str, Any], bridge_) -> Dict[str, Any]:
    """Run the real workflow on a real git project holding specs/PRINCIPLES.md and every drawn task."""
    from arbiter_helpers import arbiter_project

    project = {**project, "deps": deps, "planDurations": {str(k): 1 for k in range(1, 200)}}
    with tempfile.TemporaryDirectory() as raw:
        proj = arbiter_project(Path(raw), tasks=tasks_md(deps))
        args = {**args, "deps": deps, "remaining": list(deps), "root": str(proj.root), "targetDir": str(proj.target), "arbiterAgent": "spec:arbiter"}
        return bridge_.run(project, args, exec_ck=proj.exec_ck)


def stopped(ceiling: str, model: str) -> bool:
    return ceiling == "always" or RANK[model] <= RANK[ceiling]


def ancestors(deps: Dict[str, List[str]], t: str) -> set:
    """Every task `t` depends on, directly or through other tasks."""
    seen: set = set()
    todo = [d for d in deps.get(t, []) if d in deps]
    while todo:
        d = todo.pop()
        if d not in seen:
            seen.add(d)
            todo.extend(x for x in deps.get(d, []) if x in deps)
    return seen


@st.composite
def ladder_scenarios(draw):
    deps = draw(dags(max_tasks=5, dangling=False))
    ids = list(deps)
    start = draw(st.sampled_from(["haiku", "sonnet", "opus"]))
    escalate = draw(st.sampled_from(ESCALATES))
    guarded = {t: draw(st.sampled_from(["haiku", "sonnet", "opus", "always"])) for t in draw(st.lists(st.sampled_from(ids), unique=True, max_size=len(ids)))}
    in_revise = draw(st.booleans())
    fail_until = {t: draw(st.integers(1, 2)) for t in draw(st.lists(st.sampled_from(ids), unique=True, max_size=len(ids))) if t not in guarded}
    return deps, start, escalate, guarded, in_revise, fail_until


@given(sc=ladder_scenarios())
@settings(**RUN)
def test_p15_attempts_follow_the_ladder_rules(bridge, sc):  # noqa: F811
    """Restates run-principles P-12 (the old P-15) for the arbiter (6.2): a guard stop is followed at once by the arbiter, never by a ladder step."""
    deps, start, escalate, guarded, in_revise, fail_until = sc
    project: Dict[str, Any] = {"guardStops": dict(guarded), "failUntil": fail_until, "rulings": {t: [{"claim": f"{t}|test:tests/test_guarded.py::test_x", **CODE_RULING}] for t in guarded}}
    if in_revise:
        project["guardStopIn"] = {t: "revise" for t in guarded}
        project["reviews"] = {t: ["changes", "approve"] for t in guarded}
    out = bridge_run(deps, project, {"agents": roles(start, escalate, review=in_revise and bool(guarded)), "maxRetries": 2}, bridge)
    labels = [e for e in out["events"] if not e.endswith(" end")]
    assert not [l for l in labels if GUARD_LABEL.search(l)], f"a guard attempt ran: {labels}"
    # A task stopped on its first attempt stops again when it resumes with the same model, so its repeat goes to me and
    # it parks. A stop in the first revise call does not repeat, so the revision runs again (design: guard stops).
    parks = set() if in_revise else {t for t, c in guarded.items() if stopped(c, start)}
    for t in deps:
        mine = [l for l in labels if re.match(rf"^(task|arbiter task) {t}\b", l)]
        if ancestors(deps, t) & parks:
            assert not mine, f"task {t} depends on a parked task but has attempts: {mine}"
            continue
        arbiter = [l for l in mine if l.startswith("arbiter task")]
        assert not [l for l in labels if RETRY_LABEL.match(l) and RETRY_LABEL.match(l).group(1) == t and t in guarded], f"task {t}: a guard stop spent a retry: {labels}"
        if t not in guarded or not stopped(guarded[t], start):
            assert not arbiter, f"task {t}: the arbiter ran without a spec claim: {mine}"
            continue
        assert arbiter, f"task {t}: a guard stop never reached the arbiter: {labels}"
        stop = next(l for l in mine if (" revise " in l if in_revise else l == f"task {t}"))
        after = mine[mine.index(stop) + 1]
        assert re.match(rf"^arbiter task {t}( risky)?$", after), f"task {t}: the stop at {stop!r} was followed by {after!r}, not the arbiter"
        assert all(out["opts"][l]["model"] == start for l in mine if l in out["opts"] and not l.startswith("arbiter") and " revise " not in l), f"task {t}: an attempt climbed off {start}"


# --------------------------------------------------------------------------- P-16


@given(deps=dags(max_tasks=8, dangling=False), data=st.data())
@settings(**RUN)
def test_p16_parking_blocks_dependents_only(bridge, deps, data):  # noqa: F811
    """6.9, 6.10: a claim the arbiter cannot rule on is a needs-input entry; it blocks its dependents and nothing else."""
    from arbiter_helpers import FAILURE_FORMS

    ids = list(deps)
    parked = data.draw(st.lists(st.sampled_from(ids), unique=True, max_size=len(ids)))
    rulings = {t: [data.draw(st.sampled_from(FAILURE_FORMS))] for t in parked}
    out = bridge_run(deps, {"guardStops": {t: "always" for t in parked}, "rulings": rulings}, {}, bridge)
    result = out["result"]
    starts = {e for e in out["events"] if e.startswith("task ") and not e.endswith(" end")}
    assert not [e for e in out["events"] if GUARD_LABEL.search(e)], "a guard attempt ran after a failed arbiter call"

    # A drawn task that depends, directly or through other tasks, on another drawn task never starts, so it
    # cannot reach the arbiter: only the drawn tasks with no drawn ancestor really park.
    drawn = set(parked)
    parks = {t for t in drawn if not (ancestors(deps, t) & drawn)}

    for t in ids:
        began = any(re.match(rf"^task {t}\b", e) for e in starts)
        if ancestors(deps, t) & parks:
            assert not began, f"task {t} depends on a parked task but started"
        else:
            assert began, f"task {t} depends on no parked task but never started"
    need = {n["task"] for n in result["needsInput"]}
    assert sorted(n["task"] for n in result["needsInput"]) == sorted(set(need)), "a parked task is listed twice"
    assert need == parks
    listed = set(result["merged"]) | {f["task"] for f in result["failed"]} | {b["task"] for b in result["blockedOn"]}
    assert not (need & listed), f"parked tasks also listed elsewhere: {need & listed}"
    for t in parks:
        assert out["events"].count(f"park {t}") == 1, f"park {t} ran {out['events'].count(f'park {t}')} times"
    assert not [e for e in out["events"] if e.startswith("park ") and e.split()[1] not in parks and not e.endswith(" end")]
