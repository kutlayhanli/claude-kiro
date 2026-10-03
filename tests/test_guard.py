"""PreToolUse guard, driven through the real hook entry point with Claude Code-shaped input."""

import json
import subprocess
import sys

from claude_kiro.cli.runner import execute_hook

from conftest import CALC_TEST, git, write_tasks


def guard(project, session, tool_name, **tool_input):
    return execute_hook(
        "pre-tool-guard",
        {
            "session_id": session,
            "cwd": str(project),
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
    )


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
    assert guard(project, session, "Edit", file_path=path, old_string="x", new_string="y") is None
    write_tasks(project, t2="In Progress")
    assert decision(guard(project, session, "Edit", file_path=path, old_string="x", new_string="y")) == "ask"


def test_bash_deleting_tests_asks(project, session):
    assert decision(guard(project, session, "Bash", command="rm tests/test_calc.py")) == "ask"
    assert decision(guard(project, session, "Bash", command="cd . && rm -rf tests")) == "ask"
    assert decision(guard(project, session, "Bash", command="git rm -q tests/test_calc.py")) == "ask"
    assert decision(guard(project, session, "Bash", command="sed -i 's/assert/#/' tests/test_calc.py")) == "ask"
    assert decision(guard(project, session, "Bash", command="echo '' > tests/test_calc.py")) == "ask"


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
