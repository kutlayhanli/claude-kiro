"""Properties coverage in `ck gate`: every P-n in test-plan.md's ## Properties section
must be a key of some test file's PROPERTIES map. Driven through the real CLI
against a real git project and real pytest runs."""

import json
import os
from pathlib import Path

from click.testing import CliRunner

from claude_kiro.cli.main import cli
from claude_kiro.hooks._shared.test_heuristics import weakening

from conftest import git

TEST_PLAN = """# Test Plan: Calc

## Test Cases
| ID | Criterion | Level | Scenario | Real boundary exercised | Test file |
| TC-1 | 1.1 | UNIT | add works | none | `tests/test_props.py` |

## Properties
| ID | Requirement | Property | Generator domain | Expected decision |
|----|-------------|----------|------------------|-------------------|
| P-1 | 1.1 | for any a, b: add(a, b) == add(b, a) | ints | equal |
| P-2 | 1.2 | for any a: add(a, 0) == a | ints, including 0 and negatives | equal |

## Commands
`python -m pytest -q tests/test_props.py`
"""

TASKS = """# Implementation Tasks: Calc

**Status:** In Progress

## Task Breakdown

### Task 1: Property tests for add
**Status:** {t1}
**Track:** test
**Properties:** {props}
**Verifies:** Task 2
**Files:**
- `tests/test_props.py` - properties of add
**Verify:** `python -m pytest -q tests/test_props.py`

**Acceptance:**
- [x] Properties written

---

### Task 2: Implement add
**Status:** Done
**Track:** impl
**Verified by:** Task 1
**Files:**
- `calc.py` - add()

**Acceptance:**
- [x] add() implemented
"""

PROPS_TEST = """{map}


def test_add_commutes():
    from calc import add

    for a in range(-3, 4):
        for b in range(-3, 4):
            assert add(a, b) == add(b, a)


def test_add_zero_identity():
    from calc import add

    for a in range(-3, 4):
        assert add(a, 0) == a
"""

BOTH = 'PROPERTIES = {"P-1": "test_add_commutes", "P-2": "test_add_zero_identity"}'
ONLY_P1 = 'PROPERTIES = {"P-1": "test_add_commutes"}'


def setup_spec(project: Path, mode=None, plan=TEST_PLAN, props_map=BOTH, t1="Done", props="P-1, P-2") -> None:
    spec = project / "specs" / "calc"
    if plan is not None:
        (spec / "test-plan.md").write_text(plan)
    (spec / "tasks.md").write_text(TASKS.format(t1=t1, props=props))
    if props_map is not None:
        (project / "tests" / "test_props.py").write_text(PROPS_TEST.format(map=props_map))
    config = {"verify": ["python -m pytest -q tests/test_calc.py"]}
    if mode:
        config["properties"] = mode
    (project / "specs" / "ck.json").write_text(json.dumps(config))


def ck_gate(project: Path, *args: str):
    old = os.getcwd()
    os.chdir(project)
    try:
        return CliRunner().invoke(cli, ["gate", "calc", *args])
    finally:
        os.chdir(old)


def test_all_properties_mapped_passes(project):
    setup_spec(project, mode="required")
    result = ck_gate(project)
    assert result.exit_code == 0, result.output
    assert "Properties covered" in result.output


def test_missing_property_warns_by_default(project):
    setup_spec(project, props_map=ONLY_P1)
    result = ck_gate(project)
    assert result.exit_code == 0, result.output
    assert "⚠ Properties covered" in result.output
    assert "P-2" in result.output
    assert "P-1" not in result.output.split("Properties covered", 1)[1]


def test_missing_property_fails_when_required(project):
    setup_spec(project, mode="required", props_map=ONLY_P1)
    result = ck_gate(project)
    assert result.exit_code == 1, result.output
    assert "✗ Properties covered" in result.output
    assert "P-2" in result.output


def test_map_naming_a_test_that_does_not_exist_counts_as_missing(project):
    setup_spec(project, mode="required", props_map='PROPERTIES = {"P-1": "test_add_commutes", "P-2": "test_gone"}')
    result = ck_gate(project)
    assert result.exit_code == 1, result.output
    assert "test_gone" in result.output


