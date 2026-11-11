"""The structured failure format in `ck gate --json`, `ck gate --attribute --json` and the land JSON (Story 3).

TC-28 to TC-32, TC-36 (CLI sources), TC-37, TC-183 and TC-191. Everything runs the real ck CLI in a subprocess on
real git repos with real pytest. Every failure object is validated against `fixtures/unattended/failure.schema.json`.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from unattended_helpers import (
    ALL_DONE,
    FIXTURES,
    attr_project,
    capture_land_keys,
    ck_json,
    commit_all,
    failure_errors,
    init_repo,
    ck_text,
    task_worktree,
)

# --------------------------------------------------------------------------- helpers


def put(root: Path, rel: str, text: str) -> None:
    """Write a file in the project and commit it."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    commit_all(root, f"add {rel}")


def gate_json(root: Path, *args: str):
    """`ck gate demo ARGS... --json`: (exit code, object, stderr), the object asserted to be JSON."""
    code, obj, err = ck_json(root, "gate", "demo", *args, "--json")
    assert isinstance(obj, dict), f"ck gate did not print JSON (exit {code}): {err[-500:]}"
    return code, obj, err


def check_entries(failures) -> None:
    assert isinstance(failures, list)
    for entry in failures:
        assert failure_errors(entry) == [], (entry, failure_errors(entry))


def clear_counters(tmp_path: Path) -> None:
    shutil.rmtree(tmp_path / "counters", ignore_errors=True)
    (tmp_path / "counters").mkdir()


CALC_HEAD = "import pytest\n\n\n"


def single(tmp_path: Path, body: str, *, config=None, statuses=None):
    """A project whose only generated test file is tests/test_calc_tc.py with `body`."""
    root = attr_project(tmp_path, statuses=statuses or ALL_DONE, tests={}, config=config)
    put(root, "tests/test_calc_tc.py", CALC_HEAD + body)
    return root


# --------------------------------------------------------------------------- TC-28


def test_tc28_gate_json_one_entry_per_failing_test(tmp_path):
    root = single(
        tmp_path,
        "def test_tc1_assertion():\n    assert 1 == 2\n\n\n"
        "def test_tc1_value():\n    raise ValueError('boom')\n\n\n"
        f"def test_tc2_msg():\n    assert False, {'m' * 40!r}\n\n\n"
        "def test_tc2_ok():\n    assert True\n",
        statuses={},
    )
    code, obj, _ = gate_json(root, "--task", "2")
    assert code == 1 and obj["ok"] is False
    failures = obj["failures"]
    assert len(failures) == 3, failures
    assert {f["node"].split("::")[-1] for f in failures} == {"test_tc1_assertion", "test_tc1_value", "test_tc2_msg"}
    for f in failures:
        for key in ("node", "tcId", "tasks", "file", "kind", "message", "truncated"):
            assert key in f, (key, f)
    check_entries(failures)


# --------------------------------------------------------------------------- TC-29

KIND_CASES = {
    "bare-assert": ("def test_tc1_x():\n    assert 1 == 2\n", "assertion"),
    "assertionerror-subclass": (
        "class Mine(AssertionError):\n    pass\n\n\ndef test_tc1_x():\n    raise Mine('subclass')\n", "assertion"),
    "named-assertionerror-not-subclass": (
        "class MyAssertionError(Exception):\n    pass\n\n\ndef test_tc1_x():\n    raise MyAssertionError('no')\n",
        "error"),
    "module-importerror": ("raise ImportError('cannot import')\n\n\ndef test_tc1_x():\n    assert True\n", "import"),
    "module-modulenotfound": (
        "import module_that_does_not_exist_anywhere  # noqa: F401\n\n\ndef test_tc1_x():\n    assert True\n",
        "import"),
    "syntaxerror": ("def test_tc1_x(:\n    pass\n", "collection"),
    "setup-error": (
        "@pytest.fixture\ndef boom():\n    raise RuntimeError('setup boom')\n\n\n"
        "def test_tc1_x(boom):\n    assert True\n", "error"),
    "teardown-error": (
        "@pytest.fixture\ndef boom():\n    yield 1\n    raise RuntimeError('teardown boom')\n\n\n"
        "def test_tc1_x(boom):\n    assert True\n", "error"),
    "runtimeerror": ("def test_tc1_x():\n    raise RuntimeError('rt')\n", "error"),
}


@pytest.mark.parametrize("name", sorted(KIND_CASES))
def test_tc29_kind_table(tmp_path, name):
    body, expected = KIND_CASES[name]
    root = single(tmp_path, body)
    code, obj, _ = gate_json(root)
    assert code == 1 and obj["ok"] is False
    assert obj["failures"], "a real pytest failure must produce an entry"
    check_entries(obj["failures"])
    assert {f["kind"] for f in obj["failures"]} == {expected}, obj["failures"]


