"""Red team of Story 4 (automatic test repair): 4.2, 4.5, 4.6, 4.8.

Written from the requirements, not the implementation. The clauses under attack are the three SHALL NOT sentences:

- 4.2: a fix SHALL not land without proof. A proof that passes by not running (skipped, xfailed, deselected, deleted,
  renamed), or on the wrong branch, or that cannot be run at all, is no proof.
- 4.5: a fix that changes anything but test files and fixtures, or touches the requirements or test-plan document in
  any way, is refused before any proof run. Near-miss paths, re-tagged tests and a new looser test-plan row are the
  attacks.
- 4.6: a failed proof or a rejected review SHALL not land (workflow cases, `test_tc45_redteam_<slug>`).
- 4.8: a node that has had one automatic fix SHALL not get another test-fixer, however its id is spelled, and a damaged
  ledger never grants. CLI ledger cases are `test_tc47_redteam_<slug>`; the workflow side of 4.8 (no second fixer
  from the bridge's prior fixes, ledger checked before the fixer starts) is in the `test_tc45_redteam_<slug>` cases.

CLI cases use real git branches, real pytest and the persisted ledger; workflow cases run the real script through the
bridge and assert that no `worktree land` of the fix branch was requested.
"""

import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from arbiter_helpers import arbiter_project
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)
from test_testfix_cli import (
    HEADER,
    NODE,
    TASK4,
    TEST_FILE,
    TEST_NAME,
    commit_files,
    feat4_test,
    fixture_test,
    ledger_file,
    make_scenario,
    passing_test,
    runs,
)
from unattended_helpers import attr_project, ck_json, integration_worktree, ref, task_worktree

ANSWER_42 = {"feat4.py": "def answer():\n    return 42\n"}


def prove(root: Path, *extra: str, nodes: Optional[List[str]] = None, mode: str = "--blocked", task: str = "4"):
    args = ["testfix", "prove", "demo", "testfix1", mode, task]
    for node in nodes or [NODE]:
        args += ["--test", node]
    code, obj, err = ck_json(root, *args, *extra, "--json")
    return code, obj, err


def assert_not_proved(code: int, obj: Any, err: str, why: str, may_refuse: bool = False) -> None:
    """The command answers in JSON, the fix is in scope (so only the proof is under test), and the proof is
    absent or reports proved false. Never proved true."""
    assert obj is not None, f"{why}: no JSON (exit {code}): {err}"
    proof = obj.get("proof")
    if may_refuse and obj["scope"]["in_scope"] is False:
        # A deleted test no longer carries its TC token, so the design's scope rule may refuse the fix: refused before any proof.
        assert proof is None, f"{why}: a fix refused as out of scope was still proved: {obj}"
        return
    assert obj["scope"]["in_scope"] is True, f"{why}: the scope check refused a test-only fix: {obj['scope']}"
    assert not (proof and proof.get("proved")), f"{why}: the fix was proved: {obj}"


# --------------------------------------------------------------------------- 4.2: a proof that passes by not running


def skipped_decorator() -> str:
    return HEADER + f'import pytest\n\n\n@pytest.mark.skip(reason="x")\ndef {TEST_NAME}():\n    assert True\n'


def skipped_in_body() -> str:
    return HEADER + f'import pytest\n\n\ndef {TEST_NAME}():\n    pytest.skip("not today")\n'


def skipif_true() -> str:
    return HEADER + f'import pytest\n\n\n@pytest.mark.skipif(True, reason="x")\ndef {TEST_NAME}():\n    assert True\n'


def xfailed() -> str:
    return HEADER + f"import pytest\n\n\n@pytest.mark.xfail\ndef {TEST_NAME}():\n    assert False\n"


def xpassed() -> str:
    return HEADER + f"import pytest\n\n\n@pytest.mark.xfail\ndef {TEST_NAME}():\n    assert True\n"


def renamed() -> str:
    return HEADER + f"def {TEST_NAME}_renamed():\n    assert True\n"


def deleted_function() -> str:
    return HEADER + "def test_other_unrelated():\n    assert True\n"


def unknown_module() -> str:
    return HEADER + f"from nowhere_at_all import thing\n\n\ndef {TEST_NAME}():\n    assert thing\n"


