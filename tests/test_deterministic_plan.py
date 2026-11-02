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
    "P-8": "test_no_plan_between_finish_and_start",
    "P-14": "test_freed_slot_and_idle_arming",
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
    # Permanent gate failure is heritage behavior (fail task, block dependents);
    # P-2 is about planning survival, so bound failUntil to the harness retry
    # budget (initial + 1 retry, no escalation): transient failures only.
    sc["project"]["failUntil"] = {t: min(v, 1) for t, v in sc["project"].get("failUntil", {}).items()}
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
        # Every trigger records an entry, even when nothing was schedulable
        # (ready == []); the trigger-in-trace checks above are the anti-spurious
        # guard, not non-emptiness (1.3: count equals triggering attempts).
        assert isinstance(entry.get("ready"), list), entry


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
    # Seeded BUSY failures model the explicit failed set the expected sets check;
    # failUntil stays transient (initial + 1 retry, no escalation) so it cannot
    # create unseeded failures outside that model.
    sc["project"]["failUntil"] = {t: min(v, 1) for t, v in sc["project"].get("failUntil", {}).items()}
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
    # Bridge arms assert outcome complete, so bound failUntil to the harness retry
    # budget (initial + 1 retry, no escalation): transient failures only.
    sc["project"]["failUntil"] = {t: min(v, 1) for t, v in sc["project"].get("failUntil", {}).items()}
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
        # Single-task dep-less graphs can never yield a schedulable fallback window
        # after adoption (the only task is always done-or-running at any fallback),
        # so the arm's entry/degraded asserts are vacuous there; multi-task draws
        # cover the lastGood-layering contract.
        assume(len(ids) > 1)
        sc["project"]["planSeq"] = [{}, {"ready": [absent]}, {"ready": [absent]}]
        out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
        result = out["result"]
        assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
        assert result.get("degraded") is True, result.get("reason")
        entries = fallback_entries(result)
        assert entries, "a twice-failed plan with last-good data must record a fallback"
        for entry in entries:
            if entry.get("ready"):
                assert entry.get("source") == "last-good", entry
                assert set(entry["ready"]) <= set(last_good), (entry, last_good)
                assert absent not in entry["ready"], entry
            else:
                assert entry.get("ready") == [], entry
                assert entry.get("source") is None, entry
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
    assert bridge.ready(padded, done=["1"]) == ["3"]  # done tasks are never ready; 3's dep is met, 2's "02" is unknown
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
    assert set(fb["ready"]) == {"4"} and fb["source"] == "deps", fb  # 'a' is done: done tasks are never ready
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


# ------------------------------------------------- P-8 / P-14 (Story 3 group)
#
# Plan-agent schedule properties over the `planDecision` decision seam
# (design.md: planDecision({ trigger, scriptReadiness, planInFlight, idleArmed,
# freeSlot, readyCount, openCount, stopLaunching }) -> 'run'|'coalesce'|'none')
# plus the real script's trace through the bridge with healthy plans.
# P-8 (3.1, 3.3): with healthy plans, no non-retry `plan` occurs between a task
# finish and the next `task N` request when the deterministic ready list is
# non-empty; plan requests are only start / first-after-landing / idle.
# P-14 (3.2, 3.4): a freed slot with non-empty ready starts the next ready
# task(s) before any `plan`; an idle re-read that finds nothing ready arms no
# second re-read until a landing end or task record. Both tighten the
# landing-speed P-6 relation. Before Task 14 the freed/finished gating must
# come from the seam: a `planDecision` that returns `run` for freed/finished
# with script readiness fails the seam tables, and a script that plans after
# every freed slot fails the trace predicates for the right reason (extra plan
# agents between finish and ready start). Imports of project code stay inside
# the loader subprocess calls so the module always collects.
# ------------------------------------------------- red-team: plan-agent thrash (3.3)
#
# Requirement 3.3: no plan agent after every freed implementing slot when the
# deterministic ready list already determines what starts next. Each test below
# is an adversarial timing/slot shape where a per-freed-slot re-reader would
# smuggle an extra `plan` between a finish and the next `task N` request:
# rapid successive finishes with ready always non-empty, a freed slot racing
# a landing end, empty-then-instantly-ready transitions, and dep-less-shaped
# `deps` (`null`/`[]`/string) smuggled into a deps run. All run the real
# script through the bridge with healthy plans and script-side readiness on
# (Task 14's `planDecision` gating); the failure mode is extra plan agents
# between finish and ready start.

PLAN_DECISION_CALLER = (
    f"import {{ loadDecisions }} from {LOAD_DECISIONS_URL!r};"
    "const input = JSON.parse(process.argv[1]);"
    "const { planDecision } = loadDecisions();"
    "console.log(JSON.stringify({ decision: planDecision(input) }));"
)


