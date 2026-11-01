"""Contract property: deterministic fallback readiness vs the real `ck plan` (P-4, TC-8/TC-29).

Covers requirements 1.2, 2.4 (NFR-3): for any acyclic task graph, Done set and
exclude set, the deterministic fallback ready list equals `ck plan --json
--exclude` `ready` on the same tasks.md -- same set, same order (longest
remaining chain first, then numeric task id). Metamorphic sub-check: reordering
keys/dep lists/done/exclude never changes the list.

Each test calls the `fallbackReady` decision seam from
`plugin/workflows/spec-implement.js` (via `tests/js/load_decisions.mjs`, the
same loader the Task 9 implementation makes pass) plus the real `ck plan` CLI
on a real git repo (via `tests/landing_bridge.py` helpers, the landing-speed
readiness-oracle pattern).

Before Task 9 adds the seam, every test fails for the right reason: the loader
raises "decision function missing in the decisions block: fallbackReady".
Imports of project code stay inside test bodies/helpers so the module always
collects.
"""

import copy
import json
import random
import re
import subprocess
from pathlib import Path
from typing import Dict, List

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from landing_bridge import bridge, ck_plan, dags, git_repo, scenarios, subsets, write_spec  # noqa: F401

PROPERTIES = {
    "P-1": "test_retry_once",
    "P-2": "test_fallback_continues",
    "P-3": "test_degraded_marking",
    "P-4": "test_fallback_matches_ck_plan",
    "P-5": "test_fallback_exclusions",
    "P-6": "test_last_good_layering",
    "P-7": "test_unknown_dep_never_ready",
}

BRIDGE_SETTINGS = settings(derandomize=True, max_examples=60, database=None, deadline=None)

SETTINGS = settings(derandomize=True, max_examples=200, database=None, deadline=None)

JS_DIR = Path(__file__).parent / "js"
LOAD_DECISIONS_URL = (JS_DIR / "load_decisions.mjs").as_uri()

# One Node call per example: compute scriptReady from the existing `readyTasks`
# seam (unfiltered), then layer it through the new `fallbackReady` seam exactly
# as design D5 describes (deps layer minus excluded/done). Fails before Task 9
# with "decision function missing ... fallbackReady".
NODE_CALLER = (
    f"import {{ loadDecisions }} from {LOAD_DECISIONS_URL!r};"
    "const input = JSON.parse(process.argv[1]);"
    "const { readyTasks, fallbackReady } = loadDecisions({ extra: ['fallbackReady'] });"
    "const scriptReady = readyTasks({ order: input.order, deps: input.deps,"
    " done: input.done, exclude: [], blocked: [] });"
    "const fb = fallbackReady({ hasDeps: true, scriptReady, lastGoodReady: [],"
    " exclude: input.exclude, done: input.done });"
    "console.log(JSON.stringify({ ready: fb.ready, source: fb.source }));"
)


