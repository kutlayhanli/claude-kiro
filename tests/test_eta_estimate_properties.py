"""Simulator properties P-1, P-2, P-3, P-4, P-10, P-12, P-13 of run-followups (test-plan ## Properties).

Pure seams only: `claude_kiro.eta.history.fit_model`, `claude_kiro.progress.fold` and
`claude_kiro.eta.simulate.{simulate_implement, simulate_create, conditional_sample}`, over history the real collector
wrote (`eta_helpers.history_records` of the pools). Project modules are imported inside the test bodies, so the file
collects before they exist. Test names carry an `eta_` prefix so they are not confused with the landing-speed P-ids.
"""

import copy
import functools
import json
import statistics
from typing import Any, Dict, List, Sequence

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import eta_helpers as eh
import eta_strategies as es

pytestmark = es.NO_NETWORK

PROPERTIES = {
    "P-1": "test_eta_p1_deterministic_and_permutation_invariant",
    "P-2": "test_eta_p2_quantiles_ordered_and_cost_positive",
    "P-3": "test_eta_p3_no_finish_before_now",
    "P-4": "test_eta_p4_done_absent_open_present_once",
    "P-10": "test_eta_p10_cost_null_iff_unpriced",
    "P-12": "test_eta_p12_phase_inclusion_rate",
    "P-13": "test_eta_p13_untimed_create_records_change_nothing",
}

