"""`ck testfix`: scope, proof and the one-fix ledger on real repos (Story 4: 4.2, 4.4, 4.5, 4.8, 4.11).

TC-41, TC-44 (a), TC-47 (a), TC-185 and TC-187. Real git repositories (the `attr_project` demo spec, its integration
branch, a real Task 4 branch and a real test-fix branch made with `ck worktree create`), real pytest runs inside the
proof checkout, the real persisted ledger across separate `ck` processes, and the real `ck worktree land --gate`.
Nothing here mocks project code.

The demo spec's test-fix branch is `feat/demo-task-testfix1` (worktree `testfix1`). Every generated test appends a
line to a counter file per run, so "no proof run" is asserted as "the counter file was never written".
"""

import json
from pathlib import Path
from typing import Mapping, Optional

import pytest

from unattended_helpers import (
    attr_project,
    ck_json,
    commit_all,
    git,
    integration_worktree,
    raw,
    ref,
    task_worktree,
)

TEST_FILE = "tests/test_calc_tc.py"
TEST_NAME = "test_tc1_feat4"
NODE = f"{TEST_FILE}::{TEST_NAME}"

TASK4 = """\
### Task 4: Implement feature four
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Files:**
- `feat4.py` - answer
**Verified by:** Task 1
**Dependencies:** Task 1

**Acceptance:**
- [ ] answer works

---

"""

HEADER = '''\
import os
from pathlib import Path


def _count(name):
    base = Path(os.environ["CK_TEST_COUNTERS"])
    base.mkdir(parents=True, exist_ok=True)
    with (base / name).open("a") as fh:
        fh.write("run\\n")


'''


def feat4_test(expected: int) -> str:
    return HEADER + (
        "from feat4 import answer\n\n\n"
        f"def {TEST_NAME}():\n    _count({TEST_NAME!r})\n    assert answer() == {expected}\n"
    )


def fixture_test(expected: int) -> str:
    return HEADER + (
        "import json\n\n\n"
        f"def {TEST_NAME}():\n    _count({TEST_NAME!r})\n"
        '    data = json.loads((Path(__file__).parent / "fixtures" / "rates.json").read_text())\n'
        f'    assert data["rate"] == {expected}\n'
    )


def skipped_test() -> str:
    return HEADER + (
        "import pytest\n\n\n"
        f'@pytest.mark.skip(reason="skipped by the fix")\ndef {TEST_NAME}():\n    _count({TEST_NAME!r})\n    assert True\n'
    )


def calc_import_test() -> str:
    return HEADER + (
        "from calc import multiply\n\n\n"
        f"def {TEST_NAME}():\n    _count({TEST_NAME!r})\n    assert multiply(2, 3) == 6\n"
    )


def passing_test() -> str:
    return HEADER + f"def {TEST_NAME}():\n    _count({TEST_NAME!r})\n    assert True\n"


def commit_files(path: Path, files: Mapping[str, str], message: str) -> None:
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    commit_all(path, message)


def make_scenario(tmp_path: Path, *, base: Mapping[str, str], fix: Mapping[str, str],
                  task4: Optional[Mapping[str, str]] = None, with_task4: bool = True,
                  statuses: Optional[Mapping[str, str]] = None) -> Path:
    """The demo repo; `base` is committed on integrate/demo, then Task 4's worktree (with `task4` committed) and the
    test-fix worktree `testfix1` (with `fix` committed) are created from it. Returns the project root."""
    root = attr_project(tmp_path, extra_tasks=TASK4, statuses=statuses)
    commit_files(integration_worktree(root), base, "base tests")
    names = ["4"] if with_task4 else []
    code, obj, err = ck_json(root, "worktree", "create", "demo", *names, "testfix1", "--base", "integrate/demo", "--json")
    assert code == 0, f"worktree create failed: {obj} {err}"
    assert all(e["state"] == "CREATED" for e in obj), obj
    if task4:
        commit_files(task_worktree(root, "4"), task4, "task 4 work")
    commit_files(task_worktree(root, "testfix1"), fix, "test fix")
    return root


