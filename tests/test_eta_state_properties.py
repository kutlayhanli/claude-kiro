"""P-5, P-6, P-7: the run-state decision seams of `claude_kiro.eta.decide` (holds and effective time).

Run state comes from the real `claude_kiro.progress.fold(events)` and the graph is a `claude_kiro.eta.history.Graph`
built from the `eta_strategies.graphs()` draw, with the cycles and unknown dependencies computed here by an
independent oracle (the design takes them from `ck plan --json`). The seams are called directly: no server, no CLI.
Project modules are imported inside the test bodies so the file always collects.
"""

import re
from typing import Any, Dict, List, Set, Tuple

from hypothesis import HealthCheck, given, settings, strategies as st

import eta_strategies as es

pytestmark = es.NO_NETWORK

PROPERTIES = {
    "P-5": "test_blocked_iff_structural_block",
    "P-6": "test_park_hold_names_parked_and_dependents",
    "P-7": "test_effective_now_frozen_during_open_wait",
}

SETTINGS = settings(derandomize=True, max_examples=200, database=None, deadline=None,
                    suppress_health_check=list(HealthCheck))


# --- oracle and builders ---------------------------------------------------------------------------------

def _reach(deps: Dict[str, List[str]], start: str) -> Set[str]:
    """Every id reachable from `start` through deps (not counting `start` unless it lies on a cycle)."""
    seen: Set[str] = set()
    stack = [d for d in deps.get(start, []) if d in deps]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(d for d in deps.get(cur, []) if d in deps)
    return seen


def structure(raw: Dict[str, Dict[str, Any]]):
    """(cycles, unknown_deps): cycles are groups of mutually reachable tasks; unknown_deps maps task -> unknown ids."""
    deps = {t: list(v["deps"]) for t, v in raw.items()}
    reach = {t: _reach(deps, t) for t in deps}
    cycles: List[Tuple[str, ...]] = []
    placed: Set[str] = set()
    for t in deps:
        if t in placed or t not in reach[t]:
            continue
        group = tuple(sorted((u for u in deps if u in reach[t] and t in reach[u]), key=int))
        placed.update(group)
        cycles.append(group)
    unknown = {t: tuple(d for d in deps[t] if d not in deps) for t in deps}
    return cycles, {t: u for t, u in unknown.items() if u}


def dependents_of(raw: Dict[str, Dict[str, Any]], parked: List[str]) -> Set[str]:
    deps = {t: list(v["deps"]) for t, v in raw.items()}
    out = set(parked)
    for t in deps:
        if out & _reach(deps, t):
            out.add(t)
    return out


