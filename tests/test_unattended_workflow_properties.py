"""TC-174 (part): workflow-decision properties P-7, P-30, P-31 and P-34, driven through the Node bridge.

P-7 and P-34 call the decisions-block seams `gateOutcome` and `heldByCrossTest` (`{op: 'call'}`); P-30, P-31 and the
trace half of P-34 run the real `plugin/workflows/spec-implement.js` under the fake runtime. No server, no mock of
the project's own code. Bridge settings: `max_examples=60`, derandomized, no example database, no deadline.
The seams are missing from the decisions block until Tasks 78, 81 and 87 add them, so these tests fail first for
that reason (`decision function missing in the decisions block`).
"""

import copy
import re
from typing import Any, Dict, List, Optional, Set

from hypothesis import given, settings
from hypothesis import strategies as st

from landing_bridge import bridge, scenarios  # noqa: F401  (bridge is a module-scoped fixture)

PROPERTIES = {
    "P-7": "test_tc174_p7_gate_outcome_error_unless_valid_then_red_iff_regression",
    "P-30": "test_tc174_p30_thin_calls_carry_the_lean_type_and_schema",
    "P-31": "test_tc174_p31_attributed_gates_at_multiples_of_n_plus_final",
    "P-34": "test_tc174_p34_cross_test_hold_is_exactly_the_verified_tasks",
}

SETTINGS = settings(derandomize=True, max_examples=60, database=None, deadline=None)

STATUSES = ["regression", "excused", "flaky", "unattributed"]
RED = {"regression", "unattributed"}


# --- P-7 ----------------------------------------------------------------------------------------------------

def entry_of(status: str, i: int, with_node: bool, with_file: bool) -> Dict[str, Any]:
    e: Dict[str, Any] = {
        "node": f"tests/test_x.py::test_tc{i}_a" if with_node else None,
        "tcId": f"TC-{i}", "tasks": [str(i)], "attributedBy": "tc",
        "file": "tests/test_x.py" if with_file else None,
        "kind": "assertion", "message": "m", "truncated": False, "command": None,
        "status": status, "excusedBy": [],
    }
    return e


@st.composite
def valid_entries(draw) -> List[Dict[str, Any]]:
    statuses = draw(st.lists(st.sampled_from(STATUSES), max_size=6))
    out = []
    for i, s in enumerate(statuses):
        where = draw(st.sampled_from(["both", "node", "file"]))
        out.append(entry_of(s, i + 1, where in ("both", "node"), where in ("both", "file")))
    return out


