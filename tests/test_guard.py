"""PreToolUse guard, driven through the real hook entry point with Claude Code-shaped input."""

import json
import subprocess
import sys

from claude_kiro.cli.runner import execute_hook

from conftest import CALC_TEST, git, write_tasks


def guard(project, session, tool_name, agent_id=None, **tool_input):
    payload = {
        "session_id": session,
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return execute_hook("pre-tool-guard", payload)


AGENT = "agent-a1b2c3"  # a task agent: Claude Code sets agent_id for Workflow agents and Agent-tool subagents


def decision(result):
    return result and result["hookSpecificOutput"]["permissionDecision"]


def test_removing_an_assertion_from_existing_test_asks(project, session):
    result = guard(
        project, session, "Edit",
        file_path=str(project / "tests/test_calc.py"),
        old_string="    assert add(1, 2) == 3\n",
        new_string="    pass\n",
    )
    assert decision(result) == "ask"
    assert "removes 1 assertion" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_adding_a_skip_marker_asks(project, session):
    result = guard(
        project, session, "Edit",
        file_path="tests/test_calc.py",
        old_string="def test_add():",
        new_string="@pytest.mark.skip\ndef test_add():",
    )
    assert decision(result) == "ask"


def test_rewriting_test_file_with_fewer_tests_asks(project, session):
    result = guard(
        project, session, "Write",
        file_path=str(project / "tests/test_calc.py"),
        content="from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    )
    assert decision(result) == "ask"


def test_adding_tests_is_allowed(project, session):
    result = guard(
        project, session, "Edit",
        file_path=str(project / "tests/test_calc.py"),
        old_string="from calc import add\n",
        new_string="from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n",
    )
    assert result is None


def test_non_test_files_are_ignored(project, session):
    result = guard(project, session, "Edit", file_path=str(project / "calc.py"), old_string="a + b", new_string="0")
    assert result is None


def test_test_track_task_in_progress_owns_its_file(project, session):
    path = project / "tests/test_subtract.py"
    path.write_text("def test_sub():\n    assert True\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "wip")
    git(project, "checkout", "-q", "main")
    git(project, "merge", "-q", "feature")  # now it exists at base too
    git(project, "checkout", "-q", "feature")
    write_tasks(project, t1="In Progress")
    result = guard(project, session, "Edit", file_path=str(path), old_string="    assert True\n", new_string="    pass\n")
    assert result is None


def test_test_file_new_on_branch_is_allowed(project, session):
    path = project / "tests/test_new.py"
    path.write_text("def test_a():\n    assert 1\n")
    result = guard(project, session, "Edit", file_path=str(path), old_string="    assert 1\n", new_string="    pass\n")
    assert result is None


def test_requirements_frozen_once_implementation_starts(project, session):
    path = str(project / "specs/calc/requirements.md")
    assert guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string="x", new_string="y") is None
    write_tasks(project, t2="In Progress")
    assert decision(guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string="x", new_string="y")) == "deny"


def test_requirements_frozen_once_implementation_starts_main_session_passes(project, session):
    path = str(project / "specs/calc/requirements.md")
    write_tasks(project, t2="In Progress")
    assert guard(project, session, "Edit", file_path=path, old_string="x", new_string="y") is None


REQ_OLD = "- 1.1 WHEN asked THE SYSTEM SHALL subtract.\n  (Decided 2026-10-12.)\n\n### Story 2\n"


def test_adding_requirements_after_implementation_starts_passes(project, session):
    write_tasks(project, t2="In Progress")
    path = project / "specs/calc/requirements.md"
    added = REQ_OLD.replace("\n\n### Story 2", "\n- 1.2 WHEN asked THE SYSTEM SHALL\n  multiply.\n\n### Story 2")
    assert guard(project, session, "Edit", file_path=str(path), old_string=REQ_OLD, new_string=added) is None
    current = path.read_text()
    assert guard(project, session, "Write", file_path=str(path), content=current + "- 1.3 SHALL divide.\n") is None


def test_rewording_or_removing_a_requirement_still_asks(project, session):
    write_tasks(project, t2="In Progress")
    path = str(project / "specs/calc/requirements.md")
    reworded = REQ_OLD.replace("subtract", "subtract or return 0")
    assert decision(guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string=REQ_OLD, new_string=reworded)) == "deny"
    removed = REQ_OLD.replace("  (Decided 2026-10-12.)\n", "")
    assert decision(guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string=REQ_OLD, new_string=removed)) == "deny"
    assert decision(guard(project, session, "Write", agent_id=AGENT, file_path=path, content="# Feature: Calc\n- 1.1 anything\n")) == "deny"


