"""Task-scoped pass stamps: a task gate that passed on a tree is not re-run on the same tree.

The done-verify hook gates a task the moment it is marked Done; the task agent then commits and runs
`ck gate <spec> --task N` itself, usually on exactly the tree the hook just checked. The hook's pass is
stamped by that tree (working tree content, tracked plus untracked, as the gate saw it), and a later
`ck gate --task` on the same tree with the same commands reuses it instead of running the suite again.
Whole-spec gates and the landing gate (`ck worktree land --gate`) never reuse a stamp.
"""

import json
from pathlib import Path

import pytest

from claude_kiro import gate as gate_mod
from claude_kiro.cli.runner import execute_hook
from claude_kiro.gate import run_gate
from claude_kiro.verified import commands_sha, stamp_dir, worktree_tree

from conftest import git, write_tasks
from test_gate import implement_subtract, run_ck_gate, write_subtract_tests

TASK2_COMMANDS = ["python -m pytest -q tests/test_subtract.py"]


@pytest.fixture
def counted(monkeypatch):
    """Every command the gate actually runs."""
    ran = []
    real = gate_mod._run

    def run(command, project_dir, timeout):
        ran.append(command)
        return real(command, project_dir, timeout)

    monkeypatch.setattr(gate_mod, "_run", run)
    return ran


def ready_task2(project: Path) -> None:
    """Task 1 (its tests) Done and committed; Task 2 implemented and marked Done, uncommitted."""
    (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")  # as in any real project
    write_subtract_tests(project)
    write_tasks(project, t1="Done", a1="x")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "tests")
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")


def entries(project: Path, tree: str) -> list:
    path = stamp_dir(project) / "task" / f"{tree}.json"
    return json.loads(path.read_text()) if path.exists() else []


def test_worktree_tree_matches_the_commit_of_the_same_content(project):
    ready_task2(project)
    before = worktree_tree(project)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "task 2")
    assert before == worktree_tree(project) == git(project, "rev-parse", "HEAD^{tree}").strip()


def test_worktree_tree_ignores_test_caches_but_not_new_sources(project):
    ready_task2(project)
    tree = worktree_tree(project)
    (project / "tests" / "__pycache__").mkdir(exist_ok=True)
    (project / "tests" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
    assert worktree_tree(project) == tree
    (project / "extra.py").write_text("x = 1\n")
    assert worktree_tree(project) != tree


def test_task_gate_pass_is_stamped_by_the_tree_it_checked(project):
    ready_task2(project)
    tree = worktree_tree(project)
    assert run_gate(project, project / "specs/calc", ["2"]).ok
    [e] = entries(project, tree)
    assert TASK2_COMMANDS[0] in e["commands"] and e["sha"] == commands_sha(e["commands"])
    assert e["by"] == "ck gate" and e["scope"] == "task"


def test_failing_task_gate_is_not_stamped(project):
    ready_task2(project)
    (project / "calc.py").write_text((project / "calc.py").read_text().replace("return a - b", "return 0"))
    tree = worktree_tree(project)
    assert not run_gate(project, project / "specs/calc", ["2"]).ok
    assert entries(project, tree) == []


def test_same_tree_same_checks_is_reused(project, counted):
    ready_task2(project)
    assert run_gate(project, project / "specs/calc", ["2"], reuse=True).ok
    assert TASK2_COMMANDS[0] in counted
    counted.clear()
    again = run_gate(project, project / "specs/calc", ["2"], reuse=True)
    assert again.ok, again.report()
    assert counted == []
    report = again.report()
    assert "reused" in report.lower() and TASK2_COMMANDS[0] in report
    assert "Task 2 acceptance criteria" in report  # the cheap checks still run


def test_a_changed_tree_runs_the_checks_again(project, counted):
    ready_task2(project)
    assert run_gate(project, project / "specs/calc", ["2"], reuse=True).ok
    counted.clear()
    (project / "calc.py").write_text((project / "calc.py").read_text() + "\n# touched\n")
    assert run_gate(project, project / "specs/calc", ["2"], reuse=True).ok
    assert TASK2_COMMANDS[0] in counted


def test_reuse_is_off_unless_asked(project, counted):
    """The landing gate calls run_gate(target, spec, nums) and must always run the suite."""
    ready_task2(project)
    assert run_gate(project, project / "specs/calc", ["2"]).ok
    counted.clear()
    assert run_gate(project, project / "specs/calc", ["2"]).ok
    assert TASK2_COMMANDS[0] in counted


def test_whole_spec_gate_never_reuses(project, counted):
    ready_task2(project)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "task 2")
    assert run_gate(project, project / "specs/calc", reuse=True).ok
    counted.clear()
    assert run_gate(project, project / "specs/calc", reuse=True).ok
    assert "python -m pytest -q tests/test_calc.py" in counted


def test_ck_gate_task_reuses_and_no_reuse_forces_a_run(project, counted):
    ready_task2(project)
    assert run_ck_gate(project, "--task", "2").exit_code == 0
    counted.clear()
    out = run_ck_gate(project, "--task", "2")
    assert out.exit_code == 0, out.output
    assert counted == [] and "reused" in out.output.lower()
    out = run_ck_gate(project, "--task", "2", "--no-reuse")
    assert out.exit_code == 0, out.output
    assert TASK2_COMMANDS[0] in counted


# --- done-verify in a task worktree -------------------------------------------


def linked_worktree(project: Path, tmp_path: Path) -> Path:
    """A task worktree like `ck worktree create` makes; the session's cwd stays the main checkout."""
    wt = tmp_path / "wt-task-2"
    git(project, "worktree", "add", "-q", str(wt), "-b", "feat/calc-task-2")
    return wt


def mark_done_in(wt: Path, main: Path, session: str):
    """A workflow subagent's Edit of the worktree's tasks.md: Claude Code reports the session cwd (main)."""
    return execute_hook(
        "done-verify",
        {"session_id": session, "cwd": str(main), "tool_name": "Edit",
         "tool_input": {"file_path": str(wt / "specs/calc/tasks.md")}},
    )


def test_done_verify_gates_a_tasks_md_in_a_linked_worktree(project, tmp_path, session):
    wt = linked_worktree(project, tmp_path)
    ready_task2(wt)  # only the worktree has subtract(): a gate run in the main checkout would fail
    result = mark_done_in(wt, project, session)
    assert result and "Verification gate passed for calc: Task 2" in result["systemMessage"], result


def test_done_verify_failure_in_a_linked_worktree_blocks(project, tmp_path, session):
    wt = linked_worktree(project, tmp_path)
    write_subtract_tests(wt)
    write_tasks(wt, t1="Done", a1="x")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "tests")
    write_tasks(wt, t1="Done", t2="Done", a1="x", a2="x")  # Done without subtract()
    result = mark_done_in(wt, project, session)
    assert result and result["decision"] == "block"


def test_hook_pass_is_reused_by_the_agents_gate_after_commit(project, tmp_path, session, counted):
    wt = linked_worktree(project, tmp_path)
    ready_task2(wt)
    assert "Verification gate passed" in mark_done_in(wt, project, session)["systemMessage"]
    tree = worktree_tree(wt)
    assert [e["by"] for e in entries(wt, tree)] == ["done-verify"]
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "task 2")
    counted.clear()
    out = run_ck_gate(wt, "--task", "2")
    assert out.exit_code == 0, out.output
    assert counted == [] and "done-verify" in out.output