def sanitize(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Keep only dependencies on earlier tasks: acyclic, no dangling ids, duplicates kept."""
    ids = list(raw)
    out = {}
    for i, t in enumerate(ids):
        out[t] = dict(raw[t], deps=[d for d in raw[t]["deps"] if d in ids[:i]])
    return out


def build_graph(raw: Dict[str, Dict[str, Any]]):
    from claude_kiro.eta.history import Graph

    ids = tuple(raw)
    cycles, unknown = structure(raw)
    return Graph(
        order=ids,
        deps={t: tuple(v["deps"]) for t, v in raw.items()},
        done=frozenset(),
        chain={t: 0 for t in ids},
        tracks={t: v["track"] for t, v in raw.items()},
        risk={t: v["risk"] for t, v in raw.items()},
        complexity={t: None for t in ids},
        sizes={t: v["files"] + v["criteria"] for t, v in raw.items()},
        critical=(),
        cycles=tuple(cycles),
        unknown_deps=unknown,
        slots=3,
        slots_source="default",
    )


def ev(clock: float, kind: str, **fields: Any) -> Dict[str, Any]:
    return {"schema": es.PROGRESS_SCHEMA, "at": es.iso(clock), "kind": kind, **fields}


def start_event(ids: List[str]) -> Dict[str, Any]:
    return ev(0, es.EV_START, run="r000001", runKind=es.RUN_KIND_IMPLEMENT, spec="demo", open=ids, total=len(ids))


def names(reason: str, ident: str) -> bool:
    return re.search(r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % re.escape(ident), reason or "") is not None


def fold(events: List[Dict[str, Any]]):
    from claude_kiro import progress

    return progress.fold(events)


@st.composite
def claim_and_hand_events(draw, ids: List[str]) -> List[Dict[str, Any]]:
    """Claims and hand landings on drawn tasks (never a park, a failure or a merge)."""
    out: List[Dict[str, Any]] = []
    clock = 0
    for _ in range(draw(st.integers(0, 6))):
        clock += draw(st.integers(0, 20))
        task = draw(st.sampled_from(ids))
        if draw(st.booleans()):
            out.append(ev(clock, es.EV_HAND_LANDING, task=task))
        else:
            out.append(ev(clock, draw(st.sampled_from(es.CLAIM_EVENTS)), task=task, attempt=1, model=es.MODEL_SONNET))
    return out


# --- P-5 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(data=st.data())
def test_blocked_iff_structural_block(data):
    """P-5: blocked iff a cycle or unknown dependency exists, naming it; a hand landing never makes it blocked."""
    from claude_kiro.eta import decide

    raw = data.draw(es.graphs())
    if data.draw(st.booleans()):
        raw = sanitize(raw)  # force the complement branch often: acyclic, nothing dangling
    ids = list(raw)
    state = fold([start_event(ids), *data.draw(claim_and_hand_events(ids))])
    hold = decide.classify_hold(build_graph(raw), state)

    cycles, unknown = structure(raw)
    if cycles or unknown:
        assert hold.kind == "blocked", (raw, hold)
        named_cycle = any(all(names(hold.reason, t) for t in group) for group in cycles)
        named_unknown = any(names(hold.reason, u) and names(hold.reason, t)
                            for t, us in unknown.items() for u in us)
        assert named_cycle or named_unknown, (raw, hold.reason)
        assert hold.reason_key
    else:
        assert hold.kind == "none", (raw, hold)
        assert not hold.held


@SETTINGS
@given(data=st.data())
def test_hand_landing_never_blocks_acyclic_run(data):
    """P-5 complement: only hand landings (every task, any number) on an acyclic graph give no hold."""
    from claude_kiro.eta import decide

    raw = sanitize(data.draw(es.graphs()))
    ids = list(raw)
    landings = [ev(i + 1, es.EV_HAND_LANDING, task=t) for i, t in enumerate(
        data.draw(st.lists(st.sampled_from(ids), min_size=1, max_size=len(ids) + 3)))]
    hold = decide.classify_hold(build_graph(raw), fold([start_event(ids), *landings]))
    assert hold.kind == "none", (raw, landings, hold)


# --- P-6 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(data=st.data())
def test_park_hold_names_parked_and_dependents(data):
    """P-6: a needs-input park is blocked, held is the parked tasks plus dependents, whole_run per open work."""
    from claude_kiro.eta import decide

    raw = sanitize(data.draw(es.graphs()))
    ids = list(raw)
    parked = data.draw(st.lists(st.sampled_from(ids), min_size=1, max_size=2, unique=True))
    park_events = [ev(10 + i, es.EV_PARK, task=t, parkKind=es.PARK_KIND_NEEDS_INPUT, parkedAt=es.iso(10 + i))
                   for i, t in enumerate(parked)]
    state = fold([start_event(ids), *park_events])
    hold = decide.classify_hold(build_graph(raw), state)

    held = dependents_of(raw, parked)
    assert hold.kind == "blocked", (raw, parked, hold)
    for t in parked:
        assert "task %s parked (needs input)" % t in hold.reason, (parked, hold.reason)
    assert set(hold.held) == held, (raw, parked, hold.held)
    assert hold.whole_run == (not (set(ids) - held)), (raw, parked, hold)
    assert hold.reason_key


# --- P-7 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(data=st.data(), w=es.times(), gap1=st.integers(0, 300), gap2=st.integers(1, 300),
       via_hand=st.booleans())
def test_effective_now_frozen_during_open_wait(data, w, gap1, gap2, via_hand):
    """P-7: while a wait (question or hand landing) is open, now_eff is the same at t1 < t2; waited is t - w."""
    from claude_kiro.eta import decide

    ids = ["1", "2", "3"]
    if via_hand:
        opener = ev(w, es.EV_HAND_LANDING, task=data.draw(st.sampled_from(ids)))
    else:
        opener = ev(w, es.EV_APPROVAL_START, id="ab12cd34", reason="question")
    state = fold([start_event(ids), opener])
    t1 = w + gap1
    t2 = t1 + gap2
    now1, waited1 = decide.effective_now(state, float(t1))
    now2, waited2 = decide.effective_now(state, float(t2))
    assert now1 == now2, (w, t1, t2, now1, now2)
    assert now1 == w
    assert int(waited1) == t1 - w and int(waited2) == t2 - w, (w, t1, t2, waited1, waited2)
