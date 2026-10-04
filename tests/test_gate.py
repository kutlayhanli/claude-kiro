"""Verification gate (`ck gate`) and the Stop hook, against a real git project and real pytest runs."""

import json
from pathlib import Path

from click.testing import CliRunner

from claude_kiro.cli.main import cli
from claude_kiro.cli.runner import execute_hook
from claude_kiro.gate import run_gate

from conftest import git, write_tasks

# Imports the project module inside the test body: before subtract() exists the
# test fails, but the file still collects (a module-level import would break
# collection for the whole suite).
SUBTRACT_TEST = "def test_subtract():\n    from calc import subtract\n\n    assert subtract(5, 3) == 2\n"


def implement_subtract(project: Path) -> None:
    (project / "calc.py").write_text((project / "calc.py").read_text() + "\n\ndef subtract(a, b):\n    return a - b\n")


def write_subtract_tests(project: Path) -> None:
    (project / "tests" / "test_subtract.py").write_text(SUBTRACT_TEST)


def run_ck_gate(project: Path, *args: str):
    runner = CliRunner()
    import os

    old = os.getcwd()
    os.chdir(project)
    try:
        return runner.invoke(cli, ["gate", "calc", *args])
    finally:
        os.chdir(old)


def stop(project: Path, session: str):
    return execute_hook("stop-verify", {"session_id": session, "cwd": str(project), "hook_event_name": "Stop"})


def mark_tasks_edited(project: Path, session: str):
    """What the PostToolUse hook records when Claude edits tasks.md."""
    execute_hook(
        "post-file-ops",
        {
            "session_id": session,
            "cwd": str(project),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(project / "specs/calc/tasks.md")},
        },
    )


# --- gate --------------------------------------------------------------------


def test_impl_task_passes_when_tests_and_oracle_pass(project):
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    result = run_ck_gate(project)
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_impl_task_fails_when_its_oracle_tests_fail(project):
    write_subtract_tests(project)
    (project / "calc.py").write_text((project / "calc.py").read_text() + "\n\ndef subtract(a, b):\n    return 0\n")
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    result = run_ck_gate(project, "--task", "2")
    assert result.exit_code == 1
    assert "python -m pytest -q tests/test_subtract.py" in result.output
    assert "assert 0 == 2" in result.output  # failing output is in the report


def test_impl_task_needs_its_test_task_done_first(project):
    implement_subtract(project)
    write_tasks(project, t1="In Progress", t2="Done", a2="x")
    result = run_gate(project, project / "specs/calc", ["2"])
    assert not result.ok
    assert "must be Done first" in result.report()


def test_unchecked_acceptance_fails(project):
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2=" ")
    report = run_gate(project, project / "specs/calc", ["2"]).report()
    assert "- [ ] subtract() implemented" in report


def test_failing_project_verify_command_fails(project):
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    (project / "calc.py").write_text((project / "calc.py").read_text().replace("return a + b", "return a * b"))
    result = run_gate(project, project / "specs/calc", ["2"])
    assert not result.ok
    assert "tests/test_calc.py" in result.report()


def test_test_task_may_be_red_until_its_impl_is_done(project):
    write_subtract_tests(project)  # subtract() does not exist yet: tests fail
    write_tasks(project, t1="Done", a1="x")
    result = run_gate(project, project / "specs/calc", ["1"])
    assert result.ok, result.report()
    assert "may still fail" in result.report()


def test_collection_breakage_fails_gate(project):
    """A red-phase test file with a module-level import of missing code breaks collection for everyone."""
    (project / "tests" / "test_subtract.py").write_text("from calc import subtract\n\n\ndef test_subtract():\n    assert subtract(5, 3) == 2\n")
    write_tasks(project, t1="Done", a1="x")
    result = run_gate(project, project / "specs/calc", ["1"])
    assert not result.ok
    report = result.report()
    assert "✗ Test collection succeeds (`python -m pytest --collect-only -q`)" in report
    assert "import project modules inside test bodies" in report


def test_green_only_skips_full_suite_until_spec_complete(project):
    """Test-first: an unrelated red test task must not block an impl task's gate; the full suite runs at the end."""
    import re

    tasks_md = project / "specs" / "calc" / "tasks.md"
    # A third task: tests for multiply, verifying a fourth (unimplemented) impl task.
    extra = (
        "\n### Task 3: Tests for multiply\n**Status:** Done\n**Track:** test\n**Verifies:** Task 4\n"
        "**Files:**\n- `tests/test_multiply.py` - multiply\n**Verify:** `python -m pytest -q tests/test_multiply.py`\n"
        "- [x] cases written\n\n"
        "### Task 4: Implement multiply\n**Status:** Not Started\n**Track:** impl\n**Verified by:** Task 3\n"
        "**Files:**\n- `calc.py` - multiply()\n- [ ] done\n"
    )
    (project / "tests" / "test_multiply.py").write_text("def test_multiply():\n    from calc import multiply\n\n    assert multiply(2, 3) == 6\n")
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    tasks_md.write_text(re.sub(r"\n## Parallel Groups", extra + "\n## Parallel Groups", tasks_md.read_text()))
    # The full suite would fail (test_multiply is red until Task 4); make that the project verify.
    (project / "specs/ck.json").write_text(json.dumps({"verify": ["python -m pytest -q"]}))

    green = run_gate(project, project / "specs/calc", ["2"])
    report = green.report()
    assert green.ok, report
    assert "`python -m pytest -q tests/test_subtract.py`" in report or "test_subtract" in report
    assert "skipping 1 still waiting on unfinished tasks (Tasks 3)" in report

    from claude_kiro.config import load_config

    full = run_gate(project, project / "specs/calc", ["2"], config={**load_config(project), "verify_mode": "full"})
    assert not full.ok  # the full suite includes the red multiply test


