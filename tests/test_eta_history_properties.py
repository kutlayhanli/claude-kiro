"""Properties P-9 and P-11 over the pure seams of `claude_kiro.eta.history` (run-followups).

History comes from the records the real collector wrote (`eta_helpers.history_records`), never hand-shaped records.
Project modules are imported inside the test bodies so this file always collects.
"""

import copy
import functools

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import eta_helpers as h
from eta_strategies import MATCH_STEPS, NO_NETWORK

pytestmark = NO_NETWORK

PROPERTIES = {
    "P-9": "test_p9_size_factor_is_monotone_and_never_zero",
    "P-11": "test_p11_removing_records_never_narrows_the_match",
}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

@functools.lru_cache(maxsize=1)
def _pool():
    return tuple(h.history_records(h.POOL_IMPL))


@functools.lru_cache(maxsize=1)
def _create_pool():
    return tuple(h.history_records(h.POOL_CREATE))


@functools.lru_cache(maxsize=1)
def _sizes():
    """spec name -> {task id: (files, criteria)}: what the spec's tasks.md held (the harness builds it from `impl_plan`)."""
    out = {}
    for i in range(h.IMPL_RUNS):
        _, spec, plan = h.impl_plan(i)
        out[spec] = {str(p.defn.id): (p.defn.files, p.defn.criteria) for p in plan}
    return out


def _reader(root, spec):
    return _sizes().get(spec)


@functools.lru_cache(maxsize=1)
def _variants():
    """Real records re-shaped like the real local ones: null riskTag, track `unknown`, list-valued escalations."""
    out = []
    for n, rec in enumerate(_pool()[:24]):
        rec = copy.deepcopy(rec)
        for j, task in enumerate(rec["tasks"]):
            if j % 3 == 0:
                task["riskTag"] = None
            elif j % 3 == 1 and n % 2:
                task["track"] = "unknown"
            task["escalations"] = ["opus"] * (1 if (n + j) % 5 == 0 else 0)
        out.append(rec)
    return tuple(out)


def _all_records():
    return list(_pool()) + list(_variants())


@functools.lru_cache(maxsize=1)
def _defaults():
    from claude_kiro.eta import history

    result = history.build_defaults(list(_pool()) + list(_create_pool()), min_runs=history.MIN_DEFAULT_RUNS)
    return result.payload


SIZE_PAIRS = st.one_of(
    st.sampled_from([(0, 0), (0, 1), (1, 20)]),
    st.tuples(st.integers(0, 40), st.integers(0, 40)).map(lambda p: tuple(sorted(p))),
    st.integers(0, 40).map(lambda a: (a, a)),
)


@SETTINGS
@given(idx=st.sets(st.integers(0, h.IMPL_RUNS - 1), min_size=10), pair=SIZE_PAIRS)
def test_p9_size_factor_is_monotone_and_never_zero(idx, pair):
    from claude_kiro.eta import history

    records = [_pool()[i] for i in sorted(idx)]
    model = history.fit_model(records, None, tasks_md_reader=_reader)
    s = history.model_to_json(model)["size"]
    fit = history.SizeFit(exponent=s["exponent"], min_size=s["minSize"], median_size=s["medianSize"], source=s["source"])
    assert fit.source == "local", (len(records), fit)
    a, b = pair
    fa = history.size_factor(fit, a, None)
    fb = history.size_factor(fit, b, None)
    assert fa <= fb, (pair, fit, fa, fb)
    assert fa > 0 and fb > 0, (pair, fit, fa, fb)
    # the same holds against a recorded sample size
    sample = 5
    assert history.size_factor(fit, a, sample) <= history.size_factor(fit, b, sample)
    assert history.size_factor(fit, 0, sample) > 0


QUERIES = st.tuples(
    st.sampled_from(["impl", "test", "unknown"]),
    st.sampled_from(["sonnet", "opus", "haiku", "fable", "inherit", "unknown"]),
    st.sampled_from(["normal", "safety"]),
)


@SETTINGS
@given(data=st.data(), query=QUERIES)
def test_p11_removing_records_never_narrows_the_match(data, query):
    from claude_kiro.eta import history

    total = len(_all_records())
    big = data.draw(st.sets(st.integers(0, total - 1), max_size=total))
    small = data.draw(st.sets(st.sampled_from(sorted(big)), max_size=len(big))) if big else set()
    records = _all_records()
    defaults = _defaults()
    m_big = history.fit_model([records[i] for i in sorted(big)], defaults, tasks_md_reader=_reader)
    m_small = history.fit_model([records[i] for i in sorted(small)], defaults, tasks_md_reader=_reader)
    track, start_model, risk = query
    wide_big = history.match_pool(m_big, track, start_model, risk)
    wide_small = history.match_pool(m_small, track, start_model, risk)
    assert MATCH_STEPS.index(wide_small.step) >= MATCH_STEPS.index(wide_big.step), (query, wide_big.step, wide_small.step)
    parts_big = set(history.model_to_json(m_big)["bundledParts"])
    parts_small = set(history.model_to_json(m_small)["bundledParts"])
    assert parts_big <= parts_small, (parts_big, parts_small)
