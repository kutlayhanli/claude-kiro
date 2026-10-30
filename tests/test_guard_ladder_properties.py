"""P-14, P-15 and P-16: the guard ladder and parking, by Hypothesis through the Node bridge and the real CLI.

- P-14: `agents.guard_ladder` (the source), the bridge `ladder` op (the Workflow's `guardLadder` mirror) and
  `ck agents --guard-ladder` agree for every alias, whatever `implementer.escalate` is.
- P-15: each task's attempts follow the ladder rules, run through the real `spec-implement.js` under the fake runtime.
- P-16: parking blocks exactly the dependents of a parked task.
"""

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from hypothesis import HealthCheck, given, settings, strategies as st

from guard_helpers import ck_run_cmd
from landing_bridge import bridge, dags  # noqa: F401  (bridge is a module-scoped fixture)

PROPERTIES = {
    "P-14": "test_p14_python_bridge_and_cli_ladders_agree",
    "P-15": "test_p15_attempts_follow_the_ladder_rules",
    "P-16": "test_p16_parking_blocks_dependents_only",
}

PURE = dict(max_examples=200, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
CLI = dict(max_examples=40, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
RUN = dict(max_examples=60, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

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
    assert bridge.ladder(model) == want


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
    # The mirror takes only the attempted model: it cannot depend on implementer.escalate.
    assert bridge.ladder("haiku") == LADDER and bridge.ladder("fable") == []


# --------------------------------------------------------------------------- P-15

TASK_LABEL = re.compile(r"^task (\w+)(?: \[test\])?(?: guard (\d+) \((\w[\w.\-\[\]]*)\))?$")
RETRY_LABEL = re.compile(r"^task (\w+)(?: \[test\])? retry ")


def roles(model: str, escalate: List[str], review: bool) -> Dict[str, Any]:
    return {
        "implementer": {"model": model, "effort": "medium", "escalate": escalate},
        "test_writer": {"model": model, "effort": "medium", "escalate": escalate},
        "fixer": {"model": model, "effort": "medium", "escalate": escalate},
        "reviewer": {"model": "opus", "effort": "high", "enabled": review, "rounds": 2},
    }


def prepare(deps: Dict[str, List[str]], project: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    project = {**project, "deps": deps, "planDurations": {str(k): 1 for k in range(1, 200)}}
    return {"project": project, "args": {**args, "deps": deps, "remaining": list(deps)}}


def stopped(ceiling: str, model: str) -> bool:
    return ceiling == "always" or RANK[model] <= RANK[ceiling]


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
    deps, start, escalate, guarded, in_revise, fail_until = sc
    project: Dict[str, Any] = {"guardStops": dict(guarded), "failUntil": fail_until}
    if in_revise:
        project["guardStopIn"] = {t: "revise" for t in guarded}
        project["reviews"] = {t: ["changes", "approve"] for t in guarded}
    run = prepare(deps, project, {"agents": roles(start, escalate, review=in_revise and bool(guarded)), "maxRetries": 2})
    out = bridge.run(run["project"], run["args"])
    labels = [e for e in out["events"] if not e.endswith(" end")]
    for t in deps:
        mine = [l for l in labels if TASK_LABEL.match(l) and TASK_LABEL.match(l).group(1) == t]
        guards = [TASK_LABEL.match(l) for l in mine if TASK_LABEL.match(l).group(2)]
        if t not in guarded:
            assert not guards, f"task {t}: guard attempt without a guard stop: {mine}"
            continue
        assert not [l for l in labels if RETRY_LABEL.match(l) and RETRY_LABEL.match(l).group(1) == t], f"task {t}: a guard stop spent a retry: {labels}"
        ceiling = guarded[t]
        models = [g.group(3) for g in guards]
        assert len(models) == len(set(models)), f"task {t}: a ladder model ran twice: {models}"
        assert [g.group(2) for g in guards] == [str(i) for i in range(1, len(guards) + 1)]
        if not stopped(ceiling, start):
            assert not guards, f"task {t}: ladder ran without a guard stop"
            continue
        if in_revise:
            assert models == expected_ladder(start)[:1], f"task {t}: {models}"
        else:
            want: List[str] = []
            for m in expected_ladder(start):
                want.append(m)
                if not stopped(ceiling, m):
                    break
            assert models == want, f"task {t}: {models} != {want} (start {start}, stop {ceiling})"
        assert all(RANK[m] > RANK[start] for m in models)


# --------------------------------------------------------------------------- P-16


@given(deps=dags(max_tasks=8, dangling=False), data=st.data())
@settings(**RUN)
def test_p16_parking_blocks_dependents_only(bridge, deps, data):  # noqa: F811
    ids = list(deps)
    parked = data.draw(st.lists(st.sampled_from(ids), unique=True, max_size=len(ids)))
    run = prepare(deps, {"guardStops": {t: "always" for t in parked}}, {})
    out = bridge.run(run["project"], run["args"])
    result = out["result"]
    starts = {e for e in out["events"] if e.startswith("task ") and not e.endswith(" end")}

    def blocked(t: str, seen=()) -> bool:
        return t in parked or any(d in deps and d not in seen and blocked(d, seen + (t,)) for d in deps[t])

    for t in ids:
        began = any(re.match(rf"^task {t}\b", e) for e in starts)
        if blocked(t) and t not in parked:
            assert not began, f"task {t} depends on a parked task but started"
        else:
            assert began, f"task {t} depends on no parked task but never started"
    need = {n["task"] for n in result["needsInput"]}
    assert sorted(n["task"] for n in result["needsInput"]) == sorted(set(need)), "a parked task is listed twice"
    assert need == set(parked)
    listed = set(result["merged"]) | {f["task"] for f in result["failed"]} | {b["task"] for b in result["blockedOn"]}
    assert not (need & listed), f"parked tasks also listed elsewhere: {need & listed}"
    for t in parked:
        assert out["events"].count(f"park {t}") == 1, f"park {t} ran {out['events'].count(f'park {t}')} times"
    assert not [e for e in out["events"] if e.startswith("park ") and e.split()[1] not in parked and not e.endswith(" end")]