def seam_plan_decision(trigger: str, script_readiness: bool = True, plan_in_flight: bool = False,
                       idle_armed: bool = True, free_slot: bool = True, ready_count: int = 0,
                       open_count: int = 1, stop_launching: bool = False) -> str:
    """Call the `planDecision` decision seam (exists today; Task 14 owns the gating)."""
    payload = json.dumps({
        "trigger": trigger,
        "scriptReadiness": script_readiness,
        "planInFlight": plan_in_flight,
        "idleArmed": idle_armed,
        "freeSlot": free_slot,
        "readyCount": ready_count,
        "openCount": open_count,
        "stopLaunching": stop_launching,
    })
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", PLAN_DECISION_CALLER, payload],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"planDecision seam call failed: {proc.stderr.strip()}"
    return json.loads(proc.stdout)["decision"]


# ------------------------------------------------- P-8 / P-14 schedule helpers (Task 11)
def plan_schedule_violations(trace, ready_at) -> List[str]:
    """Check the Story-3 schedule relations on one bridge trace.

    `ready_at(i)` returns the deterministic ready list just after event `i`
    (via the `readyTasks` seam through `bridge.ready`). A violation is:
    - a non-retry plan that is neither the start plan, the first plan after a
      land end, nor a rearmed idle plan (rearm = a landing end or an
      implementer end since the previous idle plan);
    - a non-retry plan between an implementer end and the next first-task
      request while the ready list at the finish is non-empty and no land end
      intervenes (the freed slot must start ready tasks directly, 3.2/3.3).
    """
    violations = []
    idle_seen = False
    rearmed = False
    land_ended = False
    seen_plan = False
    for i, e in enumerate(trace):
        label = e["label"]
        if e["ev"] == "end" and label.startswith("land "):
            land_ended = True
            rearmed = True
        elif e["ev"] == "end" and label.startswith("task "):
            rearmed = True
        elif e["ev"] == "request" and PLAN_LABEL.match(label) and not PLAN_LABEL.match(label).group(2):
            if not seen_plan:
                seen_plan = True  # the start plan
            elif land_ended:
                pass  # the first plan after a landing
            else:
                if idle_seen and not rearmed:
                    violations.append(f"idle {label} at event {i} without a landing or task end since the previous idle plan")
                idle_seen = True
                rearmed = False
            land_ended = False
    for i, e in enumerate(trace):
        if e["ev"] != "end" or not e["label"].startswith("task "):
            continue
        if not ready_at(i):
            continue  # empty ready at the finish: a re-read may legitimately run
        for j in range(i + 1, len(trace)):
            f = trace[j]
            if f["ev"] != "request" or not TASK_LABEL.match(f["label"]):
                continue
            segment = trace[i + 1:j]
            plans_between = [g["label"] for g in segment
                             if g["ev"] == "request" and PLAN_LABEL.match(g["label"])
                             and not PLAN_LABEL.match(g["label"]).group(2)]
            lands_between = any(g["ev"] == "end" and g["label"].startswith("land ") for g in segment)
            if plans_between and not lands_between:
                violations.append(
                    f"plan {plans_between} between finish of {e['label']} (event {i}) and "
                    f"next {f['label']} (event {j}) with ready non-empty and no landing between")
            break
    return violations


def schedule_outcome_violations(bridge, sc: Dict, out: Dict) -> List[str]:
    """P-8/P-14 trace predicates with the ready oracle threaded through."""
    deps = sc["project"]["deps"]
    order = sc["args"]["remaining"]
    trace = out["trace"]
    done: set = set()
    requested: List[str] = []
    ready_cache: Dict[int, List[str]] = {}
    for i, e in enumerate(trace):
        if e["ev"] == "end" and e["label"].startswith("land "):
            done |= set(e["label"][len("land "):].split())
        elif e["ev"] == "request" and TASK_LABEL.match(e["label"]):
            requested.append(TASK_LABEL.match(e["label"]).group(1))
        ready_cache[i] = bridge.ready(deps, done=sorted(done), exclude=list(requested), order=order)
    return plan_schedule_violations(trace, ready_cache.__getitem__)


def prepare_schedule_sc(draw, plan_seq: str = "consistent") -> Dict:
    """A healthy-plan scenarios() draw with script-side readiness on (args.deps/
    remaining from the graph), the maxConcurrent 1-4 sweep, freed-slot timing
    (1 ms plans plus one slow task), and no relink/checkRed so the ready oracle
    sees the run's own deps and every landing merges."""
    sc = prepare_retry_sc(draw(scenarios(plan_seq=plan_seq), label="scenario"))
    sc["args"]["maxConcurrent"] = draw(st.sampled_from([1, 2, 3, 4]), label="maxConcurrent")
    sc["slots"] = draw(st.sampled_from([1, 2, 3, 4, 5, 6, None]), label="slots")
    sc["project"].pop("relink", None)
    sc["project"]["checkRed"] = []
    ids = list(sc["project"]["deps"])
    slow = draw(st.sampled_from(ids), label="slow-task")
    sc["project"]["durations"][slow] = draw(st.integers(100, 300), label="slow-ms")
    return sc


# --------------------------------------------------------------------------- P-8


