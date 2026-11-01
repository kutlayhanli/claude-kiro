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

import json
import random
import subprocess
from pathlib import Path
from typing import Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from landing_bridge import ck_plan, dags, git_repo, subsets, write_spec  # noqa: F401

PROPERTIES = {"P-4": "test_fallback_matches_ck_plan"}

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
