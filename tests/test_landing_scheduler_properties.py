"""TC-54: scheduler properties P-6..P-11, P-21, P-22 and P-23, driven by Hypothesis through the Node bridge.

Every example runs the real `plugin/workflows/spec-implement.js` under the fake runtime (`tests/js/bridge.mjs`,
`fake_project.mjs`). The agents are the only fakes. The properties judge by what can be seen from outside the
script: the fake runtime's own `trace` (request / start / end per agent label, in event order), the prompts the
script wrote and its returned result. No property uses a wall-clock tolerance: every timing claim is an
event-order relation on `trace`.

The scenarios run with script-side readiness: `args.deps` and `args.remaining` are the scenario's dependency map
and its tasks.md order, as the implement skill copies them from `ck plan --json` (1.1). With `slots`, the bridge
also passes `args.agentSlots = slots`, so the script's presumed queue matches the fake runtime's real queue.
"""

import copy
import re

import pytest
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from hypothesis import example, given, settings
from hypothesis import strategies as st

from landing_bridge import bridge, scenarios, subsets  # noqa: F401  (bridge is a module-scoped fixture)

PROPERTIES = {
    "P-6": "test_p6_plan_agents_only_at_start_after_landings_and_when_idle",
    "P-7": "test_p7_task_agents_create_their_own_worktrees",
    "P-8": "test_p8_no_new_task_while_a_finish_type_agent_is_queued",
    "P-9": "test_p9_finish_type_agents_are_requested_at_their_trigger",
    "P-10": "test_p10_finish_first_changes_nothing_when_nothing_queues",
    "P-11": "test_p11_terminates_with_the_same_merges_and_starts_at_the_first_free_slot",
    "P-21": "test_p21_busy_worktrees_are_recorded_and_left_alone",
    "P-22": "test_p22_each_start_is_the_head_of_ready_tasks",
    "P-23": "test_p23_a_new_task_start_never_queues",
    "P-26": "test_p10_finish_first_changes_nothing_when_nothing_queues",
}

SETTINGS = settings(derandomize=True, max_examples=60, database=None, deadline=None)

SPEC = "demo"
ROOT = "/repo"
INTO = "integrate/demo"
WTDIR = f"{ROOT}/.ck/worktrees"  # bridge args give no worktreeDir: the script's default <root>/.ck/worktrees


def wt(n: str) -> str:
    return f"{WTDIR}/{SPEC}-task-{n}"


# --- labels (the unchanged label grammar, Harness Contract item 6) -------------------------------------------

FIRST_TASK = re.compile(r"^task (\w+)(?: \[test\])?$")
RETRY = re.compile(r"^task (\w+)(?: \[test\])? retry ")
REVISE = re.compile(r"^task (\w+)(?: \[test\])? revise (\d+)")
REVIEW = re.compile(r"^review (\w+)(?: round (\d+))?$")
PLAN = re.compile(r"^plan (\d+)( retry)?$")
FIX = re.compile(r"^fix (\d+) \((.*?)\)")
RECHECK = re.compile(r"^re-check (\d+)$")
RESOLVE = re.compile(r"^resolve (\w+)")
LAND_FIX = re.compile(r"^Task (\w+) red on landing$")


def kind(label: str) -> str:
    if FIRST_TASK.match(label):
        return "task"
    if RETRY.match(label):
        return "retry"
    if REVISE.match(label):
        return "revise"
    if REVIEW.match(label):
        return "review"
    if label.startswith("land "):
        return "land"
    if PLAN.match(label):
        return "plan"
    if label.startswith("full gate @"):
        return "full gate"
    if FIX.match(label):
        return "fix"
    if RECHECK.match(label):
        return "re-check"
    if RESOLVE.match(label):
        return "resolve"
    return label  # "final gate", "report", cross-test ...