@BRIDGE_SETTINGS
@given(st.data())
def test_no_plan_between_finish_and_start(bridge, data):  # noqa: F811
    """P-8 (3.1, 3.3): healthy plans, any scenario -> no non-retry `plan`
    between a task finish and the next `task N` when deterministic ready is
    non-empty; plans only start / first-after-landing / idle.

    Generators: scenarios() with args.deps, healthy plans, maxConcurrent 1-4
    sweep, slow-task freed-slot timing, chains/diamonds for empty-ready idle
    draws. Metamorphic: the same scenario with uniform fast timing keeps the
    schedule. Negatives/complements at the seam: start/landing run, dep-less
    freed/finished run (3.6), unknown triggers never run."""
    sc = prepare_schedule_sc(data.draw, plan_seq="consistent")
    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    assert schedule_outcome_violations(bridge, sc, out) == [], out["result"].get("reason")

    # The seam agrees: freed/finished never run a plan when the script computes
    # readiness (3.2, 3.3); start and landing always run (3.1).
    assert seam_plan_decision("freed") == "none"
    assert seam_plan_decision("finished") == "none"
    assert seam_plan_decision("start") == "run"
    assert seam_plan_decision("landing") == "run"
    # Negatives: without dep data the script must plan per freed slot (3.6);
    # unknown triggers and stopped launches never run.
    assert seam_plan_decision("freed", script_readiness=False) == "run"
    assert seam_plan_decision("finished", script_readiness=False) == "run"
    assert seam_plan_decision("bogus") == "none"
    assert seam_plan_decision("landing", stop_launching=True) == "none"
    assert seam_plan_decision("start", plan_in_flight=True) == "coalesce"

    # Metamorphic: uniform fast timing keeps the same schedule relations.
    fast = copy.deepcopy(sc)
    for t in fast["project"]["durations"]:
        fast["project"]["durations"][t] = 2
    out2 = bridge.run(fast["project"], fast["args"], slots=fast["slots"])
    assert schedule_outcome_violations(bridge, fast, out2) == [], out2["result"].get("reason")


# --------------------------------------------------------------------------- P-14


@BRIDGE_SETTINGS
@given(st.data())
def test_freed_slot_and_idle_arming(bridge, data):  # noqa: F811
    """P-14 (3.2, 3.4): a freed slot with non-empty deterministic ready starts
    the next ready task(s) before any `plan`; an idle re-read that finds
    nothing ready arms no second re-read until a landing end or task record.

    Generators: same freed-slot draws as P-8 plus the idle truth table at the
    seam. Complement (3.6): a fixed dep-less chain still plans per freed slot.
    """
    # Seam first: the idle gate is exact (fails if Task 14 wired it loosely).
    assert seam_plan_decision("idle") == "run"  # freeSlot + ready 0 + open + armed
    assert seam_plan_decision("idle", idle_armed=False) == "none"  # no second re-read until re-armed
    assert seam_plan_decision("idle", ready_count=2) == "none"  # ready non-empty: start tasks, no plan
    assert seam_plan_decision("idle", open_count=0) == "none"  # nothing open: no re-read
    assert seam_plan_decision("idle", free_slot=False) == "none"
    assert seam_plan_decision("idle", plan_in_flight=True) == "coalesce"
    assert seam_plan_decision("idle", stop_launching=True) == "none"

    sc = prepare_schedule_sc(data.draw, plan_seq="consistent")
    out = bridge.run(sc["project"], sc["args"], slots=sc["slots"])
    assert schedule_outcome_violations(bridge, sc, out) == [], out["result"].get("reason")

    # Complement (3.6): without dep data the script plans per freed slot, so a
    # fixed dep-less chain shows a non-retry plan after the first task request.
    chain = {"1": [], "2": ["1"], "3": ["2"]}
    project = {
        "deps": dict(chain),
        "durations": {t: 5 for t in chain},
        "planDurations": dict(PLAN_MS_ALL),
    }
    out = bridge.run(project, {"maxConcurrent": 1}, slots=2)
    assert out["result"].get("outcome") == "complete", out["result"].get("reason")
    requests = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    first = next(i for i, label in enumerate(requests) if TASK_LABEL.match(label))
    assert any(PLAN_LABEL.match(label) and not PLAN_LABEL.match(label).group(2)
               for label in requests[first + 1:]), requests


# ------------------------------------------------- red-team thrash helpers (Task 13)
THRASH_PLAN = re.compile(r"^plan \d+$")
THRASH_TASK_END = re.compile(r"^task \d+ end$")


def thrash_plans(events) -> List[str]:
    """Non-retry `plan N` request labels in event order."""
    return [e for e in events if THRASH_PLAN.match(e)]


def thrash_plan_any(events) -> List[str]:
    """Every `plan ...` request label (including retries) in event order."""
    return [e for e in events if e.startswith("plan ")]


def thrash_land_reqs(events) -> List[str]:
    return [e for e in events if e.startswith("land ") and not e.endswith(" end")]


def thrash_land_ends(events) -> List[str]:
    return [e for e in events if e.startswith("land ") and e.endswith(" end")]


