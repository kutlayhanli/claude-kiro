"""Story 12 (TC-73, TC-75, TC-76, P-25): the scheduler harness runs on a virtual clock, so traces are exact.

The real `plugin/workflows/spec-implement.js` runs under the fake runtime; only the clock and the agents are test
doubles, both owned by `tests/js/`.
"""

import copy
import os
import re
from pathlib import Path

import pytest
from hypothesis import given, settings

import eta_strategies as es
from landing_bridge import bridge, scenarios  # noqa: F401  (bridge is a module-scoped fixture)

PROPERTIES = {"P-25": "test_p25_virtual_runs_are_identical_and_durations_exact"}

SETTINGS = settings(derandomize=True, max_examples=60, database=None, deadline=None)
ROOT = Path(__file__).resolve().parent.parent
CODE_TOKENS = ["VirtualClock", "clock.now(", "clock:", "setTimeout(", "performance.now("]


def prep(sc):
    sc = copy.deepcopy(sc)
    sc["args"]["deps"] = {t: list(d) for t, d in sc["project"]["deps"].items()}
    sc["args"]["remaining"] = list(sc["project"]["deps"])
    return sc


def vrun(bridge, sc):  # noqa: F811
    return bridge.run(sc["project"], sc["args"], slots=sc["slots"], clock="virtual")


def test_tc73_one_task_has_exact_duration_and_identical_traces(bridge):  # noqa: F811
    project = {"deps": {"1": []}, "durations": {"1": 5}, "reviewDurations": {"1": 0}, "mergeDurations": {"1": 2}}
    a = bridge.run(project, clock="virtual")
    b = bridge.run(project, clock="virtual")
    assert a["trace"] == b["trace"]
    ts = {(e["label"], e["ev"]): e["t"] for e in a["trace"]}
    assert ts[("task 1", "end")] - ts[("task 1", "start")] == 5


def leaked_tokens(text):
    return [tok for tok in CODE_TOKENS if tok in text]


def test_tc75_clock_hook_is_not_in_shipped_scripts():
    scripts = sorted((ROOT / "plugin" / "workflows").glob("*.js")) + sorted((ROOT / ".claude" / "workflows").glob("*.js"))
    assert scripts
    for path in scripts:
        assert not leaked_tokens(path.read_text()), f"{path} contains a clock code token"


def test_tc75_detector_flags_a_leaked_token():
    for tok in CODE_TOKENS:
        assert leaked_tokens(f"const x = 1\n{tok}\n") == [tok]
    assert leaked_tokens("// no clock and no I/O; clocks, randomness") == []


@SETTINGS
@given(scenarios())
def test_p25_virtual_runs_are_identical_and_durations_exact(bridge, sc):  # noqa: F811
    sc = prep(sc)
    a = vrun(bridge, sc)
    b = vrun(bridge, sc)
    assert a["trace"] == b["trace"]
    assert a["result"] == b["result"]
    starts = {}
    for e in a["trace"]:
        m = re.match(r"^task (\w+)(?: \[test\])?$", e["label"])
        if not m:
            continue
        if e["ev"] == "start":
            starts[e["label"]] = e["t"]
        elif e["ev"] == "end":
            assert e["t"] - starts[e["label"]] == sc["project"]["durations"][m.group(1)], (e, starts)


def test_tc76_load_does_not_change_virtual_traces(bridge):  # noqa: F811
    drawn = []

    @settings(derandomize=True, max_examples=20, database=None, deadline=None)
    @given(scenarios())
    def collect(sc):
        drawn.append(prep(sc))

    collect()
    assert len(drawn) >= 1
    idle = [vrun(bridge, sc)["trace"] for sc in drawn]
    with es.busy(os.cpu_count() or 2):
        loaded = [vrun(bridge, sc)["trace"] for sc in drawn]
    assert loaded == idle


@pytest.mark.skipif(not os.environ.get("CK_STRESS_P10"), reason="opt-in stress: CK_STRESS_P10=1")
def test_tc76_stress_fifty_suite_runs_under_load():
    import subprocess
    import sys

    with es.busy(os.cpu_count() or 2):
        for i in range(50):
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "auto", str(ROOT / "tests" / "test_landing_scheduler_properties.py")],
                               capture_output=True, text=True, cwd=ROOT)
            assert r.returncode == 0, f"run {i}:\n{r.stdout[-2000:]}"