FINISH_TYPE = {"review", "revise", "land"}  # the unblockers finish-first gives slots to (3.1)
IMPLEMENTER = {"task", "retry", "revise"}  # agents that hold an implementing slot (maxConcurrent)
# Agents the fake project answers at once, with no timer: they start and end inside the microtask flush that
# started them, so their `end` can fall between a trigger and the script's follow-up request without the script
# having waited for anything. Every other fake agent (task, review, land, and plan with `planDurations`, which
# `prepare` always sets) ends from a timer, so a new flush always begins with one of their `end` events.
INSTANT = {"full gate", "fix", "re-check", "resolve", "final gate", "report"}
PLAN_MS = {str(k): 1 for k in range(1, 400)}  # plan k takes 1 ms, as a real plan agent takes time


def is_instant(label: str) -> bool:
    return kind(label) in INSTANT or label.startswith("cross-test")


def first_task(label: str) -> Optional[str]:
    m = FIRST_TASK.match(label)
    return m.group(1) if m else None


def implementer_task(label: str) -> Optional[str]:
    m = FIRST_TASK.match(label) or RETRY.match(label) or REVISE.match(label)
    return m.group(1) if m else None


def land_tasks(label: str) -> List[str]:
    return label[len("land "):].split()


def tasks_named(label: str) -> Set[str]:
    """The task numbers an agent label is about (empty for plan, gates, report)."""
    k = kind(label)
    if k in IMPLEMENTER:
        return {implementer_task(label)}
    if k == "review":
        return {REVIEW.match(label).group(1)}
    if k == "land":
        return set(land_tasks(label))
    if k == "resolve":
        return {RESOLVE.match(label).group(1)}
    if k == "fix":
        m = LAND_FIX.match(FIX.match(label).group(2))
        return {m.group(1)} if m else set()
    return set()


# --- scenario preparation and runs ---------------------------------------------------------------------------

_KEEP = object()


def prepare(sc: Dict[str, Any], slots: Any = _KEEP, drop_relink: bool = False, **args: Any) -> Dict[str, Any]:
    """A deep copy of a `scenarios()` draw with script readiness on (args.deps, args.remaining from the scenario's
    graph, as the skill copies them from `ck plan --json`), plus overrides. Plan agents take 1 ms (`PLAN_MS`)."""
    sc = copy.deepcopy(sc)
    sc["project"]["planDurations"] = dict(PLAN_MS)
    deps = sc["project"]["deps"]
    sc["args"]["deps"] = {t: list(ds) for t, ds in deps.items()}
    sc["args"]["remaining"] = list(deps)
    sc["args"].update(args)
    if slots is not _KEEP:
        sc["slots"] = slots
    if drop_relink:
        sc["project"].pop("relink", None)
    return sc


def run(bridge, sc: Dict[str, Any]) -> Dict[str, Any]:  # noqa: F811
    return bridge.run(sc["project"], sc["args"], slots=sc["slots"], clock="virtual")


def events(out: Dict[str, Any]) -> List[tuple]:
    return [(e["ev"], e["label"]) for e in out["trace"]]


def with_finish_first(sc: Dict[str, Any], on: bool) -> Dict[str, Any]:
    sc = copy.deepcopy(sc)
    sc["args"]["finishFirst"] = on
    return sc


def record_of(out: Dict[str, Any], n: str) -> Optional[Dict[str, Any]]:
    return next((t for t in out["result"]["tasks"] if str(t["task"]) == n), None)


def outstanding_walk(trace: List[Dict[str, Any]]):
    """Yield (index, event, not_started, not_ended) before applying each event: Counters of labels requested and
    not yet started / not yet ended."""
    not_started: Counter = Counter()
    not_ended: Counter = Counter()
    for i, e in enumerate(trace):
        yield i, e, not_started, not_ended
        if e["ev"] == "request":
            not_started[e["label"]] += 1
            not_ended[e["label"]] += 1
        elif e["ev"] == "start":
            not_started[e["label"]] -= 1
        elif e["ev"] == "end":
            not_ended[e["label"]] -= 1


# --- P-6 -------------------------------------------------------------------------------------------------------

