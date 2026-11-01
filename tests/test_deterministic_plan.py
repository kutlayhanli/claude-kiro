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
    "P-9": "test_review_bound",
    "P-10": "test_batch_verdict_fanout",
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


# ------------------------------------------------- red-team: unknown deps (2.5)
#
# Requirement 2.5: a task whose dependency is unknown to the deterministic data
# is never started on a guess. Each test below is an adversarial shape where a
# naive readiness computation would guess: numeric-vs-string coercion (`2` vs
# `'2'` vs `'02'`), a dangling id that matches a done-only entry, a dep id
# present only in `exclude`, case/whitespace-mutated dep keys, and the plan's
# own bad `ready` fed back as `done`. Every test runs the real `readyTasks`
# seam plus the `fallbackReady` seam on the same inputs, then the real script
# through the bridge with always-inconsistent plans (Task 10's `runPlan`
# scriptReady path; needs no gating). Before Tasks 9-10 they fail for the
# right reason: the loader raises "decision function missing in the decisions
# block: fallbackReady" and the script halts with "planning step failed"
# instead of scheduling from the deterministic list.


def test_redteam_unknown_dep_numeric_string_confusion(bridge):  # noqa: F811
    """2.5 red-team: numeric-vs-string id confusion (`2` vs `'2'` vs `'02'`).

    `decisionIds` stringifies, so an int dep `2` IS the task `'2'` (met when
    Done: schedulable, never wrongly blocked), while `'02'` is a different key
    (never met, even when done lists it: a Number()-coercing guess would start
    task 2 on the `'02'` entry). Both directions are pinned: the int form
    merges end-to-end, the zero-padded form never starts.
    """
    int_deps = {"1": [], "2": [1], "3": ["1"]}
    # Int form is the same task after stringification: met once Done.
    assert bridge.ready(int_deps, done=[]) == ["1"]
    assert set(bridge.ready(int_deps, done=["1"])) == {"2", "3"}
    fb_int = seam_fallback(True, bridge.ready(int_deps, done=["1"]), [], [], ["1"])
    assert set(fb_int["ready"]) == {"2", "3"} and fb_int["source"] == "deps"

    padded = {"1": [], "2": ["02"], "3": ["1"]}
    # Zero-padded form is unknown: never ready, even with '02' in done.
    assert bridge.ready(padded, done=["1"]) == ["1"]
    assert "2" not in bridge.ready(padded, done=["1", "02"])
    fb_pad = seam_fallback(True, bridge.ready(padded, done=["1", "02"]), [], [], ["1", "02"])
    assert "2" not in fb_pad["ready"]

    # End-to-end: the int form completes, the padded form stays unready.
    cfg = redteam_project(int_deps, [{"ready": ["999"]}] * 12)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    assert out["result"].get("outcome") == "complete", out["result"].get("reason")
    assert set(out["result"]["merged"]) == set(int_deps)

    cfg = redteam_project(padded, [{"ready": ["999"]}] * 12)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    requested = task_requests(out["trace"])
    assert "2" not in requested, ("02-guess", requested)
    assert "2" not in result["merged"], result["merged"]
    assert set(result["merged"]) == {"1", "3"}, result["merged"]
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))


def test_redteam_unknown_dep_done_only_match(bridge):  # noqa: F811
    """2.5 red-team: dangling id that matches a done-only entry.

    Dep `99` is absent from the map, but `done` lists `99` (a done-only id,
    never a task). A met-check that looks only at `done` would start task 2
    on that match; the script requires key-of-deps AND Done, so 2 stays
    unready while the rest of the run proceeds.
    """
    deps = {"1": [], "2": ["99"], "3": ["1"]}
    assert bridge.ready(deps, done=["99"]) == ["1"]
    fb = seam_fallback(True, bridge.ready(deps, done=["99"]), [], [], ["99"])
    assert "2" not in fb["ready"] and fb["ready"] == ["1"] and fb["source"] == "deps"

    cfg = redteam_project(deps, [{"ready": ["999"]}] * 12)
    cfg["project"]["done"] = ["99"]
    cfg["args"]["deps"] = {t: list(ds) for t, ds in deps.items()}
    cfg["args"]["remaining"] = list(deps)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    requested = task_requests(out["trace"])
    assert "2" not in requested, ("done-only-guess", requested)
    assert "2" not in result["merged"], result["merged"]
    assert set(result["merged"]) == {"1", "3"}, result["merged"]
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))


