"""Red-team for the SHALL NOT clauses of Story 12 (run-principles 12.9 and 12.10), through `ck lint`.

12.9:  a Verify whose selection cannot be resolved statically is reported as not checked, and does not block.
12.10: a problem an existing lint rule already reports is not reported a second time.

Every case runs the real `ck lint` over a real tasks.md and real test files. The positive controls prove the rule
fires on the same shape, so a lint that reports nothing cannot satisfy both the controls and the negatives.
"""

import json
import shutil
from pathlib import Path

import pytest

from test_lint import block, ck, write_spec

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
A = "tests/test_a.py"
B = "tests/test_b.py"
PYTEST = "uv run pytest -q"


def files(root: Path) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / A).write_text("def test_core():\n    assert True\n\n\ndef test_extra():\n    assert True\n")
    (root / B).write_text("def test_b():\n    assert True\n")


def shape(x_verify=f"{PYTEST} {A}") -> str:
    """Task 1's gate runs the shared suite A unfiltered; A also serves Task 4, which verifies the not-Done Task 3."""
    out = block(1, "Core", files=["src/a.py"], verify=x_verify)
    out += block(2, "Core tests", track="test", files=[A], verifies="Task 1")
    out += block(3, "Extra", files=["src/b.py"], verify=f"{PYTEST} {B}")
    out += block(4, "Extra tests", track="test", files=[A, B], verifies="Task 3")
    return out


def lint(root: Path, *extra: str):
    out = ck(root, "lint", "sim", "--json", *extra)
    text = out.output
    data = json.loads(text[text.index("{"):])
    return out.exit_code, data


def scope(data) -> list:
    return list(data.get("verifyScope") or [])


def unchecked(data, task: str) -> list:
    return [e for e in (data.get("notChecked") or []) if str(e.get("task")) == task]


def test_positive_control_the_shared_suite_fires(tmp_path):
    files(tmp_path)
    write_spec(tmp_path, shape())
    code, data = lint(tmp_path)
    assert [(f["task"], f["suite"]) for f in scope(data)] == [("1", A)]
    assert code == 1
    assert unchecked(data, "1") == []


# --------------------------------------------------------------------------- 12.9: unresolvable selections

UNRESOLVABLE = [
    pytest.param(f'{PYTEST} "$(ls {A})"', id="command-substitution"),
    pytest.param(f"{PYTEST} `ls {A}`", id="backticks"),
    pytest.param(f"{PYTEST} $TARGET", id="shell-var"),
    pytest.param(f"{PYTEST} ${{TARGET}}", id="shell-var-braces"),
    pytest.param(f"{PYTEST} tests/$FILE", id="var-inside-path"),
    pytest.param(f'{PYTEST} "tests/test_$(echo a).py"', id="substitution-in-quoted-path"),
    pytest.param(f"{PYTEST} tests/test_*.py", id="glob-star"),
    pytest.param(f"{PYTEST} tests/test_?.py", id="glob-question"),
    pytest.param(f"{PYTEST} tests/test_[ab].py", id="glob-class"),
    pytest.param(f'{PYTEST} {A} -k "not (core"', id="unbalanced-k"),
    pytest.param(f'{PYTEST} {A} -k "$EXPR"', id="k-from-shell-var"),
    pytest.param(f"{PYTEST} {A} -k $(echo core)", id="k-from-substitution"),
    pytest.param(f"{PYTEST} {A} -k", id="k-without-value"),
    pytest.param(f"{PYTEST} tests", id="directory"),
    pytest.param(f"{PYTEST} tests/", id="directory-trailing-slash"),
    pytest.param(f"{PYTEST} .", id="dot-directory"),
    pytest.param("pytest", id="bare-pytest"),
    pytest.param("uv run pytest", id="bare-uv-pytest"),
    pytest.param(f'{PYTEST} "{A}', id="unterminated-quote"),
    pytest.param(f"{PYTEST} '{A[:-3]}$X.py'", id="single-quoted-literal-dollar"),
    pytest.param("bash scripts/run_tests.sh", id="script"),
    pytest.param("make test", id="make-target"),
    pytest.param(f"{PYTEST} tests/test_missing.py", id="file-not-on-disk"),
    pytest.param(f"{PYTEST} tests/Test_A.py", id="case-near-miss-not-on-disk"),
    pytest.param(f"{PYTEST} tests/test_a.pyc", id="extension-near-miss-not-on-disk"),
    pytest.param(f"{PYTEST} tests/sub/../test_missing.py", id="dotdot-not-on-disk"),
]


@pytest.mark.parametrize("verify", UNRESOLVABLE)
def test_129_unresolvable_selection_is_not_checked_and_does_not_block(tmp_path, verify):
    files(tmp_path)
    write_spec(tmp_path, shape(x_verify=verify))
    code, data = lint(tmp_path)
    assert unchecked(data, "1"), f"Task 1 must be reported as not checked: {data}"
    assert [f for f in scope(data) if f["task"] == "1"] == [], "an unresolved selection must not become a finding"
    assert code == 0, f"not checked must not set the exit: {data}"