def not_collected_class() -> str:
    # the function moves into a class pytest does not collect: the node id no longer exists
    return HEADER + f"class Helper:\n    def {TEST_NAME}(self):\n        assert True\n"


NOT_RUNNING = {
    "skip-decorator": skipped_decorator,
    "skip-in-body": skipped_in_body,
    "skipif-true": skipif_true,
    "xfail-fails": xfailed,
    "xfail-passes": xpassed,
    "renamed-function": renamed,
    "deleted-function": deleted_function,
    "moved-to-uncollected-class": not_collected_class,
    "unowned-import-error": unknown_module,
}


@pytest.mark.parametrize("key", sorted(NOT_RUNNING))
def test_tc41_redteam_proof_that_does_not_run_is_not_proof(tmp_path, key):
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: NOT_RUNNING[key]()},
                         task4=ANSWER_42)
    code, obj, err = prove(root)
    assert_not_proved(code, obj, err, key, may_refuse=key == "deleted-function")


def test_tc41_redteam_deleted_test_file_is_not_proof(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={"tests/test_other.py": passing_test()},
                         task4=ANSWER_42)
    from unattended_helpers import git
    git(task_worktree(root, "testfix1"), "rm", "-q", TEST_FILE)
    from unattended_helpers import commit_all
    commit_all(task_worktree(root, "testfix1"), "delete the test file")
    code, obj, err = prove(root)
    assert_not_proved(code, obj, err, "deleted file", may_refuse=True)


def test_tc41_redteam_deselecting_conftest_is_not_proof(tmp_path):
    conftest = (
        "def pytest_collection_modifyitems(config, items):\n"
        "    kept = [i for i in items if 'test_tc1_feat4' not in i.nodeid]\n"
        "    config.hook.pytest_deselected(items=[i for i in items if i not in kept])\n"
        "    items[:] = kept\n"
    )
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)},
                         fix={TEST_FILE: passing_test(), "tests/conftest.py": conftest}, task4=ANSWER_42)
    code, obj, err = prove(root)
    assert_not_proved(code, obj, err, "deselected by conftest")


def test_tc41_redteam_one_unrun_node_among_proved_ones_is_not_proof(tmp_path):
    second = "test_second_node"
    body = HEADER + (
        f"import pytest\n\n\ndef {TEST_NAME}():\n    assert True\n\n\n"
        f'@pytest.mark.skip(reason="x")\ndef {second}():\n    assert True\n'
    )
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: body}, task4=ANSWER_42)
    code, obj, err = prove(root, nodes=[NODE, f"{TEST_FILE}::{second}"])
    assert_not_proved(code, obj, err, "a skipped second node")


def test_tc41_redteam_proof_runs_on_the_blocked_branch_not_integration(tmp_path):
    # feat4.py is right on the integration branch but Task 4's branch changed it: passing on integration proves nothing.
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41), "feat4.py": "def answer():\n    return 42\n"},
                         fix={TEST_FILE: feat4_test(42)}, task4={"feat4.py": "def answer():\n    return 7\n"})
    code, obj, err = prove(root)
    assert_not_proved(code, obj, err, "passes on integration, fails on the blocked branch")


def test_tc41_redteam_fixture_conflict_with_blocked_branch_is_not_proof(tmp_path):
    # the fix and the blocked task both rewrite the fixture the test reads: no clean proof checkout exists
    root = make_scenario(
        tmp_path,
        base={TEST_FILE: fixture_test(3), "tests/fixtures/rates.json": '{"rate": 3}\n'},
        fix={TEST_FILE: fixture_test(5), "tests/fixtures/rates.json": '{"rate": 5}\n'},
        task4={"tests/fixtures/rates.json": '{"rate": 9}\n'},
    )
    code, obj, err = prove(root)
    assert_not_proved(code, obj, err, "merge conflict on a shared fixture")
    assert (task_worktree(root, "4") / "tests/fixtures/rates.json").read_text() == '{"rate": 9}\n', "proof must not touch the task branch"


def test_tc41_redteam_proof_never_changes_a_branch(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: feat4_test(41)}, fix={TEST_FILE: feat4_test(42)}, task4=ANSWER_42)
    before = {b: ref(root, b) for b in ("integrate/demo", "feat/demo-task-4", "feat/demo-task-testfix1")}
    code, obj, err = prove(root)
    assert obj is not None and obj["proof"] is not None, f"the proof did not run (exit {code}): {err}"
    after = {b: ref(root, b) for b in before}
    assert after == before, "a proof is a check: it must not move or land any branch"


