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


# ------------------------------------------------- red-team: fallback counting (1.4)
#
# Requirement 1.4: a consistent first/retry plan is adopted with NO fallback
# counted. Each test below is an adversarial shape where a naive counting
# implementation over-counts (any retry, any echoed list, any first-failure,
# empty ready, or repeated retries counted as a fallback). All run the real
# script through the bridge with adversarial `planSeq` answers (Task 10's
# `runPlan` counting; no gating needed). Before Task 10 they fail for the
# right reason: the result carries no explicit fallback counting
# (`degraded`/`planFallbacks`/`stats.planFallbacks` absent).


def assert_no_fallback_counted(out: Dict) -> Dict:
    """1.4 counting contract: consistent adoptions record an explicit zero.

    Requires the fields present (not merely falsy/absent) so the tests fail
    before Task 10 for the right reason (counting absent) and catch
    over-counting after it.
    """
    result = out["result"]
    assert result.get("degraded") is False, (
        f"consistent plan must leave degraded False, got {result.get('degraded')!r}")
    assert "planFallbacks" in result, "result must carry an explicit planFallbacks list (absent pre-Task-10)"
    assert result.get("planFallbacks") == [], result.get("planFallbacks")
    stats = result.get("stats") or {}
    assert stats.get("planFallbacks") == 0, stats.get("planFallbacks")
    return result


def redteam_project(deps: Dict[str, List[str]], plan_seq, max_concurrent: int = 2) -> Dict:
    """A deterministic bridge project/args pair with script-side readiness on."""
    project = {
        "deps": {t: list(ds) for t, ds in deps.items()},
        "durations": {t: 2 for t in deps},
        "planDurations": dict(PLAN_MS_ALL),
        "planSeq": plan_seq,
    }
    args = {
        "deps": {t: list(ds) for t, ds in deps.items()},
        "remaining": list(deps),
        "maxConcurrent": max_concurrent,
    }
    return {"project": project, "args": args}


def test_redteam_fallback_counting_retry_success_transient_error(bridge):  # noqa: F811
    """1.4 red-team: first attempt unusable (transient error), retry consistent.

    A counter that increments on any retry/any failed attempt would record a
    fallback here; the retry success must adopt with zero fallbacks.
    """
    deps = {"1": [], "2": ["1"], "3": ["2"]}
    cfg = redteam_project(deps, [{"error": "transient boom"}, {}])
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    plans = plan_requests(out["trace"])
    assert [l for l in plans if l in ("plan 1", "plan 1 retry")] == ["plan 1", "plan 1 retry"], plans
    assert_no_fallback_counted(out)
    assert set(out["result"]["merged"]) == set(deps)


def test_redteam_fallback_counting_echoed_ready_not_fallback(bridge):  # noqa: F811
    """1.4 red-team: plan echoes the deterministic ready list back verbatim.

    The system's own output fed back as input: ready equals exactly what
    `fallbackReady` would compute, so a counter keyed on "matches fallback
    list" must still record zero (it was adopted, not fallen back to).
    """
    deps = {"1": [], "2": ["1"], "3": ["1"], "4": ["2", "3"]}
    oracle = bridge.ready(deps)
    assert oracle, oracle
    cfg = redteam_project(deps, [{"ready": list(oracle)}])
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    plans = plan_requests(out["trace"])
    assert plans.count("plan 1") == 1, plans
    assert not [l for l in plans if l.endswith(" retry")], plans
    assert_no_fallback_counted(out)
    assert set(out["result"]["merged"]) == set(deps)


def test_redteam_fallback_counting_ordering_only_difference(bridge):  # noqa: F811
    """1.4 red-team: first answer has the right ready set in the wrong order.

    Order-only mismatch is inconsistent (chain-then-numeric is significant),
    so exactly one retry fires; the consistent retry must still count zero
    fallbacks (a first-failure counter would over-count).
    """
    deps = {"1": [], "2": [], "3": ["1", "2"]}
    oracle = bridge.ready(deps)
    assert len(oracle) >= 2, oracle
    assert seam_consistent(plan_view(deps, oracle), []) is True
    shuffled = list(reversed(oracle))
    assert shuffled != oracle
    assert seam_consistent(plan_view(deps, shuffled), []) is False
    cfg = redteam_project(deps, [{"ready": shuffled}, {}])
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    plans = plan_requests(out["trace"])
    assert [l for l in plans if l in ("plan 1", "plan 1 retry")] == ["plan 1", "plan 1 retry"], plans
    assert_no_fallback_counted(out)
    assert set(out["result"]["merged"]) == set(deps)


def test_redteam_fallback_counting_empty_ready_consistent(bridge):  # noqa: F811
    """1.4 red-team: consistent plan with an empty ready list and all Done.

    Boundary shape: nothing left to schedule is still a consistent adoption,
    not a fallback trigger (an empty-ready counter would over-count).
    """
    deps = {"1": [], "2": ["1"]}
    plan_seq = [{"remaining": [], "done": ["1", "2"], "ready": [], "deps": dict(deps)}]
    assert seam_consistent(
        {"remaining": [], "done": ["1", "2"], "ready": [], "deps": dict(deps)}, []) is True
    cfg = redteam_project(deps, plan_seq)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    plans = plan_requests(out["trace"])
    assert plans.count("plan 1") == 1, plans
    assert not [l for l in plans if l.endswith(" retry")], plans
    assert_no_fallback_counted(out)


def test_redteam_fallback_counting_double_retry_success(bridge):  # noqa: F811
    """1.4 red-team: two separate plan steps each fail transiently then succeed.

    Repeated retry-success episodes must still total zero fallbacks (a
    cumulative retry counter would over-count); and no step ever attempts a
    third plan call.
    """
    deps = {"1": [], "2": ["1"], "3": ["2"], "4": ["3"]}
    cfg = redteam_project(
        deps, [{"error": "boom-1"}, {}, {"error": "boom-2"}, {}], max_concurrent=1)
    cfg["args"]["mergeBatch"] = 1
    out = bridge.run(cfg["project"], cfg["args"], slots=1)
    plans = plan_requests(out["trace"])
    retries = [l for l in plans if l.endswith(" retry")]
    assert len(retries) >= 2, (plans, retries)  # start + at least one landing step retried
    for label in plans:  # never a third attempt for any step
        assert not label.endswith(" retry retry"), plans
        assert label.count("retry") <= 1, plans
    by_step: Dict[str, int] = {}
    for label in plans:
        m = PLAN_LABEL.match(label)
        assert m is not None, label
        by_step[m.group(1)] = by_step.get(m.group(1), 0) + 1
    assert all(n <= 2 for n in by_step.values()), (plans, by_step)
    assert_no_fallback_counted(out)
    assert set(out["result"]["merged"]) == set(deps)
