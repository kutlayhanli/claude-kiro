"""TC-174 (P-24, P-25): live command edges and write-set scheduling, driven by Hypothesis through the Node bridge.

P-24 calls the decisions-block seam `liveEdgeDecision` (`bridge.call`); P-25 runs the real
`plugin/workflows/spec-implement.js` under the fake runtime and judges by its trace. No server, no mocks of the
project's own code. The oracles here are written from requirements 12.5-12.7 and 13.1, 13.2 and 13.5 alone.
"""

import copy
import re
from typing import Any, Dict, List, Optional, Set

from hypothesis import given, settings
from hypothesis import strategies as st

from landing_bridge import bridge, dags, scenarios, subsets  # noqa: F401  (bridge is a module-scoped fixture)
from test_landing_scheduler_properties import implementer_task, kind, land_tasks, prepare, run, tasks_named

PROPERTIES = {
    "P-24": "test_tc174_p24_live_edge_only_for_missing_command_with_unfinished_provider",
    "P-25": "test_tc174_p25_overlapping_write_sets_never_in_flight_together",
}

SETTINGS = settings(derandomize=True, max_examples=60, database=None, deadline=None)

# --- P-24 ----------------------------------------------------------------------------------------------------

COMMANDS = ["mtr thesis new", "mtr run", "ck lint", "tool sub cmd", "app init"]


def positive_message(command: str, form: int) -> str:
    last = command.split()[-1]
    prefix = " ".join(command.split()[:-1])
    forms = [
        f"No such command '{last}'.",
        f"No such command “{last}”.",
        f"{prefix or 'prog'}: error: argument command: invalid choice: '{last}' (choose from 'a', 'b')",
        f"{prefix or 'prog'}: error: unrecognized arguments: --{last}",
        f"Error: No such option: --{last}",
    ]
    return forms[form % len(forms)]


NEGATIVE_MESSAGES = [
    "git: 'x' is not a git command. See 'git --help'.",
    "AssertionError: assert 'invalid choice' in out",
    "AssertionError: assert 'No such command' in result.output",
    "assert 'unrecognized arguments' not in err",
    "KeyError: 'x'",
    "TypeError: f() got an unexpected keyword argument 'y'",
    "ModuleNotFoundError: No module named 'numpy'",
]


def failure(message: str, missing: Optional[Dict[str, Any]], n: int) -> Dict[str, Any]:
    return {
        "node": f"tests/test_x.py::test_tc{n}_a",
        "file": "tests/test_x.py",
        "kind": "assertion",
        "message": message,
        "truncated": False,
        "missingCommand": missing,
    }


def reaches(deps: Dict[str, List[str]], start: str, goal: str) -> bool:
    seen: Set[str] = set()
    stack = [start]
    while stack:
        t = stack.pop()
        if t == goal:
            return True
        if t in seen:
            continue
        seen.add(t)
        stack.extend(deps.get(t, []))
    return False


def acyclic(deps: Dict[str, List[str]]) -> bool:
    state: Dict[str, int] = {}

    def visit(t: str) -> bool:
        if state.get(t) == 1:
            return False
        if state.get(t) == 2:
            return True
        state[t] = 1
        ok = all(visit(d) for d in deps.get(t, []))
        state[t] = 2
        return ok

    return all(visit(t) for t in list(deps))


def edge_pair(e: Any) -> tuple:
    if isinstance(e, dict):
        return (str(e.get("task")), str(e.get("dependsOn")))
    return (str(e[0]), str(e[1]))


