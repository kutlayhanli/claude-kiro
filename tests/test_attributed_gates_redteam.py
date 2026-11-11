"""Red team of Story 2: flaky reruns (2.4), excused failures never rerun (2.5), a gate that cannot run never green (2.9).

Written from requirements.md only. CLI cases run the real `ck gate demo --attribute --json` on real `attr_project`
repos with real pytest; every rerun count is read from the counter files the generated tests append to (one line per
run), so a rerun that selected the wrong node, or nothing, or a whole suite, shows up as a wrong count. Workflow
cases run the real `spec-implement.js` through the Node bridge (`bridge.run`) and call its `gateOutcome` decision
(`bridge.call`). Project imports happen only inside helpers and test bodies: this module always collects.

`attr_project` map: test task 1 owns `tests/test_calc_tc.py` (Verifies 2, 3); impl task 2 selects tc1_/tc2_; impl
task 3 selects tc3_; test task 6 owns `tests/test_more.py` (Verifies 7, 8).
"""

import copy
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import unattended_helpers as h
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

ALL_DONE = {"1": "Done", "2": "Done", "3": "Done", "6": "Done", "7": "Done", "8": "Done"}
NOT_THREE = {k: v for k, v in ALL_DONE.items() if k != "3"}

# --------------------------------------------------------------------------- CLI helpers

_PARAM_SOURCE = '''
MODES = {modes!r}


def _key(val):
    return "test_p_" + val.encode("utf-8").hex()


@pytest.mark.parametrize("val", list(MODES))
def {name}(val):
    runs = _count(_key(val))
    mode = MODES[val]
    if mode == "flaky":
        assert runs >= 1, "fails on the first run only"
    elif mode == "fail":
        assert False, "always fails"
    elif mode == "twice":
        assert runs >= 2, "fails on the first two runs"
'''


def key_of(val: str) -> str:
    return "test_p_" + val.encode("utf-8").hex()


def put_file(root: Path, rel: str, source: str) -> None:
    """Write a generated test file (with the counter header), commit it, and bring `integrate/demo` level."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(h._TEST_HEADER + textwrap.dedent(source), encoding="utf-8")
    h.commit_all(root, f"add {rel}")
    h.git(h.integration_worktree(root), "merge", "--ff-only", "-q", "main")


def put_params(root: Path, modes: dict, rel: str = "tests/test_calc_tc.py", name: str = "test_tc1_param") -> None:
    put_file(root, rel, _PARAM_SOURCE.format(modes=modes, name=name))


def gate(root: Path, *extra: str):
    code, obj, err = h.ck_json(root, "gate", "demo", *extra, "--attribute", "--json")
    assert isinstance(obj, dict), f"gate printed no JSON (exit {code}); stderr: {err[-600:]}"
    assert isinstance(obj.get("failures"), list), f"no failures list in {sorted(obj)}"
    return code, obj


def runs(root: Path, val: str) -> int:
    return h.counter_lines(root, key_of(val))


def statuses(obj) -> list:
    return sorted(f["status"] for f in obj["failures"])


ODD_IDS = [
    "a[b]", "x y", "p::q", "a]b[", "[", "]", "-k", "--collect-only", "$HOME", "*", "?", "'", '"', ";", "a b::c[d]",
    "ünï", "世界", "😀", "tab\there", "back\\slash", "new\nline", "a/b", "a.b", "..", "%s", "{0}", "x[y]::z w",
]

# --------------------------------------------------------------------------- 2.4: flaky tests (TC-15)


def test_tc15_redteam_odd_node_ids_rerun_exactly_the_failed_node(tmp_path):
    """Every odd-looking parametrized id fails once then passes: each is flaky, run exactly twice, gate green."""
    modes = {v: "flaky" for v in ODD_IDS}
    modes["sibling-pass"] = "pass"
    modes["x"] = "pass"  # near-miss siblings of "x y" and "p::q"
    modes["p"] = "pass"
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_params(root, modes)
    code, obj = gate(root)
    assert statuses(obj) == ["flaky"] * len(ODD_IDS), statuses(obj)
    assert obj["ok"] is True and code == 0
    for val in ODD_IDS:
        assert runs(root, val) == 2, f"{val!r} ran {runs(root, val)} times"
    for val in ("sibling-pass", "x", "p"):
        assert runs(root, val) == 1, f"passing sibling {val!r} was rerun ({runs(root, val)} runs)"


def test_tc15_redteam_odd_node_ids_that_keep_failing_are_not_flaky(tmp_path):
    """A rerun that selects nothing exits 5 and runs no test: it must not turn a persistent failure into a pass."""
    modes = {v: "fail" for v in ODD_IDS}
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_params(root, modes)
    code, obj = gate(root)
    assert statuses(obj) == ["regression"] * len(ODD_IDS), statuses(obj)
    assert obj["ok"] is False and code == 1
    for val in ODD_IDS:
        assert runs(root, val) == 2, f"{val!r} ran {runs(root, val)} times (a failing test runs once, then reruns once)"


def test_tc15_redteam_reported_nodes_select_exactly_one_test(tmp_path):
    """The gate's own node ids fed back to pytest must each select one test (a rerun built from them cannot be empty)."""
    modes = {v: "fail" for v in ODD_IDS}
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_params(root, modes)
    _, obj = gate(root)
    nodes = [f["node"] for f in obj["failures"]]
    assert len(set(nodes)) == len(ODD_IDS)
    env = dict(h._ck_env(root, {"CK_TEST_COUNTERS": str(tmp_path / "scratch-counters")}))
    for node in nodes:
        out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node], cwd=root, env=env,
                             capture_output=True, text=True)
        assert out.returncode == 1 and "1 failed" in out.stdout, f"{node!r} did not select one test: {out.stdout[-300:]}"