def test_tc29_kind_verify_timeout(tmp_path):
    root = single(tmp_path, "import time\n\n\ndef test_tc1_x():\n    time.sleep(60)\n", config={"verify_timeout": 3})
    code, obj, _ = gate_json(root)
    assert code == 1 and obj["ok"] is False
    check_entries(obj["failures"])
    assert "timeout" in {f["kind"] for f in obj["failures"]}, obj["failures"]


def test_tc29_kind_pytest_fail(tmp_path):
    root = single(tmp_path, "def test_tc1_x():\n    pytest.fail('explicit')\n")
    code, obj, _ = gate_json(root)
    assert code == 1
    assert len(obj["failures"]) == 1
    check_entries(obj["failures"])  # the kind is one of the five (schema enum)


# --------------------------------------------------------------------------- TC-30


def test_tc30_passing_gate_has_empty_failures(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE,
                        tests={"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "pass", "test_tc5_d": "pass"})
    code, obj, _ = gate_json(root)
    assert code == 0 and obj["ok"] is True
    assert obj["failures"] == []


def test_tc30_unchecked_acceptance_box_is_not_a_failure(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE,
                        tests={"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "pass", "test_tc5_d": "pass"})
    tasks = root / "specs" / "demo" / "tasks.md"
    text = tasks.read_text()
    assert "- [x] add works" in text
    put(root, "specs/demo/tasks.md", text.replace("- [x] add works", "- [ ] add works"))
    code, obj, _ = gate_json(root)
    assert code == 1 and obj["ok"] is False
    assert obj["failures"] == []


# --------------------------------------------------------------------------- TC-31

ATTR_STATUSES = {"regression", "excused", "flaky", "unattributed"}
MIXED = {"test_tc1_a": "pass", "test_tc2_b": "fail", "test_tc3_c": "fail", "test_tc5_d": "pass"}


def test_tc31_status_only_with_attribute(tmp_path):
    # Tasks 2 and 3 Done too, so Task 1's suite is due and the plain gate (1.11: today's selection) runs it.
    root = attr_project(tmp_path, statuses={"1": "Done", "2": "Done", "3": "Done"}, tests=MIXED)
    _, with_attr, _ = gate_json(root, "--attribute")
    assert with_attr["failures"]
    for f in with_attr["failures"]:
        assert f["status"] in ATTR_STATUSES, f
    check_entries(with_attr["failures"])
    clear_counters(tmp_path)
    _, plain, _ = gate_json(root)
    assert plain["failures"]
    for f in plain["failures"]:
        assert "status" not in f, f


# --------------------------------------------------------------------------- TC-32


def test_tc32_long_message_is_truncated_not_dropped(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc1_big": "assert-msg:5000"})
    _, obj, _ = gate_json(root)
    assert len(obj["failures"]) == 1
    f = obj["failures"][0]
    assert len(f["message"]) <= 500
    assert f["truncated"] is True
    check_entries(obj["failures"])


def test_tc32_short_message_is_not_truncated(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={"test_tc1_mid": "assert-msg:300"})
    _, obj, _ = gate_json(root)
    assert len(obj["failures"]) == 1
    assert obj["failures"][0]["truncated"] is False
    check_entries(obj["failures"])


def test_tc32_many_failures_are_all_listed(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={f"test_tc1_n{i:03d}": "fail" for i in range(120)})
    _, obj, _ = gate_json(root)
    assert len(obj["failures"]) == 120
    assert len({f["node"] for f in obj["failures"]}) == 120
    check_entries(obj["failures"])


# --------------------------------------------------------------------------- TC-36 (CLI sources)


def test_tc36_gate_json_entries_validate(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={
        "test_tc1_a": "fail", "test_tc2_b": "assert-msg:900", "broken": "import-error", "test_tc3_c": "setup-error"})
    _, obj, _ = gate_json(root)
    assert obj["failures"]
    check_entries(obj["failures"])


def test_tc36_gate_attribute_entries_validate(tmp_path):
    root = attr_project(tmp_path, statuses={"1": "Done"}, tests={
        "test_tc1_a": "pass", "test_tc2_b": "fail", "test_tc3_c": "fail", "test_tc5_d": "flaky", "broken": "import-error"})
    _, obj, _ = gate_json(root, "--attribute")
    assert obj["failures"]
    check_entries(obj["failures"])


def test_tc36_land_entries_validate(tmp_path):
    root = init_repo(tmp_path / "landpy")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n__pycache__/\n.pytest_cache/\n")
    (root / "specs" / "demo").mkdir(parents=True)
    (root / "specs" / "demo" / "tasks.md").write_text(
        "# Implementation Tasks: Demo\n\n### Task 1: one\n**Status:** Not Started\n"
        "**Verify:** `python -m pytest -q -p no:cacheprovider tests/test_one.py`\n")
    commit_all(root, "base")
    code, _, err = ck_text(root, "worktree", "create", "demo", "1")
    assert code == 0, err
    wt = task_worktree(root, 1)
    (wt / "tests").mkdir()
    (wt / "tests" / "test_one.py").write_text("def test_tc1_one():\n    assert 1 == 2\n\n\ndef test_tc1_two():\n    raise ValueError('v')\n")
    commit_all(wt, "task 1")
    code, obj, err = ck_json(root, "worktree", "land", "demo", "1", "--gate", "--json")
    assert isinstance(obj, dict), (code, err)
    entries = [f for t in obj["tasks"] for f in t.get("failures", [])]
    assert len(entries) == 2, obj["tasks"]
    check_entries(entries)


# --------------------------------------------------------------------------- TC-37


def test_tc37_land_json_keeps_every_key_path(tmp_path):
    baseline = json.loads((FIXTURES / "land-json-keys.json").read_text())
    now = capture_land_keys(tmp_path)
    for scenario, paths in baseline["scenarios"].items():
        missing = sorted(set(paths) - set(now[scenario]))
        assert missing == [], f"{scenario}: land --json lost key paths {missing}"


# --------------------------------------------------------------------------- TC-183


COMPACT_TESTS = {
    "test_tc1_f": "flaky",      # fails once, passes on the rerun: flaky
    "test_tc2_b": "fail",       # Task 2 is Done: regression
    "test_tc3_c": "fail",       # Task 3 unfinished: excused
    "test_tc3_d": "fail",
    "test_tc5_e": "fail",       # Tasks 7 and 8 unfinished: excused by both
}
COMPACT_STATUSES = {"1": "Done", "2": "Done", "6": "Done"}


def strip_timing(obj):
    return {k: v for k, v in obj.items() if k not in ("durationS", "report")}


def test_tc183_compact_form(tmp_path):
    root = attr_project(tmp_path, statuses=COMPACT_STATUSES, tests=COMPACT_TESTS)
    code_full, full, _ = gate_json(root, "--attribute")
    clear_counters(tmp_path)
    code, compact, err = ck_json(root, "gate", "demo", "--attribute", "--json", "--compact")
    assert isinstance(compact, dict), err
    assert compact["schema"] == "ck-gate-compact/1"
    assert compact["ok"] == full["ok"] and code == code_full
    for key in ("regressions", "flaky", "unattributed"):
        check_entries(compact[key])
    assert len(compact["regressions"]) == 1
    assert len(compact["flaky"]) == 1
    excused = [f for f in full["failures"] if f["status"] == "excused"]
    assert len(excused) == 3
    expected = {}
    for f in excused:
        for task in f["excusedBy"]:
            expected[task] = expected.get(task, 0) + 1
    assert compact["excusedByTask"] == expected
    report = Path(compact["report"])
    assert report.is_file()
    saved = json.loads(report.read_text())
    assert saved["schema"] == "ck-gate/1"
    assert len(saved["failures"]) == 5
    clear_counters(tmp_path)
    _, again, _ = gate_json(root, "--attribute")
    assert strip_timing(saved) == strip_timing(again)


def test_tc183_green_compact_is_empty(tmp_path):
    root = attr_project(tmp_path, statuses=ALL_DONE,
                        tests={"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "pass", "test_tc5_d": "pass"})
    code, compact, err = ck_json(root, "gate", "demo", "--attribute", "--json", "--compact")
    assert isinstance(compact, dict), err
    assert code == 0 and compact["ok"] is True
    assert compact["regressions"] == [] and compact["flaky"] == [] and compact["unattributed"] == []
    assert compact["excusedByTask"] == {}


# --------------------------------------------------------------------------- TC-191

NESTED = '''\
import os
import subprocess
import sys


def test_tc1_nested(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "test_inner.py").write_text("def test_tc2_inner():\\n    assert 1 == 2\\n")
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py"],
                         cwd=inner, env=dict(os.environ), capture_output=True, text=True)
    assert out.returncode == 1, out.stdout
'''


@pytest.mark.parametrize("workers", [None, 2], ids=["serial", "xdist-2"])
def test_tc191_nested_pytest_failure_is_not_reported(tmp_path, workers):
    flag = f"-n {workers} " if workers else ""
    root = attr_project(tmp_path, statuses=ALL_DONE, tests={},
                        config={"verify": [f"python -m pytest -q {flag}-p no:cacheprovider tests"]})
    put(root, "tests/test_calc_tc.py", NESTED)
    code, obj, _ = gate_json(root, "--attribute")
    assert code == 0 and obj["ok"] is True, obj
    assert obj["failures"] == []