def test_redteam_unknown_dep_exclude_only(bridge):  # noqa: F811
    """2.5 red-team: dep id present only in `exclude`.

    Dep `7` is absent from the map and appears only in the exclude list. An
    implementation that treats "excluded dep = satisfied dep" would start task
    2; exclusion only removes tasks from scheduling, it never satisfies a
    dependency, so 2 stays unready with any exclude combination.
    """
    deps = {"1": [], "2": ["7"], "3": ["1"]}
    script_ready = bridge.ready(deps)
    assert script_ready == ["1"], script_ready
    assert "2" not in bridge.ready(deps, exclude=["7"])
    for exclude, done in [(["7"], []), (["7"], ["1"]), (["7", "1"], []), ([], ["7"])]:
        fb = seam_fallback(True, bridge.ready(deps, done=done), [], exclude, done)
        assert "2" not in fb["ready"], (exclude, done, fb)

    cfg = redteam_project(deps, [{"ready": ["999"]}] * 12)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    requested = task_requests(out["trace"])
    assert "2" not in requested, ("exclude-only-guess", requested)
    assert "2" not in result["merged"], result["merged"]
    assert set(result["merged"]) == {"1", "3"}, result["merged"]
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))


def test_redteam_unknown_dep_key_mutation(bridge):  # noqa: F811
    """2.5 red-team: case/whitespace-mutated dep keys.

    `decisionIds`/`hasKey` are exact: dep `" 1"` (or `"1 "`) is not the task
    `"1"`, and done entries with the same mutations never satisfy the exact
    dep. A trimming/case-folding guess would start tasks 2/3; only the exact
    dep (task 4) is schedulable.
    """
    deps = {"1": [], "2": [" 1"], "3": ["1 "], "4": ["1"], "5": ["A"], "a": []}
    assert set(bridge.ready(deps, done=["1"])) == {"a", "4"}
    # The mutations in done do not help either: ' 1' is not a key, so task 2
    # stays unready even when done names the mutated string; 'A' vs 'a' likewise.
    assert "2" not in bridge.ready(deps, done=["1", " 1", "1 "])
    assert "3" not in bridge.ready(deps, done=["1", " 1", "1 "])
    assert "5" not in bridge.ready(deps, done=["1", "a", "A", " 1", "1 "])
    fb = seam_fallback(
        True, bridge.ready(deps, done=["1", " 1", "1 ", "a"]), [], [], ["1", " 1", "1 ", "a"])
    assert set(fb["ready"]) == {"a", "4"} and fb["source"] == "deps", fb
    assert "2" not in fb["ready"] and "3" not in fb["ready"] and "5" not in fb["ready"]

    run_deps = {"1": [], "2": [" 1"], "3": ["1"]}
    cfg = redteam_project(run_deps, [{"ready": ["999"]}] * 12)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    requested = task_requests(out["trace"])
    assert "2" not in requested, ("whitespace-guess", requested)
    assert set(result["merged"]) == {"1", "3"}, result["merged"]
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))


def test_redteam_unknown_dep_bad_ready_as_done(bridge):  # noqa: F811
    """2.5 red-team: fallback fed the plan's own bad `ready` as `done`.

    The system's own output fed back as input: the lying plan answered ready
    `["99"]`, and `99` now appears in `done`. Task 2 depends on `99`, which
    is still absent from the map, so it must stay unready -- the fallback
    must not launder the plan's guess into a satisfied dependency.
    """
    deps = {"1": [], "2": ["99"], "3": ["1"]}
    bad = ["99"]
    assert seam_consistent(plan_view(deps, bad), []) is False
    script_ready = bridge.ready(deps, done=list(bad))
    assert "2" not in script_ready, (bad, script_ready)
    fb = seam_fallback(True, script_ready, [], [], list(bad))
    assert "2" not in fb["ready"] and fb["ready"] == ["1"], fb

    cfg = redteam_project(deps, [{"ready": list(bad)}] * 12)
    cfg["project"]["done"] = list(bad)
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    requested = task_requests(out["trace"])
    assert "2" not in requested, ("bad-ready-laundered", requested)
    assert "2" not in result["merged"], result["merged"]
    assert set(result["merged"]) == {"1", "3"}, result["merged"]
    assert "planning step failed" not in (result.get("reason") or ""), result.get("reason")
    assert result.get("outcome") == "blocked", (result.get("outcome"), result.get("reason"))