def runs(root: Path, name: str = TEST_NAME) -> int:
    path = root.parent / "counters" / name
    return path.read_text().count("\n") if path.exists() else 0


def outcome_for(obj: dict, node: str = NODE) -> dict:
    matches = [o for o in obj["proof"]["outcomes"] if o["node"] == node]
    assert len(matches) == 1, obj["proof"]
    return matches[0]


# --------------------------------------------------------------------------- TC-41


def test_tc41_prove_fixed_test_passes_on_blocked_branch(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: feat4_test(42)},
                         task4={"feat4.py": "def answer():\n    return 42\n"})
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--blocked", "4", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["scope"]["in_scope"] is True, obj["scope"]
    assert obj["proof"]["proved"] is True, obj["proof"]
    assert obj["against"] == "blocked:4" and obj["proof"]["against"] == "blocked:4"
    assert NODE in obj["changedTests"]
    assert outcome_for(obj)["passed"] is True
    assert runs(root) >= 1  # the proof really ran the test


def test_tc41_prove_import_error_for_name_task_owns_is_proved(tmp_path):
    # Task 4's branch has not built feat4.py yet: the fixed test fails with ImportError for a module Task 4 owns.
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: feat4_test(42)})
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--blocked", "4", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["proof"]["proved"] is True, obj["proof"]
    out = outcome_for(obj)
    assert out["passed"] is False and out["cls"] == "not-built", out


def test_tc41_prove_fixture_assertion_is_not_proved(tmp_path):
    root = make_scenario(
        tmp_path,
        base={TEST_FILE: fixture_test(3), "tests/fixtures/rates.json": '{"rate": 3}\n'},
        fix={TEST_FILE: fixture_test(5)},  # the fixture still says 3: the fix is wrong
    )
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--blocked", "4", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["scope"]["in_scope"] is True, obj["scope"]
    assert obj["proof"]["proved"] is False, obj["proof"]
    out = outcome_for(obj)
    assert out["passed"] is False and out["cls"] != "not-built", out


# --------------------------------------------------------------------------- TC-44 (a)

TEST_PLAN_ROWS = "| TC-5 | 2.1 | int | more | python | tests/test_more.py |"

OUT_OF_SCOPE = {
    "code": ({"calc.py": "def add(a, b):\n    return a - b\n"}, ["calc.py"]),
    "requirements": ({"specs/demo/requirements.md": "# Feature: Demo\n\n- 1.1 WHEN anything THE SYSTEM SHALL pass\n"},
                     ["specs/demo/requirements.md"]),
    "test-plan-row-changed": ({"specs/demo/test-plan.md": "CHANGED_ROW"}, ["specs/demo/test-plan.md"]),
    "test-plan-row-appended": ({"specs/demo/test-plan.md": "APPENDED_ROW"}, ["specs/demo/test-plan.md"]),
}


def out_of_scope_fix(root: Path, key: str) -> Mapping[str, str]:
    files, _ = OUT_OF_SCOPE[key]
    files = dict(files)
    plan = (root / "specs" / "demo" / "test-plan.md").read_text()
    if key == "test-plan-row-changed":
        files["specs/demo/test-plan.md"] = plan.replace("| TC-1 | 1.1 | int | add |", "| TC-1 | 1.1 | int | anything |")
    elif key == "test-plan-row-appended":
        files["specs/demo/test-plan.md"] = plan.rstrip("\n") + "\n| TC-9 | 1.1 | int | looser | python | tests/test_calc_tc.py |\n"
    files[TEST_FILE] = passing_test()  # the test change itself is fine; only the other path is not
    return files


