"""Red team for Story 3's SHALL NOT clauses (requirements 3.1 and 3.8).

- 3.1: while any review, revise or land agent of the run is presumed queued, no new task starts. Every case runs the
  real script through `bridge.run` with `slots` (the bridge sets `args.agentSlots = slots`, so the script's estimate
  matches the fake runtime queue) and judges by the fake runtime's own `trace`: no first-attempt `request task N` while
  any review/revise/land is requested and not yet started.
- 3.8 (script side): an invalid `finishFirst` or `agentSlots` makes the script throw at load with the allowed values,
  never run silently.
"""

import re
import shutil
from collections import Counter

import pytest
from hypothesis import given, settings

from landing_bridge import BridgeError, bridge, scenarios  # noqa: F401  (module-scoped fixture)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

FIRST_ATTEMPT = re.compile(r"^task \d+( \[test\])?$")
FINISH = re.compile(r"^(review \d+|land |task \d+( \[test\])? revise \d+)")
REVIEWS = {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 2}}


def violations(trace):
    """First-attempt task requests made while a review/revise/land was requested and not started (3.1)."""
    waiting = Counter()
    bad = []
    for e in trace:
        label = e["label"]
        if e["ev"] == "request":
            if FIRST_ATTEMPT.match(label) and +waiting:
                bad.append((label, sorted((+waiting).elements())))
            if FINISH.match(label):
                waiting[label] += 1
        elif e["ev"] == "start" and FINISH.match(label) and waiting[label] > 0:
            waiting[label] -= 1
    return bad


def run_checked(bridge, project, args, slots):
    """Attack the hold in both modes: the 1.9 fallback (no `deps` in args, plan agents decide readiness) and script
    readiness (the skill passes `deps`/`remaining`, so `schedule()` starts tasks from `readyTasks`). Returns the
    script-readiness run, the production path."""
    fallback = bridge.run(project, args, slots=slots)
    assert not violations(fallback["trace"]), ("fallback", violations(fallback["trace"]))
    deps = project.get("deps", {})
    ready_args = {"deps": {k: list(v) for k, v in deps.items()}, "remaining": list(deps), **args}
    out = bridge.run(project, ready_args, slots=slots)
    assert not violations(out["trace"]), ("script readiness", violations(out["trace"]))
    return out


def ids(n):
    return {str(i): [] for i in range(1, n + 1)}


def all_merged(out, deps):
    assert sorted(out["result"]["merged"], key=int) == sorted(deps, key=int), (out["result"]["merged"], out["result"].get("reason"))


# --- 3.1 handcrafted pressure -------------------------------------------------------------------


@needs_node
def test_single_runtime_slot(bridge):
    deps = ids(4)
    out = run_checked(bridge, {"deps": deps, "durations": {"1": 3, "2": 3, "3": 3, "4": 3}}, {"maxConcurrent": 4, "agents": REVIEWS}, 1)
    all_merged(out, deps)


@needs_node
def test_max_concurrent_larger_than_slots_with_many_reviews_draining(bridge):
    deps = ids(8)
    project = {"deps": deps, "durations": {k: 5 for k in deps}, "reviewDurations": {k: 15 for k in deps}}
    out = run_checked(bridge, project, {"maxConcurrent": 8, "agents": REVIEWS, "mergeBatch": 4}, 2)
    all_merged(out, deps)


@needs_node
def test_review_rounds_two_and_more(bridge):
    deps = ids(5)
    reviewer = {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 3}}
    project = {"deps": deps, "durations": {"1": 2, "2": 10, "3": 10, "4": 10, "5": 10}, "reviews": {"1": ["changes", "changes", "approve"], "2": ["changes", "approve"]}}
    run_checked(bridge, project, {"maxConcurrent": 3, "agents": reviewer}, 2)


@needs_node
def test_revise_requested_in_the_same_tick_a_slot_frees(bridge):
    # Review 2 ends (frees a runtime slot) and asks for a revise at the moment task 1's implementer ends.
    deps = ids(5)
    project = {"deps": deps, "durations": {"1": 20, "2": 5, "3": 30, "4": 30, "5": 5}, "reviewDurations": {"2": 15}, "reviews": {"2": ["changes", "approve"]}}
    run_checked(bridge, project, {"maxConcurrent": 3, "agents": REVIEWS}, 2)