@SETTINGS
@given(scenarios())
# Found when another module's constants entered Hypothesis's pool: task 3 waits on task 1 at the start and is
# relinked free at plan 2, so the idle plan at t=3 is legitimate and the graph is not all-independent.
@example(
    sc={"project": {"deps": {"1": [], "2": [], "3": ["1"]}, "durations": {"1": 1, "2": 1, "3": 1},
                    "reviewDurations": {}, "mergeDurations": {}, "reviews": {}, "checkRed": [], "failUntil": {},
                    "relink": {"atPlan": 2, "deps": {"3": []}}},
        "args": {"mergeBatch": 1, "fullGateEvery": 1, "wave": {"1": 1, "2": 1, "3": 1}, "maxConcurrent": 1},
        "slots": 2},
)
def test_p6_plan_agents_only_at_start_after_landings_and_when_idle(bridge, sc):  # noqa: F811
    """1.4, 1.5, NFR-1: a non-retry plan request is the start plan, the first plan after a `land … end`, or idle;
    idle plans after the first need a landing end or a task end since the previous idle plan; an all-independent
    graph runs at most 1 + landing attempts plan agents."""
    sc = prepare(sc)
    out = run(bridge, sc)
    trace = out["trace"]
    requests = [e["label"] for e in trace if e["ev"] == "request"]
    assert requests and PLAN.match(requests[0]) and not PLAN.match(requests[0]).group(2), requests[:3]

    seen_plan = False
    land_ended = False
    idle_seen = False
    rearmed = False
    plans = 0
    for i, e in enumerate(trace):
        label, k = e["label"], kind(e["label"])
        if e["ev"] == "end" and k == "land":
            land_ended = True
            rearmed = True
        if e["ev"] == "end" and k in IMPLEMENTER:
            rearmed = True  # a task finished
        if e["ev"] != "request" or k != "plan" or PLAN.match(label).group(2):
            continue
        plans += 1
        if not seen_plan:
            seen_plan = True  # the start plan
        elif land_ended:
            pass  # the landing plan
        else:
            # An idle plan: only the first one may come without a landing end or task end since the previous one.
            assert not idle_seen or rearmed, f"idle {label} at event {i} without a landing or task end since the previous idle plan: {events(out)}"
            idle_seen = True
            rearmed = False
        land_ended = False

    start_deps = dict(sc["project"]["deps"])
    final_deps = dict(start_deps)
    final_deps.update((sc["project"].get("relink") or {}).get("deps", {}))
    # All-independent means independent from the start: a dependency relinked away later can make an idle plan legitimate.
    if all(not ds for ds in start_deps.values()) and all(not ds for ds in final_deps.values()):
        lands = sum(1 for e in trace if e["ev"] == "request" and kind(e["label"]) == "land")
        assert plans <= 1 + lands, f"{plans} plan agents for {lands} landing attempt(s) in an all-independent graph: {requests}"


# --- P-7 -------------------------------------------------------------------------------------------------------

@SETTINGS
@given(scenarios())
def test_p7_task_agents_create_their_own_worktrees(bridge, sc):  # noqa: F811
    """2.1, 2.5: each first task prompt creates its own worktree; plan prompts never create one; retry prompts only
    when the claim reports MISSING; revise and review prompts never; only `fix k` prompts create `fix<k>`."""
    sc = prepare(sc)
    out = run(bridge, sc)
    prompts: Dict[str, str] = out["prompts"]
    assert any(first_task(label) for label in prompts), list(prompts)
    for label, prompt in prompts.items():
        k = kind(label)
        if k == "task":
            n = first_task(label)
            create = rf"ck -C {re.escape(ROOT)} worktree create {SPEC} {n}(?!\w)[^\n]*--install --base {re.escape(INTO)}"
            assert re.search(create, prompt), f"{label} prompt does not create its worktree first:\n{prompt}"
        if k == "plan":
            assert "worktree create" not in prompt, f"{label} prompt still creates worktrees:\n{prompt}"
        if k in ("revise", "review"):
            n = tasks_named(label).pop()
            assert not re.search(rf"worktree create {SPEC} {n}(?!\w)", prompt), f"{label} prompt creates the task worktree again:\n{prompt}"
        if k == "retry":
            n = implementer_task(label)
            for m in re.finditer(rf"worktree create {SPEC} {n}(?!\w)", prompt):
                before = prompt[max(0, m.start() - 400): m.start()]
                assert "MISSING" in before, f"{label} prompt creates the worktree without the claim-MISSING condition:\n{prompt}"
        for m in re.finditer(rf"worktree create {SPEC} fix(\d+)(?!\w)", prompt):
            assert re.match(rf"^fix {m.group(1)} \(", label), f"{label} prompt creates fix{m.group(1)}:\n{prompt}"