@st.composite
def edge_cases(draw):
    deps = draw(dags(max_tasks=12, dangling=False))
    ids = list(deps)
    task = draw(st.sampled_from(ids))
    # a real run never has a Done task that depends on an unfinished one: draw `done` closed under deps, from
    # tasks that do not (transitively) depend on `task`, so `task` itself stays unfinished
    allowed = [t for t in ids if t != task and not reaches(deps, t, task)]
    done_set: Set[str] = set()
    for t in draw(subsets(allowed)):
        done_set.add(t)
        done_set.update(i for i in ids if reaches(deps, t, i))
    done = [t for t in ids if t in done_set]
    # extra live edges only run from a higher to a lower id, so deps + liveDeps stay acyclic
    live: Dict[str, List[str]] = {}
    for t in draw(subsets(ids)):
        lower = [i for i in ids if int(i) < int(t)]
        if lower:
            live[t] = [draw(st.sampled_from(lower))]
    failures: List[Dict[str, Any]] = []
    positive = draw(st.booleans())
    nfail = 0
    if positive:
        command = draw(st.sampled_from(COMMANDS))
        mode = draw(st.sampled_from(["none", "one", "two", "self", "done"] if len(ids) > 1 else ["none", "one", "self"]))
        providers: List[str] = []
        if mode == "one":
            providers = [draw(st.sampled_from(ids))]
        elif mode == "two":
            providers = draw(st.lists(st.sampled_from(ids), min_size=2, max_size=2, unique=True))
        elif mode == "self":
            providers = [task]
        elif mode == "done" and done:
            providers = [draw(st.sampled_from(done))]
        failures.append(failure(positive_message(command, draw(st.integers(0, 4))), {"command": command, "providers": providers}, nfail))
        nfail += 1
    for msg in draw(st.lists(st.sampled_from(NEGATIVE_MESSAGES), max_size=3)):
        failures.insert(draw(st.integers(0, len(failures))), failure(msg, None, nfail))
        nfail += 1
    if not failures:
        failures.append(failure(draw(st.sampled_from(NEGATIVE_MESSAGES)), None, 0))
    return {"task": task, "failures": failures, "deps": deps, "done": done, "liveDeps": live}


@SETTINGS
@given(edge_cases())
def test_tc174_p24_live_edge_only_for_missing_command_with_unfinished_provider(bridge, case):
    """P-24 (12.5, 12.6, 12.7): V -> P only for a missing-command failure whose provider P is unfinished and
    would not close a cycle; no provider -> conflict; a cycle -> blocked with the cycle; deps stay acyclic."""
    # Bridge note: the design models `done` as a Set and `liveDeps` as a task -> Set map; this test sends lists,
    # so the Task 93 bridge converts them (set(done), {t: set(ds)}) before calling the script.
    v = case["task"]
    done = set(case["done"])
    out = bridge.call("liveEdgeDecision", copy.deepcopy(case))
    assert isinstance(out, dict), out
    action = out.get("action")
    edges = [edge_pair(e) for e in (out.get("edges") or [])]
    assert action in ("none", "wait", "conflict", "blocked"), out

    positives = [f for f in case["failures"] if f["missingCommand"]]
    all_deps = {t: list(ds) + list(case["liveDeps"].get(t, [])) for t, ds in case["deps"].items()}

    if not positives:  # complement: near-misses and plain assertion failures never add an edge
        assert edges == [] and action == "none", out
    else:
        providers = [str(p) for p in positives[0]["missingCommand"]["providers"]]
        candidates = [p for p in providers if p not in done and p != v]
        acyclic_c = [p for p in candidates if not reaches(all_deps, p, v)]
        if not providers:
            assert action == "conflict" and edges == [], out
        elif acyclic_c:
            assert action == "wait", out
            assert edges and all(e[0] == v and e[1] in acyclic_c for e in edges), out
        elif candidates:  # every unfinished provider already depends on V
            assert action == "blocked" and edges == [], out
            cycle = [str(x) for x in (out.get("cycle") or [])]
            assert v in cycle and any(p in cycle for p in candidates), out
        else:  # providers all Done or V itself: no edge
            assert edges == [] and action != "wait", out

    for a, b in edges:  # whatever was returned, the graph stays acyclic and edges point at unfinished providers
        assert a == v and b not in done and b != v, out
    merged = {t: list(ds) for t, ds in all_deps.items()}
    for a, b in edges:
        merged[a] = merged[a] + [b]
    assert acyclic(merged), (merged, out)


# --- P-25 ----------------------------------------------------------------------------------------------------