# --------------------------------------------------------------------------- 4.5: scope dodges


def plan_text(root: Path) -> str:
    return (root / "specs" / "demo" / "test-plan.md").read_text()


def renamed_test(token: str) -> str:
    return HEADER + f"def test_tc{token}_feat4():\n    assert True\n"


def scope_attack(root: Path, key: str) -> Mapping[str, str]:
    plan = plan_text(root)
    good = {TEST_FILE: passing_test()}
    if key == "retag-new-row":  # test_tc999_x with a looser TC-999 row appended
        return {TEST_FILE: renamed_test("999"), "specs/demo/test-plan.md": plan.rstrip("\n") + "\n| TC-999 | 1.1 | int | anything | python | tests/test_calc_tc.py |\n"}
    if key == "retag-token-only":  # the TC token changes and no row follows it
        return {TEST_FILE: renamed_test("77")}
    if key == "plan-only-new-row":
        return {**good, "specs/demo/test-plan.md": plan.rstrip("\n") + "\n| TC-9 | 1.1 | int | looser | python | tests/test_calc_tc.py |\n"}
    if key == "plan-whitespace":
        return {**good, "specs/demo/test-plan.md": plan.rstrip("\n") + "\n\n\n"}
    if key == "requirements-whitespace":
        req = (root / "specs" / "demo" / "requirements.md").read_text()
        return {**good, "specs/demo/requirements.md": req.rstrip("\n") + "\n\n"}
    near = {
        "near-src-tests-py": "src/tests.py",
        "near-tests-old": "tests_old/helper.py",
        "near-src-tests-fixtures": "src/tests/fixtures.py",
        "near-pyproject": "pyproject.toml",
        "near-claude-dir": ".claude/x",
        "near-spec-tasks": "specs/demo/tasks.md",
        "near-spec-design": "specs/demo/design.md",
        "near-ck-json": "specs/ck.json",
        "near-hidden": ".github/workflows/ci.yml",
    }
    if key in near:
        return {**good, near[key]: "x = 1\n"}
    if key == "root-conftest":
        return {**good, "conftest.py": "import os\nos.environ['X'] = '1'\n"}
    if key == "source-edit":
        return {**good, "calc.py": "def add(a, b):\n    return 0\n"}
    raise AssertionError(key)


SCOPE_KEYS = ["retag-new-row", "retag-token-only", "plan-only-new-row", "plan-whitespace", "requirements-whitespace",
              "near-src-tests-py", "near-tests-old", "near-src-tests-fixtures", "near-pyproject", "near-claude-dir",
              "near-spec-tasks", "near-spec-design", "near-ck-json", "near-hidden", "root-conftest", "source-edit"]


@pytest.mark.parametrize("key", SCOPE_KEYS)
def test_tc44_redteam_scope_dodge_is_refused_before_any_proof(tmp_path, key):
    root = attr_project(tmp_path, extra_tasks=TASK4)
    commit_files(integration_worktree(root), {TEST_FILE: feat4_test(41)}, "base tests")
    code, obj, err = ck_json(root, "worktree", "create", "demo", "4", "testfix1", "--base", "integrate/demo", "--json")
    assert code == 0, (obj, err)
    commit_files(task_worktree(root, "4"), ANSWER_42, "task 4 work")
    commit_files(task_worktree(root, "testfix1"), scope_attack(root, key), "test fix")
    code, obj, err = prove(root)
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["scope"]["in_scope"] is False, f"{key} slipped through: {obj['scope']}"
    assert obj["proof"] is None, "an out-of-scope fix must not get a proof run"
    assert runs(root) == 0, "the scope check must run before any proof run: the test ran"


def test_tc44_redteam_retag_new_row_names_the_plan_file(tmp_path):
    root = attr_project(tmp_path, extra_tasks=TASK4)
    commit_files(integration_worktree(root), {TEST_FILE: feat4_test(41)}, "base tests")
    code, obj, err = ck_json(root, "worktree", "create", "demo", "4", "testfix1", "--base", "integrate/demo", "--json")
    assert code == 0, (obj, err)
    commit_files(task_worktree(root, "testfix1"), scope_attack(root, "retag-new-row"), "test fix")
    code, obj, err = prove(root)
    assert obj is not None and obj["scope"]["in_scope"] is False, (obj, err)
    assert "specs/demo/test-plan.md" in json.dumps(obj["scope"]), obj["scope"]