# --- P-8 -------------------------------------------------------------------------------------------------------

@SETTINGS
@given(scenarios(), st.integers(1, 6))
def test_p8_no_new_task_while_a_finish_type_agent_is_queued(bridge, sc, slots):  # noqa: F811
    """3.1: with slots and finish-first on, no first-attempt task request while a review/revise/land agent is
    requested and not started (the fake runtime's real queue)."""
    sc = prepare(sc, slots=slots)
    out = run(bridge, sc)
    for i, e, not_started, _ in outstanding_walk(out["trace"]):
        if e["ev"] == "request" and first_task(e["label"]):
            queued = sorted(label for label, c in not_started.items() if c > 0 and kind(label) in FINISH_TYPE)
            assert not queued, f"request {e['label']} at event {i} while {queued} wait for a runtime slot (slots {slots}): {events(out)}"


# --- P-9 -------------------------------------------------------------------------------------------------------

def trigger_ok(request: str, trigger: Optional[str]) -> bool:
    """Whether `trigger` (the label of the last `end` before the request) is what calls for `request`."""
    if trigger is None:
        return False
    k, tk = kind(request), kind(trigger)
    if k == "review":
        n, rnd = REVIEW.match(request).groups()
        if rnd is None:  # the first review: after the implementer's passing attempt
            return tk in ("task", "retry") and implementer_task(trigger) == n
        return tk == "revise" and implementer_task(trigger) == n
    if k == "revise":
        return tk == "review" and REVIEW.match(trigger).group(1) == implementer_task(request)
    if k == "land":
        members = set(land_tasks(request))
        if tk in ("task", "retry", "revise", "review"):  # an approval (or a passing gate without reviews)
            return bool(tasks_named(trigger) & members)
        return tk in ("land", "full gate", "fix", "re-check", "resolve")  # the in-flight landing step's last agent
    if k == "full gate":
        return tk == "land"
    if k == "fix":
        what = FIX.match(request).group(2)
        if LAND_FIX.match(what):
            return tk in ("land", "full gate", "re-check")  # the red land, or that landing step's full gate
        return tk in ("full gate", "final gate", "re-check")
    if k == "re-check":
        return tk == "fix" and FIX.match(trigger).group(1) == RECHECK.match(request).group(1)
    return True


@SETTINGS
@given(scenarios(), st.integers(1, 6))
def test_p9_finish_type_agents_are_requested_at_their_trigger(bridge, sc, slots):  # noqa: F811
    """3.4: review, revise, land, full gate, fix and re-check are requested after their trigger and before the next
    `end` of any agent, whether or not a task start is being deferred."""
    sc = prepare(sc, slots=slots)
    out = run(bridge, sc)
    ends: List[str] = []  # labels of the `end` events so far
    for i, e in enumerate(out["trace"]):
        if e["ev"] == "end":
            ends.append(e["label"])
        elif e["ev"] == "request" and kind(e["label"]) in ("review", "revise", "land", "full gate", "fix", "re-check"):
            # Walk back to the trigger. Only ends of INSTANT agents may lie between it and the request (they end
            # inside the trigger's own microtask flush); any other end means the request waited for an agent.
            between = []
            for label in reversed(ends):
                if trigger_ok(e["label"], label):
                    break
                assert is_instant(label), f"request {e['label']} at event {i} comes after end of {label}, past its trigger (ends since: {between}): {events(out)}"
                between.append(label)
            else:
                raise AssertionError(f"request {e['label']} at event {i} has no trigger before it: {events(out)}")


# --- P-10 ------------------------------------------------------------------------------------------------------

