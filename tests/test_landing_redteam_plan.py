"""Red team for Story 1's SHALL NOT clauses (requirements 1.5 and 1.10).

- 1.5: once an idle plan agent (case 1.4(c)) has run, no other idle plan agent until a landing completes or a task
  finishes. Oracle on the fake runtime's `trace`: a plan request with no `land … end` since the previous plan request is
  an idle plan; between two idle plans there must be a `land … end` or a `task … end`.
- 1.10: a task whose dependency is not a key of the dependency map is never started. The fake `ck plan` drops dangling
  ids exactly as the real one does, so its `ready` lists such a task: the plan's own output fed back must not start it.

Every workflow case drives the real script through `bridge.run` (or the real `readyTasks` seam through `bridge.ready`)
and reads only `trace`, the result, prompts and the log.
"""

import re
import shutil

import pytest

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PLAN = re.compile(r"^plan \d+$")  # retries ("plan 2 retry") are the same plan step, not a new trigger


def args_for(deps, **extra):
    """Workflow args as the skill builds them: the dependency map and the order from `ck plan --json`."""
    return {"deps": {k: list(v) for k, v in deps.items()}, "remaining": list(deps), **extra}


def requested(trace):
    return [e["label"] for e in trace if e["ev"] == "request"]


def first_task_requests(trace):
    """Task numbers of first-attempt task requests, in order."""
    out = []
    for label in requested(trace):
        m = re.match(r"^task (\S+?)( \[test\])?$", label)
        if m:
            out.append(m.group(1))
    return out


def idle_plan_violations(trace):
    """Pairs of consecutive idle plan requests with no landing end and no task end between them (1.5)."""
    idle = []  # trace indexes of idle plan requests
    prev_plan = None
    for i, e in enumerate(trace):
        if e["ev"] != "request" or not PLAN.match(e["label"]):
            continue
        if prev_plan is not None:
            landed = any(x["ev"] == "end" and x["label"].startswith("land ") for x in trace[prev_plan + 1 : i])
            if not landed:
                idle.append(i)
        prev_plan = i
    bad = []
    for a, b in zip(idle, idle[1:]):
        between = trace[a + 1 : b]
        if not any(x["ev"] == "end" and (x["label"].startswith("land ") or x["label"].startswith("task ")) for x in between):
            bad.append((trace[a]["label"], trace[b]["label"], [f"{x['ev']} {x['label']}" for x in between]))
    return bad


def plan_count(trace):
    return sum(1 for label in requested(trace) if PLAN.match(label))


def ends(trace, prefix):
    return sum(1 for e in trace if e["ev"] == "end" and e["label"].startswith(prefix))


def assert_no_idle_storm(out):
    trace = out["trace"]
    assert not idle_plan_violations(trace), idle_plan_violations(trace)
    # NFR-1 shape: plans <= start + one per landing + idle plans, idle plans <= 1 + landings + task ends.
    assert plan_count(trace) <= 2 + 2 * ends(trace, "land ") + ends(trace, "task "), requested(trace)


def land_end_index(trace, n):
    for i, e in enumerate(trace):
        if e["ev"] == "end" and e["label"].startswith("land ") and n in e["label"].split()[1:]:
            return i
    return None


# --- 1.5: no idle-plan storm -------------------------------------------------------------------


@needs_node
def test_plan_that_keeps_answering_nothing_ready_does_not_loop(bridge):
    """Task 2 waits on a task that does not exist; every plan answers the same 'nothing ready' view while task 1 runs."""
    deps = {"1": [], "2": ["99"]}
    out = bridge.run({"deps": deps, "durations": {"1": 40}}, args_for(deps, maxConcurrent=2))
    assert_no_idle_storm(out)
    assert "2" not in first_task_requests(out["trace"])
    # Between the start plan and task 1's end at most one idle plan may run (nothing else happened).
    trace = out["trace"]
    t1_end = next(i for i, e in enumerate(trace) if e["ev"] == "end" and e["label"] == "task 1")
    assert sum(1 for e in trace[:t1_end] if e["ev"] == "request" and PLAN.match(e["label"])) <= 2, requested(trace)