def test_tc44_redteam_source_moved_into_tests_is_refused(tmp_path):
    root = attr_project(tmp_path, extra_tasks=TASK4)
    commit_files(integration_worktree(root), {TEST_FILE: feat4_test(41)}, "base tests")
    code, obj, err = ck_json(root, "worktree", "create", "demo", "4", "testfix1", "--base", "integrate/demo", "--json")
    assert code == 0, (obj, err)
    from unattended_helpers import commit_all, git
    wt = task_worktree(root, "testfix1")
    git(wt, "mv", "calc.py", "tests/calc.py")  # a rename: the source file leaves its place
    (wt / TEST_FILE).write_text(passing_test())
    commit_all(wt, "move source under tests")
    code, obj, err = prove(root)
    assert obj is not None and obj["scope"]["in_scope"] is False, (obj, err)
    assert obj["proof"] is None and runs(root) == 0


def test_tc44_redteam_scope_dodge_is_refused_for_a_held_fix_too(tmp_path):
    root = make_scenario(tmp_path, base={TEST_FILE: passing_test()},
                         fix={TEST_FILE: passing_test(), "calc.py": "def add(a, b):\n    return 0\n"}, with_task4=False)
    code, obj, err = prove(root, mode="--held", task="2")
    assert obj is not None and obj["scope"]["in_scope"] is False, (obj, err)
    assert obj["proof"] is None and runs(root) == 0


# --------------------------------------------------------------------------- 4.8: the one-fix ledger

OTHER_NODE = f"{TEST_FILE}::test_tc1_subtract"
FIXED_NODE = f"{TEST_FILE}::test_tc1_add"

SPELLINGS = {
    "dot-slash": f"./{FIXED_NODE}",
    "param-id": f"{FIXED_NODE}[1-2]",
    "upper-case": FIXED_NODE.upper(),
    "backslash": FIXED_NODE.replace("/", "\\"),
    "double-slash": FIXED_NODE.replace("tests/", "tests//"),
    "trailing-space": f"{FIXED_NODE} ",
    "dot-dot": FIXED_NODE.replace("tests/", "tests/../tests/"),
}


def record(root: Path, node: str = FIXED_NODE, fix: str = "testfix1"):
    code, obj, err = ck_json(root, "testfix", "record", "demo", fix, "--test", node, "--outcome", "landed", "--json")
    assert code == 0, (obj, err)


def check(root: Path, *nodes: str):
    args = ["testfix", "check", "demo"]
    for n in nodes:
        args += ["--test", n]
    return ck_json(root, *args, "--json")


@pytest.mark.parametrize("key", sorted(SPELLINGS))
def test_tc47_redteam_respelled_node_is_still_already_fixed(tmp_path, key):
    root = attr_project(tmp_path)
    record(root)
    code, obj, err = check(root, SPELLINGS[key])
    assert obj is not None, f"no JSON (exit {code}): {err}"
    assert obj["allowed"] == [], f"{key} dodged the ledger: {obj}"
    assert len(obj["alreadyFixed"]) == 1, obj


def test_tc47_redteam_other_node_of_the_same_file_stays_allowed(tmp_path):
    # the count is per test node: the guard must not be the cruder per-file rule
    root = attr_project(tmp_path)
    record(root)
    code, obj, err = check(root, OTHER_NODE, FIXED_NODE)
    assert obj is not None, (code, err)
    assert obj["allowed"] == [OTHER_NODE], obj
    assert [e["node"] for e in obj["alreadyFixed"]] == [FIXED_NODE], obj


def test_tc47_redteam_fixed_node_repeated_in_one_call_is_reported_not_granted(tmp_path):
    root = attr_project(tmp_path)
    record(root)
    code, obj, err = check(root, FIXED_NODE, FIXED_NODE, f"./{FIXED_NODE}")
    assert obj is not None, (code, err)
    assert obj["allowed"] == [], obj