# ------------------------------------------------- P-9 / P-10 (Story 4 group)
#
# Wave-review batching bound + verdict fan-out over the `reviewGroups`
# decision seam (design.md: reviewGroups({ merged, risk }) -> { batched,
# individual }; individual = merged tasks whose risk tag is `safety`
# case-insensitive; batched = the rest; empty merged -> both empty) plus the
# real script's review labels through the bridge (TC-17..TC-22 pattern:
# coverage from the review ledger `reviewTasks`, never label text alone).
# Before Task 18 the seam is absent (the loader raises "decision function
# missing in the decisions block: reviewGroups") and the script runs one
# `review N` per task with no `review-wave` label, so every test below fails
# for the right reason (seam missing, then one-review-per-task shape).

REVIEW_GROUPS_CALLER = (
    f"import {{ loadDecisions }} from {LOAD_DECISIONS_URL!r};"
    "const input = JSON.parse(process.argv[1]);"
    "const { reviewGroups } = loadDecisions({ extra: ['reviewGroups'] });"
    "const out = reviewGroups({ merged: input.merged, risk: input.risk });"
    "console.log(JSON.stringify({ batched: out.batched, individual: out.individual }));"
)

REVIEWER = {"model": "sonnet", "effort": "low", "risky_model": "opus", "risky_effort": "high"}

# Proven wave-forcing timing (TC-17 pattern): task 1 ends fast and its landing
# is slow, so tasks 2..N finish while it lands and merge as one later wave.
WAVE_TIMING = {"durations": {1: 10}, "mergeDurations": {1: 300}}
WAVE_REST_DURATION = 50


def seam_review_groups(merged, risk: Dict) -> Dict:
    """Call the `reviewGroups` decision seam (added by Task 9; missing until then)."""
    payload = json.dumps({"merged": [str(t) for t in merged], "risk": {str(k): v for k, v in risk.items()}})
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", REVIEW_GROUPS_CALLER, payload],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"reviewGroups seam call failed: {proc.stderr.strip()}"
    return json.loads(proc.stdout)


def review_events(events) -> List[str]:
    """Review request labels in event order (the `... end` completions excluded)."""
    return [e for e in events if e.startswith("review") and not e.endswith(" end")]


def batch_review_labels(events) -> List[str]:
    return [e for e in review_events(events) if e.startswith("review-wave")]


def individual_review_labels(events) -> List[str]:
    return [e for e in review_events(events) if re.match(r"^review \d", e)]


def covers_of(out: Dict, label: str) -> List[str]:
    return sorted(out.get("reviewTasks", {}).get(label, []))


def batches_covering(out: Dict, ids) -> List[str]:
    wanted = sorted(str(t) for t in ids)
    return [label for label in batch_review_labels(out["events"]) if covers_of(out, label) == wanted]


def merged_waves(events, merged) -> List[List[str]]:
    """One entry per `land ... end` batch that merged something (the W of NFR-4)."""
    merged_set = {str(t) for t in merged}
    waves = []
    for e in events:
        if e.startswith("land ") and e.endswith(" end"):
            hit = sorted((set(e[5:-4].split()) & merged_set), key=lambda x: (len(x), x))
            if hit:
                waves.append(hit)
    return waves