@pytest.mark.parametrize("verify", [pytest.param(f"{PYTEST} $TARGET", id="shell-var"), pytest.param(f"{PYTEST} tests", id="directory"), pytest.param(f"{PYTEST} tests/test_missing.py", id="not-on-disk")])
def test_129_unchecked_task_does_not_hide_findings_of_other_tasks(tmp_path, verify):
    """Not checked is per task: Task 3 stays checked, and an unchecked Task 1 leaves lint's text and exit alone."""
    files(tmp_path)
    write_spec(tmp_path, shape(x_verify=verify) + block(5, "Other", files=["src/c.py"], verify=f"{PYTEST} {A}"))
    code, data = lint(tmp_path)
    assert unchecked(data, "3") == []
    assert unchecked(data, "1") and unchecked(data, "5") == []
    # A is shared by test tasks 2 and 4, so Task 5 may carry one finding per sharing test task (design: ScopeFinding.testTask).
    assert {(f["task"], f["suite"]) for f in scope(data)} == {("5", A)}, data
    assert {f["testTask"] for f in scope(data)} == {"2", "4"}, data
    text = ck(tmp_path, "lint", "sim")
    assert text.exit_code == 1, text.output


@pytest.mark.parametrize("verify", [pytest.param(f'{PYTEST} "{A}"', id="double-quoted"), pytest.param(f"{PYTEST} '{A}'", id="single-quoted"), pytest.param(f"{PYTEST} -x {A}", id="flag-first"), pytest.param(f"python -m pytest -q {A}", id="python-m")])
def test_129_resolvable_quoting_is_still_checked(tmp_path, verify):
    """Quoting that only looks unresolvable must not turn a checkable selection into an unchecked one."""
    files(tmp_path)
    write_spec(tmp_path, shape(x_verify=verify))
    code, data = lint(tmp_path)
    assert unchecked(data, "1") == []
    assert [(f["task"], f["suite"]) for f in scope(data)] == [("1", A)]
    assert code == 1


def test_129_file_not_on_disk_yet_is_checked_once_it_exists(tmp_path):
    """The same Verify is not checked while the file is missing and checked after it is written."""
    (tmp_path / "tests").mkdir()
    write_spec(tmp_path, shape())
    code, data = lint(tmp_path)
    assert unchecked(data, "1") and code == 0
    files(tmp_path)
    code, data = lint(tmp_path)
    assert unchecked(data, "1") == []
    assert [(f["task"], f["suite"]) for f in scope(data)] == [("1", A)]


def test_129_unchecked_task_is_absent_from_plan_verify_scope(tmp_path):
    files(tmp_path)
    write_spec(tmp_path, shape(x_verify=f"{PYTEST} $TARGET"))
    out = ck(tmp_path, "plan", "sim", "--json")
    data = json.loads(out.output[out.output.index("{"):])
    assert data.get("verifyScope") == []


def test_129_unchecked_entry_names_task_and_command(tmp_path):
    files(tmp_path)
    verify = f"{PYTEST} $TARGET"
    write_spec(tmp_path, shape(x_verify=verify))
    _, data = lint(tmp_path)
    entry = unchecked(data, "1")[0]
    assert verify in entry["command"] and entry["reason"]


# --------------------------------------------------------------------------- 12.10: no second report

PERF = "tests/test_perf.py"


def cycle_shape(extra_shared: bool = False) -> str:
    """Task 8's Verify runs PERF unfiltered. Test task 9 verifies Task 8; test task 10 owns PERF too and verifies
    Task 13, which depends on Task 8. The verify-file rule reports (8, PERF); Story 12 sees the same problem.
    With extra_shared, Task 8 also runs A unfiltered; A is owned by test task 12 (verifies Task 8) and by test task 11, which verifies the not-Done Task 14
    (no dependency on Task 8), so A is shared and (8, A) is a Story-12-only problem."""
    out = block(8, "Perf core", files=["src/perf.py"], verify=f"{PYTEST} {PERF}" + (f" {A}" if extra_shared else ""))
    out += block(9, "Perf core tests", track="test", files=[PERF], verifies="Task 8")
    out += block(10, "Report tests", track="test", files=[PERF], verifies="Task 13")
    out += block(13, "Report", files=["src/report.py"], deps="Task 8 (uses perf)")
    if extra_shared:
        out += block(14, "Other", files=["src/other.py"], verify=f"{PYTEST} {B}")
        out += block(11, "Other tests", track="test", files=[A, B], verifies="Task 14")
        out += block(12, "Perf extra tests", track="test", files=[A], verifies="Task 8")
    return out


def perf_files(root: Path) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / PERF).write_text("def test_perf():\n    assert True\n")
    files(root)


def pairs(data) -> list:
    out = [(str(e["task"]), e["file"]) for e in data.get("verifyCycles", []) if e.get("kind") == "verify-file"]
    return out + [(str(f["task"]), f["suite"]) for f in scope(data)]


