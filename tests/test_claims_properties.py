"""P-27: the claim release decision, over the full cross product of run, liveness, owner, age and threshold."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st

PROPERTIES = {"P-27": "test_tc175_p27_claim_release_truth_table"}

PURE = dict(max_examples=200, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

LIVENESS = ["running", "finished", "stale", "missing", None]
STARTING = "run-B"
OTHER_RUNS = ["run-A", "run-C", "run-b", "run-B ", ""]
AROUND = [0, 1, 58, 59, 60, 61, 62, 119, 120, 121, 300]


def age_minutes(threshold: int) -> st.SearchStrategy:
    near = st.sampled_from(sorted({m for m in AROUND} | {threshold - 1, threshold, threshold + 1} | {0}))
    return st.one_of(near.filter(lambda m: m >= 0), st.integers(0, 300), st.floats(0, 300, allow_nan=False))


@st.composite
def cases(draw):
    run_kind = draw(st.sampled_from(["none", "starting", "other"]))
    run = {"none": None, "starting": STARTING, "other": draw(st.sampled_from(OTHER_RUNS))}[run_kind]
    threshold = draw(st.one_of(st.sampled_from([1, 59, 60, 61, 120]), st.integers(1, 120)))
    return run, draw(st.sampled_from(LIVENESS)), draw(age_minutes(threshold)), threshold


def oracle(run, liveness, age_min, threshold):
    if run is None:
        return "release" if age_min * 60 > threshold * 60 else "keep"
    if run == STARTING:
        return "own"
    return "keep" if liveness == "running" else "release"


@settings(**PURE)
@given(cases())
def test_tc175_p27_claim_release_truth_table(case):
    from claude_kiro.worktrees import ClaimInfo, claim_release_decision

    run, liveness, age_min, threshold = case
    claim = ClaimInfo(owner="agent", token="tok", at="2026-01-01T00:00:00Z", run=run)
    got = claim_release_decision(claim, STARTING, liveness, age_min * 60.0, float(threshold))
    assert got == oracle(run, liveness, age_min, threshold), (run, liveness, age_min, threshold)
    if run is not None and run != STARTING and liveness == "running":
        assert got != "release"  # a running run's claim is never released, whatever its age


@settings(**PURE)
@given(cases())
def test_tc175_p27_running_claim_never_released(case):
    from claude_kiro.worktrees import ClaimInfo, claim_release_decision

    run, _, age_min, threshold = case
    if run is None:
        return
    claim = ClaimInfo(owner="agent", token=None, at=None, run=run)
    assert claim_release_decision(claim, STARTING, "running", age_min * 60.0, float(threshold)) != "release"