@needs_node
def test_many_short_tasks_finishing_back_to_back_while_nothing_is_ready(bridge):
    """Five short tasks end one after another while a slow landing runs and the sixth task waits on all of them."""
    deps = {"1": [], "2": [], "3": [], "4": [], "5": [], "6": ["1", "2", "3", "4", "5"]}
    project = {
        "deps": deps,
        "durations": {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 1},
        "mergeDurations": {"1": 40, "2": 40, "3": 40, "4": 40, "5": 40},
    }
    out = bridge.run(project, args_for(deps, maxConcurrent=6, mergeBatch=1))
    assert_no_idle_storm(out)
    trace = out["trace"]
    six = [i for i, e in enumerate(trace) if e["ev"] == "request" and e["label"] == "task 6"]
    assert six, requested(trace)
    assert all(land_end_index(trace, n) is not None and land_end_index(trace, n) < six[0] for n in ["1", "2", "3", "4", "5"])


@needs_node
def test_failed_task_record_while_idle_plan_is_in_flight(bridge):
    """Task 2 fails at once while a slow idle plan runs; the record may re-arm one idle plan, never a storm."""
    deps = {"1": [], "2": [], "3": ["1"]}
    project = {
        "deps": deps,
        "durations": {"1": 40, "2": 1},
        "failUntil": {"2": 9},
        "planDurations": {"2": 25, "3": 25},
    }
    out = bridge.run(project, args_for(deps, maxConcurrent=3, maxRetries=0))
    assert_no_idle_storm(out)
    trace = out["trace"]
    threes = [i for i, e in enumerate(trace) if e["ev"] == "request" and e["label"] == "task 3"]
    assert threes and land_end_index(trace, "1") is not None and land_end_index(trace, "1") < threes[0]


@needs_node
def test_plan_fail_on_the_idle_plan_does_not_storm_or_start_more_tasks(bridge):
    deps = {"1": [], "2": ["99"]}
    out = bridge.run({"deps": deps, "durations": {"1": 30}, "planFail": [2]}, args_for(deps, maxConcurrent=2))
    trace = out["trace"]
    assert_no_idle_storm(out)
    labels = requested(trace)
    assert labels.count("plan 2 retry") <= 1
    assert "2" not in first_task_requests(trace)
    assert "1" in out["result"]["merged"]


@needs_node
def test_idle_plans_stay_bounded_on_a_wide_graph_with_dangling_deps(bridge):
    deps = {"1": [], "2": [], "3": ["404"], "4": ["1", "404"], "5": ["2"], "6": ["405"]}
    project = {"deps": deps, "durations": {"1": 20, "2": 35, "5": 10}}
    out = bridge.run(project, args_for(deps, maxConcurrent=4, mergeBatch=2))
    assert_no_idle_storm(out)
    started = first_task_requests(out["trace"])
    assert not {"3", "4", "6"} & set(started), started
    assert sorted(out["result"]["merged"]) == ["1", "2", "5"]


# --- 1.10: never start on a guess --------------------------------------------------------------

NEAR_MISS = ["02", "2 ", " 2", "Task 2", "task 2", "2.0", "+2", "0x2", "２", "2\n", "", "T2", "-task-2", ".ck/worktrees/demo-task-2"]


@needs_node
@pytest.mark.parametrize("dep", NEAR_MISS, ids=[repr(d) for d in NEAR_MISS])
def test_near_miss_dependency_ids_are_unknown_and_never_started(bridge, dep):
    deps = {"1": [], "2": [], "3": [dep]}
    out = bridge.run({"deps": deps, "durations": {"1": 5, "2": 5}}, args_for(deps, maxConcurrent=3))
    assert "3" not in first_task_requests(out["trace"]), requested(out["trace"])
    assert sorted(out["result"]["merged"]) == ["1", "2"]
    assert out["result"].get("reason") != "planning step failed"


@needs_node
def test_numeric_dependency_never_starts_before_task_2_merges(bridge):
    deps = {"1": [], "2": [], "3": [2]}
    out = bridge.run({"deps": deps, "durations": {"2": 20}}, args_for(deps, maxConcurrent=3))
    trace = out["trace"]
    threes = [i for i, e in enumerate(trace) if e["ev"] == "request" and e["label"] == "task 3"]
    if threes:
        two = land_end_index(trace, "2")
        assert two is not None and two < threes[0], requested(trace)
    assert {"1", "2"} <= set(out["result"]["merged"])