def fallback_via_seam(order, deps: Dict[str, List[str]], done, exclude) -> Dict:
    """Call the decisions block's `fallbackReady` seam over one example's data."""
    payload = json.dumps(
        {
            "order": list(order),
            "deps": {k: list(v) for k, v in deps.items()},
            "done": list(done),
            "exclude": list(exclude),
        }
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", NODE_CALLER, payload],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"fallbackReady seam call failed: {proc.stderr.strip()}"
    return json.loads(proc.stdout)


def check_against_ck_plan(deps: Dict[str, List[str]], done, exclude) -> None:
    """One CLI-driven example: seam ready/source equal `ck plan --json` on the same spec."""
    with git_repo() as repo:
        write_spec(repo, deps, done=done)
        plan = ck_plan(repo, exclude=exclude)
    assert sorted(plan["done"], key=int) == sorted(done, key=int)  # oracle saw the Done set we wrote
    out = fallback_via_seam(plan["remaining"], plan["deps"], plan["done"], exclude)
    assert out["ready"] == plan["ready"]  # same set, same order (chain-then-numeric)
    if plan["ready"]:
        assert out["source"] == "deps"
    else:
        assert out["ready"] == [] and out["source"] is None
    # Metamorphic: reordering keys/dep lists/done/exclude never changes the list.
    # tasks.md order (plan["remaining"]) is never permuted, matching the P-4 row.
    seed = repr((sorted(deps.items()), sorted(done, key=int), sorted(exclude, key=str)))
    rng = random.Random(seed)
    keys = list(plan["deps"])
    rng.shuffle(keys)
    shuffled = {}
    for k in keys:
        lst = list(plan["deps"][k])
        rng.shuffle(lst)
        shuffled[k] = lst
    done2 = list(plan["done"])
    rng.shuffle(done2)
    exclude2 = list(exclude)
    rng.shuffle(exclude2)
    again = fallback_via_seam(plan["remaining"], shuffled, done2, exclude2)
    assert again["ready"] == out["ready"]
    assert again["source"] == out["source"]


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


# --------------------------------------------------------------------------- TC-8 / P-4


@SETTINGS
@given(st.data())
def test_fallback_matches_ck_plan(data):
    deps = data.draw(dags(), label="deps")
    done, exclude = data.draw(done_and_exclude(deps), label="done/exclude")
    check_against_ck_plan(deps, done, exclude)


# ------------------------------------------------- TC-8 table half (>= 60 CLI draws)


def table_graphs():
    """64 deterministic graphs covering every surface form the P-4 row names."""
    rng = random.Random(0xDE7)
    graphs = [
        {"1": []},  # single task, empty dep list
        {"1": [], "2": ["1"], "3": ["1"], "4": ["2", "3"]},  # diamond
        {"1": [], "2": ["1"], "3": ["2"], "4": ["3"], "5": ["4"]},  # long chain
        {"1": [], "2": ["1", "1"], "9": ["2", "2", "1"]},  # duplicate deps, multi-digit ids
        {"1": [], "2": ["99"]},  # dangling dep
        {"1": [], "2": [], "9": [], "10": [], "11": [], "100": []},  # non-contiguous multi-digit ids
        {"2": ["1"], "10": ["2", "9"], "9": ["1"], "1": [], "100": ["10", "11"], "11": []},  # mixed sizes
        {"1": [], "2": ["1"], "3": ["2"], "4": ["1", "2", "3"], "5": []},  # fan-in plus independent
    ]
    while len(graphs) < 64:
        size = rng.randint(1, 12)
        pool = sorted(rng.sample(range(1, 121), size))
        ids = [str(i) for i in pool]
        deps: Dict[str, List[str]] = {}
        for pos, tid in enumerate(ids):
            lower = [str(i) for i in pool[:pos]]
            chosen = [str(rng.choice(lower)) for _ in range(rng.randint(0, min(3, len(lower))))] if lower else []
            if lower and rng.random() < 0.12:
                chosen = chosen + [str(rng.randint(121, 130))]  # dangling dep
            deps[tid] = chosen
        graphs.append(deps)
    return graphs


def test_fallback_matches_ck_plan_table():
    rng = random.Random(0xC4A)
    for n, deps in enumerate(table_graphs()):
        ids = list(deps)
        done = [i for i in ids if rng.random() < 0.3]
        extra = [str(rng.randint(121, 130)) for _ in range(rng.randint(0, 2))]  # absent ids
        exclude = [i for i in ids if rng.random() < 0.2] + extra
        if rng.random() < 0.5:  # Done ids in exclude
            exclude = exclude + [i for i in done if rng.random() < 0.5]
        check_against_ck_plan(deps, done, exclude[:4])
        assert n < 64


# ------------------------------------------------- TC-29 / NFR-3 exclude half


def test_fallback_matches_ck_plan_exclude():
    """Exclude-focused draws: Done ids, absent ids, and ready ids in --exclude."""
    rng = random.Random(0xE29)
    graphs = table_graphs()[:16]
    for deps in graphs:
        ids = list(deps)
        present_max = max(int(i) for i in ids)
        done = [i for i in ids if rng.random() < 0.4]
        absent = [str(present_max + 1), str(present_max + 2)]
        exclude = done[:2] + absent + [i for i in ids if i not in done][:1]
        with git_repo() as repo:
            write_spec(repo, deps, done=done)
            plan = ck_plan(repo, exclude=exclude)
            bare = ck_plan(repo)
        out = fallback_via_seam(plan["remaining"], plan["deps"], plan["done"], exclude)
        assert out["ready"] == plan["ready"]
        for x in exclude:
            assert x not in out["ready"]
        # Excluding nothing recovers the full ready list.
        full = fallback_via_seam(bare["remaining"], bare["deps"], bare["done"], [])
        assert full["ready"] == bare["ready"]


# ------------------------------------------------- P-1 / P-2 / P-3 (Story 1 group)
#
# Bridge-driven properties over scenarios() x planSeq shapes (complements of the
# TC-1/TC-2 example cases). Decision seams called directly: `planConsistent`
# (P-1, P-3) and `fallbackReady` (P-2, design.md D5 shape). Before Tasks 9-10 the
# fallback tests fail for the right reason: the loader raises "decision function
# missing in the decisions block: fallbackReady", and the script halts an
# always-inconsistent run with "planning step failed" instead of continuing
# degraded. Imports stay at module level (landing_bridge exists); project-code
# seams are reached only through the Node loader / bridge subprocess calls.


PLAN_LABEL = re.compile(r"^plan (\d+)( retry)?$")
PLAN_MS_ALL = {str(k): 1 for k in range(1, 400)}  # every plan call takes 1 ms of virtual time

CONSISTENT_CALLER = (
    f"import {{ loadDecisions }} from {LOAD_DECISIONS_URL!r};"
    "const input = JSON.parse(process.argv[1]);"
    "const { planConsistent } = loadDecisions();"
    "console.log(JSON.stringify({ consistent: planConsistent(input.plan, input.exclude) }));"
)

# Design.md D5 shape: fallbackReady({ hasDeps, scriptReady, lastGoodReady, exclude, done }).
FALLBACK_CALLER = (
    f"import {{ loadDecisions }} from {LOAD_DECISIONS_URL!r};"
    "const input = JSON.parse(process.argv[1]);"
    "const { fallbackReady } = loadDecisions({ extra: ['fallbackReady'] });"
    "const out = fallbackReady({ hasDeps: input.hasDeps, scriptReady: input.scriptReady,"
    " lastGoodReady: input.lastGoodReady, exclude: input.exclude, done: input.done });"
    "console.log(JSON.stringify({ ready: out.ready, source: out.source }));"
)


def seam_consistent(plan: Dict, exclude) -> bool:
    """Call the `planConsistent` decision seam on one plan answer (exists today)."""
    payload = json.dumps({"plan": plan, "exclude": list(exclude)})
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", CONSISTENT_CALLER, payload],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"planConsistent seam call failed: {proc.stderr.strip()}"
    return json.loads(proc.stdout)["consistent"]


def seam_fallback(has_deps: bool, script_ready, last_good_ready, exclude, done) -> Dict:
    """Call the `fallbackReady` decision seam (added by Task 9; missing until then)."""
    payload = json.dumps(
        {
            "hasDeps": has_deps,
            "scriptReady": list(script_ready),
            "lastGoodReady": list(last_good_ready),
            "exclude": list(exclude),
            "done": list(done),
        }
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", FALLBACK_CALLER, payload],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"fallbackReady seam call failed: {proc.stderr.strip()}"
    return json.loads(proc.stdout)


def plan_view(deps: Dict[str, List[str]], ready, done=()) -> Dict:
    """A plan-agent answer shape for the `planConsistent` seam: truthful deps, given ready."""
    return {
        "remaining": list(deps),
        "done": list(done),
        "ready": list(ready),
        "deps": {k: list(v) for k, v in deps.items()},
    }


def prepare_retry_sc(sc: Dict) -> Dict:
    """A scenarios() draw with script-side readiness on (args.deps/remaining from the graph)."""
    sc = copy.deepcopy(sc)
    sc["project"]["planDurations"] = dict(PLAN_MS_ALL)
    deps = sc["project"]["deps"]
    sc["args"]["deps"] = {t: list(ds) for t, ds in deps.items()}
    sc["args"]["remaining"] = list(deps)
    return sc


def plan_requests(trace) -> List[str]:
    return [e["label"] for e in trace if e["ev"] == "request" and PLAN_LABEL.match(e["label"])]


def fallback_entries(result: Dict) -> List[Dict]:
    """The run's fallback records via one accessor (exact field names are Task 22's choice).

    Design shape: result["planFallbacks"] = [{planStep, trigger, source, ready}, ...].
    Harness-contract shape: {"count": int, "triggers": [label, ...]}.
    """
    fb = result.get("planFallbacks", [])
    if isinstance(fb, dict):
        return [{"trigger": t} for t in fb.get("triggers", [])]
    return list(fb or [])


def fallback_count(result: Dict) -> int:
    entries = fallback_entries(result)
    stats = result.get("stats") or {}
    return stats.get("planFallbacks", len(entries))


CORRUPTIONS = ["omitted", "unknown", "extra-nonready"]  # drawn via corruption_kinds()


def corrupt_ready(oracle: List[str], deps: Dict[str, List[str]], kind: str, rng: random.Random) -> List[str]:
    """An inconsistent first answer covering every surface form the P-1 row names.

    Every form stays inconsistent *after* the script's `normalizePlan` (unknown ids
    are filtered there, so the check in `runPlan` never sees them): that is the
    input domain `planConsistent` actually decides on (design.md D2, error case
    2.5). A redundant unknown id next to the full correct list would normalize to
    the oracle and be adopted without retry, so the "unknown" form replaces the
    list instead of extending it.
    """
    ids = list(deps)
    absent = str(max(int(t) for t in ids) + 1)  # near-miss: max id + 1, never a task
    if kind == "extra-nonready":
        # Ready holds a non-ready id (a known task, so it survives normalization).
        # Only drawn when such a task exists (caller checks).
        non_ready = [t for t in ids if t not in oracle]
        return list(oracle) + [rng.choice(non_ready)]
    if kind == "omitted":
        # Ready omits a ready id (caller guarantees a non-empty oracle).
        return list(oracle[1:])
    return [absent]  # ready lists an unknown id (and omits the real ready list)


def corruption_kinds(oracle: List[str], deps: Dict[str, List[str]]) -> List[str]:
    """Drawable P-1 corruptions for one graph: the extra-nonready form needs a
    non-ready task (impossible when every task is independent)."""
    kinds = ["omitted", "unknown"]
    if any(t not in oracle for t in deps):
        kinds.append("extra-nonready")
    return kinds


# --------------------------------------------------------------------------- P-1


@BRIDGE_SETTINGS
@given(st.data())
def test_retry_once(bridge, data):  # noqa: F811
    """P-1 (1.1): an inconsistent first answer is retried exactly once with the same
    inputs; a consistent answer is never retried. Complement of P-2's fallback arm
    (healthy second answers here). Metamorphic: the same scenario with a healthy
    first answer shows no retry and merges the same set."""
    sc = prepare_retry_sc(data.draw(scenarios(plan_seq="consistent"), label="scenario"))
    sc["args"]["maxConcurrent"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="maxConcurrent")
    sc["slots"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="slots")
    deps = sc["project"]["deps"]
    oracle = bridge.ready(deps)  # the healthy ready list over empty done/exclude
    assume(oracle)  # every draw has a root task, so the oracle is non-empty
    # Drawn after the oracle: extra-nonready needs a non-ready task in this graph.
    kind = data.draw(st.sampled_from(corruption_kinds(oracle, deps)), label="corruption")
    bad = corrupt_ready(oracle, deps, kind, random.Random(repr((sorted(deps.items()), oracle, kind))))

    # The seams agree with the corruption: bad is inconsistent, the oracle is not.
    assert seam_consistent(plan_view(deps, bad), []) is False, kind
    assert seam_consistent(plan_view(deps, oracle), []) is True

    sc["project"]["planSeq"] = [{"ready": bad}, {}]  # TC-1 shape: inconsistent, then consistent
    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    plans = plan_requests(out["trace"])
    start_attempts = [label for label in plans if label in ("plan 1", "plan 1 retry")]
    assert start_attempts == ["plan 1", "plan 1 retry"], (kind, plans)  # exactly one retry, no third attempt
    assert all(label not in ("plan 1", "plan 1 retry") for label in plans[2:]), (kind, plans)

    # The consistent retry is adopted with no fallback counted (1.4).
    result = out["result"]
    assert not result.get("degraded"), (kind, result.get("degraded"))
    assert fallback_entries(result) == [], (kind, result.get("planFallbacks"))

    # Metamorphic complement: a healthy first answer is never retried, same merged set.
    healthy = prepare_retry_sc(copy.deepcopy(sc))
    healthy["project"]["planSeq"] = [{}, {}]
    healthy_out = bridge.run(healthy["project"], healthy["args"], slots=healthy["slots"])
    healthy_plans = plan_requests(healthy_out["trace"])
    assert [label for label in healthy_plans if label == "plan 1"] == ["plan 1"], (kind, healthy_plans)
    merged = set(result["merged"])
    healthy_merged = set(healthy_out["result"]["merged"])
    assert merged == healthy_merged, (kind, merged, healthy_merged)


# --------------------------------------------------------------------------- P-2


@BRIDGE_SETTINGS
@given(st.data())
def test_fallback_continues(bridge, data):  # noqa: F811
    """P-2 (1.2): when the retry is also inconsistent, the run continues from the
    deterministic ready list and never halts with a planning stop; running tasks
    still land. Complement of P-1's retry arm. The script's scheduling agrees with
    the `fallbackReady` seam on the same inputs (TC-35 half)."""
    sc = prepare_retry_sc(data.draw(scenarios(plan_seq="always-bad"), label="scenario"))
    sc["args"]["maxConcurrent"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="maxConcurrent")
    sc["slots"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="slots")
    deps = sc["project"]["deps"]
    ids = list(deps)
    # Red landings and conflicts from the P-2 domain (small conflict draws stay resolvable).
    sc["project"]["checkRed"] = data.draw(subsets(ids), label="checkRed")
    sc["project"]["conflicts"] = data.draw(subsets(ids[:2]), label="conflicts")

    # Seam agreement on the same inputs the script will use: deps layer, ck-plan order.
    oracle = bridge.ready(deps)
    fb = seam_fallback(True, oracle, [], [], [])
    assert fb["ready"] == oracle, (oracle, fb)
    assert (fb["source"] == "deps") if oracle else (fb["ready"] == [] and fb["source"] is None)

    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    result = out["result"]
    requests = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    assert "plan 1 retry" in requests, requests[:5]  # the retry happened before any fallback
    assert result.get("reason") != "planning step failed", result.get("reason")
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    merged = set(result["merged"])
    assert merged == set(ids), (merged, set(ids))  # the deterministic-oracle merged set


# --------------------------------------------------------------------------- P-3


@BRIDGE_SETTINGS
@given(st.data())
def test_degraded_marking(bridge, data):  # noqa: F811
    """P-3 (1.3): every fallback run is marked degraded, the fallback count equals
    the number of triggering plan attempts, and each recorded trigger names a real
    `plan k` / `plan k retry` label from the trace. Same draws as P-2."""
    sc = prepare_retry_sc(data.draw(scenarios(plan_seq="always-bad"), label="scenario"))
    sc["args"]["maxConcurrent"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="maxConcurrent")
    sc["slots"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="slots")
    deps = sc["project"]["deps"]
    ids = list(deps)
    sc["project"]["checkRed"] = data.draw(subsets(ids), label="checkRed")
    sc["project"]["conflicts"] = data.draw(subsets(ids[:2]), label="conflicts")

    # The always-bad answers really are inconsistent: ready holds an id absent
    # from the graph (dynamic max id + 1, never a task), omitting the real list.
    absent = str(max(int(t) for t in ids) + 1)
    assert seam_consistent(plan_view(deps, [absent]), []) is False

    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    result = out["result"]
    requests = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    plan_labels = set(plan_requests(out["trace"]))

    # Every plan answer is inconsistent, so every plan step fails twice: each
    # `plan k retry` label in the trace is a triggering attempt that must have
    # exactly one recorded fallback. The expected count comes from the trace,
    # never from the result accessor, so an implementation that under-counts
    # (e.g. one entry for N triggering pairs) cannot pass.
    triggering = [label for label in plan_requests(out["trace"]) if label.endswith(" retry")]
    retry_steps = {PLAN_LABEL.match(label).group(1) for label in triggering}
    assert len(triggering) >= 1, (requests[:5], sorted(plan_labels))
    assert result.get("degraded") is True, (result.get("degraded"), result.get("reason"))
    entries = fallback_entries(result)
    assert len(entries) == len(triggering), (len(entries), triggering)
    assert fallback_count(result) == len(triggering), (fallback_count(result), triggering)
    for entry in entries:
        trigger = entry.get("trigger")
        step = entry.get("planStep")
        label = trigger if (isinstance(trigger, str) and PLAN_LABEL.match(trigger)) else f"plan {step}"
        assert label in plan_labels, (entry, sorted(plan_labels))  # trigger names a real trace label
        matched = PLAN_LABEL.match(label)
        assert matched is not None and matched.group(1) in retry_steps, (entry, triggering)
        if "ready" in entry:
            assert entry["ready"], entry  # a fallback that scheduled nothing is not a fallback


# ------------------------------------------------- P-5 / P-6 / P-7 (Story 2 group)
#
# Fallback layering and unknown-dep properties over the `fallbackReady` decision
# seam (design.md D5 shape: fallbackReady({ hasDeps, scriptReady, lastGoodReady,
# exclude, done })) plus bridge-run behavior. Before Tasks 9-10 every test fails
# for the right reason: the loader raises "decision function missing in the
# decisions block: fallbackReady", and an always-inconsistent run halts with
# "planning step failed" instead of continuing from the deterministic list.

TASK_LABEL = re.compile(r"^task (\d+)$")


def task_requests(trace) -> List[str]:
    """Task ids requested as `task N` agents, in trace order (exact id match)."""
    return [TASK_LABEL.match(e["label"]).group(1) for e in trace if e["ev"] == "request" and TASK_LABEL.match(e["label"])]


# --------------------------------------------------------------------------- P-5


@BRIDGE_SETTINGS
@given(st.data())
def test_fallback_exclusions(bridge, data):  # noqa: F811
    """P-5 (1.5, 2.1): no merged, running, failed, or excluded task is ever in
    the fallback ready list, and scheduling one fallback task never alters
    another task's record (excluded tasks get no agent, land in neither merged
    nor failed). Complement of P-4's order check: same oracle, minus excluded.

    The drawn exclusions are seeded into the bridge run: done/merged draws are
    already-finished at fallback time (project done: never requested, records
    untouched), running draws are slow tasks overlapping a slow
    landing-triggered plan (TC-4 pattern: a later fallback fires while they
    run and must not re-schedule them), failed draws are seeded BUSY failures
    (requested once, never merged, run continues; their dependents stay
    blocked). Absent-id exclusions are never requestable.
    """
    sc = prepare_retry_sc(data.draw(scenarios(plan_seq="always-bad"), label="scenario"))
    sc["args"]["maxConcurrent"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="maxConcurrent")
    sc["slots"] = data.draw(st.sampled_from([1, 2, 3, 4]), label="slots")
    deps = sc["project"]["deps"]
    ids = list(deps)
    assume(ids)
    done = data.draw(subsets(ids), label="done")
    rest = [t for t in ids if t not in done]
    running = data.draw(subsets(rest), label="running")
    failed = data.draw(subsets([t for t in rest if t not in running]), label="failed")
    merged = data.draw(subsets([t for t in rest if t not in running and t not in failed]), label="merged")
    extra = data.draw(subsets(absent_ids(deps)), label="extra-exclude")[:3]
    exclude = sorted(set(running) | set(failed) | set(merged) | set(extra), key=lambda x: (len(x), x))
    finished = sorted(set(done) | set(merged), key=lambda x: (len(x), x))  # already-finished at fallback time
    sc["project"]["done"] = list(finished)
    # In-flight running: slow tasks overlap a slow landing-triggered plan, so a
    # later fallback fires while they run (no in-flight overlap with 1 ms plans).
    for r in running:
        sc["project"]["durations"][r] = 200
    sc["project"]["planDurations"] = {**PLAN_MS_ALL, 2: 100}
    # Seeded failures: BUSY on the first attempt ends the task there (2.6 path).
    sc["project"]["worktree"] = {f: {"state": "BUSY", "detail": "seeded failure"} for f in failed}

    # Seam: the deps layer minus excluded/finished; nothing excluded is ever ready.
    oracle = bridge.ready(deps, done=finished)
    fb = seam_fallback(True, oracle, [], exclude, finished)
    assert set(fb["ready"]).isdisjoint(exclude)
    assert set(fb["ready"]).isdisjoint(finished)
    assert set(fb["ready"]) <= set(oracle)
    if set(oracle) - set(exclude) - set(finished):
        assert fb["ready"] == [t for t in oracle if t not in exclude and t not in finished]
        assert fb["source"] == "deps"
    else:
        assert fb["ready"] == [] and fb["source"] is None

    # Bridge: fallback scheduling never touches an excluded task's record.
    # Tasks transitively blocked by a seeded failure never merge either.
    never = set(failed)
    while True:
        newly = {t for t in ids if t not in finished and t not in never and any(d in never for d in deps[t])}
        if not newly:
            break
        never |= newly
    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    result = out["result"]
    requested = task_requests(out["trace"])
    for x in list(finished) + extra:
        assert x not in requested, (x, requested)
    rows = {t["task"] for t in result.get("tasks", [])}
    assert rows.isdisjoint(finished), rows
    assert rows.isdisjoint(extra), rows
    for r in running:
        assert requested.count(r) <= 1, (r, requested)  # never re-scheduled while in flight
    failed_ids = {f["task"] for f in result.get("failed", [])}
    assert failed_ids <= set(failed), (failed_ids, failed)
    assert failed_ids.isdisjoint(finished), failed_ids
    assert set(result["merged"]).isdisjoint(finished), result["merged"]
    assert set(result["merged"]).isdisjoint(set(failed) | set(extra)), result["merged"]
    assert set(result["merged"]) == set(ids) - set(finished) - never, (result["merged"], finished, never)
    if failed:
        assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))
    else:
        assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")


