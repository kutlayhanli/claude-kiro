"""`ck worktree create|claim|release|merge|integration|status` against a real git repository."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli

from conftest import git


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
    (root / "specs/demo/tasks.md").write_text("### Task 1: a\n**Status:** Not Started\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def wt(root: Path, n: str) -> Path:
    return root / ".claude/worktrees" / f"demo-task-{n}"


def commit_in(path: Path, name: str, content: str, msg: str):
    (path / name).write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-qm", msg)


def test_create_is_idempotent_and_reports_busy(repo):
    first = ck(repo, "worktree", "create", "demo", "1", "2")
    assert first.exit_code == 0, first.output
    assert "CREATED 1 .claude/worktrees/demo-task-1 (feat/demo-task-1) - from main" in first.output
    assert git(wt(repo, "2"), "branch", "--show-current").strip() == "feat/demo-task-2"

    again = ck(repo, "worktree", "create", "demo", "1")
    assert "EXISTS 1" in again.output and again.exit_code == 0

    (wt(repo, "1") / "wip.txt").write_text("uncommitted")
    busy = ck(repo, "worktree", "create", "demo", "1")
    assert busy.exit_code == 3
    assert "BUSY 1" in busy.output and "uncommitted" in busy.output
    assert ck(repo, "worktree", "create", "demo", "1", "--reuse-dirty").exit_code == 0


def test_create_works_from_inside_a_worktree(repo):
    ck(repo, "worktree", "create", "demo", "1")
    result = ck(wt(repo, "1"), "worktree", "create", "demo", "2", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["state"] == "CREATED"
    assert wt(repo, "2").is_dir()  # placed under the main checkout, not nested


def test_claim_blocks_second_owner_and_setup(repo):
    ck(repo, "worktree", "create", "demo", "1")
    assert "CLAIMED 1" in ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a").output
    second = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b")
    assert second.exit_code == 3 and "BUSY 1" in second.output and "agent-a" in second.output
    setup = ck(repo, "worktree", "create", "demo", "1")
    assert setup.exit_code == 3 and "claimed: agent-a" in setup.output
    takeover = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-c", "--takeover")
    assert takeover.exit_code == 0 and "took over" in takeover.output
    assert "RELEASED 1" in ck(repo, "worktree", "release", "demo", "1").output
    assert ck(repo, "worktree", "create", "demo", "1").exit_code == 0


def test_merge_removes_worktree_and_branch_and_skips_empty(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1: work")
    (wt(repo, "1") / ".venv").mkdir()  # ignored install artifacts must not block removal
    (wt(repo, "1") / ".venv" / "x").write_text("")

    result = ck(repo, "worktree", "merge", "demo", "1", "2")
    assert result.exit_code == 0, result.output
    assert "MERGED 1" in result.output and "1 commit(s) into main" in result.output
    assert "SKIP 2" in result.output
    assert (repo / "one.txt").read_text() == "1\n"
    assert not wt(repo, "1").exists()
    assert "feat/demo-task-1" not in git(repo, "branch")
    assert "Merge feat/demo-task-1 (task 1)" in git(repo, "log", "-1", "--format=%s")


def test_merge_conflict_aborts_cleanly_and_resolves_in_task_worktree(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "shared.txt", "from task 1\n", "task 1")
    commit_in(wt(repo, "2"), "shared.txt", "from task 2\n", "task 2")
    assert ck(repo, "worktree", "merge", "demo", "1").exit_code == 0

    conflict = ck(repo, "worktree", "merge", "demo", "2")
    assert conflict.exit_code == 2
    assert "CONFLICT 2" in conflict.output and "shared.txt" in conflict.output
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""

    # resolve inside the task worktree, as the merge agent is told to
    task2 = wt(repo, "2")
    import subprocess

    subprocess.run(["git", "merge", "main"], cwd=task2, capture_output=True)
    (task2 / "shared.txt").write_text("from task 1\nfrom task 2\n")
    git(task2, "add", "-A")
    git(task2, "commit", "-qm", "merge main into task 2")
    merged = ck(repo, "worktree", "merge", "demo", "2")
    assert merged.exit_code == 0, merged.output
    assert (repo / "shared.txt").read_text() == "from task 1\nfrom task 2\n"


def test_merge_refuses_claimed_or_dirty_task_and_dirty_target(repo):
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a")
    busy = ck(repo, "worktree", "merge", "demo", "1")
    assert busy.exit_code == 3 and "BUSY 1" in busy.output
    ck(repo, "worktree", "release", "demo", "1")

    (wt(repo, "1") / "one.txt").write_text("changed but not committed\n")
    dirty = ck(repo, "worktree", "merge", "demo", "1")
    assert dirty.exit_code == 3 and "uncommitted" in dirty.output
    git(wt(repo, "1"), "checkout", "--", "one.txt")

    (repo / "shared.txt").write_text("local edit on main\n")
    target = ck(repo, "worktree", "merge", "demo", "1")
    assert target.exit_code == 3 and "refusing to merge into main" in target.output


def test_merge_into_integration_branch_leaves_main_untouched(repo):
    assert "CREATED integration" in ck(repo, "worktree", "integration", "demo").output
    ck(repo, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result = ck(repo, "worktree", "merge", "demo", "1", "--into", "integrate/demo")
    assert result.exit_code == 0, result.output
    assert not (repo / "one.txt").exists()
    assert (repo / ".claude/worktrees/demo-integration/one.txt").exists()
    # the user fast-forwards main when ready
    git(repo, "merge", "--ff-only", "integrate/demo")
    assert (repo / "one.txt").exists()


def test_status_lists_claims_and_progress(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    ck(repo, "worktree", "claim", "demo", "2", "--owner", "agent-b")
    out = ck(repo, "worktree", "status", "demo").output
    assert "EXISTS 1" in out and "1 ahead of main" in out
    assert "BUSY 2" in out and "claimed: agent-b" in out