@needs_node
def test_dependency_listed_done_but_absent_from_the_map_is_not_met(bridge):
    """`ck plan` (fake and real) counts 7 as Done and so lists task 3 as ready; the script must not believe it."""
    deps = {"1": [], "3": ["7"]}
    out = bridge.run({"deps": deps, "done": ["7"]}, args_for(deps, maxConcurrent=2))
    assert "3" not in first_task_requests(out["trace"]), requested(out["trace"])
    assert "1" in out["result"]["merged"]


PROTO = ["__proto__", "constructor", "toString", "hasOwnProperty", "valueOf", "__defineGetter__", "prototype"]


@needs_node
@pytest.mark.parametrize("key", PROTO)
def test_prototype_named_dependency_listed_done_is_still_unknown(key):
    """Every plan answer says task 2 needs `key` and lists `key` as Done (so `ck plan` calls 2 ready)."""
    from test_worktree_redteam import run_driver

    deps = {"1": [], "2": [key], "3": ["1", key]}
    out = run_driver(
        {"deps": {"1": [], "2": [], "3": ["1"]}, "durations": {"1": 5}},
        args_for(deps, maxConcurrent=3),
        plan_patch={"deps": {"2": [key], "3": ["1", key]}, "done": [key]},
    )
    started = first_task_requests(out["trace"])
    assert "2" not in started and "3" not in started, requested(out["trace"])
    assert "1" in out["result"]["merged"]


@needs_node
def test_relink_to_a_dangling_dependency_mid_run_is_not_started(bridge):
    """tasks.md edited during the run: task 3 now also needs task 42, which does not exist."""
    deps = {"1": [], "2": [], "3": ["1"]}
    project = {"deps": deps, "durations": {"1": 5, "2": 30}, "relink": {"atPlan": 2, "deps": {"3": ["1", "42"]}}}
    out = bridge.run(project, args_for(deps, maxConcurrent=1, mergeBatch=1))
    assert "3" not in first_task_requests(out["trace"]), requested(out["trace"])
    assert sorted(out["result"]["merged"]) == ["1", "2"]


@needs_node
def test_dangling_dependency_with_duplicates_and_a_met_one(bridge):
    deps = {"1": [], "2": ["1", "1", "1", "88", "1"]}
    out = bridge.run({"deps": deps, "durations": {"1": 5}}, args_for(deps, maxConcurrent=2))
    assert "2" not in first_task_requests(out["trace"]), requested(out["trace"])


# --- 1.10 at the readiness seam ----------------------------------------------------------------


@needs_node
@pytest.mark.parametrize("key", PROTO)
def test_ready_seam_prototype_keys_in_done_do_not_satisfy_a_dependency(bridge, key):
    assert bridge.ready(deps={"1": [], "2": [key]}, done=[key], exclude=[]) == ["1"]


@needs_node
@pytest.mark.parametrize("key", PROTO)
def test_ready_seam_prototype_names_as_task_ids_follow_the_key_rule(bridge, key):
    # A prototype name as a key of the deps map (JSON.parse makes it an own key) is a real task id: "d is a key of
    # deps AND d in done" applies to it like any other id.
    deps = {key: [], "2": [key]}
    assert set(bridge.ready(deps=deps, done=[], exclude=[], order=[key, "2"])) == {key}
    assert set(bridge.ready(deps=deps, done=[key], exclude=[], order=[key, "2"])) == {"2"}


@needs_node
@pytest.mark.parametrize("dep", NEAR_MISS, ids=[repr(d) for d in NEAR_MISS])
def test_ready_seam_near_miss_ids_in_done_do_not_satisfy_a_dependency(bridge, dep):
    # Task 2 is Done and the near-miss form is listed Done too: the dependent still waits on an unknown id.
    assert bridge.ready(deps={"1": [], "2": [], "3": [dep]}, done=["2", dep], exclude=[]) == ["1"]


@needs_node
def test_ready_seam_dependency_done_but_absent_from_the_map(bridge):
    assert bridge.ready(deps={"1": [], "3": ["7"], "4": ["1"]}, done=["7", "1"], exclude=[], order=["1", "3", "4"]) == ["4"]


@needs_node
def test_ready_seam_control_a_known_done_dependency_is_met(bridge):
    # Control: a real, Done dependency (a key of deps) is met, so the dangling rule is not over-eager.
    assert bridge.ready(deps={"1": [], "2": ["1"]}, done=["1"], exclude=[]) == ["2"]