def full_object(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    red = any(e["status"] in RED for e in entries)
    return {"schema": "ck-gate/1", "spec": "demo", "tasks": [], "ok": not red, "attribute": True, "checks": [], "failures": entries, "warnings": []}


def compact_object(entries: List[Dict[str, Any]], excused_by_task: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    red = any(e["status"] in RED for e in entries)
    return {
        "schema": "ck-gate-compact/1", "spec": "demo", "tasks": [], "ok": not red, "attribute": True, "failedChecks": [],
        "regressions": [e for e in entries if e["status"] == "regression"],
        "unattributed": [e for e in entries if e["status"] == "unattributed"],
        "flaky": [e for e in entries if e["status"] == "flaky"],
        "excusedByTask": excused_by_task or {}, "warnings": [], "report": "/repo/.git/ck/demo/gate-reports/r.json",
    }


def call_of(js: Any, code: int = 0) -> Dict[str, Any]:
    return {"code": code, "json": js, "stderr": "", "via": "lean"}


def verdict_of(entries: List[Dict[str, Any]]) -> str:
    return "red" if any(e["status"] in RED for e in entries) else "green"


@st.composite
def malformed_calls(draw) -> Dict[str, Any]:
    entries = draw(valid_entries())
    good_entry = entry_of("regression", 1, True, True)
    kind = draw(st.sampled_from([
        "null", "number", "string", "array", "empty", "no-failures", "failures-object", "failures-string", "ok-string",
        "bad-status", "bad-exit", "no-node-no-file", "json-null", "compact-regressions-object", "compact-regressions-missing",
        "compact-neg", "compact-str", "compact-float", "compact-failures-key", "entry-not-object", "ok-missing",
        "entry-bad-kind", "entry-bad-tcid", "entry-tasks-nonarray", "entry-long-message", "entry-missing-field",
        "ok-true-exit-1", "ok-false-exit-0", "null-node-null-file", "bad-schema", "compact-key-not-task",
    ]))
    full = full_object(entries)
    comp = compact_object(entries)
    if kind == "null":
        return call_of(None)
    if kind == "json-null":
        return {"code": 0, "json": None, "stderr": "", "via": "lean", "error": "no json"}
    if kind == "number":
        return call_of(draw(st.one_of(st.integers(), st.floats(allow_nan=False, allow_infinity=False))))
    if kind == "string":
        return call_of(draw(st.text(max_size=20)))
    if kind == "array":
        return call_of(draw(st.lists(st.one_of(st.integers(), st.none(), st.just(full)), max_size=3)))
    if kind == "empty":
        return call_of({})
    if kind == "no-failures":
        full.pop("failures")
        return call_of(full)
    if kind == "failures-object":
        full["failures"] = {"a": good_entry}
        return call_of(full)
    if kind == "failures-string":
        full["failures"] = draw(st.text(max_size=10))
        return call_of(full)
    if kind == "ok-string":
        full["ok"] = draw(st.sampled_from(["true", "false", "yes", ""]))
        return call_of(full)
    if kind == "ok-missing":
        full.pop("ok")
        return call_of(full)
    if kind == "bad-status":
        full["failures"] = entries + [{**good_entry, "status": draw(st.sampled_from(["fail", "Regression", "excusd", "", "REGRESSION", None, 3]))}]
        return call_of(full)
    if kind == "bad-exit":
        return call_of(draw(st.sampled_from([full, comp])), code=draw(st.sampled_from([2, 3, 127, 255, -1, 124])))
    if kind == "no-node-no-file":
        bare = {k: v for k, v in good_entry.items() if k not in ("node", "file")}
        full["failures"] = entries + [bare]
        return call_of(full)
    if kind == "null-node-null-file":
        full["failures"] = entries + [{**good_entry, "node": None, "file": None}]
        return call_of(draw(st.sampled_from([full, {**comp, "regressions": comp["regressions"] + [{**good_entry, "node": None, "file": None}]}])))
    if kind == "bad-schema":
        target = draw(st.sampled_from([full, comp]))
        bad_schema = draw(st.sampled_from(["ck-gate/2", "ck-gate-compact/2", "CK-GATE/1", "", None, 1, "__missing__"]))
        if bad_schema == "__missing__":
            target.pop("schema")
        else:
            target["schema"] = bad_schema
        return call_of(target)
    if kind == "compact-key-not-task":
        comp["excusedByTask"] = {draw(st.sampled_from(["x", "", "Task 71", "71a", "-1", "1.5"])): 1}
        return call_of(comp)
    if kind in ("entry-bad-kind", "entry-bad-tcid", "entry-tasks-nonarray", "entry-long-message", "entry-missing-field"):
        bad = dict(good_entry)
        if kind == "entry-bad-kind":
            bad["kind"] = draw(st.sampled_from(["fail", "Assertion", "", None, 3]))
        elif kind == "entry-bad-tcid":
            bad["tcId"] = draw(st.sampled_from(["TC-", "tc-1", "TC-1a", "1", "", 7]))
        elif kind == "entry-tasks-nonarray":
            bad["tasks"] = draw(st.sampled_from(["1", 1, None, {"a": "1"}, [1]]))
        elif kind == "entry-long-message":
            bad["message"] = "x" * draw(st.integers(501, 800))
        else:
            bad.pop(draw(st.sampled_from(["tcId", "tasks", "attributedBy", "kind", "message", "truncated", "command"])))
        full["failures"] = entries + [bad]
        return call_of(draw(st.sampled_from([full, {**comp, "regressions": comp["regressions"] + [bad]}])))
    if kind in ("ok-true-exit-1", "ok-false-exit-0"):
        # the compact object's ok and exit code equal the full gate's: ok iff exit 0. An `ok` that disagrees with the
        # exit code is a deliberate strengthening of the P-7 domain (tasks.md, Task 74 notes): it is `error`.
        green = kind == "ok-true-exit-1"
        full["ok"], comp["ok"] = green, green
        full["failures"] = comp_entries = [] if green else [good_entry]
        comp["regressions"], comp["unattributed"], comp["flaky"] = comp_entries, [], []
        return call_of(draw(st.sampled_from([full, comp])), code=1 if green else 0)
    if kind == "entry-not-object":
        full["failures"] = entries + [draw(st.sampled_from([None, 3, "x", []]))]
        return call_of(full)
    if kind == "compact-regressions-object":
        comp["regressions"] = {"a": good_entry}
        return call_of(comp)
    if kind == "compact-regressions-missing":
        comp.pop(draw(st.sampled_from(["regressions", "unattributed", "flaky"])))
        return call_of(comp)
    if kind == "compact-neg":
        comp["excusedByTask"] = {"71": draw(st.integers(max_value=-1))}
        return call_of(comp)
    if kind == "compact-str":
        comp["excusedByTask"] = {"71": draw(st.sampled_from(["3", "x", None, [1]]))}
        return call_of(comp)
    if kind == "compact-float":
        comp["excusedByTask"] = {"71": draw(st.sampled_from([1.5, 0.5, 2.25]))}
        return call_of(comp)
    # compact schema carrying a `failures` key instead of its own lists
    return call_of({"schema": "ck-gate-compact/1", "ok": True, "failures": entries, "excusedByTask": {}})


@SETTINGS
@given(malformed_calls())
def test_tc174_p7_gate_outcome_error_unless_valid_then_red_iff_regression_malformed(bridge, call):  # noqa: F811
    """2.9, 2.12, 19.5: every malformed output is `error` and never green."""
    out = bridge.call("gateOutcome", call)
    assert out["state"] == "error" and not out["green"], (call, out)


@SETTINGS
@given(valid_entries(), st.dictionaries(st.integers(1, 120).map(str), st.integers(0, 40), max_size=4))
def test_tc174_p7_gate_outcome_error_unless_valid_then_red_iff_regression(bridge, entries, excused):  # noqa: F811
    """2.6, 2.8, 2.12: valid full and compact objects with the same failures give the same verdict, red iff a
    `regression` or `unattributed` entry exists, and a red gate exiting 1 is a verdict, not an error."""
    expected = verdict_of(entries)
    code = 0 if expected == "green" else 1  # ok and exit code agree: 0 iff ok
    for js in (full_object(entries), compact_object(entries, excused)):
        out = bridge.call("gateOutcome", call_of(js, code))
        assert out["state"] == expected, (js, out)
        assert out["green"] is (expected == "green"), (js, out)
    compact = bridge.call("gateOutcome", call_of(compact_object(entries, excused), code))
    assert compact["excusedByTask"] == excused


# --- P-34 ---------------------------------------------------------------------------------------------------

TEST_FILES = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]


@st.composite
def cross_cases(draw) -> Dict[str, Any]:
    impl = [str(i) for i in range(2, 13)]
    test_verifies: Dict[str, List[str]] = {}
    for f in draw(st.lists(st.sampled_from(TEST_FILES + ["tests/test_extra.py"]), unique=True, max_size=4)):
        test_verifies[f] = draw(st.lists(st.sampled_from(impl), unique=True, max_size=4))
    findings = []
    for _ in range(draw(st.integers(0, 4))):
        f = draw(st.sampled_from(TEST_FILES))
        fn = f"test_tc{draw(st.integers(1, 30))}_x"
        form = draw(st.sampled_from(["node", "bare", "file"]))
        finding: Dict[str, Any] = {"severity": "blocking", "issue": "drift"}
        if form == "node":
            finding.update(file=f, test=f"{f}::{fn}")
        elif form == "bare":
            finding.update(file=f, test=fn)
        else:
            finding.update(file=f)
        findings.append({"finding": finding, "file": f})
    return {"testVerifies": test_verifies, "findings": findings}


def held_oracle(case: Dict[str, Any]) -> Set[str]:
    files = {x["file"] for x in case["findings"]}
    return {t for f in files for t in case["testVerifies"].get(f, [])}


def as_set(v: Any) -> Set[str]:
    return {str(x) for x in v}


@SETTINGS
@given(cross_cases())
def test_tc174_p34_cross_test_hold_is_exactly_the_verified_tasks(bridge, case):  # noqa: F811
    """7.4: `heldByCrossTest` holds exactly the impl tasks verified by the files of the tests the open findings name."""
    findings = [x["finding"] for x in case["findings"]]
    held = bridge.call("heldByCrossTest", {"findings": findings, "testVerifies": case["testVerifies"]})
    assert as_set(held) == held_oracle(case), (case, held)


IMPL = ["3", "4", "5", "6"]


@SETTINGS
@given(st.lists(st.sampled_from(IMPL), unique=True, min_size=0, max_size=3), st.integers(1, 3))
def test_tc174_p34_cross_test_hold_is_exactly_the_verified_tasks_trace(bridge, verified, tick):  # noqa: F811
    """7.4: in the run trace no held task starts while its finding is open, and no other task is held by it."""
    deps = {"1": [], "2": [], **{t: ["1", "2"] for t in IMPL}}
    project = {
        "deps": deps, "durations": {k: tick for k in deps},
        "testVerifies": {"tests/test_b.py": verified},
        "testFix": {"tests/test_b.py": {}},
        "crossTest": [
            {"verdict": "changes", "findings": [{"severity": "blocking", "file": "tests/test_b.py", "test": "tests/test_b.py::test_tc2_x", "issue": "drift"}], "summary": "drift"},
            {"verdict": "approve", "findings": [], "summary": "ok"},
        ],
    }
    args = {
        "mergeBatch": 1, "fullGateEvery": 0, "deps": deps, "remaining": list(deps), "tracks": {"1": "test", "2": "test", **{t: "impl" for t in IMPL}},
        "wave": {"1": 1, "2": 1, **{t: 2 for t in IMPL}},
        "agents": {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 1, "cross_test": True}},
    }
    out = bridge.run(project, args, slots=None, clock="virtual")
    trace = out["trace"]
    ends = [i for i, e in enumerate(trace) if e["ev"] == "end" and e["label"].startswith("cross-test")]
    assert ends, "the cross-test step never ran"
    open_from = ends[0]
    # the finding closes when its test fix lands (design: "Test conflict to landed test fix"); held tasks start after
    fix_lands = [i for i, e in enumerate(trace) if e["ev"] == "start" and e["label"].startswith("land ") and "testfix" in e["label"]]
    open_to = next((i for i in fix_lands if i > open_from), len(trace))
    starts: Dict[str, int] = {}
    for i, e in enumerate(trace):
        m = re.match(r"^task (\d+)", e["label"])
        if m and e["ev"] == "start":
            starts.setdefault(m.group(1), i)
    for t in verified:
        assert not (t in starts and open_from < starts[t] < open_to), f"held task {t} started while its finding was open: {trace}"
        if open_to < len(trace):
            assert t in starts, f"held task {t} never started after its test fix landed: {trace}"
    for t in set(IMPL) - set(verified):
        assert t in starts, f"task {t} was held although no open finding names a test it verifies: {trace}"