def thrash_idle_plans(logs) -> List[str]:
    """Idle-triggered plan labels from the script logs (`plan N (idle):`)."""
    return sorted({f"plan {m}" for m in re.findall(r"plan (\d+) \(idle\)", "\n".join(logs))},
                  key=lambda label: int(label.split()[1]))


def assert_no_plan_between_finish_and_start(events, label: str) -> int:
    """No freed-slot-caused `plan` between any implementer finish and the next
    `task N` request (the 3.3 thrash predicate, TC-13 shape). Returns the pair count.

    A plan in the segment is a violation ONLY when no `land end` also falls in
    that segment (P-8's lands_between exemption, cf. plan_schedule_violations):
    3.3 forbids freed-slot-caused plans, not 3.1-mandated first-after-landing
    plans, which positionally interleave between a finish and a later task start
    under the harness FIFO runtime queue (a landing plan requested at a land end
    starts at/after the next finish when slots are full). The strict form
    contradicts 3.1/box-1 and the landed regression suites (finish_first
    TC-23/TC-28/TC-29, landing_speed TC-8/TC-10/TC-62)."""
    pairs = 0
    for i, e in enumerate(events):
        if not THRASH_TASK_END.match(e):
            continue
        nxt = next((j for j in range(i + 1, len(events)) if TASK_LABEL.match(events[j])), None)
        if nxt is None:
            continue  # last finishes have no next task start
        pairs += 1
        between = events[i + 1:nxt]
        plans_between = [x for x in between if THRASH_PLAN.match(x)]
        lands_between = any(x.startswith("land ") and x.endswith(" end") for x in between)
        assert not (plans_between and not lands_between), (
            label, f"plan smuggled between {e} and {events[nxt]}", between)
    return pairs


def assert_thrash_triggers(events, logs, label: str) -> None:
    """Every non-retry plan is the start plan, the first plan after a landing
    completed, or an idle re-read (the 3.1 trigger contract, TC-11 shape)."""
    idle = set(thrash_idle_plans(logs))
    positions = [(e, i) for i, e in enumerate(events) if THRASH_PLAN.match(e)]
    assert positions, f"{label}: at least one plan agent ran"
    assert positions[0][0] == "plan 1", (label, positions)
    for k in range(1, len(positions)):
        cur, at = positions[k]
        _, prev_at = positions[k - 1]
        if cur in idle:
            continue
        between = events[prev_at + 1:at]
        assert any(x.startswith("land ") and x.endswith(" end") for x in between), (
            label, f"{cur} is not the first plan after a landing", between)


def assert_thrash_plan_bound(events, logs, label: str) -> None:
    """Plan count stays within 1 start + landings + idle (the 3.3 count bound)."""
    plans = thrash_plans(events)
    bound = 1 + len(thrash_land_reqs(events)) + len(thrash_idle_plans(logs))
    assert len(plans) <= bound, (label, plans, bound)


def test_redteam_plan_thrash_rapid_successive_finishes(bridge):  # noqa: F811
    """3.3 red-team: rapid successive finishes with ready always non-empty.

    Six independent tasks with staggered durations cycle two slots, so every
    finish lands while ready tasks are outstanding. A per-freed-slot re-reader
    would smuggle a plan between each finish and the next start; the gated
    scheduler starts the next ready task(s) directly.
    """
    assert seam_plan_decision("freed", True, free_slot=True, ready_count=2, open_count=4) == "none"
    assert seam_plan_decision("finished", True, free_slot=True, ready_count=2, open_count=4) == "none"
    deps = {"1": [], "2": [], "3": [], "4": [], "5": [], "6": []}
    cfg = redteam_project(deps, [{}])
    cfg["project"]["durations"] = {"1": 10, "2": 20, "3": 30, "4": 40, "5": 50, "6": 60}
    cfg["args"]["maxConcurrent"] = 2
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert set(result["merged"]) == set(deps), result["merged"]
    assert not [label for label in thrash_plan_any(out["events"]) if label.endswith(" retry")], out["events"]
    requested = task_requests(out["trace"])
    for n in deps:
        assert requested.count(n) == 1, (n, requested)
    pairs = assert_no_plan_between_finish_and_start(out["events"], "rapid-finishes")
    assert pairs >= 3, (pairs, out["events"])  # the finishes really did queue up behind ready work
    assert_thrash_triggers(out["events"], out["logs"], "rapid-finishes")
    assert_thrash_plan_bound(out["events"], out["logs"], "rapid-finishes")


