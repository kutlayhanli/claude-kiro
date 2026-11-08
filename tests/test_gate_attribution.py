"""Per-test attribution and reruns through `ck gate --attribute` (unattended-correctness Story 1, 2.4, 2.5, 2.10, 2.11).

Real ck CLI on a real git repo (`attr_project`), real pytest, real `node --test` and real pytest-xdist. Failures are
read from the `ck-gate/1` JSON; run counts come from the counter files the generated tests write. Nothing here
mocks project code.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from unattended_helpers import (
    ALL_DONE,
    FIXTURES,
    GATE_BASELINE_SCENARIOS,
    _TEST_HEADER,
    attr_project,
    commit_all,
    counter_lines,
    ck_json,
    ck_text,
    normalize_gate_text,
    pytest_body,
    task_worktree,
    git,
)

DONE_12 = {"1": "Done", "2": "Done"}


# --------------------------------------------------------------------------- helpers


def gate(root: Path, *extra: str) -> Tuple[int, Dict[str, Any]]:
    """`ck gate demo --attribute --json`: (exit code, parsed ck-gate/1 object)."""
    code, obj, err = ck_json(root, "gate", "demo", *extra, "--attribute", "--json")
    assert isinstance(obj, dict), f"gate did not print JSON (exit {code}); stderr: {err[-800:]}"
    return code, obj


def entries(obj: Dict[str, Any], **match: Any) -> List[Dict[str, Any]]:
    assert "failures" in obj, f"no failures key in {sorted(obj)}"
    return [f for f in obj["failures"] if all(f.get(k) == v for k, v in match.items())]


def one(obj: Dict[str, Any], **match: Any) -> Dict[str, Any]:
    found = entries(obj, **match)
    assert len(found) == 1, f"expected one failure {match}, got {len(found)} in {json.dumps(obj['failures'], indent=1)}"
    return found[0]


def done(*extra: str, minus: Tuple[str, ...] = ()) -> Dict[str, str]:
    st = dict(ALL_DONE)
    for n in extra:
        st[n] = "Done"
    for n in minus:
        st.pop(n, None)
    return st


def write_commit(root: Path, files: Dict[str, str], message: str = "more files") -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    commit_all(root, message)


def extra_test_task(n: str, impl: str, verify: str, files: List[str], status: str) -> str:
    """A test task `n` verifying impl task `impl`, plus that impl task, in tasks.md syntax."""
    box = "x" if status == "Done" else " "
    file_lines = "\n".join(f"- `{f}` - extra" for f in files)
    return f"""\
### Task {n}: Extra tests
**Status:** {status}
**Track:** test
**Requirements:** 1.1
**Files:**
{file_lines}
**Verify:** `{verify}`
**Verifies:** Task {impl}
**Dependencies:** None

**Acceptance:**
- [{box}] Tests written

---

### Task {impl}: Extra impl
**Status:** {status}
**Track:** impl
**Requirements:** 1.1
**Files:**
- `extra_{impl}.py` - extra
**Verified by:** Task {n}
**Dependencies:** Task {n}

**Acceptance:**
- [{box}] works

---