def test_tc15_redteam_a_test_failing_on_the_rerun_too_is_not_rerun_again(tmp_path):
    """Fails twice, would pass on the third run: one rerun only, so a regression with exactly 2 runs."""
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_params(root, {"third-time-lucky": "twice"})
    code, obj = gate(root)
    assert statuses(obj) == ["regression"]
    assert obj["ok"] is False and code == 1
    assert runs(root, "third-time-lucky") == 2


def test_tc15_redteam_flaky_beside_a_real_regression(tmp_path):
    """A flaky test never hides in, or is swallowed by, a red gate: it stays `flaky`, the other stays `regression`."""
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_params(root, {"wobbly[1]": "flaky", "broken[2]": "fail", "fine": "pass"})
    code, obj = gate(root)
    assert statuses(obj) == ["flaky", "regression"]
    by = {f["status"]: f for f in obj["failures"]}
    assert "wobbly" in by["flaky"]["node"] and "broken" in by["regression"]["node"]
    assert obj["ok"] is False and code == 1
    assert (runs(root, "wobbly[1]"), runs(root, "broken[2]"), runs(root, "fine")) == (2, 2, 1)


def test_tc15_redteam_prefix_named_neighbours_are_not_rerun(tmp_path):
    """Selecting the rerun by name (-k) would also rerun `test_tc1_add_more`: only the failed node may run twice."""
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc1_add():
            runs = _count("test_tc1_add")
            assert runs >= 1


        def test_tc1_add_more():
            _count("test_tc1_add_more")


        def test_tc1_add_more_still():
            _count("test_tc1_add_more_still")


        class TestAdd:
            def test_tc1_add(self):
                _count("test_class_tc1_add")
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["flaky"] and obj["ok"] is True and code == 0
    assert h.counter_lines(root, "test_tc1_add") == 2
    for name in ("test_tc1_add_more", "test_tc1_add_more_still", "test_class_tc1_add"):
        assert h.counter_lines(root, name) == 1, f"{name} ran {h.counter_lines(root, name)} times"


def test_tc15_redteam_unicode_function_names_class_methods_and_odd_paths(tmp_path):
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc1_naïve():
            assert _count("test_uni_func") >= 1


        class TestGroup:
            def test_tc2_method(self):
                assert _count("test_class_method") >= 1
    ''')
    put_file(root, "tests/odd dir [1]/test_odd.py", '''
        def test_in_odd_dir():
            assert _count("test_odd_dir") >= 1
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["flaky"] * 3, statuses(obj)
    assert obj["ok"] is True and code == 0
    for name in ("test_uni_func", "test_class_method", "test_odd_dir"):
        assert h.counter_lines(root, name) == 2, f"{name} ran {h.counter_lines(root, name)} times"