def wave_project(ids, risk: Dict, reviews: Dict, extra_project: Dict = None) -> Dict:
    """A forced-two-wave bridge project/args pair: task 1 lands alone, the rest
    merge as one later wave (TC-17 timing, deterministic by a ~250ms margin)."""
    first, rest = ids[0], ids[1:]
    project = {
        "deps": {t: [] for t in ids},
        "durations": {first: WAVE_TIMING["durations"][1], **{t: WAVE_REST_DURATION for t in rest}},
        "mergeDurations": {first: WAVE_TIMING["mergeDurations"][1]},
        "planDurations": dict(PLAN_MS_ALL),
        "risk": {t: risk.get(t, "normal") for t in ids},
        "reviews": {t: list(v) for t, v in reviews.items()},
        **(extra_project or {}),
    }
    args = {
        "deps": {t: [] for t in ids},
        "remaining": list(ids),
        "maxConcurrent": max(4, len(ids)),
        "agents": {"reviewer": dict(REVIEWER)},
        "risk": {t: risk.get(t, "normal") for t in ids},
    }
    return {"project": project, "args": args}


@st.composite
def risk_maps(draw, ids) -> Dict[str, str]:
    """A risk map over `ids` covering the P-9 row's 0-100% safety sweep:
    no-risky, all-risky, single-risky, and random subsets in between."""
    mode = draw(st.sampled_from(["none", "all", "one", "half", "random"]))
    if mode == "none":
        return {t: "normal" for t in ids}
    if mode == "all":
        return {t: "safety" for t in ids}
    if mode == "one":
        risky = [draw(st.sampled_from(ids))]
        return {t: ("safety" if t in risky else "normal") for t in ids}
    if mode == "half":
        return {t: ("safety" if (int(t) % 2 == 0) else "normal") for t in ids}
    risky = set(draw(subsets(ids)))
    return {t: ("safety" if t in risky else "normal") for t in ids}


# --------------------------------------------------------------------------- P-9


@BRIDGE_SETTINGS
@given(st.data())
def test_review_bound(bridge, data):  # noqa: F811
    """P-9 (4.1, 4.2, 4.3, 4.6, NFR-4; hosts the TC-30 body): for any run with
    W merged waves containing >=1 non-risky task and R risky tasks, the review
    count is <= W + R; every risky task has its own individual review; waves
    with zero merged tasks get no review.

    Generators: scenarios() (healthy plans) x risk maps (0-100% safety incl.
    all-risky/no-risky waves) x verdict sequences incl. changes; wave sizes
    1-6. The count bound is asserted on approve runs (a `changes` verdict
    adds a flagged-task re-review on top of the bound by design, 4.4); the
    risky-individual and no-empty-review relations hold under both verdicts.
    """
    sc = data.draw(scenarios(plan_seq="consistent"), label="scenario")
    wave_size = data.draw(st.integers(1, 6), label="wave-size")
    ids = [str(i) for i in range(1, wave_size + 2)]  # task 1 lands alone; the rest form the wave
    risk = data.draw(risk_maps(ids), label="risk")
    verdict_mode = data.draw(st.sampled_from(["approve", "changes"]), label="verdicts")
    slots = sc["slots"] or 4

    # Seam first: the split is exact on every draw (fails pre-Task-9 with the
    # missing-seam message, the right reason).
    wave = ids[1:]
    out = seam_review_groups(wave, risk)
    assert set(out["batched"]) | set(out["individual"]) == set(wave), out
    assert set(out["batched"]) & set(out["individual"]) == set(), out
    assert set(out["individual"]) == {t for t in wave if str(risk.get(t, "")).lower() == "safety"}, out
    # Empty merged yields no review groups (4.6 at the seam).
    empty = seam_review_groups([], risk)
    assert empty["batched"] == [] and empty["individual"] == [], empty
    # Case-insensitive by design (via the existing isSafety): one fixed check.
    folded = seam_review_groups(["9"], {"9": "Safety"})
    assert folded["individual"] == ["9"] and folded["batched"] == [], folded

    reviews = {}
    if verdict_mode == "changes":
        reviews = {t: ["changes", "approve"] for t in ids}
    cfg = wave_project(ids, risk, reviews)
    out = bridge.run(cfg["project"], cfg["args"], slots=max(slots, len(ids)))
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    merged = [str(t) for t in result["merged"]]
    assert set(merged) == set(ids), (merged, ids)
    waves = merged_waves(out["events"], merged)
    assert len(waves) >= 1, out["events"]
    risky = {t for t in merged if str(risk.get(t, "normal")).lower() == "safety"}
    labels = review_events(out["events"])

    # No wave with zero merged tasks gets a review (4.6): every review covers
    # merged tasks only, and unmerged tasks are never covered.
    for label in labels:
        covered = covers_of(out, label)
        assert covered, (label, "an empty review ran")
        assert set(covered) <= set(merged), (label, covered, merged)

    # Every risky task has its own individual review (4.2, 4.3).
    individuals = individual_review_labels(out["events"])
    for r in sorted(risky):
        assert any(l == f"review {r}" or l.startswith(f"review {r} ") for l in individuals), (r, individuals)

    if verdict_mode == "approve":
        # The NFR-4 bound (TC-30): review-agent count <= W + R, counted from labels.
        assert len(labels) <= len(waves) + len(risky), (labels, waves, sorted(risky))
        # Every non-risky merged wave is covered by exactly one batched review (4.1).
        for w in waves:
            plain = [t for t in w if t not in risky]
            touched = [l for l in batch_review_labels(out["events"]) if set(covers_of(out, l)) & set(w)]
            if plain:
                assert len(touched) == 1, (w, touched)
                assert sorted(covers_of(out, touched[0])) == sorted(plain), (w, covers_of(out, touched[0]))
            else:
                # A risky-only wave runs no combined review (4.3).
                assert touched == [], (w, touched)