def test_redteam_plan_thrash_freed_slot_races_landing_end(bridge):  # noqa: F811
    """3.3 red-team: a freed slot racing a landing end.

    Task 1 is slow to implement and slower to land while tasks 2-4 cycle the
    other slot, so implementer finishes land mid-landing. Even with a
    `plan` in flight or a landing completing underneath, no plan may appear
    between a finish and the next task start the deterministic list already
    determined.
    """
    assert seam_plan_decision("freed", True, plan_in_flight=True, free_slot=True,
                              ready_count=1, open_count=2) == "none"
    assert seam_plan_decision("finished", True, free_slot=True, ready_count=1, open_count=2) == "none"
    deps = {"1": [], "2": [], "3": [], "4": []}
    cfg = redteam_project(deps, [{}])
    cfg["project"]["durations"] = {"1": 100, "2": 10, "3": 10, "4": 10}
    # Task 2 lands slowly (150 ms) so its land window (requested right after its
    # finish) still spans the later finishes and task 1's landing: several
    # implementer finishes deterministically fall between a land request and
    # the next land end on the virtual clock.
    cfg["project"]["mergeDurations"] = {"1": 150, "2": 150}
    cfg["args"]["maxConcurrent"] = 2
    cfg["args"]["mergeBatch"] = 1
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert set(result["merged"]) == set(deps), result["merged"]
    assert not [label for label in thrash_plan_any(out["events"]) if label.endswith(" retry")], out["events"]
    events = out["events"]
    # The race really happened: some implementer finish falls between a land
    # request and the next land end (landings are one at a time per target).
    land_req = [i for i, e in enumerate(events) if e.startswith("land ") and not e.endswith(" end")]
    land_end = [i for i, e in enumerate(events) if e.startswith("land ") and e.endswith(" end")]
    assert land_req and land_end, events
    raced = False
    for r in land_req:
        following = [j for j in land_end if j > r]
        if not following:
            continue
        if any(THRASH_TASK_END.match(events[k]) for k in range(r + 1, following[0])):
            raced = True
            break
    assert raced, ("no finish raced a landing end; retune the timing", events)
    assert_no_plan_between_finish_and_start(events, "race-landing")
    assert_thrash_triggers(events, out["logs"], "race-landing")
    assert_thrash_plan_bound(events, out["logs"], "race-landing")


def test_redteam_plan_thrash_empty_then_instantly_ready(bridge):  # noqa: F811
    """3.3 red-team: empty-then-instantly-ready transitions.

    A two-task chain on one slot: while task 1 lands, nothing is ready and
    nothing runs, so exactly one idle re-read fires and disarms; when the
    landing completes, task 2 becomes ready instantly. The landing trigger
    covers the re-read -- no second idle plan and no freed-slot plan may
    appear between the record arriving and task 2 starting.
    """
    assert seam_plan_decision("idle", True, free_slot=True, ready_count=0,
                              open_count=1, idle_armed=True) == "run"
    assert seam_plan_decision("idle", True, free_slot=True, ready_count=0,
                              open_count=1, idle_armed=False) == "none"
    assert seam_plan_decision("freed", True, free_slot=True, ready_count=1, open_count=1) == "none"
    deps = {"1": [], "2": ["1"]}
    cfg = redteam_project(deps, [{}])
    cfg["args"]["maxConcurrent"] = 1
    # Two runtime slots (one implementer slot): the idle re-read must overlap
    # land 1 in flight, which needs a second runtime slot — slots=1 holds the
    # only slot in land 1 and no idle plan could ever start before land 1 end.
    out = bridge.run(cfg["project"], cfg["args"], slots=2)
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert set(result["merged"]) == set(deps), result["merged"]
    events, logs = out["events"], out["logs"]
    idle = thrash_idle_plans(logs)
    assert idle == ["plan 2"], (idle, events)  # exactly one idle re-read per arming
    assert events.index("plan 2") < events.index("land 1 end"), (
        "the idle re-read ran while the landing was still in flight", events)
    # No second idle plan once the landing completed and task 2 turned ready.
    assert [p for p in idle if events.index(p) > events.index("land 1 end")] == [], (idle, events)
    # The freed slot went straight to landing with no plan smuggled in.
    between = events[events.index("task 1 end") + 1:events.index("land 1")]
    assert not any(x.startswith("plan ") for x in between), ("plan between finish and land", between)
    # After the landing, only the landing-triggered re-read precedes task 2.
    after = events[events.index("land 1 end") + 1:events.index("task 2")]
    assert not [x for x in after if x in idle], ("second idle plan before task 2", after)
    assert_thrash_triggers(events, logs, "empty-then-ready")
    assert_thrash_plan_bound(events, logs, "empty-then-ready")


def test_redteam_plan_thrash_depless_shaped_deps(bridge):  # noqa: F811
    """3.3 red-team: dep-less-shaped `deps` (`null`/`[]`/string) smuggled into a deps run.

    The system's own readiness flag fed a near-miss: `SCRIPT_READINESS` is only
    true for a plain-object `deps` map, so `null`, an array, and a string are
    all dep-less runs (3.6). Each must complete AND run a plan agent after
    freed slots -- the opposite of the 3.3 suppression. A truthiness-only
    readiness check would treat `[]`/`"1"` as deps-mode and skip those plans,
    stalling or mis-scheduling the run.
    """
    assert seam_plan_decision("freed", False, free_slot=True, ready_count=2, open_count=2) == "run"
    assert seam_plan_decision("finished", False, free_slot=True, ready_count=1, open_count=1) == "run"
    deps = {"1": [], "2": [], "3": []}
    for shape in [None, [], "1"]:
        cfg = redteam_project(deps, [{}])
        cfg["args"]["deps"] = shape
        cfg["args"]["maxConcurrent"] = 1
        out = bridge.run(cfg["project"], cfg["args"], slots=1)
        result = out["result"]
        assert result.get("outcome") == "complete", (shape, result.get("outcome"), result.get("reason"))
        assert set(result["merged"]) == set(deps), (shape, result["merged"])
        events = out["events"]
        for n in ["1", "2", "3"]:
            assert events.count(f"task {n}") == 1, (shape, n, events)
        # Dep-less mode re-plans per freed slot (3.6): a plan runs between each
        # finish and the next start. Missing it means the shape leaked into 3.3.
        for prev, nxt in [("1", "2"), ("2", "3")]:
            between = events[events.index(f"task {prev} end") + 1:events.index(f"task {nxt}")]
            assert any(THRASH_PLAN.match(x) for x in between), (
                shape, f"no plan between task {prev} end and task {nxt} (dep-less shape treated as deps-mode)", between)