def test_tc15_redteam_flaky_setup_error_is_flaky_not_red(tmp_path):
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_file(root, "tests/test_calc_tc.py", '''
        @pytest.fixture
        def sometimes():
            if _count("test_fixture_setup") < 1:
                raise RuntimeError("setup fails once")


        def test_tc1_needs_fixture(sometimes):
            _count("test_fixture_body")
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["flaky"] and obj["ok"] is True and code == 0
    assert h.counter_lines(root, "test_fixture_setup") == 2


def test_tc15_redteam_flaky_never_reaches_the_fixer_in_compact_form(tmp_path):
    """The compact gate is what the thin agent hands the fixer: flaky entries are listed apart from `regressions`."""
    root = h.attr_project(tmp_path, statuses=ALL_DONE)
    put_params(root, {"a[1]": "flaky", "b y": "flaky"})
    code, obj, err = h.ck_json(root, "gate", "demo", "--attribute", "--json", "--compact")
    assert isinstance(obj, dict), err
    assert obj["ok"] is True and code == 0
    assert obj["regressions"] == [] and obj.get("unattributed", []) == []
    assert len(obj["flaky"]) == 2 and all(f["status"] == "flaky" for f in obj["flaky"])
    assert obj["summary"]["regressions"] == 0 and obj["summary"]["flaky"] == 2


def test_tc15_redteam_a_flaky_test_in_an_unowned_file_still_passes_the_gate(tmp_path):
    root = h.attr_project(tmp_path, statuses={"1": "Done"})
    put_file(root, "tests/test_orphan.py", '''
        def test_orphan_flaky():
            assert _count("test_orphan_flaky") >= 1
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["flaky"] and obj["ok"] is True and code == 0
    assert h.counter_lines(root, "test_orphan_flaky") == 2


# --------------------------------------------------------------------------- 2.5: excused failures are never rerun (TC-17)


def test_tc17_redteam_excused_sharing_a_file_with_a_candidate(tmp_path):
    """Task 3 is open, so tc3 is excused; tc1 in the same file is a candidate. Only the candidate reruns."""
    root = h.attr_project(tmp_path, statuses=NOT_THREE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc3_sub():
            _count("test_tc3_sub")
            assert False


        def test_tc3_sub_extra():
            _count("test_tc3_sub_extra")
            assert False


        def test_tc1_add():
            _count("test_tc1_add")
            assert False


        def test_tc1_add_ok():
            _count("test_tc1_add_ok")
    ''')
    code, obj = gate(root)
    by = {f["node"].rsplit("::", 1)[-1]: f["status"] for f in obj["failures"]}
    assert by == {"test_tc3_sub": "excused", "test_tc3_sub_extra": "excused", "test_tc1_add": "regression"}, by
    assert obj["ok"] is False and code == 1
    assert h.counter_lines(root, "test_tc3_sub") == 1
    assert h.counter_lines(root, "test_tc3_sub_extra") == 1
    assert h.counter_lines(root, "test_tc1_add") == 2
    assert h.counter_lines(root, "test_tc1_add_ok") == 1


def test_tc17_redteam_excused_near_miss_param_ids(tmp_path):
    """Excused and candidate params whose ids are near-misses of each other ("a[1", "a[1]", "a[1]]")."""
    root = h.attr_project(tmp_path, statuses=NOT_THREE)
    put_file(root, "tests/test_calc_tc.py", '''
        IDS = ["a[1", "a[1]", "a[1]]", "a 1", "a::1"]


        @pytest.mark.parametrize("val", IDS)
        def test_tc3_param(val):
            _count("test_ex_" + val.encode().hex())
            assert False


        @pytest.mark.parametrize("val", IDS)
        def test_tc1_param(val):
            _count("test_cand_" + val.encode().hex())
            assert False
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["excused"] * 5 + ["regression"] * 5, statuses(obj)
    for val in ["a[1", "a[1]", "a[1]]", "a 1", "a::1"]:
        hx = val.encode().hex()
        assert h.counter_lines(root, f"test_ex_{hx}") == 1, f"excused {val!r} was rerun"
        assert h.counter_lines(root, f"test_cand_{hx}") == 2, f"candidate {val!r} ran {h.counter_lines(root, f'test_cand_{hx}')} times"


def test_tc17_redteam_an_excused_test_that_would_pass_on_rerun_stays_excused(tmp_path):
    """If the rerun were applied it would report `flaky`: an excused test must keep status `excused` and 1 run."""
    root = h.attr_project(tmp_path, statuses=NOT_THREE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc3_wobbly():
            assert _count("test_tc3_wobbly") >= 1
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["excused"]
    assert obj["failures"][0]["excusedBy"] == ["3"]
    assert obj["ok"] is True and code == 0
    assert h.counter_lines(root, "test_tc3_wobbly") == 1


def test_tc17_redteam_all_failures_excused_means_no_test_runs_twice(tmp_path):
    """No candidate at all: nothing is rerun, not even as a harmless "whole suite" pass. Test-body counts prove it
    (module imports do not: the collection check and `--collect-only` probes import each file, by design)."""
    root = h.attr_project(tmp_path, statuses={"1": "Done"})
    put_file(root, "tests/test_calc_tc.py", '''
        @pytest.mark.parametrize("n", range(4))
        def test_tc3_fail_{n}(n):
            _count("test_ex_%d" % n)
            assert False
    '''.replace("{n}", "x"))
    put_file(root, "tests/test_more.py", '''
        def test_tc5_pass():
            _count("test_pass_more")
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["excused"] * 4, statuses(obj)
    assert obj["ok"] is True and code == 0
    for n in range(4):
        assert h.counter_lines(root, f"test_ex_{n}") == 1
    assert sum(h.counter_lines(root, f"test_ex_{n}") for n in range(4)) == 4, "a test body ran again: a suite was rerun"
    assert h.counter_lines(root, "test_pass_more") == 1, "the passing file ran again: a suite was rerun"


def test_tc17_redteam_rerun_of_a_candidate_does_not_rerun_a_whole_file_or_suite(tmp_path):
    """Candidate in file A; excused failure and passing tests in file B. B's tests must run once."""
    root = h.attr_project(tmp_path, statuses=NOT_THREE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc1_cand():
            _count("test_tc1_cand")
            assert False


        def test_tc3_excused():
            _count("test_tc3_excused")
            assert False
    ''')
    put_file(root, "tests/test_more.py", '''
        def test_tc5_pass_a():
            _count("test_pass_a")


        def test_tc5_pass_b():
            _count("test_pass_b")
    ''')
    code, obj = gate(root)
    assert obj["ok"] is False and code == 1
    assert h.counter_lines(root, "test_tc1_cand") == 2
    assert h.counter_lines(root, "test_tc3_excused") == 1
    assert h.counter_lines(root, "test_pass_a") == 1 and h.counter_lines(root, "test_pass_b") == 1
    assert h.counter_lines(root, "test_pass_a") + h.counter_lines(root, "test_pass_b") == 2, "an unrelated file ran again in a rerun"


def test_tc17_redteam_same_test_name_in_two_files_one_excused_one_candidate(tmp_path):
    """A rerun selected by bare name would rerun both `test_dup`s; the excused one (Task 3 open) must run once."""
    root = h.attr_project(tmp_path, statuses=NOT_THREE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_dup():
            _count("test_dup_calc")
            assert False
    ''')
    put_file(root, "tests/test_more.py", '''
        def test_dup():
            _count("test_dup_more")
            assert False
    ''')
    code, obj = gate(root)
    by = {f["file"]: f["status"] for f in obj["failures"]}
    assert by == {"tests/test_calc_tc.py": "excused", "tests/test_more.py": "regression"}, by
    assert h.counter_lines(root, "test_dup_calc") == 1, "the excused test was rerun"
    assert h.counter_lines(root, "test_dup_more") == 2


def test_tc17_redteam_unattributed_failures_are_candidates_excused_are_not(tmp_path):
    root = h.attr_project(tmp_path, statuses={"1": "Done", "2": "Done"})
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc3_excused():
            _count("test_tc3_excused")
            assert False
    ''')
    put_file(root, "tests/test_orphan.py", '''
        def test_orphan_fail():
            _count("test_orphan_fail")
            assert False
    ''')
    code, obj = gate(root)
    assert statuses(obj) == ["excused", "unattributed"], statuses(obj)
    assert h.counter_lines(root, "test_tc3_excused") == 1
    assert h.counter_lines(root, "test_orphan_fail") == 2


def test_tc17_redteam_compact_form_is_not_a_second_run(tmp_path):
    """`--compact` prints the same gate: one run of an excused test, one rerun of a candidate, no extra pass."""
    root = h.attr_project(tmp_path, statuses=NOT_THREE)
    put_file(root, "tests/test_calc_tc.py", '''
        def test_tc3_excused():
            _count("test_tc3_excused")
            assert False


        def test_tc1_cand():
            _count("test_tc1_cand")
            assert False
    ''')
    code, obj, err = h.ck_json(root, "gate", "demo", "--attribute", "--json", "--compact")
    assert isinstance(obj, dict), err
    assert h.counter_lines(root, "test_tc3_excused") == 1
    assert h.counter_lines(root, "test_tc1_cand") == 2
    assert obj["excusedByTask"] == {"3": 1}


# --------------------------------------------------------------------------- 2.9: a gate that cannot run is never green (TC-25)

SPEC = "demo"
FAILURE = {"node": "tests/test_x.py::test_tc1_x", "tcId": "TC-1", "tasks": ["1"], "attributedBy": "tc",
           "file": "tests/test_x.py", "kind": "assertion", "message": "assert 0 == 1", "truncated": False,
           "command": "pytest", "status": "regression", "excusedBy": [], "rerun": "failed", "class": None,
           "check": None, "missingCommand": None}


def green_json():
    return {"schema": "ck-gate/1", "spec": SPEC, "tasks": [], "ok": True, "attribute": True, "checks": [],
            "failures": [], "summary": {"regressions": 0, "excused": 0, "flaky": 0, "unattributed": 0},
            "warnings": [], "durationS": 0}


def with_(base, **changes):
    out = copy.deepcopy(base)
    out.update(changes)
    return out


def call_of(json_value, code=0):
    return {"code": code, "json": json_value, "stderr": "", "via": "direct"}


def outcome(bridge, call):
    out = bridge.call("gateOutcome", call)
    assert isinstance(out, dict) and out.get("state") in ("green", "red", "error"), out
    return out


def assert_not_green(bridge, call, why):
    out = outcome(bridge, call)
    assert out["state"] != "green" and not out.get("green"), f"{why}: counted green: {out}"
    return out


def test_tc25_redteam_control_green_and_excused_flaky_stay_green(bridge):
    assert outcome(bridge, call_of(green_json()))["state"] == "green"
    excused = with_(FAILURE, status="excused", excusedBy=["3"])
    flaky = with_(FAILURE, status="flaky", rerun="passed")
    mixed = with_(green_json(), failures=[excused, flaky], summary={"regressions": 0, "excused": 1, "flaky": 1, "unattributed": 0})
    assert outcome(bridge, call_of(mixed))["state"] == "green"


def test_tc25_redteam_ok_true_with_a_regression_entry_is_not_green(bridge):
    lying = with_(green_json(), failures=[FAILURE], summary={"regressions": 1, "excused": 0, "flaky": 0, "unattributed": 0})
    assert_not_green(bridge, call_of(lying), "ok:true beside a regression entry")
    unattributed = with_(green_json(), failures=[with_(FAILURE, status="unattributed", tasks=[])])
    assert_not_green(bridge, call_of(unattributed), "ok:true beside an unattributed entry")


def test_tc25_redteam_summary_contradicting_failures_is_not_green(bridge):
    zero_summary = with_(green_json(), failures=[FAILURE])  # summary says 0 regressions
    assert_not_green(bridge, call_of(zero_summary), "summary 0 regressions, failures hold one")
    inflated = with_(green_json(), summary={"regressions": 3, "excused": 0, "flaky": 0, "unattributed": 0})
    assert_not_green(bridge, call_of(inflated), "summary 3 regressions, failures empty")
    red_but_empty = with_(green_json(), ok=False)
    assert_not_green(bridge, call_of(red_but_empty, code=1), "ok:false with no failures")


def test_tc25_redteam_exit_code_must_agree_with_ok(bridge):
    """Assumption (design 2.9): gateOutcome cross-checks the exit code against ok; a green needs ok:true and exit 0."""
    assert_not_green(bridge, call_of(green_json(), code=1), "ok:true but exit code 1")
    assert_not_green(bridge, call_of(green_json(), code=2), "ok:true but exit code 2")
    assert_not_green(bridge, call_of(green_json(), code=-1), "ok:true but the process was killed")
    assert_not_green(bridge, call_of(green_json(), code=None), "ok:true with no exit code")


def test_tc25_redteam_a_thin_envelope_whose_json_is_a_string_is_an_error(bridge):
    as_text = '{"schema": "ck-gate/1", "ok": true, "failures": []}'
    out = assert_not_green(bridge, call_of(as_text), "json is a string of JSON")
    assert out["state"] == "error"
    assert outcome(bridge, call_of([green_json()]))["state"] == "error"
    assert outcome(bridge, call_of(True))["state"] == "error"
    assert outcome(bridge, call_of(0))["state"] == "error"


# Trailing text and two JSON objects are covered through the real gateAnswers path by the bridge.run tests below.


def test_tc25_redteam_no_json_is_an_error_for_every_exit_code(bridge):
    for code in (0, 1, 2, 3, 124, 127, -1, 255):
        out = outcome(bridge, call_of(None, code=code))
        assert out["state"] == "error" and not out.get("green"), f"exit {code}: {out}"
        assert out.get("reason"), f"exit {code}: an error must say why"


def test_tc25_redteam_wrong_shapes_are_errors_not_green(bridge):
    cases = {
        "missing schema": {k: v for k, v in green_json().items() if k != "schema"},
        "wrong schema": with_(green_json(), schema="ck-gate/2"),
        "foreign schema": with_(green_json(), schema="ck-audit/1"),
        "ok as string": with_(green_json(), ok="true"),
        "ok as 1": with_(green_json(), ok=1),
        "ok missing": {k: v for k, v in green_json().items() if k != "ok"},
        "failures missing": {k: v for k, v in green_json().items() if k != "failures"},
        "failures null": with_(green_json(), failures=None),
        "failures object": with_(green_json(), failures={}),
        "failures string": with_(green_json(), failures="[]"),
        "empty object": {},
    }
    for name, obj in cases.items():
        out = outcome(bridge, call_of(obj))
        assert out["state"] == "error" and not out.get("green"), f"{name}: {out}"


def test_tc25_redteam_failure_entries_that_are_not_objects_are_not_green(bridge):
    for entries in ([None], ["tests/test_x.py::test_a"], [[]], [1]):
        assert_not_green(bridge, call_of(with_(green_json(), failures=entries)), f"failures {entries!r}")


def test_tc25_redteam_compact_answers_that_lie_are_not_green(bridge):
    compact = {"schema": "ck-gate-compact/1", "spec": SPEC, "tasks": [], "ok": True, "attribute": True,
               "failedChecks": [], "regressions": [], "unattributed": [], "flaky": [], "excusedByTask": {},
               "summary": {"regressions": 0, "excused": 0, "flaky": 0, "unattributed": 0}, "warnings": [],
               "report": None, "durationS": 0}
    assert outcome(bridge, call_of(compact))["state"] == "green"
    assert outcome(bridge, call_of(with_(compact, regressions="none")))["state"] == "error"
    assert outcome(bridge, call_of(with_(compact, regressions=None)))["state"] == "error"
    assert outcome(bridge, call_of({k: v for k, v in compact.items() if k != "regressions"}))["state"] == "error"
    assert_not_green(bridge, call_of(with_(compact, regressions=[FAILURE])), "compact ok:true with a regression")
    assert_not_green(bridge, call_of(with_(compact, unattributed=[with_(FAILURE, status="unattributed")])), "compact ok:true with unattributed")
    assert_not_green(bridge, call_of(with_(compact, failedChecks=[{"name": "Tamper", "ok": False, "detail": "a failing check", "warning": False}])), "compact ok:true with a failed check")
    assert_not_green(bridge, call_of(with_(compact, summary={"regressions": 2, "excused": 0, "flaky": 0, "unattributed": 0})), "compact summary contradicts")


def test_tc25_redteam_missing_call_is_an_error_not_a_crash(bridge):
    for call in (None, {}, {"code": 0}, "ok", [], 0):
        out = outcome(bridge, call)
        assert out["state"] == "error" and not out.get("green"), f"{call!r}: {out}"


# --- the real script: bridge.run -------------------------------------------------------------

SEQ_DEPS = {"1": [], "2": ["1"]}
MANY = 40  # more answers than any retry or fixer ladder can consume (an exhausted queue answers green)


def run_with_gates(bridge, answers, *, full_every=0, deps=None):
    """(gateAnswers: the {code, stdout} raw form and the "throw" sentinel are defined by tests/js/fake_project.mjs.) The Claude Code path (no `ck` global, a `runnerAgent`): gate calls are thin agent calls answered by `answers`."""
    out = bridge.run({"deps": deps or {"1": []}, "gateAnswers": answers},
                     args={"runnerAgent": "ck-runner", "fullGateEvery": full_every})
    return out["result"]


def assert_run_not_complete(result, why):
    assert result.get("outcome") != "complete", f"{why}: the run was reported complete: {result.get('outcome')}"
    final = result.get("finalGate")
    assert not (isinstance(final, dict) and final.get("green") is True), f"{why}: finalGate counted green: {final}"
    for entry in result.get("gates") or []:
        assert entry.get("green") is not True or entry.get("state") in (None, "green"), f"{why}: {entry}"


def test_tc25_redteam_run_json_with_trailing_text_never_completes(bridge):
    text = '{"schema": "ck-gate/1", "ok": true, "failures": []}\nGate passed, nothing to fix.'
    result = run_with_gates(bridge, [{"code": 0, "stdout": text}] * MANY)
    assert_run_not_complete(result, "trailing text")


def test_tc25_redteam_run_two_json_objects_never_completes(bridge):
    text = '{"schema": "ck-gate/1", "ok": false, "failures": []}\n{"schema": "ck-gate/1", "ok": true, "failures": []}'
    result = run_with_gates(bridge, [{"code": 0, "stdout": text}] * MANY)
    assert_run_not_complete(result, "two objects")


def test_tc25_redteam_run_ok_true_with_a_regression_entry_never_completes(bridge):
    lying = with_(green_json(), failures=[FAILURE], summary={"regressions": 1, "excused": 0, "flaky": 0, "unattributed": 0})
    result = run_with_gates(bridge, [lying] * MANY)
    assert_run_not_complete(result, "ok:true beside a regression")


def test_tc25_redteam_run_summary_contradicting_failures_never_completes(bridge):
    inflated = with_(green_json(), summary={"regressions": 2, "excused": 0, "flaky": 0, "unattributed": 0})
    result = run_with_gates(bridge, [inflated] * MANY)
    assert_run_not_complete(result, "summary contradicts failures")


def test_tc25_redteam_run_ok_true_but_exit_code_one_never_completes(bridge):
    result = run_with_gates(bridge, [{"code": 1, "stdout": '{"schema": "ck-gate/1", "ok": true, "failures": []}'}] * MANY)
    assert_run_not_complete(result, "ok:true with exit code 1")


def test_tc25_redteam_run_a_previous_green_answer_is_not_replayed_for_a_later_gate(bridge):
    """The periodic gate after merge 1 is green; every later gate cannot run. The final gate must not borrow that green."""
    result = run_with_gates(bridge, [green_json()] + ["throw"] * MANY, full_every=1, deps=SEQ_DEPS)
    assert_run_not_complete(result, "earlier green replayed")


def test_tc25_redteam_run_a_green_then_malformed_final_gate_never_completes(bridge):
    bad = {"code": 2, "stdout": "Traceback (most recent call last):\nboom"}
    result = run_with_gates(bridge, [green_json()] + [bad] * MANY, full_every=1, deps=SEQ_DEPS)
    assert_run_not_complete(result, "green then crash")


def test_tc25_redteam_run_a_thrown_gate_is_not_green_even_when_it_is_the_only_gate(bridge):
    result = run_with_gates(bridge, ["throw"] * MANY)
    assert_run_not_complete(result, "thrown gate")