@pytest.mark.parametrize("key", sorted(OUT_OF_SCOPE))
def test_tc44_out_of_scope_fix_is_refused_before_any_proof(tmp_path, key):
    root = attr_project(tmp_path, extra_tasks=TASK4)
    commit_files(integration_worktree(root), {TEST_FILE: feat4_test(41)}, "base tests")
    code, obj, err = ck_json(root, "worktree", "create", "demo", "4", "testfix1", "--base", "integrate/demo", "--json")
    assert code == 0, (obj, err)
    commit_files(task_worktree(root, "4"), {"feat4.py": "def answer():\n    return 42\n"}, "task 4 work")
    commit_files(task_worktree(root, "testfix1"), out_of_scope_fix(root, key), "test fix")
    _, paths = OUT_OF_SCOPE[key]

    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--blocked", "4", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["scope"]["in_scope"] is False, obj["scope"]
    blob = json.dumps(obj["scope"])
    for path in paths:
        assert path in blob, f"{path} not named in {obj['scope']}"
    assert obj["proof"] is None, "an out-of-scope fix must not get a proof run"
    assert runs(root) == 0, "the scope check must run before any proof run: the test ran"


def test_tc44_requirements_edit_is_flagged(tmp_path):
    root = attr_project(tmp_path, extra_tasks=TASK4)
    commit_files(integration_worktree(root), {TEST_FILE: feat4_test(41)}, "base tests")
    code, obj, err = ck_json(root, "worktree", "create", "demo", "4", "testfix1", "--base", "integrate/demo", "--json")
    assert code == 0, (obj, err)
    commit_files(task_worktree(root, "testfix1"), out_of_scope_fix(root, "requirements"), "test fix")
    _, obj, _ = ck_json(root, "testfix", "prove", "demo", "testfix1", "--blocked", "4", "--test", NODE, "--json")
    assert obj["scope"]["requirements_edited"] is True, obj["scope"]


def test_tc44_tests_and_fixtures_only_is_in_scope(tmp_path):
    root = make_scenario(
        tmp_path,
        base={TEST_FILE: fixture_test(3), "tests/fixtures/rates.json": '{"rate": 3}\n'},
        fix={TEST_FILE: fixture_test(5), "tests/fixtures/rates.json": '{"rate": 5}\n'},
    )
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--blocked", "4", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["scope"]["in_scope"] is True, obj["scope"]
    assert not obj["scope"]["outside"], obj["scope"]
    assert obj["proof"] is not None and obj["proof"]["proved"] is True, obj["proof"]


# --------------------------------------------------------------------------- TC-47 (a)


def ledger_file(root: Path) -> Path:
    common = raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(common) / "ck" / "demo" / "testfix-ledger.json"


def test_tc47_one_fix_ledger_persists_across_runs(tmp_path):
    root = attr_project(tmp_path)
    node = f"{TEST_FILE}::test_tc1_add"

    code, first, err = ck_json(root, "testfix", "check", "demo", "--test", node, "--json")
    assert code == 0 and first is not None, (code, first, err)
    assert first["allowed"] == [node], first
    assert first["alreadyFixed"] == [], first

    code, rec, err = ck_json(root, "testfix", "record", "demo", "testfix1", "--test", node, "--outcome", "landed", "--json")
    assert code == 0, (rec, err)
    ledger = ledger_file(root)
    assert ledger.is_file(), f"no ledger at {ledger}"
    entries = json.loads(ledger.read_text())
    assert node in entries and entries[node]["fix"] == "testfix1" and entries[node]["outcome"] == "landed", entries

    # A new run (a new `ck progress begin`) in a new process still knows the node was fixed.
    code, begun, err = ck_json(root, "progress", "begin", "demo", "--kind", "implement", "--json")
    assert code == 0, (begun, err)
    code, second, err = ck_json(root, "testfix", "check", "demo", "--test", node, "--json")
    assert code == 0 and second is not None, (code, second, err)
    assert second["allowed"] == [], second
    assert [e["node"] for e in second["alreadyFixed"]] == [node], second
    assert second["alreadyFixed"][0]["fix"] == "testfix1", second
    assert second["alreadyFixed"][0].get("at"), second