def test_review_bound_tc30(bridge):  # noqa: F811
    """TC-30 (NFR-4) table body, hosted by Task 16: runs with W merged waves
    (with >=1 non-risky task) and R risky tasks keep review-agent count <= W+R."""
    # Case A (4.1): one non-risky wave of 3 behind task 1 -> W=2, R=0, bound 2.
    ids = ["1", "2", "3", "4"]
    cfg = wave_project(ids, {}, {})
    out = bridge.run(cfg["project"], cfg["args"], slots=4)
    assert set(out["result"]["merged"]) == set(ids), out["result"]
    waves = merged_waves(out["events"], ids)
    assert sorted(map(sorted, waves)) == [["1"], ["2", "3", "4"]], waves
    labels = review_events(out["events"])
    assert len(labels) <= len(waves), (labels, waves)  # pre-Task-18: 4 one-per-task reviews > 2
    assert len(batches_covering(out, ["2", "3", "4"])) == 1, batch_review_labels(out["events"])
    assert individual_review_labels(out["events"]) == [], individual_review_labels(out["events"])

    # Case B (4.2): 2 normal + 1 risky share the later wave -> W=2, R=1, bound 3.
    risk = {"4": "safety"}
    cfg = wave_project(ids, risk, {})
    out = bridge.run(cfg["project"], cfg["args"], slots=4)
    assert set(out["result"]["merged"]) == set(ids), out["result"]
    labels = review_events(out["events"])
    assert len(labels) <= 2 + 1, (labels, out["events"])
    assert len(batches_covering(out, ["2", "3"])) == 1, batch_review_labels(out["events"])
    assert not [l for l in batch_review_labels(out["events"]) if "4" in covers_of(out, l)], batch_review_labels(out["events"])
    assert any(l == "review 4" for l in individual_review_labels(out["events"])), out["events"]

    # Case C (4.3): risky-only later wave -> each risky individual, no combined.
    risk = {"2": "safety", "3": "safety"}
    ids3 = ["1", "2", "3"]
    cfg = wave_project(ids3, risk, {})
    out = bridge.run(cfg["project"], cfg["args"], slots=4)
    assert set(out["result"]["merged"]) == set(ids3), out["result"]
    assert len(batches_covering(out, ["1"])) == 1, batch_review_labels(out["events"])  # the non-risky wave is still batched
    assert not [l for l in batch_review_labels(out["events"]) if set(covers_of(out, l)) & {"2", "3"}], batch_review_labels(out["events"])
    for n in ("2", "3"):
        assert any(l == f"review {n}" for l in individual_review_labels(out["events"])), out["events"]

    # Case D (4.6): an unmerged task gets no review; the merged wave is batched.
    deps = {"1": [], "2": []}
    project = {
        "deps": dict(deps),
        "durations": {"1": 10, "2": 50},
        "mergeDurations": {"1": 300},
        "planDurations": dict(PLAN_MS_ALL),
        "protectedTasks": ["2"],  # task 2 never merges
    }
    args = {
        "deps": dict(deps),
        "remaining": ["1", "2"],
        "maxConcurrent": 4,
        "agents": {"reviewer": dict(REVIEWER)},
    }
    out = bridge.run(project, args, slots=4)
    assert out["result"]["merged"] == ["1"], out["result"]
    covering2 = [l for l in review_events(out["events"]) if "2" in covers_of(out, l)]
    assert covering2 == [], (covering2, "an empty batched review ran for the unmerged task")
    assert len(batches_covering(out, ["1"])) == 1, batch_review_labels(out["events"])