def test_rewording_or_removing_a_requirement_main_session_passes(project, session):
    write_tasks(project, t2="In Progress")
    path = str(project / "specs/calc/requirements.md")
    reworded = REQ_OLD.replace("subtract", "subtract or return 0")
    assert guard(project, session, "Edit", file_path=path, old_string=REQ_OLD, new_string=reworded) is None
    removed = REQ_OLD.replace("  (Decided 2026-10-12.)\n", "")
    assert guard(project, session, "Edit", file_path=path, old_string=REQ_OLD, new_string=removed) is None
    assert guard(project, session, "Write", file_path=path, content="# Feature: Calc\n- 1.1 anything\n") is None


TP_OLD = (
    "| TC | Requirement | Check |\n"
    "|---|---|---|\n"
    "| TC-1 | 1.1 | add(1, 2) returns 3 |\n"
    "| TC-2 | 1.1 | add(-1, 1) returns 0 |\n"
)


def write_test_plan(project):
    path = project / "specs/calc/test-plan.md"
    path.write_text("# Test Plan: Calc\n\n" + TP_OLD)
    return path


def test_test_plan_frozen_once_implementation_starts(project, session):
    path = str(write_test_plan(project))
    reworded = TP_OLD.replace("returns 3", "returns a number")
    assert guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string=TP_OLD, new_string=reworded) is None
    write_tasks(project, t2="In Progress")
    result = guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string=TP_OLD, new_string=reworded)
    assert decision(result) == "deny"
    assert "test-plan.md" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_test_plan_frozen_once_implementation_starts_main_session_passes(project, session):
    path = str(write_test_plan(project))
    reworded = TP_OLD.replace("returns 3", "returns a number")
    write_tasks(project, t2="In Progress")
    assert guard(project, session, "Edit", file_path=path, old_string=TP_OLD, new_string=reworded) is None


def test_adding_test_plan_rows_after_implementation_starts_passes(project, session):
    write_tasks(project, t2="In Progress")
    path = write_test_plan(project)
    added = TP_OLD + "| TC-3 | 1.2 | add(0, 0) returns 0 |\n"
    assert guard(project, session, "Edit", file_path=str(path), old_string=TP_OLD, new_string=added) is None
    current = path.read_text()
    assert guard(project, session, "Write", file_path=str(path), content=current + "\n## Notes\nNew section.\n") is None


def test_rewording_or_deleting_a_test_plan_row_asks(project, session):
    write_tasks(project, t2="In Progress")
    path = str(write_test_plan(project))
    reworded = TP_OLD.replace("add(-1, 1) returns 0", "add(-1, 1) does not raise")
    assert decision(guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string=TP_OLD, new_string=reworded)) == "deny"
    deleted = TP_OLD.replace("| TC-2 | 1.1 | add(-1, 1) returns 0 |\n", "")
    assert decision(guard(project, session, "Edit", agent_id=AGENT, file_path=path, old_string=TP_OLD, new_string=deleted)) == "deny"
    assert decision(guard(project, session, "Write", agent_id=AGENT, file_path=path, content="# Test Plan: Calc\n")) == "deny"


