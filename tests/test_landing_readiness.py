"""Readiness contract: the workflow script's own `readyTasks` against the real `ck plan` (TC-3, P-1..P-5).

Every test calls the script's decision seam `readyTasks({order, deps, done, exclude, blocked})` through the
Node bridge (`tests/js/bridge.mjs`, which cuts the decisions block out of `plugin/workflows/spec-implement.js`).
P-1 compares it with `ck plan demo --json --exclude ...` run through the real CLI on a real git repo.
Before Task 3 adds the decisions block, every test fails with the bridge's "decision marker missing" error.
"""

from typing import Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from landing_bridge import bridge, ck_plan, dags, git_repo, subsets, write_spec  # noqa: F401

PROPERTIES = {
    "P-1": "test_tc3_p1_ready_tasks_matches_ck_plan",
    "P-2": "test_p2_dependency_missing_from_deps_keys_is_never_met",
    "P-3": "test_p3_excluding_x_removes_only_x",
    "P-4": "test_p4_input_order_does_not_change_ready",
    "P-5": "test_p5_marking_a_ready_task_done_is_monotone",
}

SETTINGS = settings(derandomize=True, max_examples=200, database=None, deadline=None)


def absent_ids(deps: Dict[str, List[str]]) -> List[str]:
    """Ids near the graph's range that are not tasks in it (including max + 1)."""
    present = {int(t) for t in deps}
    return [str(i) for i in range(1, max(present) + 3) if i not in present]


@st.composite
def done_and_exclude(draw, deps: Dict[str, List[str]]):
    """A Done subset of the graph, and an exclude subset that may hold Done ids and ids not in the graph."""
    ids = list(deps)
    done = draw(subsets(ids))
    extra = draw(subsets(absent_ids(deps)))[:3]
    exclude = draw(subsets(ids + extra))
    return done, exclude


def remaining(deps: Dict[str, List[str]], done) -> List[str]:
    done = set(done)
    return [t for t in deps if t not in done]


# --------------------------------------------------------------------------- TC-3 / P-1

@SETTINGS
@given(st.data())
def test_tc3_p1_ready_tasks_matches_ck_plan(bridge, data):
    deps = data.draw(dags(), label="deps")
    done, exclude = data.draw(done_and_exclude(deps), label="done/exclude")
    with git_repo() as repo:
        write_spec(repo, deps, done=done)
        plan = ck_plan(repo, exclude=exclude)
    assert sorted(plan["done"], key=int) == sorted(done, key=int)  # the oracle saw the Done set we wrote
    ours = bridge.ready(order=plan["remaining"], deps=plan["deps"], done=plan["done"], exclude=exclude, blocked=[])
    assert ours == plan["ready"]  # same set, same order (numeric id order: 9 < 10 < 100)


# --------------------------------------------------------------------------- P-2

@SETTINGS
@given(st.data())
def test_p2_dependency_missing_from_deps_keys_is_never_met(bridge, data):
    base = data.draw(dags(dangling=False), label="deps")
    ids = list(base)
    t = data.draw(st.sampled_from(ids), label="t")
    forms = ["max+1", "absent-in-done"] + (["removed"] if len(ids) > 1 else [])
    form = data.draw(st.sampled_from(forms), label="form")
    deps = {k: list(v) for k, v in base.items()}
    if form == "max+1":
        d = str(max(int(i) for i in ids) + 1)
    elif form == "removed":
        d = data.draw(st.sampled_from([i for i in ids if i != t]), label="removed")
        del deps[d]
    else:
        d = data.draw(st.sampled_from(absent_ids(base)), label="absent")
    assert d not in deps
    others = [x for x in deps[t] if x != d]
    rest_done = data.draw(subsets([i for i in deps if i != t]), label="more done")
    done = sorted(set(others) | set(rest_done) | {d}, key=int)  # d and every other dep of t are Done
    order = remaining(deps, done)

    # Without d, t would be ready: so the property below is not vacuous.
    baseline = {**deps, t: others}
    assert t in bridge.ready(order=order, deps=baseline, done=done, exclude=[], blocked=[])

    deps[t] = data.draw(st.permutations(others + [d] * data.draw(st.integers(1, 2))), label="t deps")
    assert t not in bridge.ready(order=order, deps=deps, done=done, exclude=[], blocked=[])


# --------------------------------------------------------------------------- P-3

@SETTINGS
@given(st.data())
def test_p3_excluding_x_removes_only_x(bridge, data):
    deps = data.draw(dags(), label="deps")
    done, exclude = data.draw(done_and_exclude(deps), label="done/exclude")
    order = remaining(deps, done)
    before = bridge.ready(order=order, deps=deps, done=done, exclude=exclude, blocked=[])
    pools = {
        "ready": before,
        "not-ready": [i for i in deps if i not in before and i not in done],
        "done": list(done),
        "absent": absent_ids(deps),
    }
    kind = data.draw(st.sampled_from([k for k, v in pools.items() if v]), label="kind")
    x = data.draw(st.sampled_from(pools[kind]), label="x")
    after = bridge.ready(order=order, deps=deps, done=done, exclude=exclude + [x], blocked=[])
    assert after == [y for y in before if y != x]


# --------------------------------------------------------------------------- P-4

@SETTINGS
@given(st.data())
def test_p4_input_order_does_not_change_ready(bridge, data):
    deps = data.draw(dags(), label="deps")
    done, exclude = data.draw(done_and_exclude(deps), label="done/exclude")
    order = remaining(deps, done)  # tasks.md order: never permuted
    expected = bridge.ready(order=order, deps=deps, done=done, exclude=exclude, blocked=[])

    keys = data.draw(st.permutations(list(deps)), label="key order")  # e.g. "100" before "9": JS reorders such keys
    shuffled = {k: data.draw(st.permutations(deps[k]), label=f"deps of {k}") for k in keys}
    done2 = data.draw(st.permutations(done), label="done order")
    exclude2 = data.draw(st.permutations(exclude), label="exclude order")
    assert bridge.ready(order=order, deps=shuffled, done=done2, exclude=exclude2, blocked=[]) == expected
    # Also against a descending insertion order, the one most unlike JS's integer-key order.
    desc = {k: list(reversed(deps[k])) for k in sorted(deps, key=int, reverse=True)}
    assert bridge.ready(order=order, deps=desc, done=list(reversed(done)), exclude=list(reversed(exclude)), blocked=[]) == expected


# --------------------------------------------------------------------------- P-5

@SETTINGS
@given(st.data())
def test_p5_marking_a_ready_task_done_is_monotone(bridge, data):
    deps = data.draw(dags(), label="deps")
    done, exclude = data.draw(done_and_exclude(deps), label="done/exclude")
    before = bridge.ready(order=remaining(deps, done), deps=deps, done=done, exclude=exclude, blocked=[])
    if not before:  # make the ready list non-empty: lift the exclusions (the first task can still be dangling)
        exclude = []
        before = bridge.ready(order=remaining(deps, done), deps=deps, done=done, exclude=exclude, blocked=[])
    if not before:  # still empty (every open task waits on a dangling dep): nothing to mark Done
        return
    r = data.draw(st.sampled_from(before), label="r")
    done2 = done + [r]
    after = bridge.ready(order=remaining(deps, done2), deps=deps, done=done2, exclude=exclude, blocked=[])
    assert r not in after
    assert set(after) >= set(before) - {r}
    for added in set(after) - set(before):
        assert r in deps[added], (added, deps[added])