# --------------------------------------------------------------------------- TC-35 (Task 32)
#
# Seam-called-by-real-code proofs (requirements 1.2, 4.1): on a fallback run
# the script's per-request scheduling agrees with `fallbackReady(...)` on the
# same inputs, and each batched review's coverage agrees with the
# `reviewGroups` seam. (TC-8/TC-29/TC-30 contract bodies live in Tasks 2/16;
# this task only proves the seams are actually called.) Fail until Tasks
# 10+18: pre-Task-10 an always-inconsistent run halts with "planning step
# failed" instead of continuing degraded; pre-Task-18 the script runs one
# `review N` per task with no `review-wave` label. Project seams are reached
# only through the Node loader / bridge subprocess calls, so collection never
# depends on the missing code.

TASK_REQ = re.compile(r"^task (\d+)$")
TASK_END = re.compile(r"^task (\d+) end$")


def test_seam_usage_fallback_scheduling(bridge):  # noqa: F811
    """TC-35 fallback half (1.2): the script's per-request scheduling agrees
    with `fallbackReady(...)` on the same inputs.

    Fixed diamond graph, every plan answer inconsistent (`badReady`, the
    scenarios() always-bad shape): the start fallback sees empty done/exclude,
    so the seam on those exact inputs must equal the deterministic oracle with
    source `deps`; every later `task N` request must be a member of the seam
    recomputed with done = tasks ended before the request; the run completes
    degraded with the full merged set.
    """
    deps = {"1": [], "2": ["1"], "3": ["1"], "4": ["2", "3"]}
    project = {
        "deps": {t: list(ds) for t, ds in deps.items()},
        "durations": {t: 2 for t in deps},
        "planDurations": dict(PLAN_MS_ALL),
        "badReady": ["999"],
    }
    args = {
        "deps": {t: list(ds) for t, ds in deps.items()},
        "remaining": list(deps),
        "maxConcurrent": 2,
    }

    # Seam agreement on the exact inputs the start fallback uses (empty
    # done/exclude: nothing runs or merges before plan 1 fails twice).
    oracle = bridge.ready(deps)
    assert oracle == ["1"], oracle
    start = seam_fallback(True, oracle, [], [], [])
    assert start["ready"] == oracle and start["source"] == "deps", start

    out = bridge.run(project, args, slots=2)
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert result.get("degraded") is True, (result.get("degraded"), result.get("reason"))
    assert set(result["merged"]) == set(deps), (result["merged"], deps)

    # Per-request agreement: each requested task is in the seam's ready list
    # for done = tasks ended before that request (observable completions).
    order = list(deps)
    done: List[str] = []
    scheduled: List[str] = []
    for e in out["events"]:
        end = TASK_END.match(e)
        if end:
            done.append(end.group(1))
            continue
        req = TASK_REQ.match(e)
        if req:
            fb = fallback_via_seam(order, deps, done, [])
            assert req.group(1) in fb["ready"], (req.group(1), fb, done)
            scheduled.append(req.group(1))
    assert scheduled, "fallback run scheduled no tasks"
    assert set(scheduled) == set(deps), (scheduled, deps)


def test_seam_usage_review_coverage(bridge):  # noqa: F811
    """TC-35 review half (4.1): each batched review's coverage agrees with the
    `reviewGroups` seam on the merged wave.

    Fixed two-wave run (task 1 lands alone, the rest merge as one wave) with
    one safety task: the non-risky tasks of each merged wave must be covered
    by exactly the seam's `batched` set, and every risky task must hold its own
    individual review matching the seam's `individual` set.
    """
    ids = ["1", "2", "3", "4"]
    risk = {"1": "normal", "2": "normal", "3": "safety", "4": "normal"}
    cfg = wave_project(ids, risk, {})
    out = bridge.run(cfg["project"], cfg["args"], slots=max(4, len(ids)))
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    merged = [str(t) for t in result["merged"]]
    assert set(merged) == set(ids), (merged, ids)

    batches = batch_review_labels(out["events"])
    assert batches, "no batched review-wave ran (one-review-per-task behavior)"
    waves = merged_waves(out["events"], merged)
    assert waves, out["events"]
    for w in waves:
        seam = seam_review_groups(w, risk)
        plain = [t for t in w if str(risk.get(t, "normal")).lower() != "safety"]
        risky = [t for t in w if str(risk.get(t, "normal")).lower() == "safety"]
        assert sorted(seam["batched"]) == sorted(plain), (w, seam)
        assert sorted(seam["individual"]) == sorted(risky), (w, seam)
        if plain:
            touched = [label for label in batches if set(covers_of(out, label)) & set(w)]
            assert len(touched) == 1, (w, touched)
            assert sorted(covers_of(out, touched[0])) == sorted(seam["batched"]), (
                w, covers_of(out, touched[0]), seam)
        for r in risky:
            covered_by = [label for label in individual_review_labels(out["events"])
                          if r in covers_of(out, label)]
            assert covered_by, (r, "risky task has no individual review")


