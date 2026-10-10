"""`ck worktree land`: batch landing with a merge-tree precheck, one gate per batch, and bisection.

Real temporary git repositories; the gate runs each task's **Verify:** line in the target checkout.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli

from conftest import git

TASKS = """# Implementation Tasks: Demo

### Task 1: one
**Status:** Not Started
**Verify:** `test -f one.txt`

### Task 2: two
**Status:** Not Started
**Verify:** `test ! -f bad.txt`

### Task 3: three
**Status:** Not Started
**Verify:** `test -f three.txt`

### Task 4: four
**Status:** Not Started
**Dependencies:** Task 3
**Verify:** `test -f four.txt`
"""


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".venv/\n.claude/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text(TASKS)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def wt(root: Path, n: str) -> Path:
    return root / ".claude/worktrees" / f"demo-task-{n}"


def commit_in(path: Path, name: str, content: str, msg: str):
    (path / name).write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-qm", msg)


def land(root: Path, *args: str):
    result = ck(root, "worktree", "land", "demo", *args, "--json")
    try:
        data = json.loads(result.output)
    except ValueError:
        raise AssertionError(result.output)
    return result, data, {t["task"]: t for t in data["tasks"]}


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def test_clean_batch_lands_with_one_gate(repo):
    ck(repo, "worktree", "create", "demo", "1", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")

    result, data, by = land(repo, "1", "3", "--gate")
    assert result.exit_code == 0, result.output
    assert [by[n]["state"] for n in ("1", "3")] == ["MERGED", "MERGED"]
    assert all(by[n]["checkGreen"] is True and by[n]["mergedAt"] for n in ("1", "3"))
    assert [sorted(g["tasks"]) for g in data["gates"]] == [["1", "3"]]  # one gate for the batch
    assert data["bisected"] is False
    assert (repo / "one.txt").exists() and (repo / "three.txt").exists()
    subjects = git(repo, "log", "--format=%s", "-2").splitlines()
    assert sorted(subjects) == ["Merge feat/demo-task-1 (task 1)", "Merge feat/demo-task-3 (task 3)"]
    assert not wt(repo, "1").exists() and "feat/demo-task-3" not in git(repo, "branch")


def test_conflict_precheck_leaves_target_untouched(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "shared.txt", "from task 1\n", "task 1")
    commit_in(wt(repo, "2"), "shared.txt", "from task 2\n", "task 2")
    assert ck(repo, "worktree", "merge", "demo", "1").exit_code == 0
    before = head(repo)

    result, data, by = land(repo, "2", "--gate")
    assert result.exit_code == 2, result.output
    assert by["2"]["state"] == "CONFLICT"
    assert by["2"]["files"] == ["shared.txt"]
    assert head(repo) == before
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert data["gates"] == []  # nothing landed, nothing gated
    assert wt(repo, "2").exists()  # the task worktree stays for the resolver


def test_tasks_that_conflict_with_each_other_split_across_batches(repo):
    ck(repo, "worktree", "create", "demo", "1", "2", "3")
    (wt(repo, "1") / "one.txt").write_text("1\n")
    commit_in(wt(repo, "1"), "shared.txt", "from task 1\n", "task 1")
    commit_in(wt(repo, "2"), "shared.txt", "from task 2\n", "task 2")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")

    result, data, by = land(repo, "1", "2", "3", "--gate")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED" and by["3"]["state"] == "MERGED"
    assert by["2"]["state"] == "DEFERRED"
    assert by["2"]["files"] == ["shared.txt"] and "Task 1" in by["2"]["detail"]
    assert (repo / "shared.txt").read_text() == "from task 1\n"
    assert wt(repo, "2").exists()


def test_critical_path_lands_first(repo):
    # Task 3 has a dependent (Task 4), so it is on the longer chain and lands before 1.
    ck(repo, "worktree", "create", "demo", "1", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")
    result, data, by = land(repo, "1", "3", "--gate")
    assert result.exit_code == 0, result.output
    assert [t["task"] for t in data["tasks"]] == ["3", "1"]
    first_parents = git(repo, "log", "--first-parent", "--format=%s", "-2").splitlines()
    assert first_parents == ["Merge feat/demo-task-1 (task 1)", "Merge feat/demo-task-3 (task 3)"]


def test_red_batch_bisects_to_the_culprit(repo):
    ck(repo, "worktree", "create", "demo", "1", "2", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    commit_in(wt(repo, "2"), "bad.txt", "breaks task 2's check\n", "task 2")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")
    before = head(repo)

    result, data, by = land(repo, "1", "2", "3", "--gate")
    assert result.exit_code == 1, result.output
    assert by["1"]["state"] == "MERGED" and by["3"]["state"] == "MERGED"
    assert by["1"]["checkGreen"] is True and by["3"]["checkGreen"] is True
    assert by["2"]["state"] == "RED" and by["2"]["checkGreen"] is False
    assert "bad.txt" in by["2"]["checkTail"]
    assert data["bisected"] is True
    assert sorted(data["gates"][0]["tasks"]) == ["1", "2", "3"] and data["gates"][0]["green"] is False
    assert len(data["gates"]) < 5  # a green left half proves the right half red without re-gating it
    # The target holds the green tasks only; the culprit's branch and worktree stay for the fixer.
    assert (repo / "one.txt").exists() and (repo / "three.txt").exists()
    assert not (repo / "bad.txt").exists()
    assert git(repo, "merge-base", "--is-ancestor", before, "HEAD") == ""
    assert "feat/demo-task-2" in git(repo, "branch") and wt(repo, "2").exists()
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_single_task_equals_merge_then_task_gate(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result, data, by = land(repo, "1", "--gate")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED" and by["1"]["checkGreen"] is True
    assert data["gates"] == [{**data["gates"][0], "tasks": ["1"], "green": True}]
    assert "Merge feat/demo-task-1 (task 1)" in git(repo, "log", "-1", "--format=%s")
    assert ck(repo, "gate", "demo", "--task", "1").exit_code == 0

    commit_in(wt(repo, "2"), "bad.txt", "x\n", "task 2")
    before = head(repo)
    result, data, by = land(repo, "2", "--gate")
    assert result.exit_code == 1
    assert by["2"]["state"] == "RED" and "bad.txt" in by["2"]["checkTail"]
    assert data["bisected"] is False and len(data["gates"]) == 1
    assert head(repo) == before  # a red task never stays on the target


def test_without_gate_it_merges_like_merge(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result, data, by = land(repo, "1", "2")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED" and by["1"]["checkGreen"] is None
    assert by["2"]["state"] == "SKIP"
    assert data["gates"] == []


def test_claimed_task_is_busy_unless_released(repo):
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a")
    result, _, by = land(repo, "1", "--gate")
    assert result.exit_code == 3 and by["1"]["state"] == "BUSY"
    result, _, by = land(repo, "1", "--gate", "--release")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED"


def test_refuses_dirty_target_and_concurrent_landing(repo):
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    (repo / "shared.txt").write_text("local edit\n")
    dirty = ck(repo, "worktree", "land", "demo", "1", "--gate")
    assert dirty.exit_code == 3 and "uncommitted" in dirty.output
    git(repo, "checkout", "--", "shared.txt")

    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    lock = common / "ck-land-main.lock"
    lock.write_text(str(os.getpid()))  # a live process holds it
    busy = ck(repo, "worktree", "land", "demo", "1", "--gate")
    assert busy.exit_code == 3 and "already landing" in busy.output
    lock.write_text("999999999")  # a dead process: stale, taken over
    assert ck(repo, "worktree", "land", "demo", "1", "--gate").exit_code == 0
    assert not lock.exists()


def test_land_into_integration_branch(repo):
    ck(repo, "worktree", "integration", "demo")
    ck(repo, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result, _, by = land(repo, "1", "--into", "integrate/demo", "--gate")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED"
    assert not (repo / "one.txt").exists()
    assert (repo / ".claude/worktrees/demo-integration/one.txt").exists()