def test_rewording_or_deleting_a_test_plan_row_main_session_passes(project, session):
    write_tasks(project, t2="In Progress")
    path = str(write_test_plan(project))
    reworded = TP_OLD.replace("add(-1, 1) returns 0", "add(-1, 1) does not raise")
    assert guard(project, session, "Edit", file_path=path, old_string=TP_OLD, new_string=reworded) is None
    deleted = TP_OLD.replace("| TC-2 | 1.1 | add(-1, 1) returns 0 |\n", "")
    assert guard(project, session, "Edit", file_path=path, old_string=TP_OLD, new_string=deleted) is None
    assert guard(project, session, "Write", file_path=path, content="# Test Plan: Calc\n") is None


def test_test_plan_guard_mode_deny_and_off(project, session):
    write_tasks(project, t2="In Progress")
    path = str(write_test_plan(project))
    edit = dict(file_path=path, old_string=TP_OLD, new_string=TP_OLD.replace("returns 3", "returns 4"))
    (project / "specs/ck.json").write_text(json.dumps({"guard": {"test_plan": "deny"}}))
    assert decision(guard(project, session, "Edit", agent_id=AGENT, **edit)) == "deny"
    (project / "specs/ck.json").write_text(json.dumps({"guard": {"test_plan": "off"}}))
    assert guard(project, session, "Edit", agent_id=AGENT, **edit) is None
    # Each guard has its own setting: switching off requirements leaves test_plan on (a task agent is denied, never asked).
    (project / "specs/ck.json").write_text(json.dumps({"guard": {"requirements": "off"}}))
    assert decision(guard(project, session, "Edit", agent_id=AGENT, **edit)) == "deny"


def test_test_plan_guard_mode_deny_and_off_main_session_passes(project, session):
    write_tasks(project, t2="In Progress")
    path = str(write_test_plan(project))
    edit = dict(file_path=path, old_string=TP_OLD, new_string=TP_OLD.replace("returns 3", "returns 4"))
    for guard_cfg in ({"test_plan": "deny"}, {"test_plan": "off"}, {"requirements": "off"}):
        (project / "specs/ck.json").write_text(json.dumps({"guard": guard_cfg}))
        assert guard(project, session, "Edit", **edit) is None


def test_bash_deleting_tests_asks(project, session):
    assert decision(guard(project, session, "Bash", command="rm tests/test_calc.py")) == "ask"
    assert decision(guard(project, session, "Bash", command="cd . && rm -rf tests")) == "ask"
    assert decision(guard(project, session, "Bash", command="git rm -q tests/test_calc.py")) == "ask"


def test_bash_in_place_edits_and_redirects_no_longer_ask(project, session):
    """Moved from test_bash_deleting_tests_asks: 1.2 lets these through (the post-check judges the result)."""
    assert guard(project, session, "Bash", command="sed -i 's/assert/#/' tests/test_calc.py") is None
    assert guard(project, session, "Bash", command="echo '' > tests/test_calc.py") is None


def test_bash_running_tests_is_allowed(project, session):
    assert guard(project, session, "Bash", command="python -m pytest -q tests/test_calc.py > out.txt 2>&1") is None
    assert guard(project, session, "Bash", command="rm -rf build && python -m pytest tests/test_calc.py") is None


def test_guard_mode_deny_and_off(project, session):
    edit = dict(file_path=str(project / "tests/test_calc.py"), old_string="    assert add(1, 2) == 3\n", new_string="")
    (project / "specs/ck.json").write_text(json.dumps({"guard": {"tests": "deny"}}))
    assert decision(guard(project, session, "Edit", **edit)) == "deny"
    (project / "specs/ck.json").write_text(json.dumps({"guard": {"tests": "off"}}))
    assert guard(project, session, "Edit", **edit) is None


def test_hook_runs_through_ck_cli(project, session):
    """End to end: Claude Code invokes `ck --hook pre-tool-guard` with JSON on stdin."""
    payload = {
        "session_id": session,
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rm tests/test_calc.py"},
    }
    proc = subprocess.run(
        [sys.executable, "-m", "claude_kiro.cli", "--hook", "pre-tool-guard"],
        input=json.dumps(payload), capture_output=True, text=True, cwd=project,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert (project / "tests/test_calc.py").read_text() == CALC_TEST