UNIVERSE = [
    "src/a.py", "src/b.py", "src/sub/c.py", "src/*.py", "src/**", "docs/x.md", "docs/*.md", "docs/**",
    "./src/a.py", "tests/t.py", "tests/**", "README.md",
]
SAMPLE_FILES = [
    "src/a.py", "src/b.py", "src/sub/c.py", "src/sub/deep/d.py", "docs/x.md", "docs/y.md", "docs/sub/z.md",
    "tests/t.py", "tests/sub/u.py", "README.md",
]


def _rx(pattern: str):
    pattern = pattern[2:] if pattern.startswith("./") else pattern
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def overlap(a: str, b: str) -> bool:
    ra, rb = _rx(a), _rx(b)
    return any(ra.match(f) and rb.match(f) for f in SAMPLE_FILES)


def overlaps_of(owns: Dict[str, List[str]]) -> Dict[str, List[str]]:
    res: Dict[str, List[str]] = {}
    for n, ps in owns.items():
        others = [m for m, qs in owns.items() if m != n and any(overlap(p, q) for p in ps for q in qs)]
        if others:
            res[n] = others
    return res


@st.composite
def write_set_scenarios(draw):
    sc = draw(scenarios())
    ids = list(sc["project"]["deps"])
    owns = {t: draw(st.lists(st.sampled_from(UNIVERSE), min_size=1, max_size=3, unique=True)) for t in draw(subsets(ids))}
    return sc, owns, draw(st.integers(1, 6))


def windows(out: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """{task: {start, end, first}}: first implementer start to the last land end (or last end naming the task)."""
    trace = out["trace"]
    res: Dict[str, Dict[str, int]] = {}
    for i, e in enumerate(trace):
        if e["ev"] == "start":
            n = implementer_task(e["label"])
            if n and n not in res:
                res[n] = {"start": i, "end": i}
    for i, e in enumerate(trace):
        if e["ev"] == "end":
            lab = e["label"]
            for n in tasks_named(lab):
                if n in res and (kind(lab) == "land" or i > res[n]["end"]):
                    if kind(lab) == "land" or not res[n].get("landed"):
                        res[n]["end"] = max(res[n]["end"], i)
                    if kind(lab) == "land":
                        res[n]["landed"] = i
    return res


@SETTINGS
@given(write_set_scenarios())
def test_tc174_p25_overlapping_write_sets_never_in_flight_together(bridge, drawn):
    """P-25 (13.1, 13.2, 13.5): no two overlapping tasks are in flight at once (start to land end), the first to
    start among overlapping ready tasks is the priority head, and every task that merges with write-sets off
    merges with them on."""
    sc, owns, slots = drawn
    overlaps = overlaps_of(owns)
    off = prepare(sc, slots, drop_relink=True)
    on = prepare(sc, slots, drop_relink=True)
    on["project"]["owns"] = owns
    on["project"]["overlaps"] = overlaps
    out_off = run(bridge, off)
    out_on = run(bridge, on)

    assert set(out_on["result"]["merged"]) == set(out_off["result"]["merged"]), (owns, out_on["result"]["merged"], out_off["result"]["merged"])

    win = windows(out_on)
    deps = on["project"]["deps"]
    merged = set(out_on["result"]["merged"])
    for a, others in overlaps.items():
        for b in others:
            if int(a) >= int(b) or a not in win or b not in win:  # each pair once (ids are ascending digits; compare as ints)
                continue
            wa, wb = win[a], win[b]
            assert wa["end"] < wb["start"] or wb["end"] < wa["start"], (a, b, wa, wb, out_on["trace"])
    for a, others in overlaps.items():
        for b in others:
            if a not in win or b not in win or win[a]["start"] >= win[b]["start"]:
                continue
            at = win[a]["start"]
            done_at = {t for t in merged if t in win and win[t].get("landed", 10**9) < at}
            started = [t for t, w in win.items() if w["start"] < at]
            ready = bridge.ready(deps, done=done_at, exclude=started)
            if a in ready and b in ready:
                assert ready.index(a) < ready.index(b), (a, b, ready, out_on["trace"])