@SETTINGS
@given(scenarios(), st.sampled_from([1000, None]))
def test_p10_finish_first_changes_nothing_when_nothing_queues(bridge, sc, agent_slots):  # noqa: F811
    """3.3, 3.5, NFR-6 (TC-74, P-26): without `slots` (nothing ever queues) and with `agentSlots` above any
    in-flight count or omitted, the request/start/end sequence (by label, in order) and the result are identical
    with finish-first on and off. One run each on the virtual clock: timers fire in simulated-time order, so no
    re-run is needed and load cannot reorder anything."""
    base = prepare(sc, slots=None)
    if agent_slots is not None:
        base["args"]["agentSlots"] = agent_slots
    on = run(bridge, with_finish_first(base, True))
    off = run(bridge, with_finish_first(base, False))
    assert events(on) == events(off)
    assert on["result"] == off["result"]


def test_p10_source_has_two_runs_and_no_loop():
    import inspect

    src = inspect.getsource(test_p10_finish_first_changes_nothing_when_nothing_queues)
    body = src.split('"""', 2)[2]
    assert len(re.findall(r"\brun\(", body)) == 2, body
    assert not re.search(r"^\s*(for|while)\b", body, re.M), body


# --- P-11 ------------------------------------------------------------------------------------------------------

def merge_resolutions(trace: List[Dict[str, Any]], check_red: Set[str]) -> List[tuple]:
    """[(event index, tasks)]: when each landing's MERGED tasks count as Done for the script. The fake project
    lands a task RED on its first landing when it is in `checkRed`, else MERGED. A batch resolves when its land
    ends, or, when that land calls a full gate, when the full gate ends (the script resolves batches after it)."""
    landings: Counter = Counter()
    out = []
    for i, e in enumerate(trace):
        if e["ev"] != "end" or kind(e["label"]) != "land":
            continue
        merged = set()
        for t in land_tasks(e["label"]):
            landings[t] += 1
            if not (t in check_red and landings[t] == 1):
                merged.add(t)
        at = i
        for j in range(i + 1, len(trace)):
            if trace[j]["ev"] == "end" and not is_instant(trace[j]["label"]):
                break
            if trace[j]["ev"] == "request" and kind(trace[j]["label"]) == "full gate":
                at = next(k for k in range(j + 1, len(trace)) if trace[k]["ev"] == "end" and trace[k]["label"] == trace[j]["label"])
                break
        out.append((at, merged))
    return out


def assert_starts_at_first_free_slot(out: Dict[str, Any], sc: Dict[str, Any]) -> None:
    """3.2, 3.9: at every `end` e after which a task is ready, an implementing slot is free (maxConcurrent), and the
    agents requested and not ended plus the non-task requests the script makes before the next `end` leave a
    runtime slot free, a first-attempt task request comes before the next `end`. "Next end" skips INSTANT agents,
    which end inside e's own microtask flush; counting their requests and keeping them in flight only makes the
    check apply less often."""
    trace = out["trace"]
    deps = sc["project"]["deps"]
    order = sc["args"]["remaining"]
    slots = sc["slots"]
    max_c = sc["args"].get("maxConcurrent") or float("inf")
    resolutions = merge_resolutions(trace, set(sc["project"].get("checkRed", [])))
    requested: Set[str] = set()
    not_ended: Counter = Counter()
    for i, e in enumerate(trace):
        label = e["label"]
        if e["ev"] == "request":
            not_ended[label] += 1
            n = first_task(label)
            if n:
                requested.add(n)
            continue
        if e["ev"] != "end":
            continue
        not_ended[label] -= 1
        done = set().union(*[ts for at, ts in resolutions if at <= i]) if resolutions else set()
        ready = [t for t in order if t not in requested and all(d in done for d in deps[t])]
        if not ready:
            continue
        # The next end that starts a new flush (INSTANT agents end inside the current one).
        nxt = next((j for j in range(i + 1, len(trace)) if trace[j]["ev"] == "end" and not is_instant(trace[j]["label"])), len(trace))
        window = trace[i + 1: nxt]
        window_requests = [w["label"] for w in window if w["ev"] == "request"]
        in_flight = sum(c for c in not_ended.values() if c > 0) + sum(1 for w in window_requests if not first_task(w))
        working = sum(c for lb, c in not_ended.items() if c > 0 and kind(lb) in IMPLEMENTER) + sum(1 for w in window_requests if kind(w) in ("retry", "revise"))
        if working >= max_c or in_flight >= slots:
            continue
        assert any(first_task(w) for w in window_requests), (
            f"after end of {label} (event {i}) tasks {ready} are ready, {working} implementing (max {max_c}), "
            f"{in_flight} of {slots} runtime slots in use, but no task is requested before the next end: {events(out)}"
        )