def test_1210_control_verify_file_rule_fires(tmp_path):
    perf_files(tmp_path)
    write_spec(tmp_path, cycle_shape())
    code, data = lint(tmp_path)
    assert ("8", PERF) in [(str(e["task"]), e["file"]) for e in data["verifyCycles"]]
    assert code == 1


def test_1210_problem_in_verify_cycles_is_not_repeated_in_verify_scope(tmp_path):
    perf_files(tmp_path)
    write_spec(tmp_path, cycle_shape())
    code, data = lint(tmp_path)
    assert pairs(data).count(("8", PERF)) == 1, data
    assert [f for f in scope(data) if f["task"] == "8" and f["suite"] == PERF] == []
    assert code == 1


def test_1210_text_output_names_the_problem_once(tmp_path):
    perf_files(tmp_path)
    write_spec(tmp_path, cycle_shape())
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 1
    lines = [ln for ln in out.output.splitlines() if "Task 8" in ln and PERF in ln]
    assert len(lines) == 1, out.output


def test_1210_dedupe_is_per_problem_not_per_task(tmp_path):
    """Task 8 has two problems: (8, PERF) is a verify-file cycle, (8, A) is a shared suite only Story 12 reports.
    Deduping the first must not drop the second."""
    perf_files(tmp_path)
    write_spec(tmp_path, cycle_shape(extra_shared=True))
    code, data = lint(tmp_path)
    cycle_pairs = [(str(e["task"]), e["file"]) for e in data["verifyCycles"] if e.get("kind") == "verify-file"]
    scope_pairs = [(str(f["task"]), f["suite"]) for f in scope(data)]
    assert cycle_pairs.count(("8", PERF)) == 1, data
    assert ("8", PERF) not in scope_pairs, data
    assert scope_pairs.count(("8", A)) == 1, data
    assert ("8", A) not in cycle_pairs, data
    assert code == 1


def test_1210_dependency_cycle_is_reported_once(tmp_path):
    """The same shape under a dependency cycle: `cycles` reports the cycle once (the scope finding is a different problem)."""
    files(tmp_path)
    blocks = block(1, "Core", files=["src/a.py"], deps="Task 3", verify=f"{PYTEST} {A}")
    blocks += block(2, "Core tests", track="test", files=[A], verifies="Task 1")
    blocks += block(3, "Extra", files=["src/b.py"], deps="Task 1", verify=f"{PYTEST} {B}")
    blocks += block(4, "Extra tests", track="test", files=[A, B], verifies="Task 3")
    write_spec(tmp_path, blocks)
    code, data = lint(tmp_path)
    assert len(data["cycles"]) == 1, data
    assert code == 1


def test_1210_each_task_and_suite_occurs_once_in_verify_scope(tmp_path):
    """Two test tasks make A shared with Task 1: one finding per (task, suite, testTask), none repeated.
    design.md: ScopeFinding.testTask names the test task that makes the suite shared, and scopeClaims groups them."""
    files(tmp_path)
    blocks = shape()
    blocks += block(5, "More", files=["src/c.py"], verify=f"{PYTEST} {B}")
    blocks += block(6, "More tests", track="test", files=[A], verifies="Task 5")
    write_spec(tmp_path, blocks)
    _, data = lint(tmp_path)
    got = [(f["task"], f["suite"], f["testTask"]) for f in scope(data)]
    assert len(got) == len(set(got)), got
    assert {t for (task, suite, t) in got if (task, suite) == ("1", A)} == {"4", "6"}, got


def test_1210_json_output_fed_back_is_stable(tmp_path):
    """The system's own output as input: a second run, and a run over the unchanged spec after reading the output."""
    perf_files(tmp_path)
    write_spec(tmp_path, cycle_shape(extra_shared=True))
    first = ck(tmp_path, "lint", "sim", "--json").output
    second = ck(tmp_path, "lint", "sim", "--json").output
    assert first == second


def test_1210_this_specs_own_tasks_md_reports_no_problem_twice(tmp_path):
    """This spec's tasks.md fed back in: no (task, suite, testTask) is in verifyScope twice, and no (task, suite) is in both verifyScope and verifyCycles."""
    shutil.copytree(REPO / "specs/run-principles", tmp_path / "specs/run-principles")
    shutil.copytree(REPO / "tests", tmp_path / "tests", ignore=shutil.ignore_patterns("__pycache__", "netguard"))
    out = ck(tmp_path, "lint", "run-principles", "--json")
    data = json.loads(out.output[out.output.index("{"):])
    assert "verifyScope" in data, "lint must carry the verifyScope key"
    scope_pairs = [(str(f["task"]), f["suite"]) for f in scope(data)]
    triples = [(str(f["task"]), f["suite"], str(f["testTask"])) for f in scope(data)]
    assert len(triples) == len(set(triples)), triples
    cycle_pairs = {(str(e["task"]), e["file"]) for e in data.get("verifyCycles", []) if e.get("kind") == "verify-file"}
    assert not (set(scope_pairs) & cycle_pairs), set(scope_pairs) & cycle_pairs
    for entry in data.get("notChecked", []):
        assert entry["task"] and entry["command"] and entry["reason"]