# ------------------------------------------------- red-team: review batching (4.3/4.6)
#
# Requirement 4.3: a risky-only wave SHALL NOT run a combined review. Each
# test below is an adversarial shape where a wave-batching implementation
# could smuggle in a spurious `review-wave` label: every task risky, risk
# tags in near-miss surface forms, an empty merged wave, a single-task wave.
# Requirement 4.6: no wave merged since the last review SHALL NOT run an
# empty batched review.
#
# All tests run the real script through the bridge plus the `reviewGroups`
# seam (Task 9, present). Before Task 18 the script runs one `review N` per
# task with no `review-wave` label, so every test with a batched-review
# assertion fails for the right reason (missing batch labels); the
# all-risky bridge shape passes pre-18 (nothing to batch yet) and guards the
# Task 18 code against adding a combined review to risky-only waves.


def test_redteam_review_batching_all_risky_no_batch(bridge):  # noqa: F811
    """4.3 red-team: an all-risky wave runs only individual reviews, no batch.

    The whole later wave is safety-tagged. A batcher keyed on "wave merged,
    so batch it" without consulting the risk split would emit one combined
    review over the risky tasks; the contract says risky-only waves get
    individuals only and no combined review.
    """
    ids = ["1", "2", "3", "4"]
    wave = ids[1:]
    risk = {t: "safety" for t in ids}
    # Seam first: the split is exact (fails pre-Task-9 with the missing-seam
    # message, the right reason).
    out = seam_review_groups(wave, risk)
    assert out["individual"] == wave, out
    assert out["batched"] == [], out

    cfg = wave_project(ids, risk, {})
    out = bridge.run(cfg["project"], cfg["args"], slots=4)
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert set(result["merged"]) == set(ids), result["merged"]
    # No combined review anywhere: zero batch labels, and no batch covers a
    # risky task even as a subset.
    assert batch_review_labels(out["events"]) == [], batch_review_labels(out["events"])
    for label in review_events(out["events"]):
        assert covers_of(out, label), (label, "an empty review ran")
    for n in wave:
        assert any(l == f"review {n}" for l in individual_review_labels(out["events"])), (n, out["events"])


def test_redteam_review_batching_risk_tag_mutations(bridge):  # noqa: F811
    """4.2/4.3 red-team: near-miss risk-tag surface forms route exactly.

    `Safety`/`SAFETY` are the safety tag in different cases (the seam folds
    case, same as `isSafety`); `' safety '` carries whitespace, a missing key
    carries no tag, and `'urgent'` is an unknown value -- none of which equal
    `safety`, so the seam batches them. A trimmer would individual-review the
    whitespace task (an extra agent over the W+R bound); a script that ignores
    the seam would misroute either side.
    """
    wave = ["2", "3", "4", "5", "6"]
    risk = {"2": "Safety", "3": "SAFETY", "4": " safety ", "6": "urgent"}
    # Task 5 has no entry at all (missing/unknown risk value).
    grouped = seam_review_groups(wave, risk)
    assert grouped["individual"] == ["2", "3"], grouped
    assert grouped["batched"] == ["4", "5", "6"], grouped

    ids = ["1"] + wave
    full_risk = {"1": "normal", **risk}
    cfg = wave_project(ids, full_risk, {})
    del cfg["project"]["risk"]["5"]
    del cfg["args"]["risk"]["5"]
    out = bridge.run(cfg["project"], cfg["args"], slots=max(4, len(ids)))
    result = out["result"]
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert set(result["merged"]) == set(ids), result["merged"]
    # Case-folded safety tasks get individuals only; no batch covers them
    # (fails pre-Task-18: reviews are one-per-task, no batch labels exist).
    for n in ("2", "3"):
        assert any(l == f"review {n}" for l in individual_review_labels(out["events"])), (n, out["events"])
    assert not [l for l in batch_review_labels(out["events"])
                if set(covers_of(out, l)) & {"2", "3"}], batch_review_labels(out["events"])
    # Whitespace/missing/unknown tags batch with the wave's non-risky tasks.
    assert len(batches_covering(out, ["4", "5", "6"])) == 1, batch_review_labels(out["events"])
    assert not [l for l in individual_review_labels(out["events"])
                if l.split()[1] in ("4", "5", "6")], individual_review_labels(out["events"])