@SETTINGS
@given(scenarios(), st.integers(1, 6))
@example(
    sc={"project": {"deps": {"1": [], "2": [], "3": [], "4": []}, "durations": {"1": 5, "2": 30, "3": 5, "4": 5},
                    "reviewDurations": {"1": 20}, "mergeDurations": {}, "reviews": {"1": ["approve"]}, "checkRed": [], "failUntil": {}},
        "args": {"mergeBatch": 1, "fullGateEvery": 2, "wave": {"1": 1, "2": 1, "3": 1, "4": 1}, "maxConcurrent": 4,
                 "agents": {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 2}}},
        "slots": None},
    slots=1,
)
def test_p11_terminates_with_the_same_merges_and_starts_at_the_first_free_slot(bridge, sc, slots):  # noqa: F811
    """3.2, 3.9: with slots and finish-first on the run terminates (no deadlock), merges the same tasks as with
    finish-first off, and a held task start happens at the first settle that leaves a runtime slot free.

    `relink` is left out: with a relink, which tasks merge can depend on timing (a relinked task may or may not
    have started already), so the merged sets of two runs would differ without any fault."""
    sc = prepare(sc, slots=slots, drop_relink=True)
    on = run(bridge, with_finish_first(sc, True))
    off = run(bridge, with_finish_first(sc, False))
    assert on["result"].get("outcome"), on["result"]
    assert sorted(on["result"]["merged"], key=int) == sorted(off["result"]["merged"], key=int)
    assert_starts_at_first_free_slot(on, sc)


# --- P-21 ------------------------------------------------------------------------------------------------------

BUSY_DETAILS = ["claimed: ck task agent on hostA", "uncommitted changes in .ck/worktrees/demo-task-x", "claimed: pid 4242 on box-2"]


@SETTINGS
@given(scenarios(), st.data())
def test_p21_busy_worktrees_are_recorded_and_left_alone(bridge, sc, data):  # noqa: F811
    """2.3: each task of S whose worktree comes back BUSY is recorded ok:false with `worktree busy: <detail>`,
    exactly one agent runs for it, no other prompt names its worktree, and the other tasks merge as in the same
    scenario where S's tasks fail (so their dependents never start).

    `relink` is left out for the same reason as in P-11: the merged set must not depend on timing."""
    sc = prepare(sc, drop_relink=True)
    deps = sc["project"]["deps"]
    ids = list(deps)
    busy = set(data.draw(subsets(ids), label="S"))
    if data.draw(st.booleans(), label="include the last task"):
        busy.add(ids[-1])
    with_dependents = [t for t in ids if any(t in deps[u] for u in ids)]
    if with_dependents and data.draw(st.booleans(), label="include a task with dependents"):
        busy.add(data.draw(st.sampled_from(with_dependents), label="task with dependents"))
    if not busy:
        busy.add(data.draw(st.sampled_from(ids), label="one task"))
    details = {n: data.draw(st.sampled_from(BUSY_DETAILS), label=f"detail {n}") for n in sorted(busy, key=int)}

    busy_sc = copy.deepcopy(sc)
    busy_sc["project"]["worktree"] = {n: {"state": "BUSY", "detail": d} for n, d in details.items()}
    failing_sc = copy.deepcopy(sc)
    failing_sc["project"]["failUntil"] = {**sc["project"].get("failUntil", {}), **{n: 99 for n in busy}}

    out = run(bridge, busy_sc)
    ref = run(bridge, failing_sc)
    merged_ref = set(ref["result"]["merged"])
    assert sorted(out["result"]["merged"], key=int) == sorted(merged_ref, key=int)

    requests = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    for n, detail in details.items():
        reachable = all(d in merged_ref for d in deps[n])
        agents = [label for label in requests if n in tasks_named(label)]
        if not reachable:
            assert agents == [], f"task {n} waits on a task that never merged, yet agents ran: {agents}"
            continue
        assert agents == [f"task {n}"], f"BUSY task {n} got agents {agents}"
        rec = record_of(out, n)
        assert rec is not None and rec["ok"] is False, rec
        assert rec["reason"] == f"worktree busy: {detail}", rec
        path = re.compile(re.escape(wt(n)) + r"(?![0-9A-Za-z_])")
        for label, prompt in out["prompts"].items():
            if label != f"task {n}":
                assert not path.search(prompt), f"{label} prompt names BUSY task {n}'s worktree"


