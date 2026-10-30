"""Red-team 12.3: the test-only clock option must not leak into real runs or into the shipped scripts."""

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

# Well-separated durations (ms): the end order is the same on any machine, with real timers.
PROJECT = {"deps": {"1": [], "2": []}, "durations": {"1": 60, "2": 10}, "mergeDurations": {}}
ARGS = {"maxConcurrent": 2, "mergeBatch": 1, "fullGateEvery": 0}


def order(out):
    return [(e["ev"], e["label"]) for e in out["trace"]]


def run_raw(bridge, **extra):  # noqa: F811
    req = {"op": "run", "project": PROJECT, "args": ARGS, "slots": None, **extra}
    out = bridge._ok(req)
    out.pop("ok")
    return out


def test_clock_absent_and_clock_null_and_real_agree_on_timing_order(bridge):  # noqa: F811
    absent = run_raw(bridge)
    null = run_raw(bridge, clock=None)
    real = run_raw(bridge, clock="real")
    assert order(absent) == order(null) == order(real)
    ends = [e["label"] for e in absent["trace"] if e["ev"] == "end" and e["label"] in ("task 1", "task 2")]
    assert ends == ["task 2", "task 1"]  # the 10 ms task ends before the 60 ms one


def test_a_virtual_run_leaves_no_trace_in_later_default_runs(bridge):  # noqa: F811
    before = run_raw(bridge)
    virtual = run_raw(bridge, clock="virtual")
    after = run_raw(bridge)
    assert order(before) == order(after) == order(virtual)
    # The default run's timestamps come from a real clock, not from the virtual one (which starts at exactly 0).
    ts = [e["t"] for e in after["trace"]]
    assert ts != [e["t"] for e in virtual["trace"]]
    assert any(t != int(t) for t in ts) or max(ts) > 0


def test_script_exports_behave_the_same_before_and_after_a_virtual_run(bridge):  # noqa: F811
    deps = {"1": [], "2": ["1"], "3": ["1", "2"], "4": []}
    args = dict(deps=deps, done=["1"], exclude=["4"], blocked=[])
    before = bridge.ready(**args)
    run_raw(bridge, clock="virtual")
    after = bridge.ready(**args)
    assert before == after == ["2"]


def test_unknown_clock_value_acts_as_the_default_clock(bridge):  # noqa: F811
    assert order(run_raw(bridge, clock="bogus")) == order(run_raw(bridge))