def test_test_task_needs_real_test_cases(project):
    (project / "tests" / "test_subtract.py").write_text("# TODO\n")
    write_tasks(project, t1="Done", a1="x")
    report = run_gate(project, project / "specs/calc", ["1"]).report()
    assert "no test cases found" in report


def test_weakened_test_on_branch_fails_tamper_check(project):
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    test_calc = project / "tests" / "test_calc.py"
    test_calc.write_text(test_calc.read_text().replace("    assert add(-1, -1) == -2\n", "    pass\n"))
    git(project, "commit", "-qam", "sneaky")
    result = run_gate(project, project / "specs/calc", ["2"])
    assert not result.ok
    assert "tests/test_calc.py: removes 1 assertion" in result.report()


def test_deleted_test_fails_tamper_check(project):
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    (project / "specs/ck.json").write_text(json.dumps({"verify": []}))
    (project / "tests" / "test_calc.py").unlink()
    report = run_gate(project, project / "specs/calc", ["2"]).report()
    assert "tests/test_calc.py: deleted" in report


def test_older_task_format_is_understood(tmp_path):
    """Specs written before Track/Status fields: "### Task1:" and status in the title."""
    from claude_kiro.hooks._shared.spec_parser import parse_tasks

    tasks_md = tmp_path / "tasks.md"
    tasks_md.write_text(
        "### Task1: Create module ✅ COMPLETE\n**Files:**\n- `a.py` - x\n\n"
        "### Task 2: Wire it up\n**Files:**\n- `b.py` - y\n- [ ] done\n"
    )
    first, second = parse_tasks(tasks_md)
    assert (first.num, first.done, first.files, first.track) == ("1", True, ["a.py"], "impl")
    assert (second.num, second.done, second.acceptance) == ("2", False, [(False, "done")])


# --- Stop hook ---------------------------------------------------------------


def test_stop_is_silent_when_nothing_is_marked_done(project, session):
    mark_tasks_edited(project, session)
    assert stop(project, session) is None


def test_stop_blocks_done_claim_that_fails_gate(project, session):
    write_subtract_tests(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")  # claims Done, no subtract()
    mark_tasks_edited(project, session)
    result = stop(project, session)
    assert result["decision"] == "block"
    assert "set the task's **Status:** back to In Progress" in result["reason"]


def test_stop_allows_after_status_reverted(project, session):
    write_subtract_tests(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    mark_tasks_edited(project, session)
    assert stop(project, session)["decision"] == "block"
    write_tasks(project, t1="Done", t2="In Progress", a1="x")
    result = stop(project, session)
    assert result is None or "decision" not in result  # Task 1 (tests, may be red) still passes


def test_stop_passes_then_skips_reverification(project, session):
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    mark_tasks_edited(project, session)
    first = stop(project, session)
    assert "Verification gate passed" in first["systemMessage"]
    assert stop(project, session) is None  # nothing changed: not re-run


def test_stop_gives_up_after_repeated_identical_failures(project, session):
    write_subtract_tests(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    mark_tasks_edited(project, session)
    for _ in range(3):
        assert stop(project, session)["decision"] == "block"
    final = stop(project, session)
    assert "decision" not in final
    assert "still failing" in final["systemMessage"]


def test_init_installs_hooks_and_keeps_foreign_ones(tmp_path):
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
    old_ck = {"matcher": "Edit", "hooks": [{"type": "command", "command": "ck --hook post-file-ops", "timeout": 5000}]}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [foreign], "PostToolUse": [old_ck]}}))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    import os

    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(cli, ["init"])
    finally:
        os.chdir(old)
    assert result.exit_code == 0, result.output

    hooks = json.loads(settings.read_text())["hooks"]
    assert foreign in hooks["PreToolUse"]
    commands = {e: [h["command"] for g in gs for h in g["hooks"]] for e, gs in hooks.items()}
    assert "ck --hook pre-tool-guard" in commands["PreToolUse"]
    assert commands["PostToolUse"] == ["ck --hook post-file-ops"]  # old ms-timeout entry replaced
    assert hooks["PostToolUse"][0]["hooks"][0]["timeout"] == 10
    assert "ck --hook stop-verify" in commands["Stop"] and "ck --hook stop-verify" in commands["SubagentStop"]
    assert json.loads((tmp_path / "specs/ck.json").read_text())["verify"] == ["pytest -q"]