# --- P-22 ------------------------------------------------------------------------------------------------------

TC55 = {
    "project": {"deps": {"1": [], "2": [], "3": ["2"], "4": ["3"], "10": []}, "durations": {"1": 5, "2": 5, "3": 5, "4": 5, "10": 5},
                "reviewDurations": {}, "mergeDurations": {}, "reviews": {}, "checkRed": [], "failUntil": {}},
    "args": {"mergeBatch": 1, "fullGateEvery": 1, "wave": {"1": 1, "2": 1, "3": 1, "4": 1, "10": 1}},
    "slots": None,
}


@SETTINGS
@given(scenarios())
@example(sc=TC55)
def test_p22_each_start_is_the_head_of_ready_tasks(bridge, sc):  # noqa: F811
    """1.2: with maxConcurrent 1, no slots, no relink/externalDone/checkRed and no periodic full gate, each first
    `task N` request is the head of `readyTasks` over the args' order and deps, done = tasks whose land ended
    (MERGED) before the request, exclude = tasks already requested (failed ones included)."""
    sc = prepare(sc, slots=None, drop_relink=True, maxConcurrent=1, fullGateEvery=0)
    sc["project"]["checkRed"] = []
    sc["project"].pop("externalDone", None)
    out = run(bridge, sc)
    done: Set[str] = set()
    requested: List[str] = []
    for e in out["trace"]:
        if e["ev"] == "end" and kind(e["label"]) == "land":
            done |= set(land_tasks(e["label"]))  # no checkRed: every landing merges
        if e["ev"] == "request" and first_task(e["label"]):
            n = first_task(e["label"])
            ready = bridge.ready(sc["args"]["deps"], done=sorted(done, key=int), exclude=requested, blocked=[], order=sc["args"]["remaining"])
            assert ready and ready[0] == n, f"requested task {n}, readyTasks head is {ready[:1]} (done {sorted(done, key=int)}, requested {requested}): {events(out)}"
            requested.append(n)
    assert requested, "no task was requested"


# --- P-23 ------------------------------------------------------------------------------------------------------

@SETTINGS
@given(scenarios(), st.integers(1, 6))
def test_p23_a_new_task_start_never_queues(bridge, sc, slots):  # noqa: F811
    """3.9: with slots and finish-first on, fewer than `slots` agents are requested and not ended just before every
    first-attempt task request, so its `start` follows its `request` with no `end` between."""
    sc = prepare(sc, slots=slots)
    out = run(bridge, sc)
    trace = out["trace"]
    for i, e, _, not_ended in outstanding_walk(trace):
        if e["ev"] == "request" and first_task(e["label"]):
            in_flight = sum(c for c in not_ended.values() if c > 0)
            assert in_flight < slots, f"request {e['label']} at event {i} with {in_flight} agents in flight (slots {slots}): {events(out)}"
            nxt = next(w for w in trace[i + 1:] if w["label"] == e["label"] or w["ev"] == "end")
            assert nxt["ev"] == "start" and nxt["label"] == e["label"], f"{e['label']} queued: {events(out)}"