# --- P-30 / P-31 --------------------------------------------------------------------------------------------

LEAN = "spec:runner"
THIN = re.compile(r"^(plan \d+( retry)?|land .*|report|park \d+|relink \d+|full gate @\d+|re-check \d+|final gate|run start|audit)$")
# a host with the `ck` global: direct calls to a ck command that prints nothing (the scenario's thin calls never reach an agent)
DIRECT_CK = {"cmd": ["python3", "-c", "pass"], "env": {}}


def prepared(sc: Dict[str, Any], **args: Any) -> Dict[str, Any]:
    sc = copy.deepcopy(sc)
    deps = sc["project"]["deps"]
    sc["args"]["deps"] = {t: list(ds) for t, ds in deps.items()}
    sc["args"]["remaining"] = list(deps)
    sc["args"].update(args)
    return sc


def run_of(bridge, sc, direct: bool):  # noqa: F811
    return bridge.run(sc["project"], sc["args"], slots=sc["slots"], clock="virtual", real_ck=DIRECT_CK if direct else None)


@SETTINGS
@given(scenarios(), st.booleans(), st.booleans())
def test_tc174_p30_thin_calls_carry_the_lean_type_and_schema(bridge, sc, direct, runner_available):  # noqa: F811
    """19.1, 19.3, 19.6: on the Claude Code path every thin call has the lean agent type and the shared schema,
    no other call has the lean type; the `ck` global path has no thin agents; when the runner is not available
    there is no type and a warning."""
    sc = prepared(sc, runnerAgent=LEAN if runner_available else None)
    out = run_of(bridge, sc, direct)
    # every recorded call, in order: a repeated label or a non-thin call reusing a thin label is checked on its own
    calls = [(c["label"], c["opts"]) for c in out["optsList"]]
    assert calls, "no agent ran"
    lean = [(label, o) for label, o in calls if o.get("agentType")]
    if direct:
        assert not lean and not any(THIN.match(label) and (o.get("schema") or {}).get("required") == ["exitCode", "json"] for label, o in calls), "OpenCode path requested a thin agent"
        return
    thin = [(label, o) for label, o in calls if THIN.match(label)]
    assert thin, "no thin call ran"
    if runner_available:
        schemas = []
        for label, o in thin:
            assert o.get("agentType") == LEAN, (label, o)
            schemas.append(o.get("schema"))
        assert all(s == schemas[0] and s for s in schemas), schemas
        non_thin_lean = [label for label, o in lean if not THIN.match(label)]
        assert not non_thin_lean, f"non-thin calls carry the lean type: {non_thin_lean}"
    else:
        assert not lean, lean
        warning = (out["result"].get("thinAgent") or {}).get("warning")
        assert warning, out["result"].get("thinAgent")


@SETTINGS
@given(scenarios(), st.integers(0, 5), st.integers(1, 4))
def test_tc174_p31_attributed_gates_at_multiples_of_n_plus_final(bridge, sc, n, batch):  # noqa: F811
    """2.1, 2.11: an attributed gate is requested exactly when a landing's merge count crosses a multiple of N,
    plus one final gate after the last merge; with N = 0 only the final gate."""
    sc = prepared(sc, fullGateEvery=n, mergeBatch=batch)
    sc["project"]["checkRed"] = []
    out = run_of(bridge, sc, False)
    requests = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    gates = [x for x in requests if x.startswith("full gate @")]
    merged_total = 0
    expected: List[str] = []
    for landing in out["result"]["landings"]:
        before = merged_total
        merged_total += len(landing["merged"])
        if landing["merged"] and n > 0 and merged_total // n > before // n:
            expected.append(f"full gate @{merged_total}")
    assert gates == expected, (n, batch, gates, expected, requests)
    assert requests.count("final gate") == (1 if merged_total else 0), requests
    for label in gates + ["final gate"]:
        if label in out["prompts"]:
            assert "--attribute" in out["prompts"][label], (label, out["prompts"][label])