def test_map_in_an_unlisted_js_test_file_counts(project):
    setup_spec(project, mode="required", props_map=ONLY_P1, props="P-1")
    (project / "tests" / "add.test.mjs").write_text(
        "export const PROPERTIES = {\n  'P-2': 'adding zero is identity',\n}\n\n"
        "test('adding zero is identity', () => { expect(add(1, 0)).toBe(1) })\n"
    )
    result = ck_gate(project)
    assert result.exit_code == 0, result.output


def test_task_gate_checks_the_test_tasks_own_properties(project):
    setup_spec(project, mode="required", props_map=ONLY_P1)
    result = ck_gate(project, "--task", "1")
    assert result.exit_code == 1, result.output
    assert "Task 1 properties mapped" in result.output
    assert "P-2" in result.output


def test_property_owned_by_an_unfinished_test_task_is_pending_not_failing(project):
    # Task 1 (owner of P-1, P-2) is not Done: its map may not exist yet.
    setup_spec(project, mode="required", props_map=None, t1="In Progress")
    result = ck_gate(project, "--task", "2")
    assert "✗ Properties" not in result.output, result.output
    whole = ck_gate(project)
    assert "✗ Properties covered" not in whole.output, whole.output


def test_property_no_test_task_owns_fails_whole_gate_when_required(project):
    setup_spec(project, mode="required", props_map=ONLY_P1, props="P-1")
    result = ck_gate(project)
    assert result.exit_code == 1, result.output
    assert "P-2" in result.output


def test_required_without_a_properties_section_fails(project):
    plan = TEST_PLAN.split("## Properties")[0] + "## Commands\n"
    setup_spec(project, mode="required", plan=plan)
    result = ck_gate(project)
    assert result.exit_code == 1, result.output
    assert "no ## Properties section" in result.output


def test_older_spec_without_properties_section_is_not_checked(project):
    plan = TEST_PLAN.split("## Properties")[0] + "## Commands\n"
    setup_spec(project, plan=plan)
    result = ck_gate(project)
    assert result.exit_code == 0, result.output
    assert "Properties" not in result.output


def test_properties_section_saying_none_passes_when_required(project):
    plan = TEST_PLAN.split("## Properties")[0] + "## Properties\nNone: no criterion is universally quantified.\n"
    setup_spec(project, mode="required", plan=plan, props="None")
    result = ck_gate(project)
    assert result.exit_code == 0, result.output


def test_dropping_a_property_from_the_map_on_the_branch_fails_tamper_check(project):
    # A property test that already exists on main, outside any test task, loses a key.
    (project / "tests" / "test_props.py").write_text(PROPS_TEST.format(map=BOTH))
    git(project, "add", "-A")
    git(project, "commit", "-qm", "props")
    base = git(project, "rev-parse", "HEAD").strip()
    setup_spec(project, props_map=ONLY_P1, props="None")
    (project / "specs" / "calc" / "tasks.md").write_text(
        (project / "specs" / "calc" / "tasks.md").read_text().replace("- `tests/test_props.py` - properties of add\n", "")
    )
    config = json.loads((project / "specs" / "ck.json").read_text())
    config["base"] = base
    (project / "specs" / "ck.json").write_text(json.dumps(config))
    result = ck_gate(project, "--task", "2")
    assert result.exit_code == 1, result.output
    assert "drops property P-2" in result.output


def test_weakening_flags_dropped_property_keys_and_fewer_examples():
    old = '@settings(max_examples=200, derandomize=True)\nPROPERTIES = {"P-1": "a", "P-2": "b"}\n'
    new = '@settings(max_examples=10, derandomize=True)\nPROPERTIES = {"P-1": "a"}\n'
    reasons = weakening(old, new)
    assert any("drops property P-2" in r for r in reasons)
    assert any("max_examples" in r for r in reasons)
    assert weakening("fc.assert(p, { numRuns: 500 })", "fc.assert(p, { numRuns: 5 })")
    assert not weakening(old, old)
    assert not weakening(new, old)  # adding properties and examples is fine