# --------------------------------------------------------------------------- P-10


@BRIDGE_SETTINGS
@given(st.data())
def test_batch_verdict_fanout(bridge, data):  # noqa: F811
    """P-10 (4.4, 4.5): for any batched review with per-task verdicts, each
    flagged task gets its own revise + re-review cycle while unflagged
    wave-mates record approval without extra agents; an all-approve batch
    records approval for every covered task.

    Generators: scenarios() (healthy plans) x wave sizes 1-6 x flagged
    subsets (none/some/all) via per-task findings on a shared batched verdict
    (the TC-20 shape: every wave task carries the same first answer, looked
    up by the wave's first covered task).
    """
    sc = data.draw(scenarios(plan_seq="consistent"), label="scenario")
    wave_size = data.draw(st.integers(1, 6), label="wave-size")
    ids = [str(i) for i in range(1, wave_size + 2)]  # task 1 lands alone; tasks 2.. form the wave
    wave = ids[1:]
    flagged = sorted(data.draw(subsets(wave), label="flagged"))  # none/some/all
    slots = sc["slots"] or 4

    # The seam agrees the all-normal wave batches together (fails pre-Task-9).
    grouped = seam_review_groups(wave, {})
    assert sorted(grouped["batched"]) == sorted(wave) and grouped["individual"] == [], grouped

    flagged_verdict = {
        "verdict": "changes",
        "findings": [
            {"task": f, "severity": "blocking", "file": "x.py", "issue": f"wave finding on {f}", "fix": f"fix {f}"}
            for f in flagged
        ],
    }
    first_answer = flagged_verdict if flagged else "approve"
    reviews = {t: [first_answer, "approve"] for t in wave}
    cfg = wave_project(ids, {}, reviews)
    out = bridge.run(cfg["project"], cfg["args"], slots=max(slots, len(ids)))
    result = out["result"]
    assert result.get("halted") is False, result.get("reason")
    assert set(result["merged"]) == set(ids), (result["merged"], ids)

    # The wave merged together and gets exactly one batched review (fails
    # pre-Task-18: reviews are one-per-task, no review-wave label exists).
    assert merged_waves(out["events"], ids)[-1] == sorted(wave), merged_waves(out["events"], ids)
    assert len(batches_covering(out, wave)) == 1, batch_review_labels(out["events"])

    covering = {n: [l for l in review_events(out["events"]) if n in covers_of(out, l)] for n in wave}
    for n in wave:
        if n in flagged:
            assert any(re.match(rf"^task {n}( \[test\])? revise", e) for e in out["events"]), (n, "the flagged task never revised")
            assert len(covering[n]) == 2, (n, covering[n], "only the flagged task is re-reviewed")
        else:
            assert not any(re.match(rf"^task {n}( \[test\])? revise", e) for e in out["events"]), (n, "an unflagged task was revised")
            assert len(covering[n]) == 1, (n, covering[n], "an unflagged task got an extra review agent")

    rows = {t["task"]: t for t in result.get("tasks", [])}
    assert set(rows) >= set(wave), rows.keys()
    for n in wave:
        assert rows[n].get("review", {}).get("verdict") == "approve", (n, rows[n].get("review"))