# --------------------------------------------------------------------------- P-6


@BRIDGE_SETTINGS
@given(st.data())
def test_last_good_layering(bridge, data):  # noqa: F811
    """P-6 (2.2): without `args.deps`, fallback uses exactly the last good
    plan's ready minus finished/excluded (never script readiness, never a
    guess); with no good plan yet, nothing is scheduled. Shapes [good, bad,
    bad] / [bad, bad] / [good]. Complement of P-5's deps layer."""
    sc = data.draw(scenarios(plan_seq="consistent"), label="scenario")
    deps = sc["project"]["deps"]
    ids = list(deps)
    assume(ids)
    absent = str(max(int(t) for t in ids) + 1)  # near-miss unknown id, never a task
    shape = data.draw(st.sampled_from(["good-bad-bad", "bad-bad", "good"]), label="shape")
    # Dep-less mode: no dependency data with the run inputs.
    sc["args"].pop("deps", None)
    sc["args"].pop("remaining", None)
    sc["project"]["planDurations"] = dict(PLAN_MS_ALL)

    # Seam: hasDeps=False ignores scriptReady exactly, even a non-empty bogus one.
    last_good = bridge.ready(deps)  # the healthy plan-1 list over empty done
    assume(last_good)
    done = data.draw(subsets(ids), label="done")
    exclude = data.draw(subsets(ids), label="exclude")
    bogus = data.draw(st.sampled_from([[absent], list(ids), last_good + [absent]]), label="bogus-scriptReady")
    fb = seam_fallback(False, bogus, last_good, exclude, done)
    assert fb["ready"] == [t for t in last_good if t not in exclude and t not in done]
    assert set(fb["ready"]).isdisjoint(done) and set(fb["ready"]).isdisjoint(exclude)
    assert absent not in fb["ready"]  # never a guess beyond the last good list
    if set(last_good) - set(exclude) - set(done):
        assert fb["source"] == "last-good"
    else:
        assert fb["ready"] == [] and fb["source"] is None
    # No good plan yet: nothing scheduled, whatever scriptReady claims.
    empty = seam_fallback(False, bogus, [], exclude, done)
    assert empty["ready"] == [] and empty["source"] is None

    if shape == "good":
        out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
        assert out["result"].get("outcome") == "complete", out["result"].get("reason")
        assert not out["result"].get("degraded"), out["result"].get("degraded")
        assert fallback_entries(out["result"]) == []
    elif shape == "good-bad-bad":
        sc["project"]["planSeq"] = [{}, {"ready": [absent]}, {"ready": [absent]}]
        out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
        result = out["result"]
        assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
        assert result.get("degraded") is True, result.get("reason")
        entries = fallback_entries(result)
        assert entries, "a twice-failed plan with last-good data must record a fallback"
        for entry in entries:
            assert entry.get("source") == "last-good", entry
            if "ready" in entry:
                assert set(entry["ready"]) <= set(last_good), (entry, last_good)
                assert absent not in entry["ready"], entry
    else:  # [bad, bad]: no good plan ever -> no task scheduled, planning halt.
        sc["project"]["planSeq"] = [{"ready": [absent]}, {"ready": [absent]}]
        out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
        assert task_requests(out["trace"]) == [], task_requests(out["trace"])
        reason = out["result"].get("reason") or ""
        assert "planning step failed" in reason, reason