_FLAGS = dict(derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
SIM = settings(max_examples=50, **_FLAGS)
PURE = settings(max_examples=200, **_FLAGS)

SEED = 20261030
N_IMPL = 100
N_CREATE = 200


# --------------------------------------------------------------------------- inputs


@functools.lru_cache(maxsize=None)
def _pool(name: str) -> tuple:
    return tuple(json.dumps(r) for r in eh.history_records(name))


def pool_records(name: str) -> List[dict]:
    return [json.loads(s) for s in _pool(name)]


@st.composite
def subset_of(draw, name: str, min_size: int = 0, max_size: int | None = None) -> List[dict]:
    """A subset (by index set) of the records the collector wrote for the pool, in an arbitrary drawn order."""
    size = len(_pool(name))
    idx = draw(st.sets(st.integers(0, size - 1), min_size=min_size, max_size=max_size))
    order = draw(st.permutations(sorted(idx)))
    return [json.loads(_pool(name)[i]) for i in order]


def _acyclic(deps: Dict[str, List[str]]) -> bool:
    state: Dict[str, int] = {}

    def visit(t: str) -> bool:
        if state.get(t) == 1:
            return False
        if state.get(t) == 2:
            return True
        state[t] = 1
        ok = all(visit(d) for d in deps.get(t, ()))
        state[t] = 2
        return ok

    return all(visit(t) for t in deps)


def build_graph(raw: Dict[str, Dict[str, Any]], done=frozenset(), slots: int = 2):
    """A `simulate.Graph` from the documented fields. Dangling dependency ids are dropped, duplicates are kept."""
    from claude_kiro.eta import history, simulate

    Graph = getattr(simulate, "Graph", None) or getattr(history, "Graph")

    order = tuple(t for t in raw if t not in done)
    deps = {t: tuple(d for d in raw[t]["deps"] if d in raw and d not in done) for t in order}
    dependents: Dict[str, List[str]] = {t: [] for t in order}
    for t, ds in deps.items():
        for d in ds:
            dependents[d].append(t)
    chain: Dict[str, int] = {}

    def depth(t: str) -> int:
        if t not in chain:
            chain[t] = 0
            chain[t] = max((1 + depth(c) for c in dependents[t]), default=0)
        return chain[t]

    for t in order:
        depth(t)
    return Graph(
        order=order, deps=deps, done=frozenset(done), chain=chain,
        tracks={t: raw[t]["track"] for t in order}, risk={t: raw[t]["risk"] for t in order},
        complexity={t: None for t in order}, sizes={t: raw[t]["files"] + raw[t]["criteria"] for t in order},
        critical=(), cycles=(), unknown_deps={}, slots=slots, slots_source="default",
    )


@st.composite
def acyclic_graphs(draw) -> Dict[str, Dict[str, Any]]:
    raw = draw(es.graphs(max_tasks=12))
    assume(_acyclic({t: [d for d in v["deps"] if d in raw] for t, v in raw.items()}))
    return raw


@st.composite
def implement_inputs(draw):
    """(raw graph, events, history records, slots, now_min) with acyclic graphs and a non-stale `now`."""
    from claude_kiro import progress

    raw = draw(acyclic_graphs())
    evs = draw(es.events(raw, max_events=25))
    recs = draw(subset_of(eh.POOL_IMPL))
    state = progress.fold(evs)
    last = state.last_event_min or 0.0
    now = last + draw(st.integers(0, 100))
    return raw, evs, recs, draw(st.integers(1, 4)), float(now)


def create_events(draw, started: int, finished: int) -> List[Dict[str, Any]]:
    titles = list(es.CREATE_PHASES)
    out = [{"schema": es.PROGRESS_SCHEMA, "at": es.iso(0), "kind": es.EV_START, "run": "c" + "0" * 5, "runKind": "create",
            "spec": "demo", "open": titles, "total": len(titles)}]
    clock = 0
    for i in range(started):
        clock += draw(st.integers(1, 30))
        out.append({"schema": es.PROGRESS_SCHEMA, "at": es.iso(clock), "kind": es.EV_PHASE_START, "phase": titles[i]})
        if i < finished:
            clock += draw(st.integers(1, 30))
            out.append({"schema": es.PROGRESS_SCHEMA, "at": es.iso(clock), "kind": es.EV_PHASE_END, "phase": titles[i]})
    return out


@st.composite
def create_inputs(draw):
    from claude_kiro import progress

    started = draw(st.integers(0, len(es.CREATE_PHASES)))
    finished = draw(st.integers(0, started))
    if finished == started and started == len(es.CREATE_PHASES):
        finished = started - 1
    evs = create_events(draw, started, finished)
    recs = draw(subset_of(eh.POOL_CREATE, min_size=1))
    state = progress.fold(evs)
    now = (state.last_event_min or 0.0) + draw(st.integers(0, 100))
    return evs, recs, float(now)


def fit(records: Sequence[dict]):
    from claude_kiro.eta import history

    return history.fit_model(list(records), None)


def run_implement(raw, evs, recs, slots, now, seed=SEED, done=frozenset(), n=N_IMPL):
    from claude_kiro import progress
    from claude_kiro.eta import simulate

    return simulate.simulate_implement(build_graph(raw, done, slots), fit(recs), progress.fold(evs),
                                       now_min=now, n=n, seed=seed)


def run_create(evs, recs, now, seed=SEED, n=N_CREATE):
    from claude_kiro import progress
    from claude_kiro.eta import simulate

    return simulate.simulate_create(fit(recs), progress.fold(evs), now_min=now, n=n, seed=seed)


def _ordered(q) -> bool:
    return all(q[i] <= q[i + 1] + 1e-9 for i in range(len(q) - 1))


# --------------------------------------------------------------------------- properties


@SIM
@given(implement_inputs(), st.integers(0, 2**31 - 1), st.data())
def test_eta_p1_deterministic_and_permutation_invariant(inp, seed, data):
    raw, evs, recs, slots, now = inp
    first = run_implement(raw, evs, recs, slots, now, seed)
    assert run_implement(raw, evs, recs, slots, now, seed) == first
    permuted = data.draw(st.permutations(recs))
    assert run_implement(raw, evs, list(permuted), slots, now, seed) == first


@SIM
@given(create_inputs(), st.integers(0, 2**31 - 1))
def test_eta_p1_create_deterministic_and_permutation_invariant(inp, seed):
    evs, recs, now = inp
    first = run_create(evs, recs, now, seed)
    assert run_create(evs, recs, now, seed) == first
    assert run_create(evs, list(reversed(recs)), now, seed) == first


def _check_result(res):
    if res.finish_min is not None:
        assert _ordered(res.finish_min)
    if res.remaining_min is not None:
        assert _ordered(res.remaining_min)
        assert all(v >= 0 for v in res.remaining_min)
    if res.usd is not None:
        assert res.usd[0] <= res.usd[1] + 1e-9
        assert res.usd[0] > 0 and res.usd[1] > 0


@SIM
@given(implement_inputs())
def test_eta_p2_quantiles_ordered_and_cost_positive(inp):
    _check_result(run_implement(*inp))


@SIM
@given(create_inputs())
def test_eta_p2_create_quantiles_ordered_and_cost_positive(inp):
    _check_result(run_create(*inp))


@SIM
@given(implement_inputs())
def test_eta_p3_no_finish_before_now(inp):
    raw, evs, recs, slots, now = inp
    res = run_implement(raw, evs, recs, slots, now)
    if res.finish_min is not None:
        assert res.finish_min[0] >= now - 1e-9
    if res.remaining_min is not None:
        assert all(v >= 0 for v in res.remaining_min)
    assert all(v >= 0 for v in res.task_median_min.values())


@SIM
@given(create_inputs())
def test_eta_p3_create_no_finish_before_now(inp):
    res = run_create(*inp)
    if res.finish_min is not None:
        assert res.finish_min[0] >= inp[2] - 1e-9
    assert all(v >= 0 for v in res.phase_median_min.values())


@PURE
@given(st.lists(st.floats(0, 600, allow_nan=False), min_size=1, max_size=30), st.integers(0, 600),
       st.floats(0, 1, exclude_max=True))
def test_eta_p3_conditional_sample_never_below_elapsed(values, elapsed, u):
    from claude_kiro.eta import simulate

    # elapsed ranges over 0..600, so it is far above the values' p90 for most draws
    out = simulate.conditional_sample(values, float(elapsed), u)
    assert out >= elapsed
    above = [v for v in values if v > elapsed]
    if above:
        assert out in above


@SIM
@given(acyclic_graphs(), st.data())
def test_eta_p4_done_absent_open_present_once(raw, data):
    ids = list(raw)
    done = frozenset(data.draw(st.sets(st.sampled_from(ids))))
    recs = data.draw(subset_of(eh.POOL_IMPL))
    evs = data.draw(es.events(raw, max_events=0))
    res = run_implement(raw, evs, recs, 2, 0.0, done=done)
    keys = list(res.task_median_min)
    assert not set(keys) & done
    assert len(keys) == len(set(keys))
    if res.finish_min is not None or keys:
        assert set(keys) == set(ids) - done
    if done == frozenset(ids):
        assert not keys


@SIM
@given(subset_of(eh.POOL_COSTLESS, min_size=1), st.data())
def test_eta_p10_cost_null_iff_unpriced(costless, data):
    raw = data.draw(acyclic_graphs())
    evs = data.draw(es.events(raw, max_events=0))
    priced_extra = data.draw(subset_of(eh.POOL_IMPL, max_size=3))
    # every record without a cost: cost is None, never 0
    res = run_implement(raw, evs, costless, 2, 0.0)
    assert res.usd is None
    # mixed with priced records: never a zero cost
    mixed = run_implement(raw, evs, costless + priced_extra, 2, 0.0)
    assert mixed.usd is None or (mixed.usd[0] > 0 and mixed.usd[1] > 0)


@SIM
@given(subset_of(eh.POOL_IMPL, min_size=20), st.data())
def test_eta_p10_priced_history_gives_positive_cost(priced, data):
    raw = data.draw(acyclic_graphs())
    evs = data.draw(es.events(raw, max_events=0))
    assume(any(d for d in raw))
    res = run_implement(raw, evs, priced, 2, 0.0)
    if res.finish_min is not None:
        assert res.usd is not None and res.usd[0] > 0


@SIM
@given(subset_of(eh.POOL_CREATE, min_size=1))
def test_eta_p12_phase_inclusion_rate(recs):
    from claude_kiro import progress
    from claude_kiro.eta import history, simulate

    model = history.fit_model(recs, None)
    presence = {t: p["presence"] for t, p in history.model_to_json(model)["phases"].items()}
    assume(presence)
    start = [{"schema": es.PROGRESS_SCHEMA, "at": es.iso(0), "kind": es.EV_START, "run": "c" + "0" * 5,
              "runKind": "create", "spec": "demo", "open": list(es.CREATE_PHASES), "total": len(es.CREATE_PHASES)}]
    state = progress.fold(start)
    runs = 500
    seen = {t: 0 for t in presence}
    for s in range(runs):  # one pass per call: a phase is in `phase_median_min` iff that pass included it
        res = simulate.simulate_create(model, state, now_min=0.0, n=1, seed=s)
        for t in presence:
            seen[t] += t in res.phase_median_min
    for t, p in presence.items():
        rate = seen[t] / runs
        assert abs(rate - p) <= 0.1, (t, p, rate)
        if p == 0:
            assert seen[t] == 0
        if p == 1:
            assert seen[t] == runs


def _medians(model) -> Dict[str, float]:
    from claude_kiro.eta import history

    return {t: statistics.median(p["minutes"]) for t, p in history.model_to_json(model)["phases"].items() if p["minutes"]}


@PURE
@given(subset_of(eh.POOL_CREATE, min_size=1), st.integers(0, 2))
def test_eta_p13_untimed_create_records_change_nothing(timed, extra):
    from claude_kiro.eta import history

    base = [r for r in timed if r.get("phases")]
    assume(base)
    untimed = []
    for r in base[:extra]:
        r = copy.deepcopy(r)
        r["phases"] = None  # the collector's shape for a record without per-phase timing
        untimed.append(r)
    before = _medians(history.fit_model(base, None))
    after = _medians(history.fit_model(base + untimed, None))
    assert after == before
    assert all(v > 0 for v in after.values())