@pytest.mark.parametrize("content", ["{not json", "", "[]", "null", '{"tests/test_calc_tc.py::test_tc1_add": "testfix1"', '"x"'])
def test_tc47_redteam_damaged_ledger_never_grants(tmp_path, content):
    root = attr_project(tmp_path)
    record(root)
    ledger_file(root).write_text(content)
    code, obj, err = check(root, FIXED_NODE)
    granted = bool(obj and FIXED_NODE in obj.get("allowed", []))
    assert not granted, f"a damaged ledger ({content!r}) granted a second fix: {obj}"
    assert code != 0 or obj is not None


def test_tc47_redteam_ledger_is_shared_by_every_worktree(tmp_path):
    root = attr_project(tmp_path, extra_tasks=TASK4)
    code, obj, err = ck_json(root, "worktree", "create", "demo", "4", "--base", "integrate/demo", "--json")
    assert code == 0, (obj, err)
    record(root)
    for where in (task_worktree(root, "4"), integration_worktree(root)):
        code, obj, err = check(where, FIXED_NODE)
        assert obj is not None, (where, code, err)
        assert obj["allowed"] == [], f"worktree {where} did not see the earlier fix: {obj}"


def test_tc47_redteam_parallel_records_lose_no_entry(tmp_path):
    root = attr_project(tmp_path)
    nodes = [f"{TEST_FILE}::test_tc1_n{i}" for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda pair: ck_json(root, "testfix", "record", "demo", f"testfix{pair[0] + 1}", "--test", pair[1], "--outcome", "landed", "--json"), enumerate(nodes)))
    code, obj, err = check(root, *nodes)
    assert obj is not None, (code, err)
    assert obj["allowed"] == [], f"a concurrent record lost an entry: {obj}"
    assert len(obj["alreadyFixed"]) == len(nodes), obj


def test_tc47_redteam_second_record_does_not_rewrite_the_first_fix(tmp_path):
    root = attr_project(tmp_path)
    record(root, fix="testfix1")
    ck_json(root, "testfix", "record", "demo", "testfix2", "--test", FIXED_NODE, "--outcome", "landed", "--json")
    code, obj, err = check(root, FIXED_NODE)
    assert obj is not None, (code, err)
    assert obj["alreadyFixed"][0]["fix"] == "testfix1", "the earlier fix must stay on record: " + json.dumps(obj)


# --------------------------------------------------------------------------- workflow cases (4.6, 4.5, 4.8)

CONFLICT = {"file": TEST_FILE, "test": TEST_NAME, "criterion": "1.1", "reason": "the test contradicts the criterion",
            "fix": "assert the criterion value"}
SECOND = {**CONFLICT, "file": "tests/test_other.py", "test": "test_other"}
LAND_FIX = re.compile(r"worktree land[^\n]*\btestfix\d+")


TASKS_1_TO_6 = "# Implementation Tasks: Demo\n\n**Status:** Not Started\n\n## Task Breakdown\n\n" + "\n".join(
    f"### Task {n}: Work {n}\n**Status:** Not Started\n**Track:** impl\n**Requirements:** 1.1\n**Description:** Work {n}.\n"
    f"**Files:**\n- `src/work{n}.py` - the work\n**Verify:** `uv run pytest -q tests/test_work{n}.py`\n**Dependencies:** None\n\n---\n"
    for n in range(1, 7))


def repair_ruling(c: Dict[str, Any]) -> Dict[str, Any]:
    """The `arbiter task 4` answer that rules conflict `c` test_repair (design D31-D33: a test conflict reaches the
    test-fixer only through a test_repair ruling); "@key" makes the fake arbiter copy the claim key from its prompt."""
    return {"claim": "@key", "status": "ruled", "outcome": "test_repair", "principle": "project:test-intent",
            "reasoning": "the test asserts a value no correct code returns; its intent stands, only the value moves",
            "testRepair": {"file": c["file"], "test": c["test"], "change": c["fix"], "why": c["reason"]}}