@needs_node
def test_land_queued_behind_plan_and_gate_agents(bridge):
    deps = ids(6)
    project = {
        "deps": deps,
        "durations": {"1": 2, "2": 4, "3": 30, "4": 6, "5": 8, "6": 2},
        "mergeDurations": {k: 10 for k in deps},
        "planDurations": {2: 25, 3: 25, 4: 25},
    }
    out = run_checked(bridge, project, {"maxConcurrent": 3, "mergeBatch": 1, "fullGateEvery": 1}, 2)
    all_merged(out, deps)


@needs_node
def test_task_finishing_while_the_idle_plan_is_queued(bridge):
    deps = {"1": [], "2": [], "3": ["99"], "4": ["1"], "5": ["2"]}
    project = {"deps": deps, "durations": {"1": 5, "2": 25}, "planDurations": {2: 20, 3: 20}}
    run_checked(bridge, project, {"maxConcurrent": 3, "agents": REVIEWS, "mergeBatch": 1}, 2)


@needs_node
def test_red_landing_with_fix_and_recheck_under_pressure(bridge):
    deps = ids(5)
    project = {"deps": deps, "durations": {k: 4 for k in deps}, "checkRed": ["2"], "reviewDurations": {"1": 10}}
    run_checked(bridge, project, {"maxConcurrent": 4, "agents": REVIEWS, "fullGateEvery": 2, "mergeBatch": 2}, 3)


@needs_node
def test_retries_do_not_open_a_back_door(bridge):
    # A retry is not a new task start, but the first attempts of others must still wait behind queued finishers.
    deps = ids(5)
    project = {"deps": deps, "durations": {k: 4 for k in deps}, "failUntil": {"1": 1, "2": 1}}
    run_checked(bridge, project, {"maxConcurrent": 5, "agents": REVIEWS}, 2)


@needs_node
@pytest.mark.parametrize("finish_first", [True, None], ids=["explicit-true", "absent"])
def test_finish_first_on_or_absent_both_hold_starts(bridge, finish_first):
    deps = ids(6)
    args = {"maxConcurrent": 6, "agents": REVIEWS}
    if finish_first is not None:
        args["finishFirst"] = finish_first
    project = {"deps": deps, "durations": {k: 3 for k in deps}, "reviewDurations": {k: 10 for k in deps}}
    run_checked(bridge, project, args, 2)


SWEEP = settings(derandomize=True, max_examples=25, database=None, deadline=None)


@needs_node
@SWEEP
@given(scenarios())
def test_generated_scenarios_never_start_a_task_while_a_finisher_queues(bridge, sc):
    slots = sc["slots"] or 2
    args = dict(sc["args"])
    args.setdefault("agents", REVIEWS)
    run_checked(bridge, sc["project"], args, slots)


# --- 3.8 script side: invalid values throw at load ----------------------------------------------

BAD_FINISH_FIRST = ["true", "false", 1, 0, "yes", "on", [], {}, "", "TRUE"]
BAD_AGENT_SLOTS = [0, -1, 1.5, "14", "abc", True, [], {}, "", -0.5]


@needs_node
@pytest.mark.parametrize("value", BAD_FINISH_FIRST, ids=[repr(v) for v in BAD_FINISH_FIRST])
def test_invalid_finish_first_throws_at_load(bridge, value):
    with pytest.raises(BridgeError) as err:
        bridge.run({"deps": ids(2)}, {"finishFirst": value})
    assert "finishFirst" in str(err.value) and "true or false" in str(err.value), str(err.value)


@needs_node
@pytest.mark.parametrize("value", BAD_AGENT_SLOTS, ids=[repr(v) for v in BAD_AGENT_SLOTS])
def test_invalid_agent_slots_throws_at_load(bridge, value):
    with pytest.raises(BridgeError) as err:
        bridge.run({"deps": ids(2)}, {"agentSlots": value})
    assert "agentSlots" in str(err.value) and "1 or more" in str(err.value), str(err.value)


@needs_node
@pytest.mark.parametrize("args", [{"finishFirst": None}, {"finishFirst": False}, {"agentSlots": None}, {"agentSlots": 1}, {"agentSlots": 10**6}], ids=["ff-null", "ff-false", "slots-null", "slots-1", "slots-huge"])
def test_valid_values_still_run(bridge, args):
    out = bridge.run({"deps": ids(2)}, args)
    assert sorted(out["result"]["merged"]) == ["1", "2"]