# --------------------------------------------------------------------------- TC-185


def test_tc185_held_proof_not_built_import_error_is_proved_against_integration(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: passing_test()}, fix={TEST_FILE: calc_import_test()}, with_task4=False)
    assert not ref(root, "feat/demo-task-2"), "the scenario must have no Task 2 branch"
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--held", "2", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["proof"]["proved"] is True, obj["proof"]
    assert obj["against"] == "integration" and obj["proof"]["against"] == "integration", obj
    out = outcome_for(obj)
    assert out["passed"] is False and out["cls"] == "not-built", out


def test_tc185_held_proof_fixture_assertion_is_not_proved(tmp_path):
    root = make_scenario(
        tmp_path,
        base={TEST_FILE: fixture_test(3), "tests/fixtures/rates.json": '{"rate": 3}\n'},
        fix={TEST_FILE: fixture_test(5)},
        with_task4=False,
    )
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--held", "2", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["proof"]["proved"] is False, obj["proof"]
    assert obj["against"] == "integration", obj


def test_tc185_held_proof_skipped_test_is_not_proved(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: passing_test()}, fix={TEST_FILE: skipped_test()}, with_task4=False)
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--held", "2", "--test", NODE, "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["proof"]["proved"] is False, obj["proof"]
    assert runs(root) == 0  # skipped: the body never ran


def test_tc185_neither_blocked_nor_held_is_a_usage_error(tmp_path):
    # base and fix differ so the fixture has a fix to commit; the refusal must not depend on the fix's content.
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: passing_test()}, with_task4=False)
    before = runs(root)
    code, obj, err = ck_json(root, "testfix", "prove", "demo", "testfix1", "--test", NODE, "--json")
    assert code != 0, "no --blocked and no --held must be refused"
    assert not (obj or {}).get("proof"), obj
    assert runs(root) == before, "a usage error must not run any proof"


# --------------------------------------------------------------------------- TC-187


def land_entry(obj: dict) -> dict:
    entries = [t for t in obj["tasks"] if str(t["task"]) == "testfix1"]
    assert len(entries) == 1, obj
    return entries[0]


def test_tc187_proved_fix_changing_owned_test_lands_with_scoped_gate(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: passing_test()},
                         with_task4=False, statuses={"1": "Done"})
    # The fixed test no longer needs feat4; test task 1 owns tests/test_calc_tc.py, so the landing gates task 1.
    before = ref(root, "integrate/demo")
    code, obj, err = ck_json(root, "worktree", "land", "demo", "testfix1", "--into", "integrate/demo", "--gate", "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    entry = land_entry(obj)
    assert entry["state"] == "MERGED", entry
    assert entry["gateTasks"] == ["1"], entry
    assert ref(root, "integrate/demo") != before
    assert (integration_worktree(root) / TEST_FILE).read_text() == passing_test()
    assert code == 0


def test_tc187_fix_changing_only_an_unowned_file_lands_red_and_ref_is_unchanged(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: passing_test()}, fix={"tests/unowned_helper.py": "VALUE = 1\n"},
                         with_task4=False, statuses={"1": "Done"})
    before = ref(root, "integrate/demo")
    code, obj, err = ck_json(root, "worktree", "land", "demo", "testfix1", "--into", "integrate/demo", "--gate", "--json")
    assert obj is not None, f"no JSON (exit {code}): {err}"
    entry = land_entry(obj)
    assert entry["state"] == "RED", entry
    assert "changes no file a test task owns" in json.dumps(entry), entry
    assert ref(root, "integrate/demo") == before, "a RED landing must leave the integration ref unchanged"
    assert ref(root, "feat/demo-task-testfix1"), "the fix branch is kept"
    assert code == 1