def run_wf(bridge, testfix: Optional[Dict[str, Any]] = None, prior: Optional[Dict[str, Any]] = None,
           conflicts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    project: Dict[str, Any] = {"deps": {"4": [], "5": ["4"]}, "testFix": testfix if testfix is not None else {CONFLICT["file"]: {}}}
    project["testConflictsList" if conflicts else "testConflicts"] = {"4": conflicts} if conflicts else {"4": {"conflict": CONFLICT}}
    project["rulings"] = {"4": [repair_ruling(c) for c in (conflicts or [CONFLICT])]}
    if prior:
        project["priorFixes"] = prior
    # the arbiter's thin steps (`ck arbiter open`, `settle`, `record`) run the real CLI on a real git project
    with tempfile.TemporaryDirectory() as tmp:
        repo = arbiter_project(Path(tmp), tasks=TASKS_1_TO_6)
        args = {"onFailure": "continue", **repo.run_args, "spec": "demo", "arbiterAgent": "spec:arbiter"}
        return bridge.run(project, args=args, clock="virtual", exec_ck=repo.exec_ck)


def fix_landed(out: Dict[str, Any]) -> bool:
    texts = list(out["prompts"].values()) + [json.dumps(c) for c in out.get("ckCalls", [])]
    labels = [e for e in out["events"] if isinstance(e, str)]
    return any(LAND_FIX.search(t) for t in texts if isinstance(t, str)) or any(re.match(r"land\b.*testfix", e) for e in labels)


def fixer_calls(out: Dict[str, Any]) -> List[str]:
    return [e for e in out["events"] if isinstance(e, str) and re.match(r"testfix \d+ \(", e)]


def review_calls(out: Dict[str, Any]) -> List[str]:
    return [e for e in out["events"] if isinstance(e, str) and e.startswith("testfix review")]


def sent_to_me(out: Dict[str, Any]) -> str:
    """The blocked task 4 is in needs-input and not merged; Task 5 never starts. Returns the report text for marker checks."""
    result = out["result"]
    assert "4" not in result["merged"], result["merged"]
    assert "4" in [str(e["task"]) for e in result.get("needsInput", [])], f"task 4 not in needs-input: {result.get('needsInput')}"
    assert "task 5" not in out["events"], "a dependent started although the fix did not land"
    records = [r for r in result.get("testFixes", []) if str(r.get("blocked")) == "4"]
    assert records and all(r.get("outcome") != "landed" for r in records), result.get("testFixes")
    return json.dumps({"needsInput": result["needsInput"], "testFixes": result["testFixes"]})


def test_tc45_redteam_control_proved_in_scope_approved_fix_does_land(bridge):  # noqa: F811
    out = run_wf(bridge)
    assert fix_landed(out), "control: with scope ok, proof proved and review approve the fix must be landed"


@pytest.mark.parametrize("proof", [
    {"proved": False, "reason": "MARK-PROOF assertion on fixture data"},
    {"proved": False, "reason": "MARK-PROOF", "outcomes": [{"node": NODE, "passed": False, "cls": "bad"}]},
    {"proved": None, "reason": "MARK-PROOF"},
    {"proved": "false", "reason": "MARK-PROOF"},
    {"proved": 0, "reason": "MARK-PROOF"},
])
def test_tc45_redteam_failed_proof_never_lands(bridge, proof):  # noqa: F811
    out = run_wf(bridge, {CONFLICT["file"]: {"proof": proof}})
    assert not fix_landed(out), "a fix whose proof did not hold was landed"
    assert not review_calls(out), "a failed proof must get no review"
    assert "MARK-PROOF" in sent_to_me(out), "needs-input must carry the proof result"


@pytest.mark.parametrize("review", [
    {"verdict": "changes", "findings": ["MARK-REVIEW weakened assertion"], "summary": "no"},
    {"verdict": "changes", "findings": [], "summary": "MARK-REVIEW"},
    {"verdict": "reject", "findings": [{"file": TEST_FILE, "issue": "MARK-REVIEW"}], "summary": "no"},
    {"verdict": "approve-ish", "findings": ["MARK-REVIEW"], "summary": "unclear"},
    {"findings": ["MARK-REVIEW"], "summary": "no verdict at all"},
    {"verdict": None, "findings": ["MARK-REVIEW"], "summary": "null verdict"},
    {"verdict": "approve", "findings": [{"severity": "blocking", "issue": "MARK-REVIEW blocking finding"}], "summary": "approve but lists a finding"},
])
def test_tc45_redteam_rejected_or_unclear_review_never_lands(bridge, review):  # noqa: F811
    out = run_wf(bridge, {CONFLICT["file"]: {"review": review}})
    assert not fix_landed(out), "a fix the reviewer did not clearly approve was landed"
    assert "MARK-REVIEW" in sent_to_me(out), "needs-input must carry the review findings"


@pytest.mark.parametrize("scope", [
    {"in_scope": False, "outside": ["calc.py"], "reason": "MARK-SCOPE source changed"},
    {"in_scope": True, "outside": ["calc.py"], "reason": "MARK-SCOPE contradictory: outside files listed"},
    {"in_scope": True, "requirements_edited": True, "reason": "MARK-SCOPE requirements edited"},
    {"in_scope": True, "spec_docs_edited": ["specs/demo/test-plan.md"], "reason": "MARK-SCOPE new row"},
])
def test_tc45_redteam_out_of_scope_fix_gets_no_review_and_no_land(bridge, scope):  # noqa: F811
    out = run_wf(bridge, {CONFLICT["file"]: {"scope": scope}})
    assert not fix_landed(out), "a scope violation was landed"
    assert not review_calls(out), f"a review agent ran for an out-of-scope fix: {review_calls(out)}"
    assert "MARK-SCOPE" in sent_to_me(out)


def test_tc45_redteam_proof_and_review_disagree_proof_wins(bridge):  # noqa: F811
    out = run_wf(bridge, {CONFLICT["file"]: {"proof": {"proved": False, "reason": "MARK-PROOF"},
                                             "review": {"verdict": "approve", "findings": [], "summary": "fine"}}})
    assert not fix_landed(out), "an approving review must not rescue a failed proof"
    sent_to_me(out)


def test_tc45_redteam_scope_ok_proof_ok_but_review_rejects_is_the_third_leg(bridge):  # noqa: F811
    out = run_wf(bridge, {CONFLICT["file"]: {"scope": {"in_scope": True}, "proof": {"proved": True},
                                             "review": {"verdict": "changes", "findings": ["MARK-REVIEW"], "summary": "x"}}})
    assert not fix_landed(out)
    sent_to_me(out)


@pytest.mark.parametrize("verdict", ["cannot-fix", "test-correct", "unknown-verdict", None])
def test_tc45_redteam_fixer_that_did_not_fix_lands_nothing(bridge, verdict):  # noqa: F811
    out = run_wf(bridge, {CONFLICT["file"]: {"verdict": verdict, "finding": {"file": TEST_FILE, "test": TEST_NAME, "issue": "MARK-FINDING"}}})
    assert fixer_calls(out), "the test-fixer was never started, so the verdict was never exercised"
    assert not fix_landed(out), f"a fixer verdict {verdict!r} led to a land"
    assert not review_calls(out), "nothing was fixed: there is nothing to review"


def test_tc45_redteam_prior_fix_starts_no_fixer_and_no_land(bridge):  # noqa: F811
    out = run_wf(bridge, prior={CONFLICT["file"]: "testfix9"})
    assert fixer_calls(out) == [], f"a second test-fixer was started: {fixer_calls(out)}"
    assert not review_calls(out) and not fix_landed(out)
    sent_to_me(out)


def test_tc45_redteam_prior_fix_on_one_file_does_not_block_the_other(bridge):  # noqa: F811
    out = run_wf(bridge, testfix={SECOND["file"]: {}}, prior={CONFLICT["file"]: "testfix9"}, conflicts=[CONFLICT, SECOND])
    calls = fixer_calls(out)
    assert len(calls) == 1, f"exactly one fixer (for the unfixed file): {calls}"
    prompts = [p for label, p in out["prompts"].items() if label in calls]
    assert prompts and all(CONFLICT["file"] not in p for p in prompts), "the prior-fixed file was given to a fixer"
    assert "4" not in out["result"]["merged"], "the blocked task cannot merge while one conflict is unresolved"


def test_tc45_redteam_ledger_is_consulted_before_the_fixer_starts(bridge):  # noqa: F811
    out = run_wf(bridge)
    events = [e for e in out["events"] if isinstance(e, str)]
    ledger = [i for i, e in enumerate(events) if e.startswith("testfix ledger")]
    fixer = [i for i, e in enumerate(events) if re.match(r"testfix \d+ \(", e)]
    assert ledger, f"the ledger was never checked: {events}"
    assert fixer and min(ledger) < min(fixer), events