"""


# --------------------------------------------------------------------------- TC-1 to TC-4


def test_tc1_attributes_each_failure_to_its_tasks(tmp_path):
    root = attr_project(tmp_path, statuses=DONE_12, tests={"test_tc1_add": "fail", "test_tc3_sub": "fail"})
    code, obj = gate(root)
    tc1 = one(obj, tcId="TC-1")
    tc3 = one(obj, tcId="TC-3")
    assert tc1["tasks"] == ["2"] and tc1["status"] == "regression"
    assert tc3["tasks"] == ["3"] and tc3["status"] == "excused" and tc3["excusedBy"] == ["3"]
    assert obj["ok"] is False and code == 1


def test_tc2_coverage_through_test_task_when_no_impl_selects_the_tc(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc5_x": "fail"})
    code, obj = gate(root)
    entry = one(obj, tcId="TC-5")
    assert entry["tasks"] == ["7", "8"]
    assert entry["status"] == "regression"
    assert code == 1


def test_tc3_task_gated_as_done_is_not_excused(tmp_path):
    root = attr_project(tmp_path, statuses=DONE_12, tests={"test_tc3_sub": "fail"})
    code, obj = gate(root, "--task", "3")
    entry = one(obj, tcId="TC-3")
    assert entry["status"] == "regression"
    assert entry.get("excusedBy", []) == []
    assert obj["ok"] is False and code == 1


def test_tc4_only_excused_failure_keeps_the_gate_green(tmp_path):
    root = attr_project(tmp_path, statuses=DONE_12, tests={"test_tc3_sub": "fail"})
    code, obj = gate(root)
    entry = one(obj, tcId="TC-3")
    assert entry["status"] == "excused" and entry["excusedBy"] == ["3"]
    assert obj["ok"] is True and code == 0


# --------------------------------------------------------------------------- TC-5 to TC-8


def test_tc5_file_attribution_for_a_test_without_a_tc_id(tmp_path):
    tests = {"test_helper_roundtrip": "fail"}
    root = attr_project(tmp_path / "a", statuses={"1": "Done", "2": "Done"}, tests=tests)
    code, obj = gate(root)
    entry = one(obj, file="tests/test_calc_tc.py", tcId=None)
    assert entry["attributedBy"] == "file"
    assert entry["tasks"] == ["2", "3"]
    assert entry["status"] == "excused" and entry["excusedBy"] == ["3"]
    assert obj["ok"] is True and code == 0

    root = attr_project(tmp_path / "b", statuses=ALL_DONE, tests=tests)
    code, obj = gate(root)
    entry = one(obj, file="tests/test_calc_tc.py", tcId=None)
    assert entry["attributedBy"] == "file" and entry["tasks"] == ["2", "3"]
    assert entry["status"] == "regression"
    assert obj["ok"] is False and code == 1


def test_tc6_test_in_an_unowned_file_is_unattributed(tmp_path):
    root = attr_project(tmp_path, statuses={"1": "Done"}, tests={"tests/test_orphan.py::test_x": "fail"})
    code, obj = gate(root)
    entry = one(obj, file="tests/test_orphan.py")
    assert entry["status"] == "unattributed"
    assert entry["tasks"] == []
    assert not entry.get("excusedBy")
    assert obj["ok"] is False and code == 1


def test_tc7_import_and_collection_failures_are_file_level_entries(tmp_path):
    tests = {
        "tests/test_more.py::test_more_imp": "import-error",
        "tests/test_calc_tc.py::test_calc_syn": "syntax-error",
    }
    root = attr_project(tmp_path, statuses=ALL_DONE, tests=tests)
    code, obj = gate(root)
    imp = one(obj, file="tests/test_more.py")
    col = one(obj, file="tests/test_calc_tc.py")
    assert imp["kind"] == "import" and col["kind"] == "collection"
    for entry in (imp, col):
        assert entry["node"] in (None, entry["file"])
        assert entry["attributedBy"] == "file"
    assert imp["tasks"] == ["7", "8"]
    assert col["tasks"] == ["2", "3"]
    assert obj["ok"] is False and code == 1


def test_tc8_node_test_failure_is_attributed_at_file_level(tmp_path):
    verify = "node --test tests/js/calc.test.mjs"
    extra = extra_test_task("9", "10", verify, ["tests/js/calc.test.mjs"], "Done")
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc1_a": "pass"}, extra_tasks=extra)
    failing = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n\n"
               "test('calc', () => {\n  assert.equal(1, 2);\n});\n")
    write_commit(root, {"tests/js/calc.test.mjs": failing})
    code, obj = gate(root)
    entry = one(obj, file="tests/js/calc.test.mjs")
    assert entry["node"] is None
    assert entry["attributedBy"] == "file"
    assert entry["tasks"] == ["10"]
    assert obj["ok"] is False and code == 1


# --------------------------------------------------------------------------- TC-9, TC-10, TC-11


@pytest.mark.parametrize("all_done", [False, True])
def test_tc9_unreadable_runner_output_falls_back_with_a_warning(tmp_path, all_done):
    verify = "sh -c 'echo boom; exit 1'"
    status = "Done" if all_done else "Not Started"
    extra = extra_test_task("9", "10", verify, ["tests/test_shwrap.py"], status)
    statuses = done("9", "10") if all_done else ALL_DONE
    root = attr_project(tmp_path, statuses=statuses, tests={"test_tc1_a": "pass"}, extra_tasks=extra)
    write_commit(root, {"tests/test_shwrap.py": _TEST_HEADER + pytest_body("test_shwrap_ok", "pass")})
    code, obj = gate(root)
    warnings = " | ".join(obj["warnings"])
    assert "echo boom" in warnings, warnings
    assert "attribution" in warnings and "unavailable" in warnings, warnings
    if all_done:
        assert obj["ok"] is False and code == 1
    else:
        assert obj["ok"] is True and code == 0


def test_tc10_unknown_tc_id_falls_back_to_file_with_a_warning(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc99_x": "fail"})
    code, obj = gate(root)
    entry = one(obj, file="tests/test_more.py")
    assert entry["attributedBy"] == "file"
    assert any("TC-99" in w for w in obj["warnings"]), obj["warnings"]


_LINT_TESTS = '''\
import pytest


def test_tc1_a():
    assert True


def test_helper():
    assert True


class TestX:
    def test_y(self):
        assert True


@pytest.mark.parametrize("p", [1, 2])
def test_tc2_b(p):
    assert p
'''


def test_tc11_lint_warns_about_tests_without_a_tc_id(tmp_path):
    root = attr_project(tmp_path, statuses={}, tests={})
    write_commit(root, {"tests/test_calc_tc.py": _LINT_TESTS})
    code, obj, err = ck_json(root, "lint", "demo", "--json")
    assert isinstance(obj, dict), err
    untagged = obj.get("untaggedTests")
    assert isinstance(untagged, list), f"no untaggedTests in {sorted(obj)}"
    texts = [json.dumps(e) for e in untagged]
    assert len([t for t in texts if "test_helper" in t]) == 1, texts
    assert len([t for t in texts if "test_y" in t]) == 1, texts
    assert not [t for t in texts if "tc1_a" in t or "tc2_b" in t], texts
    assert len(untagged) == 2, texts
    for t in texts:
        assert re.search(r"\b1\b", t), f"owning test task 1 not named: {t}"


# --------------------------------------------------------------------------- TC-12, TC-13


def _strip_timing(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items()
                if not re.search(r"duration|seconds|elapsed|time", k, re.IGNORECASE)}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


def test_tc12_attribution_is_deterministic_under_xdist(tmp_path):
    tests = {"test_tc1_add": "fail", "test_tc2_b": "fail", "test_tc3_sub": "fail", "test_tc5_d": "fail",
             "test_helper_one": "fail", "test_helper_two": "fail", "test_helper_ok": "pass"}
    cfg = {"verify": ["python -m pytest -q -p no:cacheprovider -n 2 tests"]}
    root = attr_project(tmp_path, statuses=DONE_12, tests=tests, config=cfg)
    runs = []
    for _ in range(3):
        code, obj = gate(root)
        assert code == 1 and obj["failures"], obj
        runs.append(_strip_timing(obj["failures"]))
    assert runs[0] == runs[1] == runs[2]


@pytest.mark.parametrize("name", sorted(GATE_BASELINE_SCENARIOS))
def test_tc13_gate_without_attribute_matches_the_baseline(tmp_path, name):
    sc = GATE_BASELINE_SCENARIOS[name]
    root = attr_project(tmp_path, statuses=sc["statuses"], tests=sc["tests"])
    code, out, _ = ck_text(root, "gate", "demo", *sc["args"])
    baseline = (FIXTURES / "gate-text-baseline" / f"{name}.txt").read_text()
    assert f"exit: {code}\n" + normalize_gate_text(out, root) == baseline


def test_tc13_excused_suites_do_not_run_without_attribute(tmp_path):
    sc = GATE_BASELINE_SCENARIOS["excused-unfinished"]
    root = attr_project(tmp_path, statuses=sc["statuses"], tests=sc["tests"])
    code, _, _ = ck_text(root, "gate", "demo")
    assert code == 0
    for name in ("test_tc2_b", "test_tc3_c"):
        assert counter_lines(root, name) == 0, f"{name} ran although its suite is excused"


# --------------------------------------------------------------------------- TC-15 to TC-17


def test_tc15_a_test_that_passes_on_rerun_is_flaky(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc1_add": "flaky"})
    code, obj = gate(root)
    entry = one(obj, tcId="TC-1")
    assert entry["status"] == "flaky"
    assert obj["ok"] is True and code == 0
    assert counter_lines(root, "test_tc1_add") == 2


def test_tc16_a_test_that_fails_twice_is_a_regression(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc1_add": "fail"})
    code, obj = gate(root)
    entry = one(obj, tcId="TC-1")
    assert entry["status"] == "regression"
    assert obj["ok"] is False and code == 1
    assert counter_lines(root, "test_tc1_add") == 2


def test_tc17_only_candidates_are_rerun(tmp_path):
    passing = ["test_tc2_b", "test_tc5_d", "test_p1", "test_p2", "test_p3"]
    tests = {"test_tc3_c": "fail", "test_tc1_a": "fail", **{n: "pass" for n in passing}}
    root = attr_project(tmp_path, statuses=DONE_12 | {"6": "Done", "7": "Done", "8": "Done"}, tests=tests)
    code, obj = gate(root)
    assert one(obj, tcId="TC-3")["status"] == "excused"
    assert one(obj, tcId="TC-1")["status"] == "regression"
    assert counter_lines(root, "test_tc3_c") == 1, "an excused test must not be rerun"
    assert counter_lines(root, "test_tc1_a") == 2, "a candidate runs twice"
    for name in passing:
        assert counter_lines(root, name) == 1, f"{name} ran {counter_lines(root, name)} times"


# --------------------------------------------------------------------------- TC-26b, TC-27b


def test_tc26b_landing_gate_stays_task_scoped(tmp_path):
    tests = {"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "fail"}
    root = attr_project(tmp_path, statuses={"1": "Done"}, tests=tests)
    code, _, err = ck_text(root, "worktree", "create", "demo", "2")
    assert code == 0, err
    wt = task_worktree(root, 2)
    tasks = wt / "specs" / "demo" / "tasks.md"
    tasks.write_text(tasks.read_text().replace("- [ ] add works", "- [x] add works"))
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "task 2")
    code, obj, err = ck_json(root, "worktree", "land", "demo", "2", "--gate", "--json")
    assert isinstance(obj, dict), err
    by = {t["task"]: t for t in obj["tasks"]}
    assert by["2"]["state"] == "MERGED", json.dumps(obj, indent=1)


def test_tc27b_final_gate_excuses_nothing(tmp_path):
    tests = {"test_tc1_a": "fail", "test_tc3_c": "fail", "tests/test_orphan.py::test_x": "fail"}
    root = attr_project(tmp_path, statuses=ALL_DONE, tests=tests)
    code, obj = gate(root)
    assert obj["failures"], obj
    assert not entries(obj, status="excused")
    assert obj["ok"] is False and code == 1


# --------------------------------------------------------------------------- TC-193, TC-195


_E2E_TESTS = _TEST_HEADER + (
    pytest_body("test_tc1_a", "pass") + "\n\n"
    + "@pytest.mark.e2e\n" + pytest_body("test_tc2_e2e_flow", "fail") + "\n\n"
    + pytest_body("test_tc3_c", "pass")
)


def test_tc193_deselected_e2e_node_is_not_treated_as_covered(tmp_path):
    cfg = {"verify": ['python -m pytest -q -p no:cacheprovider -m "not e2e" tests']}
    root = attr_project(tmp_path, statuses={"1": "Done", "2": "Done", "3": "Done"}, tests={}, config=cfg)
    write_commit(root, {"tests/test_calc_tc.py": _E2E_TESTS,
                        "pytest.ini": "[pytest]\nmarkers =\n    e2e: end to end\n"})
    code, obj = gate(root)
    entry = one(obj, tcId="TC-2")
    assert entry["status"] == "regression" and entry["tasks"] == ["2"]
    assert obj["ok"] is False and code == 1
    assert counter_lines(root, "test_tc2_e2e_flow") == 2  # once in the attributed run, once in the rerun
    assert counter_lines(root, "test_tc1_a") == 1
    assert counter_lines(root, "test_tc3_c") == 1


_WRAP = '''\
import pytest


@pytest.mark.parametrize("name", {names!r})
def test_run_suite(name):
    assert name != {bad!r}, "suite " + name + " failed"
'''

_THIN_JS = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n\n"
            "test('thin', () => {\n  assert.equal(1, 1);\n});\n")


def _wrap_project(tmp_path: Path, statuses: Dict[str, str], names: List[str], bad: str) -> Path:
    root = attr_project(tmp_path, statuses=statuses, tests={"test_tc1_a": "pass"})
    tasks = root / "specs" / "demo" / "tasks.md"
    text = tasks.read_text()
    anchor = "- `tests/test_more.py` - more behaviour\n"
    assert anchor in text
    tasks.write_text(text.replace(anchor, anchor + "- `tests/js/thin.test.mjs` - thin suite\n"))
    write_commit(root, {"specs/demo/tasks.md": tasks.read_text(), "tests/js/thin.test.mjs": _THIN_JS,
                        "tests/test_wrap.py": _WRAP.format(names=names, bad=bad)})
    return root


@pytest.mark.parametrize("statuses", [done(minus=("8",)), ALL_DONE], ids=["task8-open", "all-done"])
def test_tc195_wrapper_param_names_an_owned_file(tmp_path, statuses):
    root = _wrap_project(tmp_path, statuses, ["calc.test.mjs", "thin.test.mjs"], "thin.test.mjs")
    code, obj = gate(root)
    entry = one(obj, file="tests/test_wrap.py")
    assert entry["status"] == "unattributed" and entry["attributedBy"] == "none"
    assert entry["tasks"] == [] and not entry.get("excusedBy")
    assert "tests/js/thin.test.mjs" in entry["hint"]
    assert re.search(r"\b6\b", entry["hint"]), entry["hint"]
    assert obj["ok"] is False and code == 1


def test_tc195_wrapper_param_naming_no_owned_file_has_no_hint(tmp_path):
    root = _wrap_project(tmp_path, ALL_DONE, ["other.test.mjs"], "other.test.mjs")
    code, obj = gate(root)
    entry = one(obj, file="tests/test_wrap.py")
    assert entry["status"] == "unattributed" and entry["attributedBy"] == "none"
    assert "hint" not in entry
    assert obj["ok"] is False and code == 1