def test_redteam_review_batching_empty_wave_no_review(bridge):  # noqa: F811
    """4.6 red-team: an empty merged wave runs no (batched) review.

    Task 2 is protected, so it never merges: the "wave" of unmerged work is
    empty. A batcher that fires per landing or per wave slot instead of per
    merged set would emit an empty `review-wave` label or review the
    unmerged task; the contract says no review agent runs. The merged wave
    is still batched exactly once, and no already-reviewed task is
    re-covered by a stale second batch (fails pre-Task-18: no batch labels).
    """
    # Seam first: empty merged yields no review groups (the 4.6 unit oracle).
    empty = seam_review_groups([], {"1": "safety"})
    assert empty["batched"] == [] and empty["individual"] == [], empty

    deps = {"1": [], "2": []}
    project = {
        "deps": dict(deps),
        "durations": {"1": 10, "2": 50},
        "mergeDurations": {"1": 300},
        "planDurations": dict(PLAN_MS_ALL),
        "protectedTasks": ["2"],  # task 2 never merges: its wave stays empty
    }
    args = {
        "deps": dict(deps),
        "remaining": ["1", "2"],
        "maxConcurrent": 4,
        "agents": {"reviewer": dict(REVIEWER)},
    }
    out = bridge.run(project, args, slots=4)
    assert out["result"]["merged"] == ["1"], out["result"]
    # Nothing covers the unmerged task, and no review ran empty.
    covering2 = [l for l in review_events(out["events"]) if "2" in covers_of(out, l)]
    assert covering2 == [], (covering2, "an empty batched review ran for the unmerged task")
    for label in review_events(out["events"]):
        covered = covers_of(out, label)
        assert covered, (label, "an empty review ran")
        assert set(covered) <= {"1"}, (label, covered)
    # The merged wave is batched exactly once; it is never re-covered.
    assert len(batches_covering(out, ["1"])) == 1, batch_review_labels(out["events"])
    covering1 = [l for l in review_events(out["events"]) if "1" in covers_of(out, l)]
    assert len(covering1) == 1, (covering1, "the reviewed task was re-covered by a stale batch")


def test_redteam_review_batching_single_task_wave_flagged(bridge):  # noqa: F811
    """4.3/4.4 red-team: single-task waves, flagged and risky.

    A one-task non-risky wave whose batched verdict flags its only task must
    revise that task alone and re-review it (no wave-mates to touch, no extra
    agents beyond the re-review). A one-task risky wave must stay individual
    with no combined review. A batcher with a wave-size floor would skip the
    single-task batch; one without a risk check would batch the risky task.
    """
    # Part A: single non-risky task wave with a flagged batched verdict.
    ids = ["1", "2"]
    flagged_verdict = {
        "verdict": "changes",
        "findings": [{"task": "2", "severity": "blocking", "file": "x.py",
                      "issue": "single-task wave finding on 2", "fix": "fix 2"}],
    }
    # The batched verdict is looked up by the wave's first covered task.
    cfg = wave_project(ids, {}, {"2": [flagged_verdict, "approve"]})
    out = bridge.run(cfg["project"], cfg["args"], slots=4)
    result = out["result"]
    assert result.get("halted") is False, result.get("reason")
    assert set(result["merged"]) == set(ids), result["merged"]
    # The lone wave task gets exactly one batch (fails pre-Task-18: no
    # review-wave labels), then revises alone and is re-reviewed once.
    assert merged_waves(out["events"], ids)[-1] == ["2"], merged_waves(out["events"], ids)
    assert len(batches_covering(out, ["2"])) == 1, batch_review_labels(out["events"])
    assert any(re.match(r"^task 2( \[test\])? revise", e) for e in out["events"]), (
        "the flagged single-task wave never revised", out["events"])
    covering2 = [l for l in review_events(out["events"]) if "2" in covers_of(out, l)]
    assert len(covering2) == 2, (covering2, "the flagged task was not re-reviewed exactly once")
    # The untouched wave-mate approves with no revise and no extra agent.
    assert not any(re.match(r"^task 1( \[test\])? revise", e) for e in out["events"]), out["events"]
    covering1 = [l for l in review_events(out["events"]) if "1" in covers_of(out, l)]
    assert len(covering1) == 1, (covering1, "an unflagged task got an extra review agent")
    rows = {t["task"]: t for t in result.get("tasks", [])}
    assert rows["2"].get("review", {}).get("verdict") == "approve", rows["2"].get("review")

    # Part B: single risky-task wave stays individual with no combined review.
    out = seam_review_groups(["2"], {"2": "safety"})
    assert out["individual"] == ["2"] and out["batched"] == [], out
    cfg = wave_project(ids, {"2": "safety"}, {})
    out = bridge.run(cfg["project"], cfg["args"], slots=4)
    assert set(out["result"]["merged"]) == set(ids), out["result"]["merged"]
    assert not [l for l in batch_review_labels(out["events"]) if "2" in covers_of(out, l)], (
        batch_review_labels(out["events"]))
    assert any(l == "review 2" for l in individual_review_labels(out["events"])), out["events"]


