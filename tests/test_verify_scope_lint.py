"""Verify-scope lint through the real CLI (run-principles Task 18: TC-109 to 113, 117, 118, 119, 136, 145).

Everything runs the real `ck` (`python -m claude_kiro.cli`) over real generated tasks.md files and real test files in a
temp git project. Nothing of the project's own is mocked. Project modules are imported inside test bodies, so the file
collects before the feature exists and each case fails on the missing `verifyScope` behaviour.

The entry shapes of `verifyScope` / `notChecked` are not fixed key by key in the test plan for the finding message, so
assertions look at what an entry names (any leaf value). Design.md fixes `task`, `suite` and `testTask` on a finding
(`ScopeFinding`); those keys are used where a case needs them.

Layout used by most cases: test tasks 1 and 2 share `tests/test_shared.py`; task 1 verifies impl task 3, task 2 verifies
impl task 4. An unfiltered run of the file at a gate where task 4 is not Done selects task 4's tests (12.2).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, List

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from unattended_helpers import ck_json, ck_text, fake_claude, spec_graph_project  # noqa: E402

SPEC = "sim"
SHARED = "tests/test_shared.py"
SHARED_SRC = "def test_tc1_alpha():\n    assert True\n\n\ndef test_tc2_beta():\n    assert True\n"
RUN = f"uv run pytest -q {SHARED}"
RUN_T1 = f"{RUN} -k test_tc1_"
RUN_T2 = f"{RUN} -k test_tc2_"


# --------------------------------------------------------------------------- helpers


def blk(num, title, **kw) -> str:
    from test_lint import block

    return block(num, title, **kw)


def leaves(obj: Any) -> List[str]:
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in leaves(v)] + [str(k) for k in obj]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in leaves(v)]
    return [str(obj)]


def mentions(entry: Any, needle: str) -> bool:
    ls = leaves(entry)
    if needle.isdigit():
        return any(x in (needle, f"Task {needle}", f"task:{needle}") for x in ls)
    return any(needle in x for x in ls)


def lint(root: Path) -> dict:
    code, obj, err = ck_json(root, "lint", SPEC, "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    return obj


def key(obj: dict, name: str) -> list:
    assert name in obj, f"lint JSON has no {name!r} key: {sorted(obj)}"
    return list(obj[name])


def shared_project(tmp_path: Path, *, v1: str = RUN, v2: str = RUN_T2, v3: str = RUN, v4: str = "",
                   deps3: str = "Task 1", deps4: str = "Task 2", status3: str = "Not Started",
                   status4: str = "Not Started", status1: str = "Not Started", status2: str = "Not Started",
                   extra: str = "") -> Path:
    tasks = (
        blk(1, "Alpha tests", track="test", files=[SHARED], verify=v1, verifies="Task 3", status=status1)
        + blk(2, "Beta tests", track="test", files=[SHARED], verify=v2, verifies="Task 4", status=status2)
        + blk(3, "Alpha core", files=["src/mtr/a.py"], verify=v3, vby="Task 1", deps=deps3, status=status3)
        + blk(4, "Beta core", files=["src/mtr/b.py"], verify=v4, vby="Task 2", deps=deps4, status=status4)
        + extra
    )
    return spec_graph_project(tmp_path, tasks=tasks, tests={SHARED: SHARED_SRC})


def finding_for(found: list, suite: str = SHARED) -> list:
    return [e for e in found if mentions(e, suite)]


# --------------------------------------------------------------------------- TC-109 (12.1)


def test_tc109_simulate_lists_due_suites_and_selections_for_not_done_tasks(tmp_path):
    root = shared_project(tmp_path, status1="Done", status3="Done", v1=RUN_T1, v3=RUN_T1)
    # Tasks 2 and 4 are not Done: task 4's gate makes task 2's suite due and runs it.
    code, obj, err = ck_json(root, "lint", SPEC, "--simulate", "--task", "4", "--json")
    assert obj is not None, f"--simulate printed no JSON (exit {code}): {err}"
    assert str(obj["task"]) == "4"
    assert any(mentions(s, SHARED) for s in obj["suites"]), obj["suites"]
    assert any(mentions(s, SHARED) for s in obj["selected"]), obj["selected"]
    assert all({"file", "tests"} <= set(s) for s in obj["selected"]), obj["selected"]

    code, obj, err = ck_json(root, "lint", SPEC, "--simulate", "--task", "2", "--json")
    assert obj is not None, f"--simulate printed no JSON (exit {code}): {err}"
    assert str(obj["task"]) == "2"
    assert any(mentions(s, SHARED) for s in obj["selected"]), obj["selected"]


def test_tc109_done_tasks_are_absent(tmp_path):
    root = shared_project(tmp_path, status1="Done", status3="Done", v1=RUN_T1, v3=RUN_T1)
    for done in ("1", "3"):
        code, obj, err = ck_json(root, "lint", SPEC, "--simulate", "--task", done, "--json")
        assert obj is None or (not obj.get("suites") and not obj.get("selected")), (done, obj)
    result = lint(root)
    for name in ("verifyScope", "notChecked"):
        for entry in key(result, name):
            assert not mentions(entry, "1") and not mentions(entry, "3"), (name, entry)


# --------------------------------------------------------------------------- TC-110 (12.2)


def test_tc110_unfiltered_shared_suite_names_task_and_suite(tmp_path):
    root = shared_project(tmp_path)
    found = finding_for(key(lint(root), "verifyScope"))
    assert found, "no verifyScope finding for the unfiltered shared suite"
    assert any(mentions(e, "1") or mentions(e, "3") for e in found), found
    assert any(mentions(e, "2") for e in found), f"no finding names the test task that makes it shared: {found}"


def test_tc110_no_finding_when_the_second_task_is_done(tmp_path):
    root = shared_project(tmp_path, status2="Done", status4="Done")
    assert finding_for(key(lint(root), "verifyScope")) == []


# --------------------------------------------------------------------------- TC-111 (12.3)

THESIS_CMD = "mtr thesis new"
TEST_THESIS = (
    "import subprocess\n\n\ndef test_thesis_new():\n"
    '    assert subprocess.run(["mtr", "thesis", "new"]).returncode == 0\n'
)


def test_tc111_later_tasks_command_is_not_a_scope_finding_and_stays_a_command_edge(tmp_path):
    tasks = (
        blk(1, "Thesis tests", track="test", files=["tests/test_thesis.py"], verifies="Task 2")
        + blk(2, "Report", files=["src/mtr/report.py"], vby="Task 1", deps="Task 1",
              verify="uv run pytest -q tests/test_thesis.py")
        + blk(5, "Thesis command", files=["src/mtr/thesis.py"]).replace(
            "**Dependencies:**", f"**Provides:** `{THESIS_CMD}`\n**Dependencies:**", 1)
    )
    root = spec_graph_project(
        tmp_path, tasks=tasks,
        design_cli={THESIS_CMD: "src/mtr/thesis.py", "mtr version": "src/mtr/cli.py"},
        tests={"tests/test_thesis.py": TEST_THESIS},
    )
    result = lint(root)
    assert key(result, "verifyScope") == []
    edges = key(result, "commandEdges")
    assert len(edges) == 1, edges
    for needle in ("tests/test_thesis.py", THESIS_CMD, "5", "2"):
        assert mentions(edges[0], needle), (needle, edges[0])
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# --------------------------------------------------------------------------- TC-112 (12.4)


def test_tc112_control_the_unfiltered_shape_is_a_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN)
    assert finding_for(key(lint(root), "verifyScope")), "control: unfiltered impl Verify must be a finding"


def test_tc112_suite_not_due_gives_no_finding(tmp_path):
    # The only unfiltered run is on a Done task, which is never simulated; everything due is filtered.
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN, status3="Done", status1="Done")
    assert finding_for(key(lint(root), "verifyScope")) == []


def test_tc112_filter_that_excludes_the_tests_gives_no_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN_T1)
    assert finding_for(key(lint(root), "verifyScope")) == []


def test_tc112_direct_dependency_gives_no_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN, deps3="Task 1, Task 4")
    assert finding_for(key(lint(root), "verifyScope")) == []


def test_tc112_transitive_dependency_gives_no_finding(tmp_path):
    extra = blk(5, "Middle", files=["src/mtr/m.py"], deps="Task 4")
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN, deps3="Task 1, Task 5", extra=extra)
    assert finding_for(key(lint(root), "verifyScope")) == []


# --------------------------------------------------------------------------- TC-113 (12.5)


def test_tc113_finding_fails_lint_and_appears_in_the_plan(tmp_path):
    root = shared_project(tmp_path)
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code != 0
    code, obj, err = ck_json(root, "plan", SPEC, "--json")
    assert obj is not None, f"ck plan --json printed no JSON (exit {code}): {err}"
    assert finding_for(key(obj, "verifyScope")), obj.get("verifyScope")


def test_tc113_lint_exits_zero_without_a_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN_T1)
    result = lint(root)
    assert key(result, "verifyScope") == []
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# --------------------------------------------------------------------------- TC-117 (12.8)


def test_tc117_direct_lint_starts_no_agent_and_names_task_and_suite(tmp_path):
    root = shared_project(tmp_path)
    bin_dir = fake_claude(tmp_path)
    env = {"PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), os.environ.get("PATH", "")])}

    code, out, _ = ck_text(root, "lint", SPEC, env=env)
    assert code != 0
    lines = [ln.lower() for ln in out.splitlines() if SHARED in ln]
    assert any(("task 1" in ln or "task 3" in ln) for ln in lines), out

    code, obj, err = ck_json(root, "lint", SPEC, "--json", env=env)
    assert obj is not None, err
    found = finding_for(key(obj, "verifyScope"))
    assert found and any(mentions(e, "1") or mentions(e, "3") for e in found), found

    assert not (bin_dir / "record.json").exists(), "ck lint started an agent"


# --------------------------------------------------------------------------- TC-118 (12.9)


@pytest.mark.parametrize("verify", ["uv run pytest -q $(ls tests)", "bash scripts/run_tests.sh"])
def test_tc118_unresolvable_verify_is_not_checked_and_does_not_block(tmp_path, verify):
    tasks = blk(1, "Core", files=["src/mtr/a.py"], verify=verify)
    root = spec_graph_project(tmp_path, tasks=tasks, tests={SHARED: SHARED_SRC, "scripts/run_tests.sh": "pytest\n"})
    result = lint(root)
    entries = key(result, "notChecked")
    assert any(mentions(e, "1") for e in entries), entries
    assert key(result, "verifyScope") == []
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# --------------------------------------------------------------------------- TC-119 (12.10)


def test_tc119_problem_already_in_verify_cycles_appears_once(tmp_path):
    from test_lint_verify_files import spec_blocks, PERF

    root = spec_graph_project(tmp_path, tasks=spec_blocks(), tests={})
    result = lint(root)
    cycles = [e for e in key(result, "verifyCycles") if mentions(e, PERF)]
    assert len(cycles) == 1, cycles
    assert [e for e in key(result, "verifyScope") if mentions(e, PERF)] == []
    assert [e for e in key(result, "notChecked") if mentions(e, PERF)] == []


# --------------------------------------------------------------------------- TC-136 (3.1, 12.2)


def test_tc136_justification_findings_agree_with_the_cli(tmp_path):
    from claude_kiro.justifications import justification_findings

    root = shared_project(tmp_path)
    text = (
        "# Requirements: Sim\n\n### Story 1: One\n\n**As a** user **I want** x **So that** y.\n\n"
        "**Without it:** something breaks.\n\n### Story 2: Two\n\n**As a** user **I want** x **So that** y.\n"
    )
    (root / "specs" / SPEC / "requirements.md").write_text(text)
    seam = {str(f.story) for f in justification_findings(text)}
    assert seam == {"2"}, seam
    cli = {str(e["story"]) for e in key(lint(root), "justifications")}
    assert cli == seam


def test_tc136_scope_findings_agree_with_the_cli(tmp_path):
    from claude_kiro.hooks._shared.spec_parser import parse_tasks
    from claude_kiro.lint_scope import SimInput, SimTest, scope_findings

    root = shared_project(tmp_path, v1="", v2=RUN_T2, v3=RUN)
    cli = {(str(e["task"]), e["suite"], str(e["testTask"])) for e in key(lint(root), "verifyScope")}
    assert cli, "the CLI reported no finding for the failing fixture"

    tasks = {t.num: t for t in parse_tasks(root / "specs" / SPEC / "tasks.md")}
    sim = SimInput(
        tasks=tasks,
        runs={"3": {SHARED: None}},
        unresolved={},
        tests={SHARED: [SimTest(name="test_tc1_alpha", cls="", source="def test_tc1_alpha():\n    assert True\n"),
                        SimTest(name="test_tc2_beta", cls="", source="def test_tc2_beta():\n    assert True\n")]},
        module_source={SHARED: ""},
        programs=("mtr",),
    )
    seam = {(f.task, f.suite, f.testTask) for f in scope_findings(sim).findings}
    assert seam <= cli and seam, (seam, cli)
    assert {t for t, _, _ in seam} == {"3"}


# --------------------------------------------------------------------------- TC-145 (12.9)


def test_tc145_selected_test_file_not_on_disk_is_not_checked(tmp_path):
    tasks = blk(1, "Core", files=["src/mtr/a.py"], verify="uv run pytest -q tests/test_missing.py")
    root = spec_graph_project(tmp_path, tasks=tasks, tests={})
    assert not (root / "tests/test_missing.py").exists()
    result = lint(root)
    entries = key(result, "notChecked")
    assert any(mentions(e, "1") for e in entries), entries
    assert key(result, "verifyScope") == []
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0