# --------------------------------------------------------------------------- P-7


@BRIDGE_SETTINGS
@given(st.data())
def test_unknown_dep_never_ready(bridge, data):  # noqa: F811
    """P-7 (2.5): a task whose dep is absent from the map is never scheduled by
    fallback, even when that id appears in `done`. Unknown forms: max-id+1, a
    removed task id, a done-only id. Complement: deps all known-and-Done plus
    not excluded plus sole open task IS schedulable."""
    deps = data.draw(dags(max_tasks=8, dangling=False), label="deps")
    ids = list(deps)
    assume(len(ids) >= 2)
    present_max = max(int(t) for t in ids)
    form = data.draw(st.sampled_from(["max-plus-one", "removed", "done-only"]), label="unknown-form")
    deps2 = {k: list(v) for k, v in deps.items()}
    done_extra: List[str] = []
    if form == "max-plus-one":
        unknown = str(present_max + 1)
    elif form == "removed":
        victim = data.draw(st.sampled_from(ids), label="victim")
        t_cand = [t for t in ids if t != victim]
        assume(t_cand)
        unknown = victim
        del deps2[unknown]
        for k in deps2:
            deps2[k] = [d for d in deps2[k] if d != unknown]
    else:
        unknown = str(present_max + 2)
        done_extra = [unknown]  # done-only id: in done, absent from the map
    t = data.draw(st.sampled_from([k for k in deps2]), label="blocked-task")
    deps2[t] = list(deps2[t]) + [unknown]
    done = data.draw(subsets([k for k in deps2 if k != t]), label="done")
    if data.draw(st.booleans(), label="unknown-in-done"):
        done = list(done) + [unknown]  # unknown id appears in done: still never ready
    done = sorted(set(done) | set(done_extra), key=lambda x: (len(x), x))

    # Seam input keeps the unknown-done negative: the unknown id stays in done,
    # so the "even when that id appears in done" case reaches the seam.
    script_ready = bridge.ready(deps2, done=done)
    assert t not in script_ready, (form, t, unknown, script_ready)
    fb = seam_fallback(True, script_ready, [], [], done)
    assert t not in fb["ready"], (form, t, unknown, fb)

    # Bridge: the full done set (unknown ids included) reaches the run; relink
    # stays off so the unknown dep is never rewritten into a known one.
    sc = prepare_retry_sc(data.draw(scenarios(plan_seq="always-bad"), label="scenario"))
    sc["project"].pop("relink", None)  # relink rewrites deps mid-run; the unknown dep must stay unknown
    sc["project"]["deps"] = {k: list(v) for k, v in deps2.items()}
    sc["project"]["done"] = list(done)
    sc["project"]["badReady"] = [str(present_max + 9)]  # inconsistent, keeps deps truthful
    sc["args"]["deps"] = {k: list(v) for k, v in deps2.items()}
    sc["args"]["remaining"] = list(deps2)
    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    result = out["result"]
    requested = task_requests(out["trace"])
    assert t not in requested, (form, t, unknown, requested)
    assert t not in result["merged"], (form, t, result["merged"])
    # The run proceeds with the rest instead of halting on the plan; t stays
    # unstarted, so the outcome is blocked on t — never a planning halt.
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))
    knownDone = {d for d in done if d in deps2}
    if not any(t in v for v in deps2.values()):
        # t blocks nothing: everything else merges.
        assert set(result["merged"]) == set(deps2) - {t} - knownDone, (
            form, result["merged"], done)
    else:
        # Per-task readiness with t treated as never-Done: every merged task
        # had all deps satisfied, and every other open task still waits on an
        # unmerged dep (no exact merged set: t's dependents stay blocked too).
        mergedSet = set(result["merged"])
        assert mergedSet.isdisjoint(knownDone), (form, mergedSet)
        for u in mergedSet:
            assert set(deps2[u]) <= knownDone | mergedSet, (form, u, deps2[u])
        for u in set(deps2) - mergedSet - knownDone - {t}:
            assert not set(deps2[u]) <= knownDone | mergedSet, (form, u, deps2[u])

    # Complement: known-and-Done deps + not excluded + sole open task IS schedulable.
    small = {"3": [], "7": ["3"]}
    assert bridge.ready(small, done=["3"]) == ["7"]
    fb_small = seam_fallback(True, ["7"], [], [], ["3"])
    assert fb_small["ready"] == ["7"] and fb_small["source"] == "deps"
